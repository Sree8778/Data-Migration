"""Find personal data in legacy columns, so that it is treated as personal data without anybody having to remember to say so.

GDPR art. 25 asks for data protection by design and by default. Until now only the SAP *target* fields carried a classification; a legacy column was
INTERNAL unless a person changed it, so its sample values (names, e-mail addresses, phone numbers, tax numbers) sat unmasked in the catalog and in the profile
screens. Two independent signals are used, and the stricter wins; a classification is only ever raised, never lowered:

* the column NAME (EMAIL, TELF1, STCD1, IBAN, STRAS, NAME1, GEBDAT ...): known before a single value is read;
* the VALUES (an e-mail address, an IBAN, a card number that passes the Luhn check, a national-id shape): catches the column called ZFIELD7.

Levels follow the seeded SAP catalog: direct identifiers and contact/address/financial data are RESTRICTED_PII (stored only as shape masks); names and free
text are CONFIDENTIAL (kept out of AI prompts and masked on the profile screens).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from src.db.enums import PiiClassification

_RESTRICTED_NAME = re.compile(
    r"(^|_)(E_?MAIL|SMTP|MAIL|PHONE|TEL|TELF\d?|TELEFON|TELEPHONE|MOBILE|HANDY|FAX|"
    r"TAX|VAT|STCD\d?|STCEG|USTID|GSTIN|EIN|TIN|SSN|SIN|NIF|CNPJ|CPF|PASSPORT|NATIONAL_?ID|ID_?NUMBER|SOCIAL|"
    r"IBAN|BIC|SWIFT|BANK|BANKN|BANKL|KONTO|ACCOUNT_?(NO|NUM|NUMBER)|CARD|CC_?NUM|"
    r"DOB|BIRTH\w*|GEBURT\w*|GEBDAT|GENDER|SEX|RELIGION|ETHNIC\w*|HEALTH|"
    r"STREET|STRAS|STRASSE|ADDR|ADDRESS|HOUSE\w*|POSTAL|POST_?CODE|PSTLZ|ZIP|CITY|ORT\d*|TOWN|LAT|LON|LONGITUDE|LATITUDE|IP_?ADDR\w*)(_|\d|$)",
    re.IGNORECASE)
_CONFIDENTIAL_NAME = re.compile(
    r"(^|_)(NAME\d?|FIRST\w*|LAST\w*|SURNAME|FULL_?NAME|CUST_?NAME|CONTACT\w*|ATTN|SALUTATION|SORTL|SEARCH|"
    r"NOTES?|COMMENTS?|REMARKS?|DESCRIPTION|TEXT|MEMO|CREDIT\w*|KLIMK|SALARY|INCOME)(_|\d|$)", re.IGNORECASE)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
_PHONE = re.compile(r"^\+?[\d][\d \-./()]{6,18}\d$")
_CARD = re.compile(r"^\d{13,19}$")
_SSN = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_MIN_SAMPLES = 3


def _luhn(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
        flip = not flip
    return total % 10 == 0


def classify_name(column_name: str) -> PiiClassification:
    """What the name alone says. INTERNAL = nothing suspicious in it."""
    if _RESTRICTED_NAME.search(column_name or ""):
        return PiiClassification.RESTRICTED_PII
    if _CONFIDENTIAL_NAME.search(column_name or ""):
        return PiiClassification.CONFIDENTIAL
    return PiiClassification.INTERNAL


def classify_values(values: Iterable[object]) -> Optional[PiiClassification]:
    """RESTRICTED_PII when most of the sampled values have the shape of an e-mail address, IBAN, phone number, card number or national id, else None."""
    texts = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if len(texts) < _MIN_SAMPLES:
        return None

    def share(test) -> float:
        return sum(1 for t in texts if test(t)) / len(texts)

    compact = lambda t: re.sub(r"[ \-]", "", t)
    if share(lambda t: bool(_EMAIL.match(t))) >= 0.6:
        return PiiClassification.RESTRICTED_PII
    if share(lambda t: bool(_IBAN.match(compact(t).upper()))) >= 0.6:
        return PiiClassification.RESTRICTED_PII
    if share(lambda t: bool(_CARD.match(compact(t))) and _luhn(compact(t))) >= 0.6:
        return PiiClassification.RESTRICTED_PII
    if share(lambda t: bool(_SSN.match(t))) >= 0.6:
        return PiiClassification.RESTRICTED_PII
    if share(lambda t: bool(_PHONE.match(t)) and sum(ch.isdigit() for ch in t) >= 7 and ("+" in t or " " in t or "-" in t or "/" in t or "(" in t)) >= 0.6:
        return PiiClassification.RESTRICTED_PII
    return None


_ORDER = {PiiClassification.PUBLIC: 0, PiiClassification.INTERNAL: 1, PiiClassification.CONFIDENTIAL: 2, PiiClassification.RESTRICTED_PII: 3}


def stricter(a: PiiClassification, b: Optional[PiiClassification]) -> PiiClassification:
    return a if b is None or _ORDER[a] >= _ORDER[b] else b


def classify(column_name: str, values: Iterable[object] = (), current: PiiClassification = PiiClassification.INTERNAL) -> PiiClassification:
    """The classification a column should have: the strictest of what it already has, what its name says and what its values look like."""
    return stricter(stricter(current, classify_name(column_name)), classify_values(values))
