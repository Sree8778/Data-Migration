"""SAP ingestion clients: a deterministic sandbox simulator and a live OData adapter.

Both implement `SapClient.create_business_partners(records) -> list[SapRecordResult]`, returning
SAP-style BAPIRET2 messages (TYPE / ID / NUMBER / MESSAGE) per record, so the loader never cares
which one it talks to.

* `SandboxSapClient`  - in-memory simulator for tests / dry runs. Applies the checks SAP's BP
  creation would (category, grouping, organisation name, country, valid roles, unique external
  key) and hands out sequential partner numbers. Message IDs/numbers are simulator values.
* `HttpSapClient`     - S/4HANA `API_BUSINESS_PARTNER` OData v2 (deep insert with roles, address,
  customer company-code and sales-area data). It fetches and refreshes the CSRF token, maps OData
  errors onto BAPIRET2 and raises `SapAuthError` / `SapUnavailableError` when the whole batch cannot
  proceed. Entity and field names follow the published API; confirm against your system's
  `$metadata`, and set `SAP_BP_EXTERNAL_ID_FIELD` if the legacy key should be sent to SAP.

Live configuration (environment): SAP_BASE_URL, SAP_USER, SAP_PASSWORD, optional SAP_CLIENT and
SAP_BP_EXTERNAL_ID_FIELD.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol

import httpx

from src.schemas.sap import BapiRet2, BusinessPartnerRecord, SapRecordResult
from src.services.bp_model import VALID_ROLES

BP_ENTITY = "/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner"
BP_SERVICE_ROOT = "/sap/opu/odata/sap/API_BUSINESS_PARTNER/"


class SapError(Exception):
    pass


class SapAuthError(SapError):
    """401: credentials rejected. Loading stops."""


class SapUnavailableError(SapError):
    """SAP unreachable or failing with 5xx after a retry. Loading stops."""


class SapClient(Protocol):
    def create_business_partners(self, records: list[BusinessPartnerRecord]) -> list[SapRecordResult]: ...


# ====================================================================== sandbox
class SandboxSapClient:
    def __init__(
        self,
        first_partner: int = 1000000001,
        reject: Optional[Callable[[BusinessPartnerRecord], Optional[BapiRet2]]] = None,
    ) -> None:
        self._next = first_partner
        self._reject = reject
        self.created: dict[str, str] = {}  # BP_EXT -> partner number
        self.batch_sizes: list[int] = []

    def create_business_partners(self, records: list[BusinessPartnerRecord]) -> list[SapRecordResult]:
        self.batch_sizes.append(len(records))
        return [self._create(r) for r in records]

    def _create(self, rec: BusinessPartnerRecord) -> SapRecordResult:
        errors = self._checks(rec)
        if errors:
            return SapRecordResult(source_pk=rec.source_pk, messages=errors)
        partner = str(self._next).zfill(10)
        self._next += 1
        self.created[rec.bp_ext or rec.source_pk] = partner
        messages = [BapiRet2(TYPE="S", ID="R1", NUMBER="000", MESSAGE=f"Business partner {partner} created")]
        if rec.address.get("COUNTRY") in ("US", "CA", "IN") and "REGION" not in rec.address:
            messages.append(BapiRet2(TYPE="W", ID="R1", NUMBER="050", MESSAGE="Region is empty for this country"))
        return SapRecordResult(source_pk=rec.source_pk, partner=partner, messages=messages)

    def _checks(self, rec: BusinessPartnerRecord) -> list[BapiRet2]:
        def err(number: str, message: str) -> BapiRet2:
            return BapiRet2(TYPE="E", ID="R1", NUMBER=number, MESSAGE=message)

        out: list[BapiRet2] = []
        category = rec.general.get("TYPE")
        if not category:
            out.append(err("006", "Business partner category is missing"))
        elif category not in ("1", "2", "3"):
            out.append(err("007", f"Business partner category {category} is invalid"))
        if not rec.general.get("BU_GROUP"):
            out.append(err("008", "Business partner grouping is missing"))
        if category == "2" and not rec.general.get("NAME_ORG1"):
            out.append(err("009", "Name 1 is required for an organization"))
        if rec.address and not rec.address.get("COUNTRY"):
            out.append(err("011", "Country is required in the address"))
        bad_roles = [r for r in rec.roles if r not in VALID_ROLES]
        if bad_roles or not rec.roles:
            out.append(err("012", f"BP role {bad_roles or 'missing'} is not allowed"))
        if rec.bp_ext and rec.bp_ext in self.created:
            out.append(err("010", f"Business partner with external key {rec.bp_ext} already exists"))
        if self._reject is not None:
            custom = self._reject(rec)
            if custom is not None:
                out.append(custom)
        return out


# ====================================================================== live OData adapter
@dataclass(frozen=True)
class SapConfig:
    base_url: str
    user: str
    password: str = field(repr=False)
    client: Optional[str] = None
    external_id_field: Optional[str] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Optional["SapConfig"]:
        env = os.environ if env is None else env
        url, user, password = (env.get(k, "").strip() for k in ("SAP_BASE_URL", "SAP_USER", "SAP_PASSWORD"))
        if not (url and user and password):
            return None
        return cls(
            base_url=url.rstrip("/"), user=user, password=password,
            client=(env.get("SAP_CLIENT") or "").strip() or None,
            external_id_field=(env.get("SAP_BP_EXTERNAL_ID_FIELD") or "").strip() or None,
        )


def build_odata_payload(rec: BusinessPartnerRecord, external_id_field: Optional[str] = None) -> dict[str, Any]:
    """Deep-insert body for A_BusinessPartner (omits empty fields)."""
    def pick(source: Mapping[str, str], mapping: Mapping[str, str]) -> dict[str, str]:
        return {odata: source[k] for k, odata in mapping.items() if k in source}

    body: dict[str, Any] = pick(rec.general, {
        "TYPE": "BusinessPartnerCategory", "BU_GROUP": "BusinessPartnerGrouping",
        "NAME_ORG1": "OrganizationBPName1", "NAME_ORG2": "OrganizationBPName2", "BU_SORT1": "SearchTerm1",
    })
    if external_id_field and rec.bp_ext:
        body[external_id_field] = rec.bp_ext
    body["to_BusinessPartnerRole"] = {"results": [{"BusinessPartnerRole": r} for r in rec.roles]}
    if rec.address:
        body["to_BusinessPartnerAddress"] = {"results": [pick(rec.address, {
            "COUNTRY": "Country", "STREET": "StreetName", "HOUSE_NUM1": "HouseNumber", "CITY1": "CityName",
            "POST_CODE1": "PostalCode", "REGION": "Region", "LANGU": "Language"})]}
    customer: dict[str, Any] = {}
    if rec.company_code:
        customer["to_CustomerCompany"] = {"results": [pick(rec.company_code, {
            "BUKRS": "CompanyCode", "AKONT": "ReconciliationAccount", "ZTERM": "PaymentTerms",
            "ZWELS": "PaymentMethodsList"})]}
    if rec.sales_area:
        customer["to_CustomerSalesArea"] = {"results": [pick(rec.sales_area, {
            "VKORG": "SalesOrganization", "VTWEG": "DistributionChannel", "SPART": "Division",
            "WAERS": "Currency", "INCO1": "IncotermsClassification"})]}
    if customer:
        body["to_Customer"] = customer
    return body


class HttpSapClient:
    def __init__(
        self,
        config: SapConfig,
        transport: Optional[httpx.BaseTransport] = None,
        retry_delay: float = 1.0,
    ) -> None:
        self._config = config
        self._retry_delay = retry_delay
        params = {"sap-client": config.client} if config.client else None
        self._http = httpx.Client(
            base_url=config.base_url, auth=httpx.BasicAuth(config.user, config.password), timeout=60.0,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            params=params, transport=transport,
        )
        self._csrf: Optional[str] = None

    def create_business_partners(self, records: list[BusinessPartnerRecord]) -> list[SapRecordResult]:
        return [self._create(r) for r in records]

    # ------------------------------------------------------------------ internals
    def _fetch_csrf(self) -> str:
        response = self._send("GET", BP_SERVICE_ROOT, headers={"X-CSRF-Token": "Fetch"})
        token = response.headers.get("x-csrf-token")
        if not token:
            raise SapUnavailableError("SAP did not return a CSRF token")
        self._csrf = token
        return token

    def _create(self, rec: BusinessPartnerRecord) -> SapRecordResult:
        body = build_odata_payload(rec, self._config.external_id_field)
        for attempt in (1, 2):
            token = self._csrf or self._fetch_csrf()
            response = self._send("POST", BP_ENTITY, json=body, headers={"X-CSRF-Token": token})
            if response.status_code == 403 and response.headers.get("x-csrf-token", "").lower() == "required" and attempt == 1:
                self._csrf = None  # token expired: fetch a fresh one and retry once
                continue
            break
        if response.status_code in (200, 201):
            partner = (response.json().get("d") or {}).get("BusinessPartner")
            if partner:
                return SapRecordResult(source_pk=rec.source_pk, partner=str(partner), messages=[
                    BapiRet2(TYPE="S", ID="OData", NUMBER=str(response.status_code),
                             MESSAGE=f"Business partner {partner} created")])
            return SapRecordResult(source_pk=rec.source_pk, messages=[
                BapiRet2(TYPE="E", ID="OData", NUMBER="000", MESSAGE="response contained no BusinessPartner key")])
        return SapRecordResult(source_pk=rec.source_pk, messages=self._parse_error(response))

    @staticmethod
    def _parse_error(response: httpx.Response) -> list[BapiRet2]:
        try:
            error = response.json()["error"]
        except (ValueError, KeyError, TypeError):
            return [BapiRet2(TYPE="E", ID="HTTP", NUMBER=str(response.status_code), MESSAGE=response.text[:300] or "error")]

        def to_ret(code: str, message: str, severity: str = "error") -> BapiRet2:
            msg_id, _, number = (code or "").partition("/")
            return BapiRet2(TYPE="W" if severity == "warning" else "E", ID=msg_id[:20], NUMBER=number[:3],
                            MESSAGE=(message or "")[:500])

        out = [to_ret(error.get("code", ""), (error.get("message") or {}).get("value", ""))]
        for detail in (error.get("innererror") or {}).get("errordetails", []) or []:
            out.append(to_ret(detail.get("code", ""), detail.get("message", ""), detail.get("severity", "error")))
        return out

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in (1, 2):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise SapUnavailableError(f"cannot reach SAP: {exc!r}") from exc
                time.sleep(self._retry_delay)
                continue
            if response.status_code == 401:
                raise SapAuthError("SAP rejected the credentials (HTTP 401)")
            if response.status_code >= 500 or response.status_code == 429:
                if attempt == 2:
                    raise SapUnavailableError(f"SAP returned HTTP {response.status_code}")
                time.sleep(self._retry_delay)
                continue
            return response
        raise SapUnavailableError("unreachable")  # pragma: no cover
