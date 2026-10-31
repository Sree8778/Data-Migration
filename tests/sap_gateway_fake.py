"""A stateful stand-in for an S/4HANA Gateway serving API_BUSINESS_PARTNER (OData v2), strict where a real one is strict.

What it enforces, because a real system does and a simple canned mock does not:
  * HTTP basic authentication (401), a required `sap-client` query parameter when the system has one, an activated service (404 when not)
  * the CSRF handshake: `X-CSRF-Token: Fetch` on a GET returns a token AND sets a session cookie; every write needs both the token header and that
    cookie, otherwise 403 with `x-csrf-token: Required`; the token expires after `token_lifetime` writes (so a long load meets the 403 mid-way)
  * JSON in, `{"d": {...}}` out, SAP's error structure `{"error": {"code": "...", "message": {"lang": "en", "value": "..."}, "innererror": {...}}}`
  * validation: category, grouping, name for organisations, valid roles, country in the address, unknown properties (400), duplicate external key
  * $metadata (EDMX) listing the entity types, their properties with MaxLength and the navigations
Faults can be injected: `fail_next(status, n)` for 5xx / 429, `drop_cookie` to behave like a proxy that strips Set-Cookie, `drop_property` / `rename_property`
to make the release differ from the published API.
"""

from __future__ import annotations

import base64
import itertools
import json
from typing import Optional

import httpx

ROOT = "/sap/opu/odata/sap/API_BUSINESS_PARTNER/"
EDM = "http://schemas.microsoft.com/ado/2008/09/edm"

TYPES = {
    "A_BusinessPartnerType": ("A_BusinessPartner", {"BusinessPartner": 10, "BusinessPartnerCategory": 1, "BusinessPartnerGrouping": 4, "OrganizationBPName1": 40,
                                                     "OrganizationBPName2": 40, "SearchTerm1": 20, "FirstName": 40, "LastName": 40, "YY1_LegacyKey_bus": 20},
                              ["to_BusinessPartnerRole", "to_BusinessPartnerAddress", "to_Customer", "to_Supplier", "to_BusinessPartnerTaxNumber", "to_BusinessPartnerBank"]),
    "A_BusinessPartnerRoleType": ("A_BusinessPartnerRole", {"BusinessPartner": 10, "BusinessPartnerRole": 6}, []),
    "A_BusinessPartnerAddressType": ("A_BusinessPartnerAddress", {"BusinessPartner": 10, "AddressID": 10, "Country": 3, "StreetName": 60, "HouseNumber": 10,
                                                                   "CityName": 40, "PostalCode": 10, "Region": 3, "Language": 2},
                                   ["to_EmailAddress", "to_PhoneNumber", "to_FaxNumber"]),
    "A_AddressEmailAddressType": ("A_AddressEmailAddress", {"AddressID": 10, "EmailAddress": 241}, []),
    "A_AddressPhoneNumberType": ("A_AddressPhoneNumber", {"AddressID": 10, "PhoneNumber": 30, "PhoneNumberExtension": 10}, []),
    "A_AddressFaxNumberType": ("A_AddressFaxNumber", {"AddressID": 10, "FaxNumber": 30, "FaxNumberExtension": 10}, []),
    "A_BusinessPartnerTaxNumberType": ("A_BusinessPartnerTaxNumber", {"BusinessPartner": 10, "BPTaxType": 4, "BPTaxNumber": 20}, []),
    "A_BusinessPartnerBankType": ("A_BusinessPartnerBank", {"BusinessPartner": 10, "BankIdentification": 4, "BankCountryKey": 3, "BankNumber": 15, "BankAccount": 18,
                                                             "BankAccountHolderName": 60, "IBAN": 34}, []),
    "A_SupplierType": ("A_Supplier", {"Supplier": 10}, ["to_SupplierCompany", "to_SupplierPurchasingOrg"]),
    "A_SupplierCompanyType": ("A_SupplierCompany", {"Supplier": 10, "CompanyCode": 4, "ReconciliationAccount": 10, "PaymentTerms": 4, "PaymentMethodsList": 10}, []),
    "A_SupplierPurchasingOrgType": ("A_SupplierPurchasingOrg", {"Supplier": 10, "PurchasingOrganization": 4, "PurchaseOrderCurrency": 5, "PaymentTerms": 4,
                                                                 "IncotermsClassification": 3}, []),
    "A_CustomerType": ("A_Customer", {"Customer": 10}, ["to_CustomerCompany", "to_CustomerSalesArea"]),
    "A_CustomerCompanyType": ("A_CustomerCompany", {"Customer": 10, "CompanyCode": 4, "ReconciliationAccount": 10, "PaymentTerms": 4, "PaymentMethodsList": 10}, []),
    "A_CustomerSalesAreaType": ("A_CustomerSalesArea", {"Customer": 10, "SalesOrganization": 4, "DistributionChannel": 2, "Division": 2, "Currency": 5,
                                                         "IncotermsClassification": 3}, []),
}
VALID_ROLES = {"FLCU00", "FLCU01", "FLVN00", "FLVN01", "BUP001"}
# navigation -> entity type it leads to, for checking the properties of a deep insert level by level
NAV_TYPES = {"to_BusinessPartnerRole": "A_BusinessPartnerRoleType", "to_BusinessPartnerAddress": "A_BusinessPartnerAddressType", "to_Customer": "A_CustomerType",
             "to_CustomerCompany": "A_CustomerCompanyType", "to_CustomerSalesArea": "A_CustomerSalesAreaType", "to_EmailAddress": "A_AddressEmailAddressType",
             "to_PhoneNumber": "A_AddressPhoneNumberType", "to_FaxNumber": "A_AddressFaxNumberType", "to_BusinessPartnerTaxNumber": "A_BusinessPartnerTaxNumberType",
             "to_BusinessPartnerBank": "A_BusinessPartnerBankType", "to_Supplier": "A_SupplierType", "to_SupplierCompany": "A_SupplierCompanyType",
             "to_SupplierPurchasingOrg": "A_SupplierPurchasingOrgType"}


class FakeGateway:
    def __init__(self, user: str = "MIGUSER", password: str = "pw", client: Optional[str] = "100", token_lifetime: int = 1000, first_partner: int = 1000000001,
                 service_active: bool = True) -> None:
        self.user, self.password, self.client, self.token_lifetime = user, password, client, token_lifetime
        self.service_active = service_active
        self.drop_cookie = False
        self.drop_property: set[str] = set()
        self.rename_property: dict[str, str] = {}
        self.max_length_override: dict[str, int] = {}
        self.partners: dict[str, dict] = {}
        self.deep: dict[str, dict] = {}  # partner number -> the navigations (children) that came with the deep insert
        self.requests: list[tuple[str, str]] = []
        self._numbers = itertools.count(first_partner)
        self._faults: list[int] = []
        self._token, self._cookie, self._writes_on_token = None, None, 0
        self._sessions = itertools.count(1)

    def fail_next(self, status: int, times: int = 1) -> None:
        self._faults += [status] * times

    # ------------------------------------------------------------------ transport
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self._faults:
            status = self._faults.pop(0)
            return httpx.Response(status, json={"error": {"code": "SY/530", "message": {"lang": "en", "value": "temporarily unavailable"}}})
        if not self._authorised(request):
            return httpx.Response(401, headers={"www-authenticate": 'Basic realm="SAP"'}, text="Unauthorized")
        if self.client and request.url.params.get("sap-client") != self.client:
            return httpx.Response(404, text="client not found")
        if not self.service_active or not request.url.path.startswith(ROOT):
            return httpx.Response(404, json={"error": {"code": "/IWFND/MED/170", "message": {"lang": "en", "value": "No service found for namespace '', name 'API_BUSINESS_PARTNER'"}}})
        path = request.url.path[len(ROOT):]
        if request.method == "GET":
            return self._get(request, path)
        if request.method == "POST" and path == "A_BusinessPartner":
            return self._post_partner(request)
        return httpx.Response(405, text="method not allowed")

    def _authorised(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:  # noqa: BLE001
            return False
        return (user, password) == (self.user, self.password)

    # ------------------------------------------------------------------ reads
    def _get(self, request: httpx.Request, path: str) -> httpx.Response:
        if path in ("", "/"):
            headers = {}
            if request.headers.get("x-csrf-token", "").lower() == "fetch":
                self._token = f"TOKEN{next(self._sessions)}=="
                self._cookie = f"SAP_SESSIONID_X_{next(self._sessions)}"
                self._writes_on_token = 0
                headers["x-csrf-token"] = self._token
                if not self.drop_cookie:
                    headers["set-cookie"] = f"sap-contextid={self._cookie}; path=/; HttpOnly"
            return httpx.Response(200, headers=headers, content=b"<service/>")
        if path == "$metadata":
            return httpx.Response(200, headers={"content-type": "application/xml"}, text=self._metadata())
        if path == "A_BusinessPartner" or path.startswith("A_BusinessPartner("):
            if path == "A_BusinessPartner":
                top = int(request.url.params.get("$top", "50"))
                rows = [{"BusinessPartner": k} for k in list(self.partners)[:top]]
                return httpx.Response(200, json={"d": {"results": rows}})
            key = path[len("A_BusinessPartner('"):-2]
            if key not in self.partners:
                return httpx.Response(404, json={"error": {"code": "/IWBEP/CM_MGW_RT/020", "message": {"lang": "en", "value": f"Resource not found for segment 'A_BusinessPartner'"}}})
            return httpx.Response(200, json={"d": self.partners[key]})
        return httpx.Response(404, text="unknown resource")

    def _metadata(self) -> str:
        types, sets = [], []
        for type_name, (set_name, props, navs) in TYPES.items():
            rows = []
            for name, length in props.items():
                if name in self.drop_property:
                    continue
                name = self.rename_property.get(name, name)
                length = self.max_length_override.get(name, length)
                rows.append(f'<Property Name="{name}" Type="Edm.String" MaxLength="{length}"/>')
            rows += [f'<NavigationProperty Name="{n}" Relationship="API_BUSINESS_PARTNER.assoc_{n}" FromRole="From" ToRole="To"/>' for n in navs]
            types.append(f'<EntityType Name="{type_name}">{"".join(rows)}</EntityType>')
            sets.append(f'<EntitySet Name="{set_name}" EntityType="API_BUSINESS_PARTNER.{type_name}"/>')
        return ('<?xml version="1.0" encoding="utf-8"?><edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">'
                f'<edmx:DataServices><Schema Namespace="API_BUSINESS_PARTNER" xmlns="{EDM}">{"".join(types)}'
                f'<EntityContainer Name="API_BUSINESS_PARTNER_Entities">{"".join(sets)}</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>')

    # ------------------------------------------------------------------ writes
    def _error(self, status: int, code: str, message: str, details: Optional[list[tuple[str, str, str]]] = None) -> httpx.Response:
        body = {"error": {"code": code, "message": {"lang": "en", "value": message}}}
        if details:
            body["error"]["innererror"] = {"errordetails": [{"code": c, "message": m, "severity": s} for c, m, s in details]}
        return httpx.Response(status, json=body)

    def _post_partner(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("x-csrf-token")
        if not token or token != self._token or f"sap-contextid={self._cookie}" not in request.headers.get("cookie", "") or self._writes_on_token >= self.token_lifetime:
            return httpx.Response(403, headers={"x-csrf-token": "Required"}, json={"error": {"code": "/IWBEP/CM_MGW_RT/161", "message": {"lang": "en", "value": "CSRF token validation failed"}}})
        self._writes_on_token += 1
        try:
            body = request.read() and json.loads(request.read())
        except ValueError:
            return self._error(400, "/IWBEP/CM_MGW_RT/005", "Invalid JSON")
        bad = self._unknown_property(body, "A_BusinessPartnerType")
        if bad:
            return self._error(400, "/IWBEP/CM_MGW_RT/021", f"Property '{bad}' is invalid")
        problems: list[tuple[str, str, str]] = []
        category = body.get("BusinessPartnerCategory")
        if category not in ("1", "2", "3"):
            problems.append(("R11/007", f"Business partner category {category} is invalid", "error"))
        if not body.get("BusinessPartnerGrouping"):
            problems.append(("R11/008", "Business partner grouping is missing", "error"))
        if category == "2" and not body.get("OrganizationBPName1"):
            problems.append(("R11/009", "Name 1 is required for an organization", "error"))
        roles = self._results(body.get("to_BusinessPartnerRole"))
        if not roles or any(r.get("BusinessPartnerRole") not in VALID_ROLES for r in roles):
            problems.append(("R11/012", "BP role is missing or not allowed", "error"))
        addresses = self._results(body.get("to_BusinessPartnerAddress"))
        if addresses and not addresses[0].get("Country"):
            problems.append(("R11/011", "Country is required in the address", "error"))
        if category == "1" and not body.get("LastName"):
            problems.append(("R11/013", "Last name is required for a person", "error"))
        for address in addresses:
            for mail in self._results(address.get("to_EmailAddress")):
                if "@" not in mail.get("EmailAddress", ""):
                    problems.append(("R11/014", "E-mail address is invalid", "error"))
            if (address.get("to_EmailAddress") or address.get("to_PhoneNumber") or address.get("to_FaxNumber")) and not address.get("Country"):
                problems.append(("R11/011", "Country is required in the address", "error"))
        for bank in self._results(body.get("to_BusinessPartnerBank")):
            if not bank.get("BankCountryKey"):
                problems.append(("R11/015", "Bank country is required for bank details", "error"))
        if body.get("to_Supplier") and not any(r.get("BusinessPartnerRole") in ("FLVN00", "FLVN01") for r in roles):
            problems.append(("R11/016", "Vendor data needs a supplier role", "error"))
        external = body.get("YY1_LegacyKey_bus")
        if external and any(p.get("YY1_LegacyKey_bus") == external for p in self.partners.values()):
            problems.append(("R11/010", f"Business partner with external key {external} already exists", "error"))
        if problems:
            first = problems[0]
            return self._error(400, first[0], first[1], problems[1:] or None)
        number = str(next(self._numbers)).zfill(10)
        self.partners[number] = {"BusinessPartner": number, **{k: v for k, v in body.items() if not k.startswith("to_")}}
        self.deep[number] = {k: v for k, v in body.items() if k.startswith("to_")}
        return httpx.Response(201, json={"d": self.partners[number]})

    def _unknown_property(self, node: dict, type_name: str) -> Optional[str]:
        """The first property (or navigation) of a deep-insert level that the entity type does not have, looking into every navigation."""
        props, navs = TYPES[type_name][1], TYPES[type_name][2]
        for name, value in node.items():
            if name in navs:
                target = NAV_TYPES[name]
                children = self._results(value) if name != "to_Customer" and name != "to_Supplier" else [value]
                for child in children:
                    if isinstance(child, dict):
                        bad = self._unknown_property(child, target)
                        if bad:
                            return bad
            elif name not in props:
                return name
        return None

    @staticmethod
    def _results(value) -> list[dict]:
        if value is None:
            return []
        return value.get("results", []) if isinstance(value, dict) else value
