"""SAP S/4HANA Migration Cockpit staging workbook generator (multi-sheet OOXML `.xlsx`, a ZIP of XML).

Input is Module 5's `valid.parquet`. Output is one workbook with five sheets, each keyed by
`BP_EXT` in column A:

    1  General Data (BUT000)        one row per business partner
    2  BP Roles (BUT100)            one row per partner and role (FLCU00 / FLCU01 ...)
    3  Address Data (ADRC)          partners with address data
    4  Company Code Data (KNB1)     partners with a company code (BUKRS)
    5  Sales Org Data (KNVV)        partners with a full sales area (VKORG/VTWEG/SPART)

Values are written as text so leading zeros survive; ALPHA conversion (zero-padding of numeric
keys to 10 characters) is applied by the shared `bp_model`. The generated file is read back and
checked for referential consistency before it is released: unique non-empty keys on sheet 1,
every child row's key present on sheet 1, and at least one role per partner.

This is a standards-compliant workbook with the cockpit's object structure and technical field
names. The proprietary LTMC template download (with its object-specific sheet layout and field
lists per migration object) differs by release; map these sheets onto it when loading.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Iterator, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from src.schemas.sap import BusinessPartnerRecord, CockpitResult
from src.services.bp_model import KEY_LENGTH, BpConfig, iter_bp_records

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

GENERAL, ROLES, ADDRESS, COMPANY, SALES = (
    "General Data (BUT000)", "BP Roles (BUT100)", "Address Data (ADRC)",
    "Company Code Data (KNB1)", "Sales Org Data (KNVV)",
)
SHEET_COLUMNS: dict[str, list[str]] = {
    GENERAL: ["BP_EXT", "TYPE", "BU_GROUP", "NAME_ORG1", "NAME_ORG2", "BU_SORT1"],
    ROLES: ["BP_EXT", "RLTYP"],
    ADDRESS: ["BP_EXT", "COUNTRY", "STREET", "HOUSE_NUM1", "CITY1", "POST_CODE1", "REGION", "LANGU"],
    COMPANY: ["BP_EXT", "BUKRS", "AKONT", "ZTERM", "ZWELS"],
    SALES: ["BP_EXT", "VKORG", "VTWEG", "SPART", "WAERS", "INCO1"],
}
SHEET_ORDER = [GENERAL, ROLES, ADDRESS, COMPANY, SALES]
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MAX_ISSUES = 20


class CockpitConsistencyError(Exception):
    def __init__(self, issues: list[str]) -> None:
        super().__init__("cockpit workbook failed validation: " + "; ".join(issues[:_MAX_ISSUES]))
        self.issues = issues


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _row_xml(row_number: int, values: list[Optional[str]]) -> str:
    cells = []
    for col, value in enumerate(values):
        if value is None or value == "":
            continue
        text = escape(_XML_ILLEGAL.sub("", value))
        cells.append(
            f'<c r="{column_letter(col)}{row_number}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'
        )
    return f'<row r="{row_number}">{"".join(cells)}</row>'


class CockpitGenerator:
    def __init__(self, config: BpConfig = BpConfig(), chunk_size: int = 1000) -> None:
        self._config = config
        self._chunk_size = chunk_size

    # ------------------------------------------------------------------ generate
    def generate(self, valid_parquet: str | Path, output_path: str | Path) -> CockpitResult:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_final = out.with_suffix(out.suffix + ".tmp")

        with tempfile.TemporaryDirectory(prefix="cockpit_") as tmp:
            spools = {name: open(Path(tmp) / f"sheet{i}.xml", "w+", encoding="utf-8") for i, name in enumerate(SHEET_ORDER, 1)}
            counts = {name: 1 for name in SHEET_ORDER}  # row 1 is the header
            try:
                partners, warnings, blocking = self._write_rows(valid_parquet, spools, counts)
                if blocking:
                    raise CockpitConsistencyError(blocking)
                self._assemble(tmp_final, spools)
            finally:
                for fh in spools.values():
                    fh.close()

        issues = validate_workbook(tmp_final)
        if issues:
            tmp_final.unlink(missing_ok=True)
            raise CockpitConsistencyError(issues)
        shutil.move(str(tmp_final), str(out))
        return CockpitResult(
            path=str(out), sheet_rows={n: c - 1 for n, c in counts.items()}, business_partners=partners,
            warnings=warnings,
        )

    def _write_rows(self, parquet, spools, counts) -> tuple[int, list[str], list[str]]:
        partners = 0
        warning_counts: dict[str, int] = {}
        blocking: list[str] = []

        def emit(sheet: str, values: list[Optional[str]]) -> None:
            counts[sheet] += 1
            spools[sheet].write(_row_xml(counts[sheet], values))

        for rec in iter_bp_records(parquet, self._config, self._chunk_size):
            if rec.blocking_issues:
                blocking.extend(f"record {rec.source_pk}: {issue}" for issue in rec.blocking_issues)
                if len(blocking) >= _MAX_ISSUES:
                    break
                continue
            partners += 1
            for w in rec.warnings:
                warning_counts[w] = warning_counts.get(w, 0) + 1
            emit(GENERAL, [rec.bp_ext] + [rec.general.get(c) for c in SHEET_COLUMNS[GENERAL][1:]])
            for role in rec.roles:
                emit(ROLES, [rec.bp_ext, role])
            if rec.address:
                emit(ADDRESS, [rec.bp_ext] + [rec.address.get(c) for c in SHEET_COLUMNS[ADDRESS][1:]])
            if rec.company_code:
                emit(COMPANY, [rec.bp_ext] + [rec.company_code.get(c) for c in SHEET_COLUMNS[COMPANY][1:]])
            if rec.sales_area:
                emit(SALES, [rec.bp_ext] + [rec.sales_area.get(c) for c in SHEET_COLUMNS[SALES][1:]])
        warnings = [f"{n} record(s): {w}" for w, n in sorted(warning_counts.items())]
        return partners, warnings, blocking

    @staticmethod
    def _assemble(path: Path, spools) -> None:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            sheets_xml = "".join(
                f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(SHEET_ORDER, 1))
            overrides = "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(SHEET_ORDER) + 1))
            zf.writestr("[Content_Types].xml",
                        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                        '<Default Extension="xml" ContentType="application/xml"/>'
                        '<Override PartName="/xl/workbook.xml" '
                        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                        f"{overrides}</Types>")
            zf.writestr("_rels/.rels",
                        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        f'<Relationships xmlns="{PKG_REL_NS}"><Relationship Id="rId1" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                        'Target="xl/workbook.xml"/></Relationships>')
            zf.writestr("xl/workbook.xml",
                        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{NS}" '
                        f'xmlns:r="{REL_NS}"><sheets>{sheets_xml}</sheets></workbook>')
            rels = "".join(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(SHEET_ORDER) + 1))
            zf.writestr("xl/_rels/workbook.xml.rels",
                        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{PKG_REL_NS}">{rels}</Relationships>')
            for i, name in enumerate(SHEET_ORDER, 1):
                header = _row_xml(1, SHEET_COLUMNS[name])
                with zf.open(f"xl/worksheets/sheet{i}.xml", "w") as entry:
                    entry.write(
                        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{NS}"><sheetData>{header}'.encode())
                    spools[name].seek(0)
                    while chunk := spools[name].read(1 << 20):
                        entry.write(chunk.encode("utf-8"))
                    entry.write(b"</sheetData></worksheet>")


# ---------------------------------------------------------------------- reading / validation
def _sheet_files(zf: zipfile.ZipFile) -> dict[str, str]:
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels}
    result = {}
    for sheet in workbook.iter(f"{{{NS}}}sheet"):
        result[sheet.get("name")] = "xl/" + targets[sheet.get(f"{{{REL_NS}}}id")]
    return result


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def iter_sheet_rows(path: str | Path, sheet: str) -> Iterator[list[str]]:
    """Yield each row of a sheet as a list of strings ('' for empty cells), parsed incrementally."""
    with zipfile.ZipFile(path) as zf:
        files = _sheet_files(zf)
        if sheet not in files:
            raise KeyError(f"sheet '{sheet}' not in workbook (has {sorted(files)})")
        with zf.open(files[sheet]) as fh:
            for _, el in ET.iterparse(fh, events=("end",)):
                if el.tag != f"{{{NS}}}row":
                    continue
                cells: dict[int, str] = {}
                for c in el.iter(f"{{{NS}}}c"):
                    cells[_col_index(c.get("r"))] = "".join(t.text or "" for t in c.iter(f"{{{NS}}}t"))
                width = max(cells) + 1 if cells else 0
                yield [cells.get(i, "") for i in range(width)]
                el.clear()


def list_sheets(path: str | Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return list(_sheet_files(zf))


def validate_workbook(path: str | Path) -> list[str]:
    """Referential-consistency check of a generated cockpit workbook; returns the issues found."""
    issues: list[str] = []
    sheets = list_sheets(path)
    if sheets != SHEET_ORDER:
        return [f"unexpected sheets {sheets}, expected {SHEET_ORDER}"]

    general_keys: set[str] = set()
    for i, row in enumerate(iter_sheet_rows(path, GENERAL)):
        if i == 0:
            if row[: len(SHEET_COLUMNS[GENERAL])] != SHEET_COLUMNS[GENERAL]:
                issues.append(f"{GENERAL}: unexpected header {row}")
            continue
        key = row[0] if row else ""
        if not key:
            issues.append(f"{GENERAL} row {i + 1}: empty BP_EXT")
        elif len(key) > KEY_LENGTH:
            issues.append(f"{GENERAL} row {i + 1}: BP_EXT '{key}' longer than {KEY_LENGTH}")
        elif key in general_keys:
            issues.append(f"{GENERAL} row {i + 1}: duplicate BP_EXT '{key}'")
        general_keys.add(key)

    roles_seen: set[str] = set()
    for name in (ROLES, ADDRESS, COMPANY, SALES):
        for i, row in enumerate(iter_sheet_rows(path, name)):
            if i == 0:
                if row[: len(SHEET_COLUMNS[name])] != SHEET_COLUMNS[name]:
                    issues.append(f"{name}: unexpected header {row}")
                continue
            key = row[0] if row else ""
            if key not in general_keys:
                issues.append(f"{name} row {i + 1}: BP_EXT '{key}' has no General Data row")
            elif name == ROLES:
                roles_seen.add(key)
    for key in sorted(general_keys - roles_seen)[:_MAX_ISSUES]:
        issues.append(f"{ROLES}: BP_EXT '{key}' has no role")
    return issues[: _MAX_ISSUES * 2]
