"""A library of data quality rules for legacy customer / business-partner data, and the engine that assesses a landed table against it.

Why a library. A migration succeeds or fails on what is wrong with the legacy data, and most of what is wrong is the same everywhere: addresses with no usable
postal code, e-mail addresses with two addresses in one field, a tax number that is `00000000`, names mangled by a code page, `12/03/2021` that is the 12th of
March in one office and the 3rd of December in another. Established migration practice (assess first, cleanse, then map) puts a reusable rule set in front of
the mapping, so the business sees the findings, owns the fixes, and can measure the improvement between mock loads. The rules here are organised by the
dimensions of the data quality literature (ISO 8000 / ISO/IEC 25012 / DAMA): completeness, validity, uniqueness, consistency, conformity, credibility, integrity.

How it works. Every column is given a ROLE from its name (and, for e-mail, from its values): email, phone, tax, postal, country, name, date, amount, key, or
"any". Each rule says which roles it applies to and gives the condition, in SQL over the all-text landed column, that makes a value a violation. The whole table
is assessed in a few vectorised DuckDB passes. Findings carry a few example values (shape-masked for columns that hold personal data) and a hint what to do.
Every assessment is stored, so the next one shows the change: that is the monitoring.

What it is not. A format check is not proof: a well-formed VAT number can still be unregistered (that needs the VIES service), and a plausible postal code can
still not exist. Rules flag what is certainly or probably wrong, never "fix" anything; the person who owns the data decides.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from src.db.enums import PiiClassification
from src.schemas.quality import ColumnQuality, QualityHistoryItem, QualityReport, RuleInfo, RuleResult
from src.services._duckdb_util import DEFAULT_MEMORY_LIMIT_MB, bounded_connection, file_relation, fold, lit, q
from src.services.domain_provider import ISO_3166_ALPHA2
from src.services.pii_detector import classify_values

WEIGHT = {"HIGH": 3.0, "MEDIUM": 2.0, "LOW": 1.0}
_BATCH = 12   # columns per scan
_CONTROL = r"[\x00-\x1f\x7f-\x9f\x{202A}-\x{202E}\x{2066}-\x{2069}]"   # the pattern as DuckDB (RE2) reads it
_CONTROL_PY = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")   # the same set for Python

# ------------------------------------------------------------------ roles
_ROLE_NAMES = [
    ("email", r"(^|_)(E_?MAIL|SMTP)(_|\d|$)"),
    ("phone", r"(^|_)(PHONE|TEL|TELF\d?|TELEFON|TELEPHONE|MOBILE|HANDY|FAX)(_|\d|$)"),
    ("tax", r"(^|_)(TAX|VAT|STCD\d?|STCEG|USTID|GSTIN|EIN|TIN|NIF|CNPJ)(_|\d|$)"),
    ("postal", r"(^|_)(POSTAL|POST_?CODE|PSTLZ|ZIP)(_|\d|$)"),
    ("country", r"(^|_)(COUNTRY|LAND\d?|CTRY|NATION)(_|\d|$)"),
    ("name", r"(^|_)(NAME\d?|CUST_?NAME|FIRST\w*|LAST\w*|SURNAME|COMPANY)(_|\d|$)"),
    ("date", r"(^|_)(DATE|DAT|ERDAT|AEDAT|BIRTH\w*|CREATED\w*|CHANGED\w*|VALID_?(FROM|TO))(_|\d|$)|DATE$"),
    ("amount", r"(^|_)(AMOUNT|AMT|PRICE|BALANCE|KLIMK|LIMIT|REVENUE|TURNOVER)(_|\d|$)"),
]
_ROLE_PATTERNS = [(role, re.compile(rx, re.IGNORECASE)) for role, rx in _ROLE_NAMES]


def column_role(name: str, samples: list[str], is_key: bool = False) -> str:
    if is_key:
        return "key"
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(name or ""):
            return role
    values = [s for s in samples if s and s.strip()]
    if len(values) >= 5 and sum(1 for v in values if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", v.strip())) / len(values) >= 0.6:
        return "email"
    return "any"


# ------------------------------------------------------------------ reference data
COUNTRY_NAMES = {   # recognisable country names / spellings -> ISO 3166-1 alpha-2 (what a lookup table would translate)
    "USA": "US", "U.S.A": "US", "U.S.A.": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "AMERICA": "US",
    "GERMANY": "DE", "DEUTSCHLAND": "DE", "ALLEMAGNE": "DE", "FRANCE": "FR", "FRANKREICH": "FR", "ITALY": "IT", "ITALIA": "IT", "ITALIEN": "IT",
    "SPAIN": "ES", "ESPANA": "ES", "ESPAÑA": "ES", "SPANIEN": "ES", "UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB", "ENGLAND": "GB", "GROSSBRITANNIEN": "GB",
    "NETHERLANDS": "NL", "HOLLAND": "NL", "NEDERLAND": "NL", "NIEDERLANDE": "NL", "BELGIUM": "BE", "BELGIE": "BE", "BELGIQUE": "BE", "BELGIEN": "BE",
    "AUSTRIA": "AT", "OSTERREICH": "AT", "ÖSTERREICH": "AT", "SWITZERLAND": "CH", "SCHWEIZ": "CH", "SUISSE": "CH", "SVIZZERA": "CH", "PORTUGAL": "PT",
    "POLAND": "PL", "POLSKA": "PL", "POLEN": "PL", "SWEDEN": "SE", "SVERIGE": "SE", "SCHWEDEN": "SE", "DENMARK": "DK", "DANMARK": "DK", "NORWAY": "NO", "NORGE": "NO",
    "FINLAND": "FI", "SUOMI": "FI", "IRELAND": "IE", "LUXEMBOURG": "LU", "CZECH REPUBLIC": "CZ", "CZECHIA": "CZ", "GREECE": "GR", "HUNGARY": "HU", "ROMANIA": "RO",
    "CANADA": "CA", "KANADA": "CA", "MEXICO": "MX", "BRAZIL": "BR", "BRASIL": "BR", "BRASILIEN": "BR", "ARGENTINA": "AR", "CHILE": "CL", "COLOMBIA": "CO",
    "INDIA": "IN", "INDIEN": "IN", "CHINA": "CN", "JAPAN": "JP", "SOUTH KOREA": "KR", "KOREA": "KR", "AUSTRALIA": "AU", "NEW ZEALAND": "NZ", "SINGAPORE": "SG",
    "TURKEY": "TR", "TURKIYE": "TR", "SOUTH AFRICA": "ZA", "UAE": "AE", "UNITED ARAB EMIRATES": "AE", "SAUDI ARABIA": "SA", "ISRAEL": "IL", "EGYPT": "EG",
}
POSTAL_PATTERNS = {   # per ISO country: the shape of a postal code (after trim and upper-casing)
    "DE": r"\d{5}", "FR": r"\d{5}", "IT": r"\d{5}", "ES": r"\d{5}", "US": r"\d{5}(-\d{4})?", "GB": r"[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}", "NL": r"\d{4} ?[A-Z]{2}",
    "AT": r"\d{4}", "CH": r"\d{4}", "BE": r"\d{4}", "PT": r"\d{4}-\d{3}", "BR": r"\d{5}-?\d{3}", "JP": r"\d{3}-?\d{4}", "CA": r"[A-Z]\d[A-Z] ?\d[A-Z]\d", "IN": r"\d{6}",
    "AU": r"\d{4}", "PL": r"\d{2}-\d{3}", "SE": r"\d{3} ?\d{2}", "DK": r"\d{4}", "NO": r"\d{4}", "FI": r"\d{5}", "IE": r"[A-Z]\d{2} ?[A-Z\d]{4}", "LU": r"\d{4}",
}
TAX_PATTERNS = {   # per ISO country: the shape of a VAT / tax number once spaces and punctuation are removed (the country prefix is optional)
    "DE": r"(DE)?\d{9}", "AT": r"(ATU)?\d{8}", "FR": r"(FR)?[A-Z0-9]{2}\d{9}", "IT": r"(IT)?\d{11}", "ES": r"(ES)?[A-Z0-9]\d{7}[A-Z0-9]", "NL": r"(NL)?\d{9}B\d{2}",
    "GB": r"(GB)?(\d{9}|\d{12}|GD\d{3}|HA\d{3})", "US": r"(US)?\d{9}", "BE": r"(BE)?0?\d{9,10}", "PL": r"(PL)?\d{10}", "PT": r"(PT)?\d{9}", "SE": r"(SE)?\d{12}",
    "CH": r"(CHE)?\d{9}(MWST|TVA|IVA)?", "BR": r"(BR)?\d{14}", "DK": r"(DK)?\d{8}", "FI": r"(FI)?\d{8}", "IE": r"(IE)?\d[A-Z0-9+*]\d{5}[A-Z]{1,2}", "LU": r"(LU)?\d{8}",
}
PLACEHOLDERS = ("n/a", "na", "null", "none", "unknown", "tbd", "?", "??", "???", "-", "--", "---", "xxx", "test", "dummy", "not applicable", "missing", "undefined", "nan", "n.a.", "tba")
DATE_FORMATS = {"ISO yyyy-mm-dd": "%Y-%m-%d", "dd.mm.yyyy": "%d.%m.%Y", "dd/mm/yyyy": "%d/%m/%Y", "mm/dd/yyyy": "%m/%d/%Y", "yyyymmdd": "%Y%m%d",
                "dd-Mon-yyyy": "%d-%b-%Y", "Mon dd, yyyy": "%b %d, %Y", "yyyy/mm/dd": "%Y/%m/%d"}


# ------------------------------------------------------------------ the rules
@dataclass
class Ctx:
    country: Optional[str] = None   # SQL for the normalised ISO country of the row, when the table has a country column


@dataclass(frozen=True)
class Rule:
    rule_id: str
    title: str
    dimension: str
    severity: str
    roles: tuple[str, ...]            # "any" = every column
    hint: str
    violation: Optional[Callable[[str, Ctx], Optional[str]]] = None   # SQL boolean (True = violation) over the quoted column `c`, None = not applicable here
    kind: str = "row"                 # row | unique | mixture
    variants: dict[str, str] = field(default_factory=dict)            # mixture: label -> strptime format or pattern


def _digits(c: str) -> str:
    return f"length(regexp_replace({c}, '[^0-9]', '', 'g'))"


def _alnum(c: str) -> str:
    return f"upper(regexp_replace({c}, '[^A-Za-z0-9]', '', 'g'))"


def _country_pattern_case(c: str, ctx: Ctx, patterns: dict[str, str], value: str) -> Optional[str]:
    """True when the row's country has a known pattern and `value` does not match it (rows of other countries are not judged)."""
    if not ctx.country:
        return None
    arms = " ".join(f"WHEN {lit(cc)} THEN NOT regexp_full_match({value}, {lit(rx)})" for cc, rx in patterns.items())
    return f"CASE {ctx.country} {arms} ELSE false END"


RULES: list[Rule] = [
    # ---- any column
    Rule("G01", "Leading or trailing blanks", "Conformity", "LOW", ("any",), "Trim the value; blanks make equal values look different and break lookups.",
         lambda c, x: f"{c} <> trim({c})"),
    Rule("G02", "Only blanks", "Completeness", "MEDIUM", ("any",), "Treat as empty and fill from another source, or leave out.", lambda c, x: f"trim({c}) = ''"),
    Rule("G03", "Control characters or bidi overrides", "Integrity", "HIGH", ("any",), "NUL, line breaks, tabs and bidi overrides cannot be stored in SAP; fix at the source.",
         lambda c, x: f"regexp_matches({c}, {lit(_CONTROL)})"),
    Rule("G04", "Placeholder instead of a value", "Credibility", "MEDIUM", ("any",), "N/A, TBD, ???, TEST are not data: agree what to do with them (empty or a default).",
         lambda c, x: f"lower(trim({c})) IN ({', '.join(lit(p) for p in PLACEHOLDERS)})"),
    Rule("G05", "One character repeated", "Credibility", "LOW", ("any",), "AAAA or 000000 is a filler, not a value.",
         lambda c, x: f"length(trim({c})) >= 4 AND trim({c}) = repeat(left(trim({c}), 1), length(trim({c})))"),
    Rule("G06", "Garbled characters (wrong code page)", "Integrity", "MEDIUM", ("any",), "Mojibake such as 'Ã¼' for 'ü': re-export from the source with the right encoding.",
         lambda c, x: f"regexp_matches({c}, {lit('Ã.|â€|Â[\\x{00A0}-\\x{00BF}]|\\x{FFFD}')})"),
    Rule("G07", "HTML or markup inside the value", "Conformity", "MEDIUM", ("any",), "Strip the markup or the entities at the source.",
         lambda c, x: f"regexp_matches({c}, {lit('<[A-Za-z/][^>]*>|&[A-Za-z]{2,6};|&#[0-9]+;')})"),
    Rule("G08", "Repeated blanks inside the value", "Conformity", "LOW", ("any",), "Collapse double spaces.", lambda c, x: f"{c} LIKE '%  %'"),
    Rule("G09", "Longer than 255 characters", "Conformity", "LOW", ("any",), "Check whether the field is really free text; no SAP address or name field is this long.",
         lambda c, x: f"length({c}) > 255"),
    # ---- e-mail
    Rule("E01", "Not a valid e-mail address", "Validity", "HIGH", ("email",), "Correct the address or leave it empty.",
         lambda c, x: f"NOT regexp_full_match(trim({c}), {lit('[^@\\s,;]+@[^@\\s,;]+\\.[A-Za-z]{2,}')})"),
    Rule("E02", "Several addresses in one field", "Conformity", "MEDIUM", ("email",), "Keep one main address per partner; load the others as additional contacts.",
         lambda c, x: f"regexp_matches({c}, {lit('[;,]|\\s+and\\s+|\\s+/\\s+')})"),
    Rule("E03", "E-mail shared by several partners", "Uniqueness", "MEDIUM", ("email",), "A shared mailbox is fine, one person's address on several partners usually is not: decide which is the owner.",
         kind="unique"),
    # ---- phone
    Rule("P01", "Implausible number of digits", "Validity", "MEDIUM", ("phone",), "A phone number has 6 to 15 digits (E.164 allows 15).", lambda c, x: f"{_digits(c)} NOT BETWEEN 6 AND 15"),
    Rule("P02", "Letters in a phone number", "Conformity", "LOW", ("phone",), "Move extensions and remarks ('ext. 12', 'mobile') into their own field.", lambda c, x: f"regexp_matches({c}, '[A-Za-z]{{3,}}')"),
    Rule("P03", "Same digit throughout", "Credibility", "LOW", ("phone",), "0000000 or 1111111 is a filler.",
         lambda c, x: f"{_digits(c)} >= 6 AND regexp_replace({c}, '[^0-9]', '', 'g') = repeat(left(regexp_replace({c}, '[^0-9]', '', 'g'), 1), {_digits(c)})"),
    # ---- tax number
    Rule("T01", "Tax number too short to be one", "Validity", "HIGH", ("tax",), "Fewer than 5 letters or digits cannot be a VAT or tax number.", lambda c, x: f"length({_alnum(c)}) < 5"),
    Rule("T02", "Tax number does not fit the country's format", "Validity", "MEDIUM", ("tax",),
         "Format only (a well-formed number can still be unregistered; check with VIES). Shown only for rows whose country is known.",
         lambda c, x: _country_pattern_case(c, x, TAX_PATTERNS, _alnum(c))),
    Rule("T03", "Tax number used by several partners", "Uniqueness", "HIGH", ("tax",), "One tax number, several partners: duplicates, branches or a typo. Resolve before the load.", kind="unique"),
    # ---- postal code, country
    Rule("Z01", "Postal code does not fit the country's format", "Validity", "MEDIUM", ("postal",), "Shown only for rows whose country is known.",
         lambda c, x: _country_pattern_case(c, x, POSTAL_PATTERNS, f"upper(trim({c}))")),
    Rule("Z02", "Postal code has an implausible shape", "Validity", "LOW", ("postal",), "A postal code is up to 10 letters, digits, blanks and hyphens.",
         lambda c, x: f"NOT regexp_full_match(upper(trim({c})), '[A-Z0-9][A-Z0-9 \\-]{{1,9}}')"),
    Rule("C01", "Not a country SAP or a lookup can recognise", "Validity", "HIGH", ("country",), "Not an ISO code and not a recognisable name: correct it or map it by hand.",
         lambda c, x: f"{fold(c)} NOT IN ({', '.join(lit(v) for v in sorted(ISO_3166_ALPHA2 | set(COUNTRY_NAMES)))})"),
    Rule("C02", "Country written as a name, not an ISO code", "Conformity", "LOW", ("country",), "SAP wants the 2-letter key: this needs a lookup (see 'Draft lookups with AI').",
         lambda c, x: f"{fold(c)} NOT IN ({', '.join(lit(v) for v in sorted(ISO_3166_ALPHA2))}) AND {fold(c)} IN ({', '.join(lit(v) for v in sorted(COUNTRY_NAMES))})"),
    # ---- names
    Rule("N01", "Name of one character", "Completeness", "MEDIUM", ("name",), "Too short to identify anybody.", lambda c, x: f"length(trim({c})) < 2"),
    Rule("N02", "Name made of digits and punctuation only", "Validity", "MEDIUM", ("name",), "Probably an account number or a shifted column.", lambda c, x: f"regexp_full_match(trim({c}), '[0-9 .,/\\-]+')"),
    Rule("N03", "E-mail address in a name field", "Validity", "MEDIUM", ("name",), "A shifted column or a wrong export.", lambda c, x: f"{c} LIKE '%@%'"),
    Rule("N04", "Test or dummy customer", "Credibility", "MEDIUM", ("name",), "Leave these out with an exclusion rule (Studio: rows to leave out).",
         lambda c, x: f"regexp_matches({c}, {lit('(?i)(^|[^a-z])(test|dummy|do not use|delete me|xxx)([^a-z]|$)')})"),
    Rule("N05", "Name in capitals only", "Conformity", "LOW", ("name",), "Decide on a convention (SAP search terms are upper-case anyway); mixed case reads better in correspondence.",
         lambda c, x: f"upper({c}) = {c} AND regexp_matches({c}, '[A-Z]{{4,}}')"),
    # ---- dates
    Rule("D01", "Date that no known format can read", "Validity", "MEDIUM", ("date",), "Correct it or agree a rule for it.",
         lambda c, x: "(" + " AND ".join(f"try_strptime(trim({c}), {lit(f)}) IS NULL" for f in DATE_FORMATS.values()) + ")"),
    Rule("D02", "Day and month can be read either way", "Consistency", "HIGH", ("date",),
         "01/02/2021 is the 2nd of January in the US and the 1st of February in Europe. A load that guesses is wrong for half of them: fix the format at the source.",
         lambda c, x: f"try_strptime(trim({c}), '%d/%m/%Y') IS NOT NULL AND try_strptime(trim({c}), '%m/%d/%Y') IS NOT NULL AND "
                      f"try_strptime(trim({c}), '%d/%m/%Y') <> try_strptime(trim({c}), '%m/%d/%Y')"),
    Rule("D03", "Several date formats in one column", "Consistency", "MEDIUM", ("date",), "Convert to one format at the source; the load must not guess which one a row uses.",
         kind="mixture", variants=DATE_FORMATS),
    # ---- amounts
    Rule("A01", "Not a number", "Validity", "MEDIUM", ("amount",), "Fix the value, or the column holds text (currency symbols, remarks).",
         lambda c, x: f"NOT regexp_full_match(trim({c}), {lit('[-+]?[0-9]{1,3}([., ][0-9]{3})*([.,][0-9]+)?|[-+]?[0-9]+([.,][0-9]+)?')})"),
    Rule("A02", "Decimal comma and decimal point mixed", "Consistency", "HIGH", ("amount",), "1.234,56 and 1,234.56 in one column: a load that guesses is wrong; convert at the source.",
         kind="mixture", variants={"decimal comma": r"[0-9],[0-9]{1,2}$", "decimal point": r"[0-9]\.[0-9]{1,2}$"}),
    # ---- keys
    Rule("K01", "Key used by more than one row", "Uniqueness", "HIGH", ("key",), "A legacy key must identify one record.", kind="unique"),
    Rule("K02", "Key is blank", "Completeness", "HIGH", ("key",), "Every record needs its key.", lambda c, x: f"trim({c}) = ''"),
]
RULE_BY_ID = {r.rule_id: r for r in RULES}


def library() -> list[RuleInfo]:
    return [RuleInfo(rule_id=r.rule_id, title=r.title, dimension=r.dimension, severity=r.severity, applies_to=list(r.roles), hint=r.hint) for r in RULES]


def _applies(rule: Rule, role: str) -> bool:
    return "any" in rule.roles or role in rule.roles


# ------------------------------------------------------------------ assessment
def _mask(value: str) -> str:
    return "".join("9" if ch.isdigit() else ("A" if ch.isupper() else "a") if ch.isalpha() else ch for ch in value)


class QualityService:
    def __init__(self, workspace: Path, memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB) -> None:
        self._workspace = Path(workspace)
        self._memory = memory_limit_mb

    def _dir(self, table_id: int) -> Path:
        return self._workspace / "tables" / str(table_id) / "quality"

    # ------------------------------------------------------------------ run
    def assess(self, table_id: int, table_name: str, source: Path, user: str, key_columns: list[str], pii: dict[str, PiiClassification]) -> QualityReport:
        src = file_relation(source)[1]   # read_parquet(...) for a landed snapshot, read_csv(...) for a table that predates landing
        notes: list[str] = []
        with bounded_connection(self._memory) as con:
            names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
            rows = int(con.execute(f"SELECT count(*) FROM {src}").fetchone()[0])
            keys = {k.lower() for k in key_columns}
            roles: dict[str, str] = {}
            for n in names:
                sample = [r[0] for r in con.execute(f"SELECT {q(n)} FROM {src} WHERE {q(n)} IS NOT NULL LIMIT 200").fetchall()]
                roles[n] = column_role(n, sample, n.lower() in keys)
                if roles[n] == "any" and classify_values(sample) == PiiClassification.RESTRICTED_PII and n not in pii:
                    pii[n] = PiiClassification.RESTRICTED_PII   # looks personal even though its name says nothing
            country_cols = [n for n in names if roles[n] == "country"]
            ctx = Ctx()
            if country_cols:
                c = f"nullif({fold(q(country_cols[0]))}, '')"
                arms = " ".join(f"WHEN {lit(k)} THEN {lit(v)}" for k, v in sorted(COUNTRY_NAMES.items()))
                ctx.country = f"(CASE WHEN {c} IN ({', '.join(lit(v) for v in sorted(ISO_3166_ALPHA2))}) THEN {c} ELSE CASE {c} {arms} END END)"
                if len(country_cols) > 1:
                    notes.append(f"country-dependent rules use column {country_cols[0]} (the table has {len(country_cols)} country columns)")
            else:
                notes.append("no country column found: postal code and tax number formats were not judged per country")

            results: list[RuleResult] = []
            filled: dict[str, int] = {}
            for batch in (names[i:i + _BATCH] for i in range(0, len(names), _BATCH)):
                self._row_rules(con, src, batch, roles, ctx, rows, results, filled, pii)
            for n in names:
                for rule in RULES:
                    if rule.kind == "unique" and _applies(rule, roles[n]):
                        results.append(self._unique(con, src, rule, n, roles[n], pii))
                    elif rule.kind == "mixture" and _applies(rule, roles[n]):
                        results.append(self._mixture(con, src, rule, n, roles[n], pii))

        report = self._score(table_id, table_name, rows, user, names, roles, filled, results, pii, notes)
        return self._store(report)

    @staticmethod
    def _fix_filled(n: str, filled: dict[str, int]) -> int:
        return filled.get(n, 0)

    def _row_rules(self, con, src: str, batch: list[str], roles: dict, ctx: Ctx, rows: int, results: list, filled: dict, pii: dict) -> None:
        plan: list[tuple[str, Rule, str]] = []
        aggs = []
        for n in batch:
            c = q(n)
            aggs.append(f"count_if({c} IS NOT NULL)")
            for rule in RULES:
                if rule.kind != "row" or not _applies(rule, roles[n]):
                    continue
                cond = rule.violation(c, ctx)
                if cond is None:
                    continue
                aggs.append(f"count_if({c} IS NOT NULL AND coalesce({cond}, false))")
                plan.append((n, rule, cond))
        row = con.execute(f"SELECT {', '.join(aggs)} FROM {src}").fetchone()
        i = 0
        counts: dict[tuple[str, str], int] = {}
        for n in batch:
            filled[n] = int(row[i])
            i += 1
            for _, rule, _c in (p for p in plan if p[0] == n):
                counts[(n, rule.rule_id)] = int(row[i])
                i += 1
        for n, rule, cond in plan:
            v = counts[(n, rule.rule_id)]
            samples: list[str] = []
            if v:
                got = con.execute(f"SELECT DISTINCT {q(n)} FROM {src} WHERE {q(n)} IS NOT NULL AND coalesce({cond}, false) LIMIT 5").fetchall()
                samples = [self._show(str(x[0]), n, pii) for x in got]
            results.append(RuleResult(rule_id=rule.rule_id, column=n, role=roles[n], title=rule.title, dimension=rule.dimension, severity=rule.severity, checked=filled[n],
                                      violations=v, rate=round(v / filled[n], 4) if filled[n] else 0.0, samples=samples, hint=rule.hint))

    @staticmethod
    def _show(value: str, column: str, pii: dict) -> str:
        text = value if len(value) <= 60 else value[:57] + "..."
        text = _CONTROL_PY.sub("?", text)
        return _mask(text) if pii.get(column) in (PiiClassification.RESTRICTED_PII, PiiClassification.CONFIDENTIAL) else text

    def _norm(self, rule: Rule, role: str, c: str) -> str:
        if role == "tax":
            return f"nullif({_alnum(c)}, '')"
        if role == "email":
            return f"nullif(lower(trim({c})), '')"
        return f"nullif(trim({c}), '')"

    def _unique(self, con, src: str, rule: Rule, column: str, role: str, pii: dict) -> RuleResult:
        norm = self._norm(rule, role, q(column))
        total, rows_in_groups = con.execute(
            f"SELECT count(norm), count_if(n > 1) FROM (SELECT {norm} AS norm, count(*) OVER (PARTITION BY {norm}) AS n FROM {src}) WHERE norm IS NOT NULL").fetchone()
        samples: list[str] = []
        if rows_in_groups:
            got = con.execute(f"SELECT norm, count(*) FROM (SELECT {norm} AS norm FROM {src}) WHERE norm IS NOT NULL GROUP BY norm HAVING count(*) > 1 ORDER BY count(*) DESC, norm LIMIT 5").fetchall()
            samples = [f"{self._show(str(v), column, pii)} (x{n})" for v, n in got]
        return RuleResult(rule_id=rule.rule_id, column=column, role=role, title=rule.title, dimension=rule.dimension, severity=rule.severity, checked=int(total),
                          violations=int(rows_in_groups), rate=round(int(rows_in_groups) / int(total), 4) if total else 0.0, samples=samples, hint=rule.hint)

    def _mixture(self, con, src: str, rule: Rule, column: str, role: str, pii: dict) -> RuleResult:
        c = q(column)
        tests = {}
        for label, spec in rule.variants.items():
            tests[label] = (f"try_strptime(trim({c}), {lit(spec)}) IS NOT NULL" if "%" in spec else f"regexp_matches(trim({c}), {lit(spec)})")
        row = con.execute("SELECT count_if(" + f"{c} IS NOT NULL" + "), " + ", ".join(f"count_if({c} IS NOT NULL AND {t})" for t in tests.values()) + f" FROM {src}").fetchone()
        checked, counts = int(row[0]), {label: int(n) for label, n in zip(tests, row[1:])}
        used = {k: v for k, v in counts.items() if v}
        minority = 0
        if len(used) > 1:
            # a value can fit several formats (01/02/2021 is both day/month and month/day), so the minority is counted by row: recognised by some format, but not by the dominant one
            lead = max(used, key=used.get)
            other = con.execute(f"SELECT count_if({c} IS NOT NULL AND NOT ({tests[lead]}) AND ({' OR '.join(tests.values())})) FROM {src}").fetchone()[0]
            minority = int(other) if other >= max(2, 0.01 * checked) else 0   # a real mixture needs more than a stray value
        samples = [f"{label}: {n:,} row(s)" for label, n in sorted(used.items(), key=lambda kv: -kv[1])] if minority else []
        return RuleResult(rule_id=rule.rule_id, column=column, role=role, title=rule.title, dimension=rule.dimension, severity=rule.severity, checked=checked,
                          violations=minority, rate=round(minority / checked, 4) if checked else 0.0, samples=samples, hint=rule.hint)

    # ------------------------------------------------------------------ scoring and history
    def _score(self, table_id, table_name, rows, user, names, roles, filled, results, pii, notes) -> QualityReport:
        by_col: dict[str, list[RuleResult]] = {n: [] for n in names}
        for r in results:
            by_col[r.column].append(r)

        def score(rs: list[RuleResult]) -> float:
            weight = sum(WEIGHT[r.severity] for r in rs if r.checked)
            return 100.0 if weight == 0 else round(100.0 * (1 - sum(WEIGHT[r.severity] * r.rate for r in rs if r.checked) / weight), 1)

        columns = [ColumnQuality(column=n, role=roles[n], pii=(pii.get(n) or PiiClassification.INTERNAL).value, filled=filled.get(n, 0), score=score(by_col[n]),
                                 findings=sum(1 for r in by_col[n] if r.violations)) for n in names]
        dims: dict[str, float] = {}
        for dim in sorted({r.dimension for r in results}):
            dims[dim] = score([r for r in results if r.dimension == dim])
        findings = sorted((r for r in results if r.violations), key=lambda r: (-WEIGHT[r.severity], -r.violations, r.column, r.rule_id))   # by severity, then by how many rows
        overall = round(sum(c.score for c in columns) / len(columns), 1) if columns else 100.0
        previous = self.latest(table_id)
        return QualityReport(table_id=table_id, table_name=table_name, assessment_no=(previous.assessment_no + 1) if previous else 1, assessed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             assessed_by=user, rows=rows, rules_run=len(results), overall_score=overall, dimension_scores=dims, columns=columns, findings=findings,
                             previous_score=previous.overall_score if previous else None, change=round(overall - previous.overall_score, 1) if previous else None, notes=notes)

    def _store(self, report: QualityReport) -> QualityReport:
        folder = self._dir(report.table_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"assessment_{report.assessment_no:04d}.json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
        return report

    def latest(self, table_id: int) -> Optional[QualityReport]:
        files = sorted(self._dir(table_id).glob("assessment_*.json"))
        if not files:
            return None
        try:
            return QualityReport.model_validate_json(files[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def get(self, table_id: int, assessment_no: int) -> Optional[QualityReport]:
        path = self._dir(table_id) / f"assessment_{assessment_no:04d}.json"
        try:
            return QualityReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def history(self, table_id: int) -> list[QualityHistoryItem]:
        out = []
        for path in sorted(self._dir(table_id).glob("assessment_*.json")):
            try:
                r = QualityReport.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append(QualityHistoryItem(assessment_no=r.assessment_no, assessed_at=r.assessed_at, assessed_by=r.assessed_by, overall_score=r.overall_score, findings=len(r.findings),
                                          high_findings=sum(1 for f in r.findings if f.severity == "HIGH"), rows=r.rows))
        return out
