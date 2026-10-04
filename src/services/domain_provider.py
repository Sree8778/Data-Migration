"""Check-table domain sets for the pre-load validation (SAP check-table / foreign-key checks).

`DomainProvider` is the plug-in point: the default `SeedDomainProvider` is backed by the Module 4
seed sets, and `CsvDomainProvider` loads real exports (`<CHECKTABLE>.csv`) so production domains
can replace the seeds without touching the validation engine.

The seed domains are NOT authoritative SAP content. Country keys are the ISO 3166-1 alpha-2 list
(what SAP T005 holds for most installations); payment-term keys are only the codes the demo lookup
produces. Load the customer's real T052 / T005 / T002 extracts through `CsvDomainProvider`.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Mapping, Optional, Protocol

from src.services.lookup_service import COUNTRY_SEED, PAYMENT_TERMS_SEED

ISO_3166_ALPHA2 = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW
BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI
FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN
IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME
MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF
PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV
SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE
YT ZA ZM ZW
""".split())


class DomainProvider(Protocol):
    def get_domain(self, check_table: str) -> Optional[frozenset[str]]:
        """Valid keys of `check_table`, or None when the domain is unknown (check is then skipped)."""
        ...


class SeedDomainProvider:
    """Domains derived from the Module 4 lookup seeds (see module docstring for caveats)."""

    def __init__(self, extra: Optional[Mapping[str, frozenset[str]]] = None) -> None:
        payment_terms = frozenset(PAYMENT_TERMS_SEED.values())
        self._domains: dict[str, frozenset[str]] = {
            "T005": ISO_3166_ALPHA2 | frozenset(COUNTRY_SEED.values()),  # country
            "T052": payment_terms,  # payment terms
            "TVZBT": payment_terms,  # payment-terms texts (same keys)
            "TB0BK": frozenset({"1", "2", "3"}),  # BP category: person / organisation / group
            "T002": frozenset({"E", "D", "F", "S", "I", "P", "J"}),  # language keys
        }
        for name, values in (extra or {}).items():
            self._domains[name.upper()] = frozenset(values)

    def get_domain(self, check_table: str) -> Optional[frozenset[str]]:
        return self._domains.get(check_table.upper())


class CsvDomainProvider:
    """Reads `<directory>/<CHECKTABLE>.csv`; the key is the `value` column (or the first column)."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        self._cache: dict[str, Optional[frozenset[str]]] = {}

    def get_domain(self, check_table: str) -> Optional[frozenset[str]]:
        key = check_table.upper()
        if key not in self._cache:
            path = self._directory / f"{key}.csv"
            if not path.is_file():
                self._cache[key] = None
            else:
                with open(path, newline="", encoding="utf-8") as fh:
                    reader = csv.reader(fh)
                    header = next(reader, [])
                    col = header.index("value") if "value" in header else 0
                    self._cache[key] = frozenset(row[col].strip() for row in reader if row and row[col].strip())
        return self._cache[key]


class ChainedDomainProvider:
    """First provider that knows the check table wins (e.g. CSV exports first, seeds as fallback)."""

    def __init__(self, *providers: DomainProvider) -> None:
        self._providers = providers

    def get_domain(self, check_table: str) -> Optional[frozenset[str]]:
        for provider in self._providers:
            domain = provider.get_domain(check_table)
            if domain is not None:
                return domain
        return None


def default_domain_provider() -> DomainProvider:
    return SeedDomainProvider()
