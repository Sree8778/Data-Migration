"""Diagnostics for the live SAP connection: find what would go wrong BEFORE a load, in a system we cannot see from here.

The live adapter has only ever been exercised against a simulator and mocks. The most likely real-world failures are not logic errors but
mismatches with the particular system: a service that is not activated, a user without authorisation, a session that does not survive a load
balancer, a field name the release spells differently, a field that holds fewer characters than our validation allows. Each of those is
checked here against the system's own answers, in order, and every failure says what to do about it:

    1 connection      the Business Partner service answers and accepts the credentials
    2 csrf_session    a CSRF token is issued and the session cookie that must accompany it is set
    3 metadata        the entities and every property this application sends exist in the system's $metadata, and no field is shorter than our validation assumes
    4 read_access     the user may read business partners
    5 write_test      (only when asked) one clearly marked test partner is created, read back and compared
    6 error_mapping   (only with the write test) an invalid payload is rejected and its SAP message is understood

Nothing is written unless `write_test` is passed; even then it is one partner, named so that it can be recognised and archived.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Mapping, Optional

import httpx

from src.schemas.sap import BusinessPartnerRecord
from src.services.sap_client import (
    BP_ENTITY, BP_SERVICE_ROOT, ENTITY_PROPERTIES, HttpSapClient, SapAuthError, SapConfig, SapUnavailableError, build_odata_payload)

METADATA_URL = BP_SERVICE_ROOT + "$metadata"
NAVIGATIONS = ("to_BusinessPartnerRole", "to_BusinessPartnerAddress", "to_Customer", "to_Supplier", "to_BusinessPartnerTaxNumber", "to_BusinessPartnerBank")
# navigations of the other entities we deep-insert through
ENTITY_NAVIGATIONS = {"A_BusinessPartnerAddress": ("to_EmailAddress", "to_PhoneNumber", "to_FaxNumber")}
TEST_MARK = "MIGRATION TEST - SAFE TO ARCHIVE"


@dataclass
class Diagnostic:
    name: str
    status: str  # pass | warn | fail | skip
    detail: str
    hint: str = ""
    data: dict = field(default_factory=dict)


class SapDiagnostics:
    def __init__(self, config: SapConfig, transport: Optional[httpx.BaseTransport] = None) -> None:
        self._config = config
        self._transport = transport
        self._client = HttpSapClient(config, transport=transport, retry_delay=0.2)

    # ------------------------------------------------------------------ the sequence
    def run(self, write_test: bool = False, expected_lengths: Optional[Mapping[str, int]] = None, test_grouping: str = "BP01") -> list[Diagnostic]:
        out = [self._connection()]
        if out[0].status == "fail":
            return out + [Diagnostic(n, "skip", "not run: the connection failed") for n in ("csrf_session", "metadata", "read_access", "write_test", "error_mapping")]
        out.append(self._csrf())
        out.append(self._metadata(expected_lengths or {}))
        out.append(self._read())
        if write_test:
            created = self._write(test_grouping)
            out.append(created)
            out.append(self._errors())
        else:
            out += [Diagnostic("write_test", "skip", "not run: it creates one test partner, so it only runs when you ask for it"),
                    Diagnostic("error_mapping", "skip", "not run (needs the write test)")]
        return out

    # ------------------------------------------------------------------ 1 connection
    def _get(self, path: str, **kwargs):
        return self._client._http.get(path, **kwargs)

    def _connection(self) -> Diagnostic:
        try:
            r = self._get(BP_SERVICE_ROOT)
        except httpx.HTTPError as exc:
            return Diagnostic("connection", "fail", f"cannot reach {self._config.base_url}: {type(exc).__name__}",
                              "check SAP_BASE_URL, the port, the network route and the TLS certificate of the system")
        if r.status_code == 401:
            return Diagnostic("connection", "fail", "SAP rejected the user name or password (HTTP 401)", "check SAP_USER / SAP_PASSWORD, and that the user is not locked in SU01")
        if r.status_code == 403:
            return Diagnostic("connection", "fail", "the user is authenticated but not allowed to use the service (HTTP 403)",
                              "give the user the authorisation for service API_BUSINESS_PARTNER (authorization object S_SERVICE) and for business partner maintenance")
        if r.status_code == 404:
            return Diagnostic("connection", "fail", "the service API_BUSINESS_PARTNER was not found (HTTP 404)",
                              "activate it in the Gateway (transaction /IWFND/MAINT_SERVICE) and check the base URL and SAP_CLIENT")
        if r.status_code >= 400:
            return Diagnostic("connection", "fail", f"the service answered HTTP {r.status_code}", "look at the system log (SM21 / /IWFND/ERROR_LOG)")
        return Diagnostic("connection", "pass", f"{self._config.base_url} answered HTTP {r.status_code} and accepted the credentials"
                          + (f" (client {self._config.client})" if self._config.client else ""))

    # ------------------------------------------------------------------ 2 csrf + session
    def _csrf(self) -> Diagnostic:
        try:
            r = self._client._http.get(BP_SERVICE_ROOT, headers={"X-CSRF-Token": "Fetch"})
        except httpx.HTTPError as exc:
            return Diagnostic("csrf_session", "fail", f"the token request failed: {type(exc).__name__}", "retry; if it persists check the network")
        token = r.headers.get("x-csrf-token")
        if not token or token.lower() == "required":
            return Diagnostic("csrf_session", "fail", "SAP did not issue a CSRF token", "the service must be reached through the Gateway; check the URL and any proxy that strips headers")
        cookies = [c for c in self._client._http.cookies.jar]
        if not cookies:
            return Diagnostic("csrf_session", "warn", "a token was issued but no session cookie was set",
                              "SAP ties the token to the session cookie: a proxy or load balancer that drops cookies makes every POST fail with 403")
        return Diagnostic("csrf_session", "pass", f"a CSRF token was issued and {len(cookies)} session cookie(s) will accompany it")

    # ------------------------------------------------------------------ 3 metadata
    def _metadata(self, expected_lengths: Mapping[str, int]) -> Diagnostic:
        try:
            r = self._get(METADATA_URL, headers={"Accept": "application/xml"})
        except httpx.HTTPError as exc:
            return Diagnostic("metadata", "fail", f"$metadata could not be read: {type(exc).__name__}", "retry; the service may be restarting")
        if r.status_code != 200:
            return Diagnostic("metadata", "fail", f"$metadata answered HTTP {r.status_code}", "the user needs read access to the service document")
        try:
            sets, types = parse_metadata(r.text)
        except ET.ParseError as exc:
            return Diagnostic("metadata", "fail", f"$metadata is not valid XML ({exc})", "the answer may come from a proxy or a login page instead of SAP")
        problems, notes = [], []
        for entity_set, wanted in ENTITY_PROPERTIES.items():
            type_name = sets.get(entity_set)
            if type_name is None:
                problems.append(f"entity set {entity_set} does not exist")
                continue
            props = types.get(type_name, {})
            missing = [p for p in wanted if p not in props]
            if missing:
                problems.append(f"{entity_set} has no property {', '.join(missing)}")
        bp_type = sets.get("A_BusinessPartner")
        if bp_type:
            navs = types.get(bp_type, {}).get("__navigations__", [])
            missing_nav = [n for n in NAVIGATIONS if n not in navs]
            if missing_nav:
                problems.append("A_BusinessPartner has no navigation " + ", ".join(missing_nav))
            for entity_set, wanted_navs in ENTITY_NAVIGATIONS.items():
                have = types.get(sets.get(entity_set, ""), {}).get("__navigations__", [])
                absent = [n for n in wanted_navs if n not in have]
                if absent and sets.get(entity_set):
                    problems.append(f"{entity_set} has no navigation " + ", ".join(absent))
            ext = self._config.external_id_field
            if ext and ext not in types.get(bp_type, {}):
                problems.append(f"the configured external id field {ext} is not a property of A_BusinessPartner")
        short = []
        for odata_prop, ours in expected_lengths.items():
            for entity_set in ENTITY_PROPERTIES:
                props = types.get(sets.get(entity_set, ""), {})
                if odata_prop in props and props[odata_prop].get("max_length") and props[odata_prop]["max_length"] < ours:
                    short.append(f"{odata_prop} holds {props[odata_prop]['max_length']} characters in this system, our validation allows {ours}")
        if problems:
            return Diagnostic("metadata", "fail", "; ".join(problems),
                              "the payload names follow the published API_BUSINESS_PARTNER; this release differs: adjust the field maps in sap_client.py or activate the missing parts of the service",
                              {"problems": problems})
        if short:
            return Diagnostic("metadata", "warn", "; ".join(short), "SAP would reject those values: tighten the validation length for the field, or shorten the values in the mapping", {"short": short})
        return Diagnostic("metadata", "pass", f"{sum(len(v) for v in ENTITY_PROPERTIES.values())} properties across {len(ENTITY_PROPERTIES)} entities exist in $metadata; "
                          "navigations and field lengths fit" + (f"; external id field {self._config.external_id_field} found" if self._config.external_id_field else
                                                                  "; no external id field is configured, so SAP assigns the partner numbers"))

    # ------------------------------------------------------------------ 4 read
    def _read(self) -> Diagnostic:
        try:
            r = self._get(BP_ENTITY, params={"$top": "1", "$select": "BusinessPartner"}, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            return Diagnostic("read_access", "fail", f"reading failed: {type(exc).__name__}", "retry")
        if r.status_code == 403:
            return Diagnostic("read_access", "fail", "the user may not read business partners (HTTP 403)", "extend the user's authorisation for business partner display (B_BUPA_RLT / B_BUPA_GRP)")
        if r.status_code != 200:
            return Diagnostic("read_access", "fail", f"reading business partners answered HTTP {r.status_code}", "look at /IWFND/ERROR_LOG")
        try:
            rows = (r.json().get("d") or {}).get("results") or []
        except ValueError:
            return Diagnostic("read_access", "fail", "the answer was not JSON", "the request may not have reached SAP")
        return Diagnostic("read_access", "pass", "business partners can be read" + (f" (first: {rows[0].get('BusinessPartner')})" if rows else " (the system holds none yet)"))

    # ------------------------------------------------------------------ 5 + 6 write test
    def _sample(self, grouping: str, category: str = "2") -> BusinessPartnerRecord:
        stamp = time.strftime("%Y%m%d%H%M%S")
        return BusinessPartnerRecord(
            source_pk=f"DIAG{stamp}", bp_ext=f"DIAG{stamp}"[-10:],
            general={"TYPE": category, "BU_GROUP": grouping, "NAME_ORG1": f"{TEST_MARK} {stamp}"[:40], "BU_SORT1": "MIGTEST"},
            roles=["FLCU01"], address={"COUNTRY": "DE", "CITY1": "Walldorf", "POST_CODE1": "69190", "STREET": "Dietmar-Hopp-Allee"})

    def _write(self, grouping: str) -> Diagnostic:
        record = self._sample(grouping)
        try:
            result = self._client.create_business_partners([record])[0]
        except (SapAuthError, SapUnavailableError) as exc:
            return Diagnostic("write_test", "fail", str(exc), "the write was not possible; nothing was created")
        if not result.partner:
            return Diagnostic("write_test", "fail", "SAP refused the test partner: " + "; ".join(m.MESSAGE for m in result.messages)[:300],
                              f"check the grouping '{grouping}' (a number range with external numbering also needs SAP_BP_EXTERNAL_ID_FIELD), the role FLCU01 and the customer data",
                              {"messages": [m.MESSAGE for m in result.messages]})
        key = result.partner
        r = self._get(f"{BP_ENTITY}('{key}')", headers={"Accept": "application/json"})
        if r.status_code != 200:
            return Diagnostic("write_test", "fail", f"partner {key} was created but could not be read back (HTTP {r.status_code})", "check read authorisation for the new partner", {"partner": key})
        got = (r.json().get("d") or {})
        diffs = [f"{k}: sent '{v}', SAP holds '{got.get(k)}'" for k, v in (("OrganizationBPName1", record.general["NAME_ORG1"]),
                                                                             ("BusinessPartnerCategory", record.general["TYPE"]),
                                                                             ("BusinessPartnerGrouping", record.general["BU_GROUP"]))
                 if str(got.get(k, "")).strip() != v]
        if diffs:
            return Diagnostic("write_test", "fail", f"partner {key} was created but differs from what was sent: " + "; ".join(diffs),
                              "a conversion exit or a customer enhancement changes values on the way in", {"partner": key})
        return Diagnostic("write_test", "pass", f"test partner {key} was created, read back and matches what was sent",
                          f"it is named '{TEST_MARK}...': archive or block it in transaction BP; the OData API cannot delete business partners", {"partner": key})

    def _errors(self) -> Diagnostic:
        record = self._sample("BP01", category="9")                                 # category 9 does not exist
        try:
            result = self._client.create_business_partners([record])[0]
        except (SapAuthError, SapUnavailableError) as exc:
            return Diagnostic("error_mapping", "fail", str(exc))
        if result.partner:
            return Diagnostic("error_mapping", "fail", f"SAP accepted a partner with the impossible category 9 (number {result.partner})",
                              "the system does not validate as expected; archive that partner")
        if not result.messages or not result.messages[0].MESSAGE:
            return Diagnostic("error_mapping", "fail", "SAP rejected the invalid partner but no message could be read from its answer", "send us the raw answer so the parser can be extended")
        first = result.messages[0]
        return Diagnostic("error_mapping", "pass", f"SAP's rejection is understood: {first.TYPE} {first.ID}/{first.NUMBER} '{first.MESSAGE[:120]}'")


def parse_metadata(xml_text: str) -> tuple[dict[str, str], dict[str, dict]]:
    """(entity set -> entity type name, entity type name -> {property: {max_length}, '__navigations__': [...]}) from an OData v2 $metadata document."""
    root = ET.fromstring(xml_text)
    types: dict[str, dict] = {}
    for et in root.iterfind(".//{*}EntityType"):
        props: dict = {}
        for p in et.findall("{*}Property"):
            ml = p.get("MaxLength")
            props[p.get("Name")] = {"max_length": int(ml) if ml and ml.isdigit() else None}
        props["__navigations__"] = [n.get("Name") for n in et.findall("{*}NavigationProperty")]
        types[et.get("Name")] = props
    sets = {s.get("Name"): (s.get("EntityType") or "").split(".")[-1] for s in root.iterfind(".//{*}EntitySet")}
    return sets, types


def expected_odata_lengths(catalog_lengths: Mapping[str, Optional[int]]) -> dict[str, int]:
    """{'BUT000.NAME_ORG1': 40, ...} (our SAP catalog) -> {'OrganizationBPName1': 40, ...}: what our validation allows, by OData property."""
    from src.services import sap_client as sc
    table_maps = {"BUT000": sc.GENERAL_FIELDS, "ADRC": sc.ADDRESS_FIELDS, "KNB1": sc.COMPANY_FIELDS, "KNVV": sc.SALES_FIELDS, "ADR6": sc.EMAIL_FIELDS,
                  "ADR2": sc.PHONE_FIELDS, "ADR3": sc.FAX_FIELDS, "DFKKBPTAXNUM": sc.TAX_FIELDS, "BUT0BK": sc.BANK_FIELDS,
                  "LFB1": sc.VENDOR_COMPANY_FIELDS, "LFM1": sc.VENDOR_PURCHASING_FIELDS}
    out: dict[str, int] = {}
    for qualified, length in catalog_lengths.items():
        table, _, column = qualified.partition(".")
        odata = table_maps.get(table, {}).get(column)
        if odata and length:
            out[odata] = int(length)
    return out
