"""Source landing: every supported format lands as the same all-text Parquet snapshot (no database needed)."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import zipfile
from datetime import date, datetime

import duckdb
import pandas as pd
import pytest
from openpyxl import Workbook

from src.schemas.source import FixedColumn, LandingOptions
from src.services.profiler_service import ProfilerService
from src.services.source_landing import (
    SourceLandingError,
    SourceLandingService,
    detect_delimiter,
    detect_encoding,
    infer_types,
)
from tests.test_module4_transform_dedupe import build_dataset

svc = SourceLandingService()
HEADER = ["CUST_ID", "CUST_NAME", "ZIP", "AMOUNT"]
ROWS = [["1001", "Müller GmbH", "02134", "10.50"], ["1002", "Smith, Jones & \"Sons\"", "10001", "7"], ["1003", "Café Ñandú", "", "0.25"]]


def land(tmp_path, name: str, content: bytes | str, options: LandingOptions | None = None):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    out = tmp_path / f"out_{name}"
    info = svc.land(path, name, options, out, "alice")
    return info, out


def table(out) -> list[tuple]:
    return duckdb.connect().execute("SELECT * FROM read_parquet(?)", [str(out / "source.parquet")]).fetchall()


def csv_text(delimiter=",", rows=ROWS, header=HEADER) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


# ------------------------------------------------------------------ delimited
@pytest.mark.parametrize("delimiter,name", [(",", "a.csv"), (";", "b.txt"), ("\t", "c.tsv"), ("|", "d.psv")])
def test_delimiter_is_detected_and_data_lands_identically(tmp_path, delimiter, name):
    path = tmp_path / name
    path.write_text(csv_text(delimiter), encoding="utf-8")
    assert detect_delimiter(path) == delimiter
    preview = svc.preview(path, name)
    assert preview.delimiter == delimiter and preview.columns == HEADER and preview.rows[0] == ROWS[0]
    info, out = land(tmp_path, name, csv_text(delimiter))
    assert info.rows_landed == 3 and info.format == "delimited" and info.detection["delimiter"] == delimiter
    assert table(out) == [tuple(r[:2] + [r[2] or None, r[3]]) for r in ROWS]  # blank -> NULL, zero kept, quotes/commas intact


@pytest.mark.parametrize("encoding,bom,basis", [
    ("utf-8", b"", "valid UTF-8"), ("utf-8", b"\xef\xbb\xbf", "UTF-8 byte-order mark"),
    ("utf-16", b"", "UTF-16 byte-order mark"), ("cp1252", b"", "assumed Windows-1252"),
])
def test_encodings_are_detected_and_decoded(tmp_path, encoding, bom, basis):
    raw = bom + csv_text().encode(encoding)
    info, out = land(tmp_path, "e.csv", raw)
    assert basis in info.detection["encoding_basis"]
    rows = table(out)
    assert rows[0][1] == "Müller GmbH" and rows[2][1] == "Café Ñandú"
    assert rows[0][0] == "1001"  # a byte-order mark never ends up in the first column name
    assert [c.name for c in info.columns] == HEADER


def test_utf16_without_a_mark_and_explicit_encoding_override(tmp_path):
    raw = csv_text().encode("utf-16-le")
    path = tmp_path / "n.csv"
    path.write_bytes(raw)
    assert detect_encoding(path)[0] == "utf-16-le"
    info, out = land(tmp_path, "n.csv", raw)
    assert table(out)[0][1] == "Müller GmbH"
    # an explicit encoding wins over detection, and undecodable bytes are replaced and reported
    info2, _ = land(tmp_path, "o.csv", csv_text().encode("cp1252"), LandingOptions(encoding="ascii"))
    assert info2.detection["encoding_basis"] == "specified" and any("could not be decoded" in w for w in info2.warnings)
    with pytest.raises(ValueError, match="unknown encoding"):
        LandingOptions(encoding="not-a-codec")


def test_gzip_and_zip_containers(tmp_path):
    info, out = land(tmp_path, "g.csv.gz", gzip.compress(csv_text().encode()))
    assert info.rows_landed == 3 and table(out)[0][2] == "02134"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data/customers.csv", csv_text())
    info2, _ = land(tmp_path, "z.zip", buf.getvalue())
    assert info2.rows_landed == 3 and info2.options.member == "data/customers.csv"

    multi = io.BytesIO()
    with zipfile.ZipFile(multi, "w") as zf:
        zf.writestr("a.csv", csv_text())
        zf.writestr("b.csv", csv_text(rows=ROWS[:1]))
    (tmp_path / "m.zip").write_bytes(multi.getvalue())
    with pytest.raises(SourceLandingError, match="choose one with the 'member' option"):
        svc.land(tmp_path / "m.zip", "m.zip", None, tmp_path / "o1", "a")
    chosen, _ = land(tmp_path, "m2.zip", multi.getvalue(), LandingOptions(member="b.csv"))
    assert chosen.rows_landed == 1
    assert svc.preview(tmp_path / "m.zip", "m.zip", LandingOptions(member="a.csv")).members == ["a.csv", "b.csv"]
    with pytest.raises(SourceLandingError, match="not in the zip"):
        svc.land(tmp_path / "m.zip", "m.zip", LandingOptions(member="nope.csv"), tmp_path / "o2", "a")


def test_hostile_archives_are_refused(tmp_path):
    small = SourceLandingService(max_unpacked_mb=1)
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.csv", "A,B\n" + "x,y\n" * 800_000)  # ~3 MB unpacked, a few KB packed
    (tmp_path / "bomb.zip").write_bytes(bomb.getvalue())
    with pytest.raises(SourceLandingError, match="zip-bomb"):
        small.land(tmp_path / "bomb.zip", "bomb.zip", None, tmp_path / "o", "a")
    (tmp_path / "bomb.csv.gz").write_bytes(gzip.compress(b"A,B\n" + b"x,y\n" * 800_000))
    with pytest.raises(SourceLandingError, match="zip-bomb"):
        small.land(tmp_path / "bomb.csv.gz", "bomb.csv.gz", None, tmp_path / "o3", "a")

    slip = io.BytesIO()
    with zipfile.ZipFile(slip, "w") as zf:
        zf.writestr("../../evil.csv", csv_text())  # member names are never used as paths
    info, out = land(tmp_path, "slip.zip", slip.getvalue())
    assert info.rows_landed == 3 and not (tmp_path.parent / "evil.csv").exists()

    (tmp_path / "fake.zip").write_text("not a zip")
    with pytest.raises(SourceLandingError, match="not a valid .zip"):
        svc.land(tmp_path / "fake.zip", "fake.zip", None, tmp_path / "o4", "a")
    (tmp_path / "bad.csv.gz").write_bytes(b"\x1f\x8b garbage")
    with pytest.raises(SourceLandingError, match="decompress"):
        svc.land(tmp_path / "bad.csv.gz", "bad.csv.gz", None, tmp_path / "o5", "a")


def test_malformed_rows_fail_the_landing_unless_tolerated(tmp_path):
    text = "A,B,C\n1,2,3\n4,5\n6,7,8,9\n10,11,12\n"
    with pytest.raises(SourceLandingError, match="2 malformed row"):
        svc.land(_write(tmp_path, "r.csv", text), "r.csv", None, tmp_path / "o", "a")
    info, out = land(tmp_path, "r2.csv", text, LandingOptions(tolerate_rejects=True))
    assert info.rows_landed == 2 and info.rejected_rows == 2 and any("NOT landed" in w for w in info.warnings)
    assert table(out) == [("1", "2", "3"), ("10", "11", "12")]


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_control_total_null_tokens_header_and_skip_options(tmp_path):
    info, _ = land(tmp_path, "c.csv", csv_text(), LandingOptions(expected_rows=3))
    assert info.control_status == "MATCH" and info.expected_rows == 3
    with pytest.raises(SourceLandingError, match="control total mismatch: 3 row.*reported 4"):
        svc.land(_write(tmp_path, "c2.csv", csv_text()), "c2.csv", LandingOptions(expected_rows=4), tmp_path / "o", "a")

    _, out = land(tmp_path, "n.csv", "A,B\n1,NULL\n2,\\N\n3,x\n", LandingOptions(null_tokens=["NULL", "\\N"]))
    assert table(out) == [("1", None), ("2", None), ("3", "x")]

    _, out2 = land(tmp_path, "h.csv", "junk line\n1,2\n3,4\n", LandingOptions(header=False, skip_rows=1))
    assert table(out2) == [("1", "2"), ("3", "4")]
    info2, _ = land(tmp_path, "h2.csv", "junk line\n1,2\n", LandingOptions(header=False, skip_rows=1))
    assert [c.name for c in info2.columns] == ["COLUMN_1", "COLUMN_2"]


def test_empty_and_unsupported_files(tmp_path):
    for name, content, message in [("e.csv", b"", "empty"), ("h.csv", b"A,B\n", "no data rows"), ("x.xls", b"abc", ".xls"),
                                   ("x.docx", b"abc", "unsupported file type"), ("bad.parquet", b"not parquet", "cannot read the file as Parquet"),
                                   ("bad.json", b"{ not json", "cannot read the file as JSON")]:
        (tmp_path / name).write_bytes(content)
        with pytest.raises(SourceLandingError, match=message):
            svc.land(tmp_path / name, name, None, tmp_path / f"o_{name}", "a")


# ------------------------------------------------------------------ type inference
def test_types_are_inferred_from_text_and_leading_zeros_stay_text(tmp_path):
    rows = [["1", "02134", "1.5", "2024-01-31", "2024-01-31 10:00:00", "true", "x", "12345678901234567890", ""],
            ["2", "10001", "2", "2024-02-01", "2024-02-01 11:30:00", "FALSE", "y", "1", ""]]
    header = ["I", "ZIP", "D", "DT", "TS", "B", "S", "BIG", "EMPTY"]
    info, out = land(tmp_path, "t.csv", csv_text(rows=rows, header=header))
    assert {c.name: c.detected_type for c in info.columns} == {
        "I": "BIGINT", "ZIP": "VARCHAR", "D": "DOUBLE", "DT": "DATE", "TS": "TIMESTAMP", "B": "BOOLEAN", "S": "VARCHAR",
        "BIG": "VARCHAR", "EMPTY": "VARCHAR"}
    assert table(out)[0][1] == "02134"  # the landed text is untouched
    assert infer_types(out / "source.parquet") == info.detected_types


def test_profiler_reports_the_landed_types_not_text(tmp_path):
    info, out = land(tmp_path, "p.csv", csv_text())
    result = ProfilerService().profile_file(out / "source.parquet", detected_types=info.detected_types)
    types = {c.column_name: c.detected_type for c in result.columns}
    assert types == {"CUST_ID": "BIGINT", "CUST_NAME": "VARCHAR", "ZIP": "VARCHAR", "AMOUNT": "DOUBLE"}
    assert not any(c.leading_zero_risk for c in result.columns)
    assert {c.column_name: c.detected_type for c in ProfilerService().profile_file(out / "source.parquet").columns}["CUST_ID"] == "VARCHAR"


# ------------------------------------------------------------------ excel
def make_workbook(path, build):
    wb = Workbook()
    build(wb)
    wb.save(path)


def test_excel_sheets_header_row_text_fidelity_and_warnings(tmp_path):
    def build(wb):
        info = wb.active
        info.title = "Info"
        info.append(["Customer export", "generated 2024"])
        ws = wb.create_sheet("Customers")
        ws.append(["Confidential"]); ws.append([])
        ws.append(["CUST_ID", "NAME", "ZIP", "ZIP", None, "CREATED", "ACTIVE"])  # header on row 3, duplicate + blank header
        ws.append(["00123", "Müller", 2134, "x", "y", datetime(2024, 1, 31), True])  # ZIP is a NUMBER: zeros already lost
        ws.append([None, None, None, None, None, None, None])  # blank row is skipped
        ws.append(["00124", "Smith", "02134", "z", "w", datetime(2024, 2, 1, 9, 30), False])
        wb.create_sheet("Hidden").sheet_state = "hidden"
    path = tmp_path / "w.xlsx"
    make_workbook(path, build)

    preview = svc.preview(path, "w.xlsx")
    assert preview.sheets == ["Info", "Customers"] and "Hidden" not in preview.sheets
    assert any("2 sheets" in w or "using 'Info'" in w for w in preview.warnings)

    opts = LandingOptions(sheet="Customers", header_row=3)
    info = svc.land(path, "w.xlsx", opts, tmp_path / "o", "alice")
    assert info.rows_landed == 2 and [c.name for c in info.columns] == ["CUST_ID", "NAME", "ZIP", "ZIP_2", "COLUMN_5", "CREATED", "ACTIVE"]
    rows = table(tmp_path / "o")
    assert rows[0] == ("00123", "Müller", "2134", "x", "y", "2024-01-31", "TRUE")
    assert rows[1][2] == "02134" and rows[1][5] == "2024-02-01 09:30:00" and rows[1][6] == "FALSE"
    assert any("duplicate column header 'ZIP'" in w for w in info.warnings) and any("column 5 has no header" in w for w in info.warnings)
    assert any("stored column(s) ['ZIP'" in w and "leading zeros" in w for w in info.warnings)

    with pytest.raises(SourceLandingError, match="sheet 'Nope' not found"):
        svc.land(path, "w.xlsx", LandingOptions(sheet="Nope"), tmp_path / "o2", "a")
    with pytest.raises(SourceLandingError, match="has no column names"):
        svc.land(path, "w.xlsx", LandingOptions(sheet="Customers", header_row=2), tmp_path / "o3", "a")


def test_excel_default_sheet_and_invalid_workbooks(tmp_path):
    path = tmp_path / "s.xlsx"
    make_workbook(path, lambda wb: (wb.active.append(["A", "B"]), wb.active.append([1, 2.5]), wb.active.append(["x", date(2024, 5, 1)])))
    info = svc.land(path, "s.xlsx", None, tmp_path / "o", "a")
    assert table(tmp_path / "o") == [("1", "2.5"), ("x", "2024-05-01")] and info.format == "excel"
    (tmp_path / "bad.xlsx").write_text("not a workbook")
    with pytest.raises(SourceLandingError, match="not a valid .xlsx"):
        svc.land(tmp_path / "bad.xlsx", "bad.xlsx", None, tmp_path / "o2", "a")
    empty = tmp_path / "empty.xlsx"
    make_workbook(empty, lambda wb: None)
    with pytest.raises(SourceLandingError):
        svc.land(empty, "empty.xlsx", None, tmp_path / "o3", "a")


# ------------------------------------------------------------------ json / fixed-width / parquet
def test_json_array_ndjson_and_nested_values(tmp_path):
    records = [{"CUST_ID": "1001", "NAME": "Müller", "ZIP": "02134", "TAGS": ["a", "b"], "ADDR": {"city": "Berlin"}, "N": None},
               {"CUST_ID": "1002", "NAME": "Smith", "ZIP": "10001", "TAGS": [], "ADDR": {"city": "Paris"}, "N": None}]
    info, out = land(tmp_path, "a.json", json.dumps(records))
    assert info.rows_landed == 2 and table(out)[0][:3] == ("1001", "Müller", "02134")
    row = dict(zip([c.name for c in info.columns], table(out)[0]))
    assert json.loads(row["TAGS"]) == ["a", "b"] and json.loads(row["ADDR"]) == {"city": "Berlin"}
    assert any("nested column(s)" in w for w in info.warnings)

    info2, out2 = land(tmp_path, "b.ndjson", "\n".join(json.dumps(r) for r in records))
    assert info2.rows_landed == 2 and table(out2)[1][0] == "1002"
    assert svc.preview(tmp_path / "a.json", "a.json").format == "json"


def test_fixed_width_layout_encoding_and_validation(tmp_path):
    layout = [FixedColumn(name="CUST_ID", start=1, width=6), FixedColumn(name="NAME", start=7, width=14), FixedColumn(name="ZIP", start=21, width=5)]
    lines = ["HEADER LINE", "001001" + "Müller GmbH".ljust(14) + "02134", "001002" + "Smith".ljust(14) + "10001", "", "001003Café Ñandú"]
    raw = ("\n".join(lines) + "\n").encode("cp1252")
    info, out = land(tmp_path, "f.dat", raw, LandingOptions(format="fixed", columns=layout, skip_rows=1))
    assert info.rows_landed == 3 and table(out) == [("001001", "Müller GmbH", "02134"), ("001002", "Smith", "10001"), ("001003", "Café Ñandú", None)]
    assert any("right-trimmed" in w for w in info.warnings)
    assert svc.preview(tmp_path / "f.dat", "f.dat", LandingOptions(format="fixed", columns=layout)).rows[0][0] == "HEADER"[:6]
    with pytest.raises(SourceLandingError, match="column layout"):
        svc.land(tmp_path / "f.dat", "f.dat", LandingOptions(format="fixed"), tmp_path / "o", "a")
    with pytest.raises(SourceLandingError, match="unique"):
        svc.land(tmp_path / "f.dat", "f.dat", LandingOptions(format="fixed", columns=[layout[0], layout[0]]), tmp_path / "o2", "a")
    with pytest.raises(ValueError):
        FixedColumn(name="X", start=0, width=1)


def test_parquet_with_native_types_lands_as_text(tmp_path):
    frame = pd.DataFrame({"ID": [1, 2], "D": [date(2024, 1, 1), date(2024, 1, 2)], "AMT": [10.5, 2.0], "S": ["02134", None]})
    pq = tmp_path / "n.parquet"
    con = duckdb.connect()
    con.register("f", frame)
    con.execute(f"COPY f TO '{pq.as_posix()}' (FORMAT parquet)")
    info, out = land(tmp_path, "n.parquet", pq.read_bytes())
    assert table(out) == [("1", "2024-01-01", "10.5", "02134"), ("2", "2024-01-02", "2.0", None)]
    assert {c.name: c.detected_type for c in info.columns} == {"ID": "BIGINT", "D": "DATE", "AMT": "DOUBLE", "S": "VARCHAR"}


# ------------------------------------------------------------------ snapshot evidence
def test_snapshot_records_evidence_and_keeps_the_original(tmp_path):
    raw = csv_text(";").encode("cp1252")
    info, out = land(tmp_path, "evidence file.csv", raw)
    assert info.original_sha256 == hashlib.sha256(raw).hexdigest() and info.original_bytes == len(raw) and info.landed_by == "alice"
    assert info.options.format == "delimited" and info.detection == {"encoding": "cp1252", "encoding_basis": info.detection["encoding_basis"], "delimiter": ";"}
    saved = json.loads((out / "snapshot.json").read_text())
    assert saved["rows_landed"] == 3 and saved["control_status"] == "NOT_PROVIDED" and saved["original_filename"] == "evidence file.csv"
    originals = list((out / "original").iterdir())
    assert len(originals) == 1 and originals[0].read_bytes() == raw and originals[0].name.startswith(info.original_sha256[:12])


# ------------------------------------------------------------------ ACCEPTANCE: the same data in every format
def normalised(path) -> list[tuple]:
    """What the pipeline sees: text, trimmed, blank = NULL, in file order."""
    con = duckdb.connect()
    cols = [r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()]
    select = ", ".join(f"nullif(trim(\"{c}\"), '') AS \"{c}\"" for c in cols)
    return con.execute(f"SELECT {select} FROM read_parquet(?)", [str(path)]).fetchall()


def write_all_formats(tmp_path, rows: list[dict]) -> dict[str, tuple[str, bytes, LandingOptions]]:
    cols = list(rows[0])
    values = [[r[c] for c in cols] for r in rows]
    widths = {c: max(len(str(r[c])) for r in rows) + 2 for c in cols}
    starts, pos = {}, 1
    for c in cols:
        starts[c], pos = pos, pos + widths[c]
    layout = [FixedColumn(name=c, start=starts[c], width=widths[c]) for c in cols]

    def delimited(delim, encoding):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=delim, lineterminator="\r\n")
        w.writerow(cols); w.writerows(values)
        return buf.getvalue().encode(encoding)

    def xlsx():
        wb = Workbook(); ws = wb.active; ws.append(cols)
        for row in values:
            ws.append(row)
        b = io.BytesIO(); wb.save(b); return b.getvalue()

    fixed = "\n".join("".join(str(r[c]).ljust(widths[c]) for c in cols) for r in rows).encode("utf-8")
    con = duckdb.connect(); con.register("df", pd.DataFrame(values, columns=cols))
    pq = tmp_path / "ref.parquet"; con.execute(f"COPY df TO '{pq.as_posix()}' (FORMAT parquet)")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("export/customers.csv", delimited(",", "utf-8"))
    dicts = [dict(zip(cols, v)) for v in values]
    return {
        "csv-utf8": ("f1.csv", delimited(",", "utf-8"), LandingOptions()),
        "semicolon-cp1252": ("f2.txt", delimited(";", "cp1252"), LandingOptions()),
        "pipe-gzip": ("f3.psv.gz", gzip.compress(delimited("|", "utf-8")), LandingOptions()),
        "tsv-utf16": ("f4.tsv", b"\xff\xfe" + delimited("\t", "utf-16-le"), LandingOptions()),
        "csv-zip": ("f5.zip", zbuf.getvalue(), LandingOptions()),
        "excel": ("f6.xlsx", xlsx(), LandingOptions()),
        "json-array": ("f7.json", json.dumps(dicts).encode(), LandingOptions()),
        "ndjson": ("f8.ndjson", "\n".join(json.dumps(d) for d in dicts).encode(), LandingOptions()),
        "fixed-width": ("f9.dat", fixed, LandingOptions(format="fixed", columns=layout)),
        "parquet": ("f10.parquet", pq.read_bytes(), LandingOptions()),
    }


def test_same_data_in_ten_formats_lands_identically(tmp_path):
    rows = [{k: v for k, v in r.items()} for r in build_dataset()[:300]]
    # characters that stress encodings: accents, commas, quotes (Excel / fixed-width friendly: no control chars)
    rows[0]["CUST_NAME"], rows[1]["CUST_NAME"] = "Müller & Söhne, \"Ltd\"", "Café Ñandú GmbH"
    files = write_all_formats(tmp_path, rows)
    results = {}
    for label, (name, content, options) in files.items():
        path = tmp_path / name
        path.write_bytes(content)
        info = svc.land(path, name, options, tmp_path / f"out_{label}", "alice")
        assert info.rows_landed == 300, label
        results[label] = (info, normalised(tmp_path / f"out_{label}" / "source.parquet"))

    reference_info, reference = results["csv-utf8"]
    assert reference[0][1] == "Müller & Söhne, \"Ltd\"" and reference[1][1] == "Café Ñandú GmbH"
    assert any(None in r for r in reference)  # the dirty data really contains blanks
    for label, (info, data) in results.items():
        assert data == reference, label  # identical values, identical order, identical blanks
        assert info.detected_types == reference_info.detected_types, label  # identical inferred types

    # and the profile (what the steward sees) is identical too
    def profile(label):
        out = tmp_path / f"out_{label}" / "source.parquet"
        result = ProfilerService().profile_file(out, detected_types=results[label][0].detected_types)
        return [(c.column_name, c.detected_type, c.null_count, c.distinct_count, c.min_length, c.max_length,
                 [t.model_dump() for t in c.top_values]) for c in result.columns], result.total_rows

    expected = profile("csv-utf8")
    for label in files:
        assert profile(label) == expected, label
