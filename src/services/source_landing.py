"""Source landing: turn an uploaded file in any supported format into an all-text Parquet snapshot.

Why text: the legacy value is what gets migrated. Reading everything as text keeps leading zeros and exact
spelling, and makes every format behave the same way. Column types are *inferred from the text afterwards*
(see `infer_types`) and stored beside the snapshot, so profiling and the catalog still show BIGINT / DATE...

Supported: delimited text (any delimiter; encoding detected: BOM, UTF-8, else Windows-1252; .gz and .zip),
Excel .xlsx (sheet + header-row selection), JSON / NDJSON, fixed-width (explicit column layout), Parquet.

Controls: a SHA-256 of the original, the landed row count, rows the reader rejected (fails the landing
unless tolerated) and an optional expected row count from the source system (a mismatch fails it).
Everything is streamed / chunked and DuckDB is memory-capped; decompression is size-capped (zip bombs).
Not covered here: direct database connections (a later step) and .xls (convert to .xlsx).
"""

from __future__ import annotations

import codecs
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import duckdb
import pandas as pd
from openpyxl import load_workbook

from src.schemas.source import ColumnInfo, LandingOptions, SnapshotInfo, SourceFormat, SourcePreview
from src.services._duckdb_util import DEFAULT_MEMORY_LIMIT_MB, bounded_connection, lit, q

EXT_FORMAT: dict[str, SourceFormat] = {
    ".csv": "delimited", ".tsv": "delimited", ".txt": "delimited", ".psv": "delimited", ".dat": "delimited", ".tab": "delimited",
    ".xlsx": "excel", ".xlsm": "excel", ".json": "json", ".ndjson": "json", ".jsonl": "json",
    ".parquet": "parquet", ".pq": "parquet",
}
SUPPORTED = "csv, tsv, txt, psv, dat, xlsx, json, ndjson, parquet (optionally .gz or .zip)"
CANDIDATE_DELIMITERS = [",", ";", "\t", "|"]
SNAPSHOT_FILE, PARQUET_FILE = "snapshot.json", "source.parquet"
_CHUNK = 1 << 20
_ROWS_PER_BATCH = 50_000
_PREVIEW_BYTES = 8 * 1024 * 1024
_NUMERIC_TEXT_HINT = re.compile(r"POST|ZIP|PLZ|PIN|PHONE|TEL|FAX|TAX|VAT|(^|_)ID($|_)|NUM|CODE|ACCOUNT|ACCT", re.IGNORECASE)


class SourceLandingError(ValueError):
    """The file cannot be landed as asked (unreadable, wrong layout, control total mismatch...)."""


# ---------------------------------------------------------------------- small helpers
def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def detect_encoding(path: Path) -> tuple[str, str]:
    """(encoding, how it was decided). BOM first, then 'is it valid UTF-8', else Windows-1252."""
    with open(path, "rb") as fh:
        head = fh.read(_CHUNK)
    if not head:
        return "utf-8", "empty file"
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", "UTF-8 byte-order mark"
    if head.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32", "UTF-32 byte-order mark"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16", "UTF-16 byte-order mark"
    if head.count(b"\x00") > len(head) // 4:
        return ("utf-16-le" if head[1:2] == b"\x00" else "utf-16-be"), "many NUL bytes (UTF-16 without a mark)"
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)  # a cut multi-byte tail is fine
        return "utf-8", "valid UTF-8"
    except UnicodeDecodeError:
        return "cp1252", "not valid UTF-8, assumed Windows-1252"


def detect_delimiter(path: Path, quote: str = '"') -> str:
    """The candidate that splits the first rows into the same number (>= 2) of fields most consistently."""
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        sample = fh.read(262_144)
    best, best_score = ",", (0.0, 0)
    for candidate in CANDIDATE_DELIMITERS:
        try:
            counts = [len(r) for _, r in zip(range(40), csv.reader(io.StringIO(sample), delimiter=candidate, quotechar=quote or '"')) if r]
        except csv.Error:
            continue
        if not counts:
            continue
        modal = max(set(counts), key=counts.count)
        score = (counts.count(modal) / len(counts), modal)
        if modal >= 2 and score > best_score:
            best, best_score = candidate, score
    return best


def _transcode(path: Path, encoding: str, work: Path, warnings: list[str]) -> Path:
    """Stream the file to UTF-8 (replacing undecodable bytes, which are counted and reported)."""
    out = work / "transcoded.txt"
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    replaced = 0
    with open(path, "rb") as src, open(out, "w", encoding="utf-8", newline="") as dst:
        while chunk := src.read(_CHUNK):
            text = decoder.decode(chunk)
            replaced += text.count("�")
            dst.write(text)
        dst.write(decoder.decode(b"", final=True))
    if replaced:
        warnings.append(f"{replaced} character(s) could not be decoded as {encoding} and were replaced")
    return out


def _unique_names(raw: list[Any], warnings: list[str]) -> list[str]:
    names, seen = [], set()
    for i, value in enumerate(raw, start=1):
        base = (str(value).strip() if value is not None else "") or f"COLUMN_{i}"
        if value is None or not str(value).strip():
            warnings.append(f"column {i} has no header; named {base}")
        name, n = base, 1
        while name.lower() in seen:
            n += 1
            name = f"{base}_{n}"
        if name != base:
            warnings.append(f"duplicate column header '{base}' renamed to {name}")
        seen.add(name.lower())
        names.append(name)
    return names


def _cell_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def infer_types(parquet: Path, memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB) -> dict[str, str]:
    """Infer a type per all-text column in ONE vectorised pass. Values with leading zeros stay VARCHAR."""
    with bounded_connection(memory_limit_mb) as con:
        names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit(parquet.as_posix())})").fetchall()]
        aggs = []
        for n in names:
            v = f"nullif(trim({q(n)}), '')"
            aggs += [
                f"count({v})",
                f"count_if(regexp_full_match({v}, '-?(0|[1-9][0-9]{{0,17}})'))",
                f"count_if(regexp_full_match({v}, '-?(0|[1-9][0-9]*)[.][0-9]+'))",
                f"count_if(regexp_full_match({v}, '[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}') AND TRY_CAST({v} AS DATE) IS NOT NULL)",
                f"count_if(regexp_full_match({v}, '[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}[ T][0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}([.][0-9]+)?') "
                f"AND TRY_CAST({v} AS TIMESTAMP) IS NOT NULL)",
                f"count_if(lower({v}) IN ('true', 'false'))",
            ]
        row = con.execute(f"SELECT {', '.join(aggs)} FROM read_parquet({lit(parquet.as_posix())})").fetchone()
    result = {}
    for i, n in enumerate(names):
        nn, ints, decs, dates, stamps, bools = row[i * 6 : i * 6 + 6]
        if nn == 0:
            result[n] = "VARCHAR"
        elif ints == nn:
            result[n] = "BIGINT"
        elif ints + decs == nn:
            result[n] = "DOUBLE"
        elif dates == nn:
            result[n] = "DATE"
        elif stamps == nn:
            result[n] = "TIMESTAMP"
        elif bools == nn:
            result[n] = "BOOLEAN"
        else:
            result[n] = "VARCHAR"
    return result


def key_problems(parquet: Path, key_columns: list[str], memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB) -> Optional[str]:
    """Why the declared primary key does not identify rows (blank parts or repeated values), or None when it does.
    Compared trimmed, as the pipeline sees it; the message names a few offending values so the file can be fixed."""
    if not key_columns:
        return None
    label = ", ".join(key_columns)
    parts = [f"nullif(trim({q(c)}), '')" for c in key_columns]
    with bounded_connection(memory_limit_mb) as con:
        src = lit(parquet.as_posix())
        blank = con.execute(f"SELECT count(*) FROM read_parquet({src}) WHERE {' OR '.join(p + ' IS NULL' for p in parts)}").fetchone()[0]
        dupes = con.execute(
            f"SELECT concat_ws(' / ', {', '.join(parts)}) AS k, count(*) AS n FROM read_parquet({src}) "
            f"WHERE {' AND '.join(p + ' IS NOT NULL' for p in parts)} GROUP BY ALL HAVING count(*) > 1 ORDER BY n DESC, k LIMIT 6").fetchall()
        dupe_count = con.execute(
            f"SELECT count(*) FROM (SELECT 1 FROM read_parquet({src}) WHERE {' AND '.join(p + ' IS NOT NULL' for p in parts)} "
            f"GROUP BY {', '.join(parts)} HAVING count(*) > 1)").fetchone()[0] if dupes else 0
    problems = []
    if blank:
        problems.append(f"{blank} row(s) have a blank key")
    if dupes:
        shown = ", ".join(f"{k} ({n} rows)" for k, n in dupes[:5])
        problems.append(f"{dupe_count} key value(s) are used by more than one row, e.g. {shown}")
    return f"primary key ({label}) does not identify rows uniquely: " + "; ".join(problems) if problems else None


# ---------------------------------------------------------------------- the service
class SourceLandingService:
    def __init__(self, memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB, threads: int = 2, max_unpacked_mb: int = 8192) -> None:
        self._memory = memory_limit_mb
        self._threads = threads
        self._cap = max_unpacked_mb * 1024 * 1024

    # ------------------------------------------------------------------ container / format resolution
    def _resolve(self, filename: str, options: LandingOptions, path: Path) -> tuple[str, list[str], Optional[str]]:
        """-> (inner file name, zip members, chosen member). Looks inside a .zip; strips .gz."""
        lower = filename.lower()
        if lower.endswith(".zip"):
            if not zipfile.is_zipfile(path):
                raise SourceLandingError("the file is not a valid .zip archive")
            with zipfile.ZipFile(path) as zf:
                members = [i.filename for i in zf.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX")]
            if not members:
                raise SourceLandingError("the zip archive is empty")
            chosen = options.member or (members[0] if len(members) == 1 else None)
            if chosen is None:
                raise SourceLandingError(f"the zip holds {len(members)} files; choose one with the 'member' option: {members[:10]}")
            if chosen not in members:
                raise SourceLandingError(f"'{chosen}' is not in the zip (members: {members[:10]})")
            return os.path.basename(chosen), members, chosen
        return (filename[:-3] if lower.endswith(".gz") else filename), [], None

    @staticmethod
    def _format(inner: str, options: LandingOptions) -> SourceFormat:
        if options.format:
            return options.format
        ext = Path(inner).suffix.lower()
        if ext == ".xls":
            raise SourceLandingError("unsupported file type '.xls': save the workbook as .xlsx first")
        if ext not in EXT_FORMAT:
            raise SourceLandingError(f"unsupported file type '{ext or inner}' (supported: {SUPPORTED}; fixed-width needs format=fixed)")
        return EXT_FORMAT[ext]

    def _unwrap(self, path: Path, filename: str, member: Optional[str], work: Path, head_only: bool) -> Path:
        """Return a plain file: decompress .gz / extract a zip member (size-capped, never trusts member paths)."""
        lower = filename.lower()
        if not (lower.endswith(".gz") or lower.endswith(".zip")):
            return path
        out = work / "unwrapped"
        limit = _PREVIEW_BYTES if head_only else self._cap
        try:
            archive = zipfile.ZipFile(path) if lower.endswith(".zip") else None
            with (archive.open(member) if archive else gzip.open(path, "rb")) as src, open(out, "wb") as dst:
                written = 0
                while chunk := src.read(min(_CHUNK, limit - written + 1)):
                    written += len(chunk)
                    if written > limit:
                        if head_only:
                            break
                        raise SourceLandingError(f"the unpacked file is larger than {self._cap // (1024 * 1024)} MB (zip-bomb protection)")
                    dst.write(chunk)
            if archive:
                archive.close()
        except (OSError, EOFError, zipfile.BadZipFile) as exc:
            raise SourceLandingError(f"cannot decompress the file: {exc}") from exc
        return out

    # ------------------------------------------------------------------ preview
    def preview(self, path: str | Path, filename: str, options: Optional[LandingOptions] = None, rows: int = 20) -> SourcePreview:
        options = options or LandingOptions()
        path = Path(path)
        inner, members, member = self._resolve(filename, options, path)
        fmt = self._format(inner, options)
        warnings: list[str] = []
        with tempfile.TemporaryDirectory(prefix="preview_") as tmp:
            work = Path(tmp)
            data = self._unwrap(path, filename, member, work, head_only=fmt in ("delimited", "fixed"))
            truncated = data != path and data.stat().st_size >= _PREVIEW_BYTES
            encoding = basis = delimiter = None
            sheets: list[str] = []
            if fmt == "delimited":
                encoding, basis = (options.encoding, "specified") if options.encoding else detect_encoding(data)
                utf8 = data if encoding.lower() in ("utf-8", "utf8") else _transcode(data, encoding, work, warnings)
                delimiter = options.delimiter or detect_delimiter(utf8, options.quote)
                with open(utf8, "r", encoding="utf-8", errors="replace", newline="") as fh:
                    reader = csv.reader(fh, delimiter=delimiter, quotechar=options.quote or '"')
                    for _ in range(options.skip_rows):
                        next(reader, None)
                    head = next(reader, None)
                    if head is None:
                        raise SourceLandingError("the file is empty")
                    body = [r for _, r in zip(range(rows), reader)]
                width = len(head)
                columns = _unique_names(head, warnings) if options.header else [f"COLUMN_{i}" for i in range(1, width + 1)]
                data_rows = body if options.header else [head] + body[: rows - 1]
                out_rows = [[(c if c != "" else None) for c in (r + [""] * (width - len(r)))[:width]] for r in data_rows]
            elif fmt == "excel":
                sheets = self._sheet_names(data)
                columns, gen, _ = self._excel_iter(data, options, warnings)
                out_rows = []
                for r in gen:
                    out_rows.append([_cell_text(c) for c in r])
                    if len(out_rows) >= rows:
                        break
                gen.close()  # releases the workbook
            elif fmt == "fixed":
                if not options.columns:
                    raise SourceLandingError("fixed-width files need a column layout (name, start, width for each column)")
                encoding, basis = (options.encoding, "specified") if options.encoding else detect_encoding(data)
                utf8 = data if encoding.lower() in ("utf-8", "utf8") else _transcode(data, encoding, work, warnings)
                columns = [c.name for c in options.columns]
                out_rows = []
                for r in self._fixed_iter(utf8, options):
                    out_rows.append(r)
                    if len(out_rows) >= rows:
                        break
            else:
                columns, out_rows = self._duckdb_preview(data, fmt, rows, warnings)
        return SourcePreview(
            format=fmt, encoding=encoding, encoding_basis=basis, delimiter=delimiter, sheets=sheets, members=members,
            columns=columns, rows=out_rows, truncated=truncated, warnings=warnings,
            options=options.model_copy(update={"format": fmt, "encoding": encoding if fmt in ("delimited", "fixed") else options.encoding,
                                              "delimiter": delimiter or options.delimiter, "member": member}))

    def _duckdb_preview(self, data: Path, fmt: str, rows: int, warnings: list[str]):
        with bounded_connection(self._memory, self._threads) as con:
            rel = self._json_relation(data) if fmt == "json" else f"read_parquet({lit(data.as_posix())})"
            try:
                cur = con.execute(f"SELECT * FROM {rel} LIMIT {int(rows)}")
            except duckdb.Error as exc:
                raise SourceLandingError(f"cannot read the file: {str(exc).splitlines()[0][:240]}") from exc
            names = [d[0] for d in cur.description]
            kinds = [d[1] for d in cur.description]
            out = []
            for r in cur.fetchall():
                out.append([None if v is None else (json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)) for v in r])
            nested = [n for n, k in zip(names, kinds) if "STRUCT" in str(k) or "LIST" in str(k) or "MAP" in str(k)]
            if nested:
                warnings.append(f"nested column(s) {nested} will be kept as JSON text")
        return names, out

    # ------------------------------------------------------------------ land
    def land(self, path: str | Path, filename: str, options: Optional[LandingOptions], out_dir: str | Path, user: str,
             origin: Optional[dict[str, Any]] = None) -> SnapshotInfo:
        """Land `path` into out_dir/source.parquet (+ snapshot.json and a copy of the original)."""
        options = options or LandingOptions()
        path, out_dir = Path(path), Path(out_dir)
        if not path.is_file() or path.stat().st_size == 0:
            raise SourceLandingError("the file is empty")
        out_dir.mkdir(parents=True, exist_ok=True)
        inner, _members, member = self._resolve(filename, options, path)
        fmt = self._format(inner, options)
        warnings: list[str] = []
        detection: dict[str, Any] = {}
        original_sha = sha256_of(path)

        with tempfile.TemporaryDirectory(prefix="landing_") as tmp:
            work = Path(tmp)
            data = self._unwrap(path, filename, member, work, head_only=False)
            parquet = work / PARQUET_FILE
            if fmt == "delimited":
                rejected = self._land_delimited(data, options, work, parquet, warnings, detection)
            elif fmt == "excel":
                rejected = self._land_excel(data, options, parquet, warnings, detection)
            elif fmt == "json":
                rejected = self._land_json(data, parquet, warnings)
            elif fmt == "fixed":
                rejected = self._land_fixed(data, options, work, parquet, warnings, detection)
            else:
                rejected = self._land_parquet(data, parquet)

            with bounded_connection(self._memory, self._threads) as con:
                rows = int(con.execute(f"SELECT count(*) FROM read_parquet({lit(parquet.as_posix())})").fetchone()[0])
            if rows == 0:
                raise SourceLandingError("the file contains no data rows")
            if options.expected_rows is not None and options.expected_rows != rows:
                raise SourceLandingError(
                    f"control total mismatch: {rows} row(s) were read but the source system reported {options.expected_rows}"
                    + (f" ({rejected} row(s) were rejected by the reader)" if rejected else ""))
            types = infer_types(parquet, self._memory)
            info = SnapshotInfo(
                format=fmt, original_filename=os.path.basename(filename), original_sha256=original_sha, original_bytes=path.stat().st_size,
                rows_landed=rows, rejected_rows=rejected, expected_rows=options.expected_rows,
                control_status="MATCH" if options.expected_rows is not None else "NOT_PROVIDED",
                columns=[ColumnInfo(name=n, detected_type=t) for n, t in types.items()],
                options=options.model_copy(update={"format": fmt, "member": member}), detection=detection, warnings=warnings,
                origin=origin or {}, landed_at=datetime.now(timezone.utc), landed_by=user)

            safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename))[:80]
            originals = out_dir / "original"
            originals.mkdir(exist_ok=True)
            shutil.copy2(path, originals / f"{original_sha[:12]}_{safe}")
            (out_dir / SNAPSHOT_FILE).write_text(info.model_dump_json(indent=2), encoding="utf-8")
            shutil.copyfile(parquet, out_dir / PARQUET_FILE)  # the work dir is removed on exit
        return info

    # ---- delimited
    def _land_delimited(self, data: Path, options: LandingOptions, work: Path, parquet: Path, warnings: list[str], detection: dict) -> int:
        encoding, basis = (options.encoding, "specified") if options.encoding else detect_encoding(data)
        utf8 = data if encoding.lower() in ("utf-8", "utf8") else _transcode(data, encoding, work, warnings)
        delimiter = options.delimiter or detect_delimiter(utf8, options.quote)
        detection.update(encoding=encoding, encoding_basis=basis, delimiter=delimiter)
        nulls = "[" + ", ".join(lit(t) for t in ["", *options.null_tokens]) + "]"
        rel = (f"read_csv({lit(utf8.as_posix())}, all_varchar=true, header={str(options.header).lower()}, delim={lit(delimiter)}, "
               f"quote={lit(options.quote or chr(34))}, skip={options.skip_rows}, nullstr={nulls}, store_rejects=true)")
        with bounded_connection(self._memory, self._threads) as con:
            try:
                names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()]
                select = ", ".join(f"{q(n)} AS {q(n if options.header else f'COLUMN_{i}')}" for i, n in enumerate(names, start=1))
                con.execute(f"COPY (SELECT {select} FROM {rel}) TO {lit(parquet.as_posix())} (FORMAT parquet)")
            except duckdb.Error as exc:
                raise SourceLandingError(f"cannot read the file as delimited text ({delimiter!r}, {encoding}): {str(exc).splitlines()[0][:240]}") from exc
            rejected = int(con.execute("SELECT count(DISTINCT line) FROM reject_errors").fetchone()[0])
            if rejected:
                sample = con.execute("SELECT line, error_type, error_message FROM reject_errors ORDER BY line LIMIT 5").fetchall()
                detail = "; ".join(f"line {line}: {kind}" for line, kind, _ in sample)
                if not options.tolerate_rejects:
                    raise SourceLandingError(
                        f"{rejected} malformed row(s) were rejected ({detail}). Fix the file, or set tolerate_rejects to land the rest.")
                warnings.append(f"{rejected} malformed row(s) were rejected and NOT landed ({detail})")
        return rejected

    # ---- excel
    @staticmethod
    def _sheet_names(path: Path) -> list[str]:
        SourceLandingService._check_xlsx(path)
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            return [ws.title for ws in wb.worksheets if ws.sheet_state == "visible"]
        finally:
            wb.close()

    @staticmethod
    def _check_xlsx(path: Path) -> None:
        if not zipfile.is_zipfile(path):
            raise SourceLandingError("the file is not a valid .xlsx workbook")
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > 2000 or sum(i.file_size for i in infos) > 4 * 1024 ** 3:
                raise SourceLandingError("the workbook is unreasonably large when unpacked")

    def _excel_iter(self, path: Path, options: LandingOptions, warnings: list[str]):
        """-> (column names, row generator of raw cell values, close callback). Streams the sheet."""
        self._check_xlsx(path)
        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - any parser failure means "not a usable workbook"
            raise SourceLandingError(f"cannot read the workbook: {exc}") from exc
        visible = [ws for ws in wb.worksheets if ws.sheet_state == "visible"]
        if options.sheet:
            if options.sheet not in wb.sheetnames:
                wb.close()
                raise SourceLandingError(f"sheet '{options.sheet}' not found (sheets: {wb.sheetnames})")
            ws = wb[options.sheet]
        elif visible:
            ws = visible[0]
            if len(visible) > 1:
                warnings.append(f"the workbook has {len(visible)} sheets; using '{ws.title}' (choose another with the 'sheet' option)")
        else:
            wb.close()
            raise SourceLandingError("the workbook has no visible sheet")
        rows = ws.iter_rows(values_only=True)
        for _ in range(options.header_row - 1):
            next(rows, None)
        header = next(rows, None)
        if header is None:
            wb.close()
            raise SourceLandingError("the sheet is empty")
        width = max((i for i, v in enumerate(header) if v is not None and str(v).strip()), default=-1) + 1
        if width == 0:
            wb.close()
            raise SourceLandingError(f"row {options.header_row} has no column names; set 'header_row'")
        names = _unique_names(list(header[:width]), warnings)

        def generate() -> Iterator[list[Any]]:
            try:
                for r in rows:
                    cells = list(r[:width]) + [None] * (width - len(r))
                    if any(c is not None and not (isinstance(c, str) and not c.strip()) for c in cells):
                        yield cells
            finally:
                wb.close()
        return names, generate(), wb

    def _land_excel(self, data: Path, options: LandingOptions, parquet: Path, warnings: list[str], detection: dict) -> int:
        names, rows, _ = self._excel_iter(data, options, warnings)
        detection["sheet"] = options.sheet or "(first visible)"
        numeric_cols: set[int] = set()
        with bounded_connection(self._memory, self._threads) as con:
            con.execute("CREATE TEMP TABLE t (" + ", ".join(f"{q(n)} VARCHAR" for n in names) + ")")
            batch: list[list[Optional[str]]] = []

            def flush() -> None:
                if batch:
                    con.register("chunk", pd.DataFrame(batch, columns=[f"c{i}" for i in range(len(names))], dtype=object))
                    con.execute("INSERT INTO t SELECT * FROM chunk")
                    con.unregister("chunk")
                    batch.clear()

            for cells in rows:
                for i, c in enumerate(cells):
                    if isinstance(c, (int, float)) and not isinstance(c, bool):
                        numeric_cols.add(i)
                batch.append([_cell_text(c) for c in cells])
                if len(batch) >= _ROWS_PER_BATCH:
                    flush()
            flush()
            con.execute(f"COPY t TO {lit(parquet.as_posix())} (FORMAT parquet)")
        risky = [names[i] for i in sorted(numeric_cols) if _NUMERIC_TEXT_HINT.search(names[i])]
        if risky:
            warnings.append(f"Excel stored column(s) {risky} as numbers: leading zeros may already have been lost in the workbook; "
                            "format those columns as Text in the source before exporting")
        return 0

    # ---- json
    @staticmethod
    def _json_relation(path: Path) -> str:
        return f"read_json({lit(path.as_posix())}, format='auto', sample_size=100000, maximum_object_size=67108864)"

    def _land_json(self, data: Path, parquet: Path, warnings: list[str]) -> int:
        with bounded_connection(self._memory, self._threads) as con:
            try:
                described = con.execute(f"DESCRIBE SELECT * FROM {self._json_relation(data)}").fetchall()
                parts, nested = [], []
                for name, kind, *_ in described:
                    if any(tag in str(kind) for tag in ("STRUCT", "LIST", "MAP", "[]")):
                        nested.append(name)
                        parts.append(f"CAST(to_json({q(name)}) AS VARCHAR) AS {q(name)}")
                    else:
                        parts.append(f"CAST({q(name)} AS VARCHAR) AS {q(name)}")
                con.execute(f"COPY (SELECT {', '.join(parts)} FROM {self._json_relation(data)}) TO {lit(parquet.as_posix())} (FORMAT parquet)")
            except duckdb.Error as exc:
                raise SourceLandingError(f"cannot read the file as JSON: {str(exc).splitlines()[0][:240]}") from exc
        if nested:
            warnings.append(f"nested column(s) {nested} were kept as JSON text; flatten them in a source view if their parts are needed")
        return 0

    # ---- fixed width
    @staticmethod
    def _fixed_iter(utf8: Path, options: LandingOptions) -> Iterator[list[Optional[str]]]:
        layout = options.columns or []
        with open(utf8, "r", encoding="utf-8", errors="replace", newline="") as fh:
            for n, line in enumerate(fh):
                if n < options.skip_rows:
                    continue
                line = line.rstrip("\r\n")
                if not line.strip():
                    continue
                yield [line[c.start - 1 : c.start - 1 + c.width].rstrip() or None for c in layout]

    def _land_fixed(self, data: Path, options: LandingOptions, work: Path, parquet: Path, warnings: list[str], detection: dict) -> int:
        if not options.columns:
            raise SourceLandingError("fixed-width files need a column layout (name, start, width for each column)")
        names = [c.name for c in options.columns]
        if len({n.lower() for n in names}) != len(names):
            raise SourceLandingError("fixed-width column names must be unique")
        encoding, basis = (options.encoding, "specified") if options.encoding else detect_encoding(data)
        detection.update(encoding=encoding, encoding_basis=basis)
        utf8 = data if encoding.lower() in ("utf-8", "utf8") else _transcode(data, encoding, work, warnings)
        with bounded_connection(self._memory, self._threads) as con:
            con.execute("CREATE TEMP TABLE t (" + ", ".join(f"{q(n)} VARCHAR" for n in names) + ")")
            batch: list[list[Optional[str]]] = []
            for row in self._fixed_iter(utf8, options):
                batch.append(row)
                if len(batch) >= _ROWS_PER_BATCH:
                    con.register("chunk", pd.DataFrame(batch, columns=[f"c{i}" for i in range(len(names))], dtype=object))
                    con.execute("INSERT INTO t SELECT * FROM chunk")
                    con.unregister("chunk")
                    batch.clear()
            if batch:
                con.register("chunk", pd.DataFrame(batch, columns=[f"c{i}" for i in range(len(names))], dtype=object))
                con.execute("INSERT INTO t SELECT * FROM chunk")
            con.execute(f"COPY t TO {lit(parquet.as_posix())} (FORMAT parquet)")
        warnings.append("fixed-width values are right-trimmed (padding is not data); leading spaces are kept")
        return 0

    # ---- parquet
    def _land_parquet(self, data: Path, parquet: Path) -> int:
        with bounded_connection(self._memory, self._threads) as con:
            try:
                names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit(data.as_posix())})").fetchall()]
                select = ", ".join(f"CAST({q(n)} AS VARCHAR) AS {q(n)}" for n in names)
                con.execute(f"COPY (SELECT {select} FROM read_parquet({lit(data.as_posix())})) TO {lit(parquet.as_posix())} (FORMAT parquet)")
            except duckdb.Error as exc:
                raise SourceLandingError(f"cannot read the file as Parquet: {str(exc).splitlines()[0][:240]}") from exc
        return 0
