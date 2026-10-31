"""The setup guide: a chat that sits beside the step the person is doing and says what that step needs, fills in what the person tells it, explains what went wrong and
what to do about it, and points at the next step.

Everything it needs to be useful is rule-based and instant: what each step requires, the common reasons a connection or a file fails, which tables look like the customer
master, and what the next step of the migration is. A language model is only asked about a free-form question the rules do not cover, and only when the application is
set to use one that is quick (Gemini); on a slow local model the person would wait minutes for a chat answer.

What it never does with a password: it does not take one. A message that contains one is not echoed, not kept, not sent to any model; the reply says where to type it
(the password field of the form), and the form never reports the password back. Nothing a person types here is stored.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from src.api.context import AppContext
from src.schemas.assistant import AnswerOut
from src.schemas.guide import GuideAction, GuideProgress, GuideReply, GuideRequest, HeldBack, ProgressStep
from src.services.llm_mapper_service import LLMOutputError, LLMUnavailableError

# ====================================================================== what each provider needs
PRODUCTS: list[tuple[str, tuple[str, ...], str, int]] = [  # kind, words that name it, label, default port
    ("mssql", ("sql server", "mssql", "microsoft sql", "sqlserver"), "Microsoft SQL Server", 1433),
    ("oracle", ("oracle",), "Oracle Database", 1521),
    ("mysql", ("mysql", "mariadb"), "MySQL / MariaDB", 3306),
    ("postgresql", ("postgres", "postgresql", "psql"), "PostgreSQL", 5432),
    ("mongodb", ("mongo",), "MongoDB", 27017),
    ("firestore", ("firestore", "firebase", "google cloud database"), "Google Firebase (Firestore)", 443),
    ("s3", ("s3", "amazon bucket", "minio"), "Amazon S3", 443),
    ("azure_blob", ("azure blob", "data lake", "adls"), "Azure Blob Storage", 443),
    ("sftp", ("sftp",), "SFTP", 22),
    ("file", ("csv", "excel", "xlsx", "json", "parquet", "fixed-width", "fixed width", "flat file", "a file", "extract file"), "an extract file", 0),
]
DB_KINDS = {"mssql", "oracle", "mysql", "postgresql", "mongodb"}
_LABEL = {k: label for k, _, label, _ in PRODUCTS}
_DEFAULT_PORT = {k: port for k, _, _, port in PRODUCTS}
_DB_NAME_WORD = {"oracle": "service name", "mongodb": "database", "mssql": "database", "mysql": "database", "postgresql": "database"}

_STOP = {"is", "the", "a", "an", "name", "called", "named", "to", "on", "at", "of", "and", "my", "our", "it", "this", "that", "with", "for", "as", "should", "would", "could", "must", "can", "will",
         "use", "need", "needs", "has", "have", "i", "we", "you", "which", "what", "how", "be", "are", "was", "does", "do"}
# a keyword, then "=", ":" or only spaces, optionally "is" and "called" / "named" ("the database is called ERP"); the keyword must end where the word ends (db1 is not "db" + "1", users is not "user" + "s")
_JOIN = r"(?:\s*[=:]\s*|\s+(?:is\s+)?(?:(?:called|named)\s+)?)"
_VALUE = r"([A-Za-z0-9][A-Za-z0-9_.@\\-]{0,100})"


def _kv(words: str, joiner: str = _JOIN) -> re.Pattern:
    return re.compile(r"\b(?:" + words + r")\b" + joiner + _VALUE, re.I)


_HOST = _kv(r"host(?:name)?|server|address|ip", r"(?:\s*[=:]\s*|\s+(?:is\s+|at\s+)?)")
_HOST_PORT = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3}|[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+|localhost):(\d{2,5})\b")
_BARE_IP = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b")
_LOCALHOST = re.compile(r"\blocalhost\b", re.I)
_PORT = re.compile(r"\bport\b\s*(?:is|=|:|number)?\s*(\d{2,5})\b", re.I)
_DATABASE = _kv(r"database|db|service(?:\s*name)?|catalog|schema")
_USER = _kv(r"user(?:name)?(?:\s*id)?|login|account")
_NAME = re.compile(r"\b(?:call(?:ed)?\s+(?:it|the\s+source|this\s+source)|source\s+name(?:\s+is)?|name\s+it)\s*[:=]?\s*([A-Za-z][A-Za-z0-9_.-]{1,60})", re.I)
_QUESTION = re.compile(r"^\s*(?:what|which|how|why|is|are|does|do|can|could|should|where|when|who)\b", re.I)
_ASSIGNMENT = re.compile(r"\b(?:host|hostname|server|port|database|db|user|username|login|service)\s*[=:]", re.I)
_PASSWORD = re.compile(r"\b(?:password|passwd|pwd|passcode|secret|token|api[\s_-]?key)\b\s*(?:is|=|:)?\s*\S*", re.I)
_SECRET_SHAPED = re.compile(r"\b[A-Za-z0-9+/_-]{28,}={0,2}\b")
# a service-account key pasted whole (Google Firebase): the PEM block, or the "private_key" field of the JSON file
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)|\"private_key\"\s*:\s*\"[^\"]*\"?", re.S)


def redact(text: str) -> str:
    """The message as it may be shown to anything else: passwords and key-shaped strings gone, addresses generalised."""
    text = _PRIVATE_KEY.sub("[secret removed]", text)
    text = _PASSWORD.sub("[secret removed]", text)
    text = _SECRET_SHAPED.sub("[secret removed]", text)
    text = _HOST_PORT.sub("<host>", text)
    return _BARE_IP.sub("<host>", text)


_SECRET_FOLLOWER = re.compile(r"\b(?:password|passwd|pwd|passcode|secret|token)\b\s*(is|=|:)?\s*(\S+)", re.I)
_NOT_A_SECRET = {"is", "the", "a", "an", "field", "form", "here", "in", "for", "to", "or", "and", "should", "must", "do", "does", "will", "can", "how", "what", "why", "where", "goes", "go",
                 "please", "box", "then", "first", "later", "wrong", "correct", "expired", "reset", "manager", "policy", "hint", "use", "need", "i", "my", "it"}


def has_secret(text: str) -> bool:
    """A password or key in the message: a key-shaped string, or the word 'password' followed by something that is not a plain word (or by anything after 'is', '=' or ':')."""
    if _PRIVATE_KEY.search(text):
        return True
    for m in _SECRET_FOLLOWER.finditer(text):
        token = m.group(2).strip(".,;\"'")
        if not token or token.lower() in _NOT_A_SECRET:
            continue
        if m.group(1) or re.search(r"[0-9]|[^A-Za-z0-9]", token) or (token != token.lower() and token != token.capitalize()):
            return True
    return bool(_SECRET_SHAPED.search(text))


# ====================================================================== reading what the person said
def extract_fields(message: str, current_kind: str = "") -> dict[str, str]:
    """The connection details a person gave in plain words. Only what is clearly there; a password is never one of them."""
    if (message.strip().endswith("?") or _QUESTION.match(message)) and not _ASSIGNMENT.search(message):
        return {}                                                   # a question is not an instruction: "what port does oracle use?" does not choose Oracle
    text = _PASSWORD.sub(" ", message)
    out: dict[str, str] = {}
    lowered = text.lower()
    for kind, words, _, _ in PRODUCTS:
        if any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", lowered) for w in words):
            out["kind"] = kind
            break
    host_port = _HOST_PORT.search(text)
    if host_port:
        out["host"], out["port"] = host_port.group(1), host_port.group(2)
    else:
        m = _HOST.search(text)
        if m and m.group(1).lower() not in _STOP:
            out["host"] = m.group(1).rstrip(".")
        elif _BARE_IP.search(text):
            out["host"] = _BARE_IP.search(text).group(1)
        elif _LOCALHOST.search(text):
            out["host"] = "localhost"
    m = _PORT.search(text)
    if m:
        out["port"] = m.group(1)
    for key, pattern in (("database", _DATABASE), ("user", _USER), ("name", _NAME)):
        m = pattern.search(text)
        if m and m.group(1).lower() not in _STOP:
            out[key] = m.group(1).rstrip(".,")
    if "port" in out and not (1 <= int(out["port"]) <= 65535):
        del out["port"]
    if out.get("host") and out.get("kind") is None and current_kind in ("", "file"):
        out.pop("kind", None)
    return out


# ====================================================================== disagreeing with what the person said
ADMIN_LOGINS = {"sa", "root", "admin", "administrator", "system", "sys", "sysdba", "postgres", "dba", "mongoadmin"}
SYSTEM_DATABASES = {"master", "msdb", "model", "tempdb", "mysql", "information_schema", "performance_schema", "sys", "postgres", "template0", "template1", "admin", "local", "config"}
PORT_OWNER = {1433: "mssql", 1521: "oracle", 3306: "mysql", 5432: "postgresql", 27017: "mongodb"}


def challenge_fields(fill: dict[str, str], current_kind: str = "") -> tuple[dict[str, str], list[HeldBack]]:
    """(what may go into the form, what is held back and why). A value that looks wrong is not filled in: the person is told why and decides. Nothing is refused for good."""
    kind = fill.get("kind") or current_kind
    ok = dict(fill)
    held: list[HeldBack] = []

    def hold(field: str, why: str, suggestion: str) -> None:
        held.append(HeldBack(field=field, value=ok.pop(field), why=why, suggestion=suggestion))

    if fill.get("user", "").lower() in ADMIN_LOGINS:
        hold("user", f"'{fill['user']}' is an administrator account. A source should be read through a login that can only read: if the application had a bug, or the password leaked, an administrator login could change or delete legacy data.",
             "Ask your database administrator for a read-only login (db_datareader on SQL Server, a SELECT-only user elsewhere).")
    if "port" in fill and kind in _LABEL and PORT_OWNER.get(int(fill["port"])) not in (None, kind):
        owner = _LABEL[PORT_OWNER[int(fill["port"])]]
        hold("port", f"{fill['port']} is the usual port of {owner}, not of {_LABEL[kind]} (which is {_DEFAULT_PORT.get(kind)}). A wrong port usually ends in a timeout.",
             f"Use {_DEFAULT_PORT.get(kind)} unless your administrator told you that this {_LABEL[kind]} listens on {fill['port']}.")
    if fill.get("database", "").lower() in SYSTEM_DATABASES and kind in DB_KINDS:
        hold("database", f"'{fill['database']}' is a system database of the server. Your legacy tables are not in it, and reading it would show you the server's own tables.",
             "Give the name of the database that holds the legacy ERP tables.")
    return ok, held


# ====================================================================== what each step needs
def required_now(step: str, kind: str) -> list[tuple[str, str]]:
    """(what, the form field it belongs to): the checklist of the step."""
    if step == "provider":
        return [("A name for this source, for example LEGACY_ERP: it groups the tables of one legacy system", "name"), ("Where the data is: an extract file, or a database", "kind")]
    if step == "connection":
        if kind in ("", "file"):
            return [("The extract file (CSV, Excel, JSON, fixed-width or Parquet): choose it, or drop it into this chat", "file")]
        if kind in DB_KINDS:
            return [("Server: host name or IP address", "host"), (f"Port (the usual one for {_LABEL.get(kind, kind)} is {_DEFAULT_PORT.get(kind)})", "port"), (f"{_DB_NAME_WORD.get(kind, 'Database').capitalize()}", "database"),
                    ("User ID: a read-only account", "user"), ("Password: type it in the password field of the form, not here", "password")]
        if kind == "firestore":
            return [("Firebase project ID: Firebase console > Project settings > Project ID", "database"),
                    ("Service-account key: press Load key file in the form (never paste it into this chat)", "password")]
        return [("The storage account details shown in the form", "host")]
    if step == "tables":
        return [("Tick the table(s) or file(s) to bring in. Start with the customer master", "tables")]
    if step == "options":
        return [("Choose what should happen when the import is done (profile the data, open the Mapping Studio)", "options")]
    if step == "summary":
        return [("Check the values and press Finish: the data is read, checked against the source's own row count, and stored as a snapshot", "finish")]
    return []


STEP_INTRO = {
    "provider": "Step 1: tell the application where your legacy data lives. A file is the quickest way to try things: drop one into this chat. For a database, choose it in the form (use a read-only account).",
    "connection_file": "Step 2: give me the extract file. I detect its format, encoding and delimiter; you check what I found and press Test.",
    "connection_db": "Step 2: how to reach the database. You can type the details here in plain words (for example: \"SQL Server at 10.0.0.5, database ERP, user reader\") and I fill the form in. The password goes in the form only.",
    "tables": "Step 3: pick what to bring in. Each table becomes a snapshot that can be profiled and mapped. The customer master is the right first choice for a business partner migration.",
    "options": "Step 4: choose what happens right after the import. Profiling now means the data health is ready when you open the Profiler.",
    "summary": "Step 5: last look. Nothing has been stored yet; Finish reads the data and proves that every row arrived.",
    "done": "The source is in. Next: look at how healthy the data is, then map it to SAP.",
}

# questions a person asks in these steps, and plain answers. First match by keyword.
FAQ: list[tuple[tuple[str, ...], str]] = [
    (("firebase", "firestore", "service account", "service-account", "key file", "project id"),
     "For Google Firebase (Firestore) you need two things: the project ID (Firebase console > Project settings) and a service-account key. In the Google Cloud console open IAM & Admin > "
     "Service accounts, pick or create an account, give it only the 'Cloud Datastore Viewer' role, then Keys > Add key > JSON. Press 'Load key file' in the form: the file is read in your "
     "browser, its project and account are filled in for you, and the key is kept in the server's memory only. Never paste a key into this chat. Each collection becomes a table, one "
     "document per row; Firestore bills one read per document, and sub-collections are not followed. Delete the key after the migration."),
    (("read-only", "read only", "readonly", "which account", "what user", "what login"),
     "Use an account that can only read the tables you need (db_datareader on SQL Server, a SELECT-only user elsewhere). The application never writes to a source, and a read-only account makes that a guarantee, not a promise."),
    (("password",),
     "Type the password in the password field of the form, not in this chat. It is kept in the server's memory only (or read from an environment variable you name) and is never stored or shown again."),
    (("port",),
     "Leave the port empty to use the default: 1433 for SQL Server, 1521 for Oracle, 3306 for MySQL, 5432 for PostgreSQL, 27017 for MongoDB. Only change it if your database administrator says it listens elsewhere, or if the database runs in Docker on a mapped port."),
    (("service name", "sid"),
     "For Oracle the form asks for the service name (for example ORCLPDB1 or XEPDB1), not the SID. If you only know a SID, ask your administrator for the service name or try the one shown by `lsnrctl status`."),
    (("delimiter", "separator", "semicolon"),
     "The delimiter is the character between columns: comma, semicolon, tab or pipe. Leave it on auto-detect; if all the values land in one column, choose the right one and press Test again."),
    (("encoding", "accent", "umlaut", "garbled", "weird characters"),
     "If accents look wrong (Müller shows as MÃ¼ller), the encoding is wrong. Legacy Windows exports are often cp1252, newer ones utf-8. Type the encoding in the form and press Test again."),
    (("primary key", "key column", "which key"),
     "The key is the column (or columns) that identify one record, such as the customer number. It is optional but recommended: a key that repeats or is empty is rejected at the door, so duplicates are caught at once."),
    (("control total", "row count", "expected rows"),
     "The control total is the number of rows your legacy team says the extract has. If it differs from what was read, the import is refused: that is how the application proves that nothing was lost."),
    (("which table", "what table", "which tables", "customer master", "what should i pick", "what should i select"),
     "For a customer migration choose the customer master table first (names like CUSTOMER, KUNDE, KNA1, ACCOUNT, PARTNER). Add address, contact and payment-term tables later, when the master works."),
    (("view",),
     "A view is a saved query, not a table. It can be imported like a table, but it may hide rows or join tables in a way you did not intend: check its row count against the table behind it."),
    (("how does", "how it works", "overview", "stages", "how do i migrate", "how does a migration"),
     "A migration has six stages: (1) connect your legacy data, (2) look at its health, (3) map it to the SAP business partner fields, (4) check the mapping and have somebody else approve it, "
     "(5) run it: transform, remove duplicates, validate, (6) resolve what the run found, have a steward sign off, load, and reconcile. I tell you which stage you are in and what to do."),
    (("what is this", "what do i do", "what now", "what next", "help", "explain", "what is needed", "what do you need"),
     ""),   # answered with the step's own checklist
]

ERROR_PATTERNS: list[tuple[tuple[str, ...], str, list[str]]] = [
    (("rejected", "login failed", "authentication failed", "access denied", "invalid password", "password authentication", "invalid credentials", "login timeout expired"),
     "The server answered but refused the user name or password.",
     ["Check the user ID and re-type the password (it is case-sensitive).", "Make sure the account is allowed to log in to this database, not only to the server.", "On SQL Server, the login must be mapped to a user in that database."]),
    (("timed out", "timeout", "could not connect", "connection refused", "refused", "unreachable", "no route", "getaddrinfo", "name or service not known", "failed to resolve", "cannot assign", "network is unreachable"),
     "The server could not be reached at all.",
     ["Check the host name or IP address and the port.", "Make sure the database is running and listens on that port (a database in Docker needs its mapped port, for example 11433).", "A firewall or VPN may block the port: ask your network administrator, or try from the server that runs this application.", "On the same machine use 127.0.0.1 instead of the computer name."]),
    (("certificate", "ssl", "tls", "encrypt", "handshake"),
     "The secure connection could not be set up.",
     ["Cloud databases (Azure and others) require encryption: choose the matching cloud preset in the provider list, or tick Encrypt in the optional settings.", "A self-signed certificate on a test server: untick 'validate host' in the optional settings (never for production)."]),
    (("does not exist", "unknown database", "cannot open database", "ora-12514", "ora-12505", "invalid catalog", "no such database", "database name"),
     "The server is there, but it has no database (or service) with that name.",
     ["Check the spelling of the database name; for Oracle give the service name, not the SID.", "Ask the administrator which database holds the legacy tables."]),
    (("permission", "not authorized", "not authorised", "denied", "insufficient privileges", "ora-01031", "select permission"),
     "You are logged in, but the account may not read what it needs.",
     ["Ask the administrator to grant read access: db_datareader (SQL Server), SELECT on the schema (others).", "Choose another schema if the tables are somewhere else."]),
    (("delimiter", "could not read the file", "cannot read the file", "unbalanced quote", "binary file"),
     "The file could not be read as the format it was taken for.",
     ["Choose the right format and delimiter in the form and press Test again.", "A file that was renamed to .csv from another format (a PDF, a screenshot) cannot be read: export it again from the source system."]),
    (("unsupported file type", ".xls", "not a valid .xlsx"),
     "That file type cannot be read directly.",
     ["Save an old .xls workbook as .xlsx, or export the sheet as CSV, and choose the new file."]),
    (("control total", "were read but"),
     "The number of rows read differs from the number you said the extract has.",
     ["Check the control total you typed; it must be the row count of the extract itself, header excluded.", "If the file really has a different count, ask the legacy team why: rows may have been cut in transfer."]),
]

LIKELY_MASTER = re.compile(r"(CUST|CLIENT|KUNDE|KUNDEN|PARTNER|\bBP\b|KNA1|LFA1|VENDOR|SUPPLIER|ACCOUNT|ACCT|DEBITOR|CREDITOR)", re.I)
LIKELY_DETAIL = re.compile(r"(ADDR|ADRESS|CONTACT|BANK|TERMS|PAYMENT)", re.I)


def explain_error(error: str) -> tuple[str, list[str]]:
    text = error.lower()
    for words, cause, steps in ERROR_PATTERNS:
        if any(w in text for w in words):
            return cause, steps
    return ("The step did not complete.", ["Read the message in the form: it names the first thing that went wrong.", "Fix that field and press Test again; if it keeps failing, copy the message to your administrator."])


def suggest_tables(tables) -> tuple[list[str], list[str]]:
    """(the sentences about which table to pick first, the table names) from the list the form shows."""
    scored = []
    for t in tables:
        name = t.name.split(".")[-1]
        score = (3 if LIKELY_MASTER.search(name) else 0) + (1 if LIKELY_DETAIL.search(name) else 0) + (1 if (t.rows or 0) > 1000 else 0) - (1 if (t.kind or "").upper() == "VIEW" or name.lower().startswith("v_") else 0)
        scored.append((score, t))
    scored.sort(key=lambda x: (-x[0], -(x[1].rows or 0)))
    notes: list[str] = []
    masters = [t for s, t in scored if s >= 3]
    if masters:
        top = masters[0]
        notes.append(f"{top.name}" + (f" ({top.rows:,} rows)" if top.rows else "") + " looks like the customer master: tick it first.")
    details = [t for s, t in scored if 0 < s < 3 and LIKELY_DETAIL.search(t.name)]
    if details:
        notes.append("These look like detail tables for later, once the master works: " + ", ".join(t.name for t in details[:4]) + ".")
    views = [t for t in tables if (t.kind or "").upper() == "VIEW"]
    if views:
        notes.append(f"{len(views)} of the entries are views: a view can hide rows, so compare its row count with the table behind it.")
    if not notes and tables:
        notes.append("Nothing is named like a customer table. Open a few entries to see their columns, and tick the one that holds customer names and addresses.")
    return notes, [t.name for t in masters[:3]]


# ====================================================================== the reply
class Guide:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    def _start_reply(self, req: GuideRequest) -> GuideReply:
        """The start page: where the person is in the whole migration, and details or a file they give turn into the Add Source wizard, filled in."""
        prog = progress(self._ctx)
        message = req.message.strip()
        reply = GuideReply(reply="", suggestions=["What should I do next?", "How does a migration work?", "I have a CSV file", "SQL Server at 127.0.0.1 port 1433, database ERP, user reader"])
        if has_secret(message):
            reply.warning = "That message looks like it contains a password or a key. I did not keep it and did not use it. Type passwords only in the password field of the form."
        parts: list[str] = []
        fill, reply.held_back = challenge_fields(extract_fields(message) if message else {})
        for h in reply.held_back:
            parts.append(f"I did not include {h.field} = {h.value}. {h.why} {h.suggestion}")
        if fill:
            shown = ", ".join(f"{k} = {v}" for k, v in fill.items())
            parts.append(f"Got it: {shown}. Press the button and I open Add Source with these filled in; you type the password there.")
            reply.fill = fill
            reply.actions = [GuideAction(label="Open Add Source with these details", do="open_wizard", arg=json.dumps(fill))]
        else:
            answered = self._faq(message) if message else None
            if answered:
                parts.append(answered)
        if not fill:
            parts.append(prog.headline if not parts or not message else "Where you are now: " + prog.headline)
            reply.actions = prog.actions
        reply.reply = " ".join(parts)
        return reply

    def reply(self, req: GuideRequest) -> GuideReply:
        if req.step == "start":
            return self._start_reply(req)
        step, kind = req.step, (req.kind or "").lower()
        checklist = required_now(step, kind)
        held = {k for k, v in req.fields.items() if v}
        missing = [what for what, field in checklist if field not in held and field not in ("password", "finish", "options")]
        reply = GuideReply(reply="", required_now=[w for w, _ in checklist], still_missing=missing, suggestions=self._suggestions(step, kind))
        message = req.message.strip()
        secret = has_secret(message)
        if secret:
            reply.warning = "That message looks like it contains a password or a key. I did not keep it and did not use it. Type passwords only in the password field of the form."
            message = redact(message)

        parts: list[str] = []
        if message:
            fill = extract_fields(req.message, kind)   # the original: a password is cut out inside, and the host in the message is a detail to fill in, not a secret
            fill, reply.held_back = challenge_fields(fill, kind)
            for h in reply.held_back:
                parts.append(f"I did not put {h.field} = {h.value} into the form. {h.why} {h.suggestion} If you are sure, say so below and I will use it.")
            if fill:
                reply.fill = fill
                shown = ", ".join(f"{k} = {v}" for k, v in fill.items())
                parts.append(f"I put this into the form: {shown}. Please check it.")
                held |= {k for k in fill}
                reply.still_missing = [what for what, field in checklist if field not in held and field not in ("password", "finish", "options")]
                if step == "connection" and (fill.get("kind") or kind) in DB_KINDS:
                    parts.append("The password I cannot take: type it in the password field, then press Test.")
            else:
                answered = self._faq(message)
                if answered:
                    parts.append(answered)
        if req.error:
            cause, steps = explain_error(req.error)
            parts.append(cause + " " + " ".join(f"({i}) {s}" for i, s in enumerate(steps, 1)))
            reply.tips = steps
        if req.file_name and step in ("provider", "connection") and not req.error:
            detected = req.fields.get("detected", "")
            if detected:
                parts.append(f"I read {req.file_name}: {detected}. If that looks right press Next; if everything landed in one column, or accents look wrong, change the delimiter or the encoding and press Test again.")
            else:
                parts.append(f"I have {req.file_name}. Press Test in the form to read it.")
        if step == "tables" and req.tables:
            notes, picks = suggest_tables(req.tables)
            parts.extend(notes)
            if picks:
                reply.fill = {**reply.fill, "pick_tables": ",".join(picks)}
        if not parts and message:
            asked = self._ask_model(req, message)
            if asked:
                parts.append(asked)
                reply.source = "model"
        if not parts:
            parts.append(self._intro(step, kind))
            if message and reply.source == "rules":
                parts.insert(0, "I can answer questions about this step and take connection details in plain words.")
        if reply.still_missing and step in ("provider", "connection") and not req.error:
            parts.append("Still needed: " + "; ".join(reply.still_missing) + ".")
        reply.reply = " ".join(parts)
        reply.actions = self._actions(step)
        return reply

    # ------------------------------------------------------------------ pieces
    @staticmethod
    def _intro(step: str, kind: str) -> str:
        if step == "connection":
            return STEP_INTRO["connection_file" if kind in ("", "file") else "connection_db"]
        return STEP_INTRO.get(step, "Tell me what you are trying to do and I will say what the step needs.")

    @staticmethod
    def _faq(message: str) -> Optional[str]:
        text = message.lower()
        for words, answer in FAQ:
            if any(w in text for w in words):
                return answer or None
        return None

    @staticmethod
    def _suggestions(step: str, kind: str) -> list[str]:
        if step == "provider":
            return ["What can I connect to?", "Which account should I use?", "I have a CSV file"]
        if step == "connection":
            return (["Why does the delimiter matter?", "My accents look wrong"] if kind in ("", "file") else ["Which port should I use?", "Which account should I use?", "SQL Server at 127.0.0.1 port 1433, database ERP, user reader"])
        if step == "tables":
            return ["Which table should I pick?", "What is a view?"]
        if step == "options":
            return ["What does profiling do?"]
        if step == "summary":
            return ["What happens when I press Finish?"]
        return ["What should I do next?"]

    @staticmethod
    def _actions(step: str) -> list[GuideAction]:
        return []

    def _ask_model(self, req: GuideRequest, message: str) -> Optional[str]:
        """A free-form question the rules do not cover: only when a quick model is in use, and the model sees nothing but the step, the provider and the redacted question."""
        ctx = self._ctx
        if getattr(ctx, "llm_provider", "") != "gemini":
            return None
        try:
            if not ctx.gemini_keys.has_usable_keys():
                return None
            llm = ctx.mapping_orchestrator().llm
            system = ("You help a person set up a data migration source in a wizard. Answer in at most three plain sentences, for a business user. Do not invent product screens or menu names; "
                      "refer only to the wizard's step and fields. Never ask for or repeat a password. Reply with one JSON object: {\"answer\": \"...\"}.")
            user = f"Wizard step: {req.step}. Source type: {req.kind or 'not chosen'}. Question: {redact(message)}"
            return llm.ask(system, user, AnswerOut).answer.strip()[:600]
        except (LLMUnavailableError, LLMOutputError, Exception):  # noqa: BLE001 - the guide works without a model
            return None


# ====================================================================== where the person is in the whole migration
def progress(ctx: AppContext) -> GuideProgress:
    """The six stages of a migration, which are done, and what to do now: from what the workspace holds."""
    from src.api.routes.audit import list_runs
    from src.api.routes.mapping import list_mapping_sets
    from src.api.routes.sources import list_tables
    tables = list_tables(ctx)
    sets = list_mapping_sets(source_table_id=None, include_archived=False, ctx=ctx)
    runs = list_runs(ctx)
    profiled = [t for t in tables if t.last_profiled_at]
    approved = [s for s in sets if s.status == "APPROVED"]
    drafts = [s for s in sets if s.status in ("DRAFT", "SUBMITTED")]
    latest = runs[0] if runs else None
    done = {
        "source": bool(tables), "profile": bool(profiled), "mapping": bool(sets), "approve": bool(approved), "run": bool(runs),
        "release": bool(latest and (latest.records_loaded or 0) > 0),
    }
    labels = [("source", "Connect your data"), ("profile", "Look at the data's health"), ("mapping", "Map it to SAP"), ("approve", "Check and approve the mapping"),
              ("run", "Run the migration"), ("release", "Resolve defects, sign off, load")]
    first_open = next((k for k, _ in labels if not done[k]), None)
    steps = []
    details = {"source": f"{len(tables)} table(s)" if tables else "", "profile": f"{len(profiled)} profiled" if profiled else "", "mapping": f"{len(sets)} mapping set(s)" if sets else "",
               "approve": f"{len(approved)} approved" if approved else "", "run": f"{len(runs)} run(s)" if runs else "", "release": "loaded" if done["release"] else ""}
    for key, label in labels:
        steps.append(ProgressStep(key=key, label=label, status="done" if done[key] else ("current" if key == first_open else "todo"), detail=details[key]))
    actions: list[GuideAction] = []
    if first_open == "source":
        headline, actions = "Start by connecting your legacy data: drop a file into this chat, or add a database.", [GuideAction(label="Add a source", do="open_wizard")]
    elif first_open == "profile":
        t = tables[0]
        headline, actions = f"Look at how healthy {t.table_name} is before mapping it.", [GuideAction(label=f"Profile {t.table_name}", do="open_tab", arg=f"/ui/profiler/{t.table_id}")]
    elif first_open == "mapping":
        t = tables[0]
        headline, actions = f"Map {t.table_name} to the SAP business partner fields: the AI proposes, you review.", [GuideAction(label="Open the Mapping Studio", do="open_tab", arg=f"/ui/studio/{t.table_id}")]
    elif first_open == "approve":
        s = drafts[0] if drafts else sets[0]
        headline = (f"Mapping set #{s.mapping_set_id} is {s.status.lower()}: run 'Check rules on sample', fix what it finds with the Fix it buttons, then submit it. A different person approves." if s.status == "DRAFT"
                    else f"Mapping set #{s.mapping_set_id} waits for approval by somebody other than {s.created_by}.")
        actions = [GuideAction(label="Open the Mapping Studio", do="open_tab", arg=f"/ui/studio/{s.source_table_id}")]
    elif first_open == "run":
        s = approved[0]
        headline, actions = f"Mapping set #{s.mapping_set_id} is approved: run the migration.", [GuideAction(label="Open the Mapping Studio", do="open_tab", arg=f"/ui/studio/{s.source_table_id}")]
    elif first_open == "release":
        headline = f"Run #{latest.run_id} is done: open its Governance page, resolve the defects (ask 'how to fix the errors'), have a steward sign off, then load."
        actions = [GuideAction(label=f"Open run #{latest.run_id}", do="open_tab", arg=f"/ui/governance/{latest.run_id}")]
    else:
        headline = "Everything has been done at least once. Compare cycles on the Dashboard to see what changed."
        actions = [GuideAction(label="Open the Dashboard", do="open_tab", arg="/ui/dashboard")]
    return GuideProgress(steps=steps, headline=headline, actions=actions)
