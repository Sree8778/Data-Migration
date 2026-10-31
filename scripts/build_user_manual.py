"""Builds docs/user-manual/index.html (and, with --pdf, docs/user-manual/ERP-Migration-User-Manual.pdf): the professional user manual of the application.

    python scripts/make_guide_screenshots.py --base http://127.0.0.1:8775 --out docs/user-manual/images --stage import,quality,...,more   (real screenshots)
    python scripts/build_user_manual.py [--pdf]

Same raw material as the illustrated guide (docs/user-guide: written for a first-time reader, with a story): the real screenshots and the flow charts of scripts/guide_diagrams.py,
but organised as a manual: document control, roles, concepts, installation, one procedure per task (purpose, prerequisites, steps, expected result, controls),
the control framework, troubleshooting and reference appendices. A picture that is missing is reported and left out, so the manual can be rebuilt while screenshots are retaken.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import date
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import guide_diagrams as D  # noqa: E402
from build_user_guide import CSS as BASE_CSS  # noqa: E402

OUT = ROOT / "docs" / "user-manual"
IMAGES = OUT / "images"
GUIDE_IMAGES = ROOT / "docs" / "user-guide" / "images"
BORROWED = ["14_studio_empty.png", "15_studio_buttons.png", "16_ai_running.png", "17_ai_result.png", "18_ai_rules.png"]   # the AI step takes ~40 min on a laptop: photographed once, for the guide
missing: list[str] = []
VERSION = "1.0"
TODAY = date.today()


def git_hash() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - the hash is a nicety on the cover
        return "unknown"


EXTRA_CSS = """
.cover { background:linear-gradient(135deg,#0f1f3d,#1d3b6e); padding:56px 46px 40px; }
.cover .kicker { font-size:14px; letter-spacing:.16em; text-transform:uppercase; opacity:.8; margin:0 0 14px; }
.cover h1 { font-size:44px; }
.cover .docctl { margin-top:34px; width:100%; max-width:760px; border-collapse:collapse; font-size:15px; }
.cover .docctl th { text-align:left; font-weight:600; opacity:.75; padding:6px 14px 6px 0; width:170px; vertical-align:top; }
.cover .docctl td { padding:6px 0; }
.cover .docctl tr { border-top:1px solid rgba(255,255,255,.22); }
h2.sec { counter-reset:sub; border-bottom:2px solid var(--line); padding-bottom:6px; }
h2.sec .num { border-radius:8px; width:auto; min-width:46px; padding:0 12px; }
h3 .n { color:var(--brand); margin-right:8px; }
.proc { border:1px solid var(--line); border-radius:14px; background:var(--card); margin:20px 0; overflow:hidden; }
.proc .ph { background:var(--brand-soft); padding:9px 16px; font-weight:700; letter-spacing:.01em; }
.proc .pr { display:grid; grid-template-columns:150px 1fr; border-top:1px solid var(--line); }
.proc .pr > b { padding:10px 14px; background:rgba(120,130,160,.07); font-size:13px; letter-spacing:.07em; text-transform:uppercase; color:var(--muted); }
.proc .pr > div { padding:8px 16px; min-width:0; } .proc .pr > div > :first-child { margin-top:2px; } .proc .pr > div > :last-child { margin-bottom:2px; }
.proc ol.steps { margin:6px 0; }
.box.control { background:var(--ok-soft); border-color:transparent; } .box.control .tag { color:var(--ok); }
.box.note { background:var(--brand-soft); border-color:transparent; } .box.note .tag { color:var(--brand); }
figure .fn { font-weight:700; color:var(--brand); margin-right:6px; }
.diagram .cap .fn { font-weight:700; color:#4f46e5; margin-right:6px; }
.tocpage ol { list-style:none; padding:0; margin:0; }
.tocpage li { display:flex; gap:8px; align-items:baseline; padding:2px 0; }
.tocpage li .dots { flex:1; border-bottom:1px dotted #9aa3b4; transform:translateY(-4px); }
.tocpage li.l1 { font-weight:700; margin-top:7px; } .tocpage li.l2 { padding-left:26px; font-size:.95em; }
.tocpage a { color:inherit; text-decoration:none; } .tocpage .pg { font-variant-numeric:tabular-nums; min-width:28px; text-align:right; }
.revtable td, .revtable th { font-size:14.5px; }
@media (max-width: 700px) { .proc .pr { grid-template-columns:1fr; } .proc .pr > b { padding-bottom:2px; } }
@media print {
  .cover { padding:40px 34px; min-height:230mm; break-after:page; page-break-after:always; } .cover h1 { font-size:36px; }
  .proc .pr { break-inside:avoid; page-break-inside:avoid; } .proc .ph { break-after:avoid; page-break-after:avoid; } .tocpage { break-after:page; page-break-after:always; font-size:10.3px; line-height:1.25; }
  .proc .ph, .proc .pr > b { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  pre { white-space:pre-wrap; overflow-wrap:anywhere; }
}
"""
CSS = BASE_CSS + EXTRA_CSS

figno = [0]


def _fn() -> str:
    figno[0] += 1
    return f'<span class="fn">Figure {figno[0]}.</span>'


def img(name: str, caption: str, alt: str = "") -> str:
    path = IMAGES / name
    if not path.is_file():
        missing.append(name)
        return f'<div class="box careful"><span class="tag">Picture missing</span><p>{escape(name)}</p></div>'
    return f'<figure><img src="images/{name}" alt="{escape(re.sub("<[^>]+>", "", alt or caption))}" loading="lazy"><figcaption>{_fn()} {caption}</figcaption></figure>'


def chart(name: str, caption: str) -> str:
    return f'<div class="diagram">{D.ALL[name]()}<div class="cap">{_fn()} {caption}</div></div>'


def box(kind: str, title: str, body: str) -> str:
    return f'<div class="box {kind}"><span class="tag">{escape(title)}</span>{body}</div>'


def note(body: str) -> str:
    return box("note", "Note", body)


def caution(body: str) -> str:
    return box("careful", "Caution", body)


def control(body: str) -> str:
    return box("control", "Control", body)


def expect(body: str) -> str:
    return box("see", "Expected result", body)


def steps(*items: str) -> str:
    return '<ol class="steps">' + "".join(f"<li>{i}</li>" for i in items) + "</ol>"


def table(head: list[str], rows: list[list[str]]) -> str:
    return '<div class="tw"><table class="t"><thead><tr>' + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table></div>"


def proc(title: str, purpose: str, who: str, before: str, body: str, result: str = "") -> str:
    """One procedure in the house format: purpose, who, prerequisites, then the steps and what you should see."""
    rows = [("Purpose", purpose), ("Performed by", who), ("Before you start", before)]
    out = f'<div class="proc"><div class="ph">{title}</div>' + "".join(f'<div class="pr"><b>{k}</b><div>{v}</div></div>' for k, v in rows)
    out += f'<div class="pr"><b>Procedure</b><div>{body}</div></div>'
    if result:
        out += f'<div class="pr"><b>Expected result</b><div>{result}</div></div>'
    return out + "</div>"


Section = tuple[str, str, str, list[tuple[str, str]]]
S: list[Section] = []                    # (id, "N. Title", html, [(sub id, "N.M Title")])


def chapter(sid: str, num: str, title: str, lead: str, body_fn, page_break: bool = True) -> None:
    subs: list[tuple[str, str]] = []
    count = [0]

    def sub(title2: str) -> str:
        count[0] += 1
        ident = f"{sid}-{count[0]}"
        label = f"{num}.{count[0]}"
        subs.append((ident, f"{label} {title2}"))
        return f'<h3 id="{ident}"><span class="n">{label}</span>{title2}</h3>'

    body = body_fn(sub)
    head = f'<h2 class="sec{" sec-break" if page_break else ""}" id="{sid}"><span class=num>{num}</span>{title}</h2><p class="lead">{lead}</p>'
    S.append((sid, f"{num}. {title}", head + body, subs))


# ================================================================================================================ 1. introduction
def c_intro(sub) -> str:
    return f"""
{sub("Purpose of this manual")}
<p>This manual describes how to use the <b>ERP Migration Accelerator</b>, a web application that moves master data (here: customers) from a legacy source into <b>SAP S/4HANA</b>
(Business Partner), or into a database or Google Firestore, under controls that make the result provable. It explains the process end to end, the role of each person involved,
every screen and button used, and the evidence the application produces.</p>
{sub("Who should read it")}
{table(["Reader", "Read chapters", "Why"], [
    ["Migration author (builds the mapping)", "2, 3, 4, 5, 6", "The whole procedure, from registering a source to a delivered run."],
    ["Approver / steward (reviews, approves, signs off, loads)", "3, 6.7 to 6.14, 7", "Your decisions and the evidence you rely on."],
    ["Auditor or project owner", "2, 3, 7, 8", "How the controls work and where the proof is."],
    ["Administrator", "4, 9, appendices C and D", "Installation, settings and what was and was not tested."],
])}
{sub("Scope and what was verified")}
<p>The manual covers every function of the application as of the build named on the cover. All screenshots are <b>real</b>: they were taken by a script that operated the application exactly as a
person would, on a practice file of 1,980 invented customers (<code>mock_data/legacy_ar_customers.csv</code>). Only the red numbered rings were added, to point at parts of a screen.</p>
{caution("<p><b>SAP itself was not used.</b> Every SAP delivery in this manual went to a <b>stand-in gateway</b> that follows SAP's published interface. It proves that the application builds correct messages and handles success and failure; it does not prove that a particular company's SAP system will accept them. Appendix D lists what was tested against what.</p>")}
{sub("Conventions")}
{table(["Element", "Meaning"], [
    ["<b>Bold</b>", "The name of a button, menu or field exactly as it appears on screen."],
    ["<code>Code font</code>", "A value you type, a file or table name, or a technical identifier."],
    ["Procedure block", "A framed block with Purpose, Performed by, Before you start, Procedure and Expected result. Each task in chapter 6 has one."],
    [box("note", "Note", "<p>Extra information.</p>"), "Useful context that does not change what you do."],
    [box("careful", "Caution", "<p>Risk.</p>"), "Something that can cost time or produce a wrong result if ignored."],
    [box("control", "Control", "<p>Safeguard.</p>"), "A safeguard the application enforces, and what it records as evidence."],
])}
"""




# ================================================================================================================ 2. concepts
def c_concepts(sub) -> str:
    return f"""
{sub("The migration in one paragraph")}
<p>Data is read from a legacy <b>source</b> and frozen into a locked <b>snapshot</b>. It is profiled and quality-checked. A <b>mapping set</b> (a list of rules) says which legacy column fills
which SAP field and how. A second person <b>approves</b> the rules, after which they are locked. A <b>run</b> applies them to every row, finds duplicates and validates against SAP's constraints.
The <b>gate</b> blocks while critical defects remain. A named <b>steward signs off</b>, the data is <b>delivered</b> and read back, and a <b>reconciliation certificate</b> proves that every source row is accounted for.</p>
{chart("architecture", "Architecture: sources are only read; the application keeps the snapshot and the catalogue; results go to a target, with evidence.")}
{sub("The process at a glance")}
{chart("big_picture", "The end-to-end process. Blue steps are performed by the application, amber steps need a person, purple is AI assistance that a person checks.")}
{table(["Step", "Procedure", "Result", "Section"], [
    ["1", "Register a source", "A locked snapshot with a row count and a fingerprint", "6.1, 6.2"],
    ["2", "Profile and assess quality", "Column statistics; findings ranked by severity", "6.3, 6.4"],
    ["3", "Combine tables (optional)", "A new source table made from several", "6.5"],
    ["4", "Write the mapping", "Rules per SAP field, tested on a sample", "6.6"],
    ["5", "Approve", "Locked rules; a second person's name on record", "6.7"],
    ["6", "Run", "Converted, de-duplicated, validated rows", "6.8"],
    ["7", "Read the gate; fix; run again", "READY FOR LOAD", "6.9, 6.10, 6.11"],
    ["8", "Sign off", "A named person accepts the run", "6.12"],
    ["9", "Deliver and verify", "Rows in the target, read back and compared", "6.13"],
    ["10", "Certificate; roll back if needed", "Proof of completeness; a precise undo", "6.14, 6.15"],
])}
{sub("Glossary")}
<dl class="words">
<dt>Source / legacy</dt><dd>The old system or file data is read from. Never modified.</dd>
<dt>Snapshot</dt><dd>A locked copy of the source taken at registration; all later work uses it. It has a row count and a fingerprint.</dd>
<dt>Fingerprint (hash)</dt><dd>A short code computed from the data; it changes if any value changes. Used to prove two data sets are identical.</dd>
<dt>Control total</dt><dd>The row count you expect, entered at registration. A different count stops the registration.</dd>
<dt>Key</dt><dd>The column (or columns) whose value is unique per row, such as <code>CUST_ID</code>. It lets every finding name the exact record.</dd>
<dt>Profile</dt><dd>Statistics per column: type, blank ratio, distinct values, lengths, most frequent values, a masked sample, a health score.</dd>
<dt>Mapping set / rule</dt><dd>A set is the complete list; a rule fills one SAP field from legacy data. Kinds: <code>DIRECT_COPY</code>, <code>STATIC_VALUE</code>, <code>VALUE_MAPPING</code>, <code>SQL_EXPRESSION</code>.</dd>
<dt>Lookup</dt><dd>A dictionary inside a <code>VALUE_MAPPING</code> rule, for example <code>Deutschland → DE</code>. Matching ignores letter case.</dd>
<dt>Status</dt><dd><b>DRAFT</b> (editable), <b>SUBMITTED</b> (awaiting a second person), <b>APPROVED</b> (locked).</dd>
<dt>Run</dt><dd>One complete pass of all rows through an approved mapping set, with validation and de-duplication.</dd>
<dt>Defect</dt><dd>A group of rows that broke one rule, with examples and a root cause. Severities: CRITICAL, HIGH, MEDIUM, LOW. Only CRITICAL blocks the gate.</dd>
<dt>Gate</dt><dd>The release check of a run: <b>BLOCKED</b> while a critical defect is open, <b>READY FOR LOAD</b> when none is.</dd>
<dt>Exclusion</dt><dd>A rule that leaves rows out on purpose, with a recorded reason. Excluded rows are kept, not deleted.</dd>
<dt>Sign-off</dt><dd>A steward's recorded acceptance of a run that passed the gate. Required before load.</dd>
<dt>Target</dt><dd>Where data is delivered: SAP (workbook, sandbox, live OData), a database, or Firestore.</dd>
<dt>Certificate</dt><dd>The reconciliation report: every source row accounted for, every control listed, who approved and signed off.</dd>
<dt>Rollback</dt><dd>Removal of exactly the rows one delivery wrote (each row carries its run number).</dd>
<dt>Content pack</dt><dd>An exported approved mapping (rules, lookups, exclusions; no customer values) reusable in another migration.</dd>
</dl>
"""




# ================================================================================================================ 3. roles
def c_roles(sub) -> str:
    return f"""
{sub("Roles")}
<p>The application records the name in the <b>Acting as</b> box (top right of the window) against every action. In an installation with API keys configured, the name comes from the person's key;
without keys, anyone may type any name, which is acceptable for practice and not for real data (see 4.4).</p>
{table(["Role", "May", "May not"], [
    ["<b>Author</b>", "Register sources, profile, write or generate rules, test them on a sample, exclude rows, submit a mapping set, run an approved set.", "Approve a set they submitted."],
    ["<b>Approver</b>", "Read a submitted set and approve it (rules are then locked).", "Approve their own submission."],
    ["<b>Steward</b>", "Sign off a run that passed the gate; execute the load; verify; roll back.", "Sign off a run whose mapping they authored (default policy)."],
    ["<b>Administrator</b>", "Install, configure, manage keys, allowed servers, who may approve (<code>MIGRATION_APPROVERS</code>).", "Bypass the gate."],
    ["<b>Auditor</b>", "Read the certificate, audit log, compliance page, dashboard.", "(read only)"],
])}
{chart("roles_and_gate", "Who may do what. The author prepares and submits; a different person approves; the gate and the sign-off protect the target.")}
{control("<p><b>Segregation of duties.</b> The application refuses an approval by the author of the set. The certificate lists the author and the approver and fails its control check if they are the same person.</p>")}
{sub("Life of a mapping set")}
{chart("mapping_lifecycle", "Statuses of a mapping set. An approved set is never edited: a change makes a new version, which goes through the same approval.")}
"""




# ================================================================================================================ 4. getting started
def c_start(sub) -> str:
    return f"""
{sub("Requirements")}
{table(["Item", "Requirement"], [
    ["Server", "A computer that runs Python 3 (the practice runs used Python 3.13 on Windows 11); the catalogue needs <b>PostgreSQL</b> (Docker is the quickest way).."],
    ["Browser", "A current Microsoft Edge or Google Chrome. The user interface is a single web page; nothing is installed on user computers."],
    ["Disk", "A workspace folder for snapshots and run results. It holds copies of customer data: back it up and treat it as confidential."],
    ["AI (optional)", "Either <b>Ollama</b> with a local model (default <code>qwen2.5-coder:7b</code>; needs about 6 GB of memory and runs on a CPU, slowly) or an online <b>Gemini</b> key. Everything except AI mapping works without either."],
    ["SAP (optional)", "For live delivery: the address of the SAP OData service and a technical user. Without them the sandbox stand-in and the Migration Cockpit workbook are available."],
])}
{sub("Installation")}
<pre><code># 1. a PostgreSQL database for the catalogue
docker compose up -d postgres

# 2. Python packages
python -m venv .venv
.venv\\Scripts\\python -m pip install -r requirements.txt

# 3. settings: a file called .env in the project folder
DATABASE_URL=postgresql+psycopg2://migration:&lt;password&gt;@localhost:5432/migration_catalog
MIGRATION_WORKSPACE=workspace

# 4. create the tables and load the SAP field catalogue
.venv\\Scripts\\python -m alembic upgrade head
.venv\\Scripts\\python -m src.seeds.seed_sap_catalog

# 5. start the application, then open http://127.0.0.1:8000
.venv\\Scripts\\python -m uvicorn --factory src.api.main:create_app --host 127.0.0.1 --port 8000</code></pre>
{note("<p>After upgrading to a build that changes the SAP catalogue (for example the German column-name aliases), run step 4 again so the new descriptions are loaded.</p>")}
{sub("The main window")}
{img("02_home_tour.png", "The main window. 1 Menu bar · 2 Toolbar · 3 Explorer · 4 Tabs · 5 Work area · 6 Status bar · 7 Acting as.")}
{table(["#", "Part", "Use"], [
    ["1", "<b>Menu bar</b>", "File, View, Tools, Window, Help."],
    ["2", "<b>Toolbar</b>", "<b>New Source</b>, <b>New Target</b>, <b>Start</b>, <b>Sources</b>, <b>Combine</b>, <b>Dashboard</b>, <b>Tasks</b>, <b>Packs</b>, <b>Compliance</b>, <b>Refresh</b>, then <b>Profiler</b>, <b>Quality</b>, <b>Mapping</b>, <b>Governance</b>, <b>Duplicates</b>, <b>Certificate</b> (greyed until a table or run is selected), and <b>Keys</b>, <b>Help</b>."],
    ["3", "<b>Explorer</b>", "A tree: <b>Sources</b> (systems and tables, with their mapping sets), <b>Targets</b>, <b>Runs</b>, <b>Settings</b>. <b>Click</b> to select; <b>double-click</b> to open."],
    ["4", "<b>Tabs</b>", "Every page opens in a tab. Open tabs are restored when the page is reloaded."],
    ["5", "<b>Work area</b>", "The page itself; the first is the Start Page."],
    ["6", "<b>Status bar</b>", "Database state, SAP mode (here <i>sandbox, simulated</i>), the active AI provider."],
    ["7", "<b>Acting as</b>", "Your identity for the audit trail."],
])}
{caution("<p><b>Select first, then press.</b> Toolbar buttons act on the table or run selected in the Explorer. A grey button almost always means nothing is selected.</p>")}
{img("56_help.png", "The Start Page: a progress checklist and, on the right, the Guide helper that explains the next step.")}
{sub("Configuration reference")}
{table(["Setting", "Effect"], [
    ["<code>LLM_PROVIDER</code>", "<code>ollama</code> (default), <code>gemini</code> (online; keys are added under <b>Keys</b> and kept in memory only) or <code>ollama-only</code>."],
    ["<code>OLLAMA_MODEL</code>", "The local model, default <code>qwen2.5-coder:7b</code>."],
    ["<code>MIGRATION_REQUIRE_AUTH</code>", "<code>true</code>: refuse to start unless <code>MIGRATION_API_KEYS</code> is set. Recommended for real data."],
    ["<code>MIGRATION_API_KEYS</code>", "Maps keys to user names; the application then ignores a typed name."],
    ["<code>MIGRATION_APPROVERS</code>", "Who may approve and sign off. Default: anybody except the author."],
    ["<code>MIGRATION_WORKSPACE</code>", "Folder for snapshots and results."],
    ["<code>MIGRATION_DB_ALLOWED_HOSTS</code>", "Servers the application may connect to. Default: any, because a user enters the address. <b>Set this in production.</b>"],
    ["<code>MIGRATION_AUTO_SEED</code>", "Load the SAP field catalogue automatically at start (default on)."],
])}
"""




# ================================================================================================================ 5. practice data
def c_data(sub) -> str:
    return f"""
{sub("The practice file")}
<p>Procedures in chapter 6 are illustrated with <code>legacy_ar_customers.csv</code>: <b>1,980 rows, 10 columns</b>, invented names, deliberately untidy in the ways real extracts are.</p>
<pre><code>CUST_ID,CUST_NAME,TAX_ID,EMAIL,PHONE,ADDR_STREET,CITY,COUNTRY,POSTAL_CODE,PAYMENT_TERMS
100000,Penene Labs LLC,US974844773,accounts@penene0.example.com,+33-315-7664202,258 Birch Road 0,Miami,usa,24514,NET60
100001,Nomili Labs Ltd,,accounts@nomili1.example.com,+91-996-9238128,408 River Road 1,,India,714169,COD</code></pre>
{table(["Condition in the data", "Rows", "Consequence for SAP"], [
    ["City blank", "62", "Mandatory field <code>ADRC.CITY1</code> would be empty."],
    ["Country blank", "42", "Mandatory field <code>ADRC.COUNTRY</code> would be empty."],
    ["Countries that do not exist (Atlantis, Narnia, Wakanda)", "44", "No lookup entry; rows cannot be mapped."],
    ["Country written as USA, usa, US, United States, Deutschland, DE, UK, GB …", "most", "SAP needs the two-letter code: a lookup is required."],
    ["Payment terms written seven ways, including <code>WHEN-RECEIVED</code>", "~1,700", "SAP needs fixed codes; a person decides what <code>WHEN-RECEIVED</code> means."],
    ["Names longer than 40 characters", "14", "<code>BUT000.NAME_ORG1</code> holds 40."],
    ["The same company twice (same tax number)", "~100", "Should arrive once."],
])}
<p>The conditions are the realistic reason a first run is blocked, which is the point of the exercise: the controls are shown working against genuine problems.</p>
"""




# ================================================================================================================ 6. procedures
def c_procedures(sub) -> str:
    p: list[str] = []

    # ---- 6.1 register a file
    p.append(sub("Register a file source"))
    p.append(chart("landing", "Registration: read every row as written, keep all values as text, count, and fingerprint."))
    p.append(proc("PR-1 Register a source from a file",
                  "Freeze an extract file into a locked snapshot with a verified row count.", "Author",
                  "The extract file, the expected number of rows (control total), the name of the column(s) that identify a row.",
                  steps("In the toolbar press <b>New Source</b>. A five-page wizard opens.",
                        "<b>Page 1.</b> Give the source a name, for example <code>LEGACY_ERP</code> (the name of the legacy <i>system</i>; one system can hold many tables). Leave <b>Database</b> on <i>Extract file</i> and <b>Provider</b> on <i>Auto-detect</i>.",
                        "<b>Page 2.</b> Choose the file and press <b>Test</b>. The wizard reports the detected format, encoding, delimiter and column count.",
                        "<b>Page 3.</b> Enter the table name (<code>AR_CUSTOMERS</code>), tick the key column (<code>CUST_ID</code>) and enter the control total (<code>1980</code>).",
                        "<b>Page 4.</b> Tick <b>Profile the table now</b>. <b>Page 5</b> shows a summary; press <b>Finish</b>."),
                  "<p>The wizard closes, a <b>Profiler</b> tab opens and a message reads <i>Source LEGACY_ERP / AR_CUSTOMERS registered: 1,980 rows, 10 columns</i>.</p>"))
    p.append(note("<p>A shorter route exists for a plain file: the <b>Sources</b> page (shown in 6.2) has a <b>Register a legacy source</b> form with the same fields and a <b>Register &amp; upload</b> button.</p>"))
    p.append(img("03_wizard_provider.png", "Page 1: name the source. The Setup guide on the right explains each page and can fill in fields."))
    p.append(img("04_wizard_file.png", "Page 2: the application detected a delimited, utf-8, comma-separated file with 10 columns."))
    p.append(img("05_wizard_table.png", "Page 3: table name, key column and control total."))
    p.append(img("06_wizard_actions.png", "Page 4: actions to run when the table is registered."))
    p.append(img("07_wizard_summary.png", "Page 5: summary before anything is written."))
    p.append(caution("<p>If <b>Test</b> shows everything in one column or damaged accents, change <b>Delimiter</b> or <b>Encoding</b> and test again. If the row count differs from the control total, registration stops: obtain a complete extract or correct the number.</p>"))
    p.append(control("<p>The source is only read. All values are stored as text so nothing is rounded or re-typed. The row count and fingerprint of the snapshot are recorded and appear on the certificate.</p>"))

    # ---- 6.2 other sources
    p.append(sub("Register a database, document store or cloud source"))
    p.append(proc("PR-2 Register a database, Firestore, MongoDB or cloud-storage source",
                  "Read a table or collection from a live system into a snapshot, without installing anything on it.", "Author",
                  "The server address (or project), a read-only account, and permission to connect. In production, list the permitted servers in <code>MIGRATION_DB_ALLOWED_HOSTS</code>.",
                  steps("Press <b>New Source</b> and enter a name (letters, digits, <code>.</code> <code>_</code> <code>-</code>, up to 64).",
                        "In <b>Database</b> choose the kind: PostgreSQL, SQL Server, Oracle, MySQL, <b>MongoDB</b>, <b>Google Firestore</b>, or cloud file storage (Azure Blob, S3, SFTP). The Provider line shows the driver used.",
                        "<b>Connection.</b> Enter server, port, database, user and password (or, for Firestore, the project and service-account file) and press <b>Test</b>. The wizard really connects.",
                        "<b>Tables.</b> Browse the schemas and tables (or collections), choose one, set the key and the control total, and finish as in PR-1."),
                  "<p>The chosen table or collection is read, counted against the control total and frozen. For MongoDB and Firestore each document becomes one row and nested fields become dotted column names (for example <code>address.geo.lat</code>).</p>"))
    p.append(img("61_wizard_kinds.png", "Page 1 for a MongoDB source: the kind list groups Files, Databases (read only) and Cloud file storage."))
    p.append(img("62_wizard_mongodb.png", "Page 2 for MongoDB: the connection details, with the <b>Setup guide</b> on the right. It lists what the step needs, answers questions (<i>Which port should I use?</i>) and fills in a field when you tell it the server, database or user. The password goes only into the masked form field, never into the chat."))
    p.append(control("<p>A password is never written to disk and never echoed back after a failed attempt. MongoDB is read-only by design. Firestore bills one read per document; the wizard says so next to the driver.</p>"))
    p.append(img("60_sources.png", "The <b>Sources &amp; Runs</b> page: a quick form to register an extract file, the list of source tables (with buttons to open the Profiler or Mapping Studio) and the migration runs (with buttons for Governance and Certificate)."))

    # ---- 6.3 profile
    p.append(sub("Profile a table"))
    p.append(proc("PR-3 Profile a table",
                  "Measure what the data contains before any rule is written.", "Author",
                  "A registered table.",
                  steps("Select the table in the Explorer and press <b>Profiler</b> (it also opens automatically after registration).",
                        "Read the summary cards, then the column table, then the duplicates panel."),
                  "<p>Cards show rows, columns, average health, fully identical rows and candidate keys; one line per column follows.</p>"))
    p.append(img("08_profile.png", "The Profiler: 1,980 rows, 10 columns, average health 89.3, 3 candidate keys."))
    p.append(table(["Column", "Meaning"], [
        ["<b>Type</b>", "The detected type: BIGINT, VARCHAR …"],
        ["<b>Null ratio</b>", "Share of blank cells (<code>TAX_ID</code> 27.1%, <code>CITY</code> 3.1%)."],
        ["<b>Distinct</b>", "Different values: <code>CUST_ID</code> 100% (a key), <code>COUNTRY</code> 22 spellings."],
        ["<b>Length</b>", "Shortest and longest value: <code>CUST_NAME</code> 14 to 70 against SAP's 40."],
        ["<b>Top values</b>", "Most frequent values: <code>USA</code> 188, <code>usa</code> 183, <code>US</code> 180: one country, three spellings."],
        ["<b>Sample (masked)</b>", "Examples with letters shown as <code>A</code>/<code>a</code> and digits as <code>9</code>, so personal data is not exposed."],
        ["<b>Health</b>", "0 to 100; green is good, amber deserves a look."],
    ]))
    p.append(img("09_profile_columns.png", "The column table."))
    p.append(img("10_profile_duplicates.png", "Duplicates and keys: <code>CUST_ID</code> is unique; 60 surplus rows share a name, 40 a street."))

    # ---- 6.4 quality
    p.append(sub("Assess data quality"))
    p.append(proc("PR-4 Assess data quality",
                  "Run a library of tests and receive ranked findings with a recommended action each.", "Author",
                  "A profiled table.",
                  steps("Select the table and press <b>Quality</b>.", "Press <b>Assess data quality now</b>.", "Sort the findings by severity and read the <b>what to do</b> column."),
                  "<p>A score, the number of tests run and a findings table ranked worst first.</p>"))
    p.append(img("11_quality_before.png", "Quality page before the assessment."))
    p.append(img("12_quality_result.png", "Result: 110 tests, score 99.4, 8 findings (2 HIGH)."))
    p.append(img("13_quality_findings.png", "Each finding names the test, column, row count, an example and the recommended action."))
    p.append(table(["Finding", "Meaning"], [
        ["<span class='pill red'>HIGH</span> T03 Tax number used by several partners", "120 customers share a tax number: duplicates, branches or a typing error. A person decides."],
        ["<span class='pill red'>HIGH</span> C01 Not a country", "44 rows name Wakanda, Narnia or Atlantis."],
        ["<span class='pill amber'>MEDIUM</span> E03 E-mail shared", "A shared mailbox is fine; one person's address on several customers usually is not."],
        ["<span class='pill green'>LOW</span> C02 Country written as a name", "SAP needs <code>US</code>, not <code>United States</code>: a lookup is required."],
        ["<span class='pill green'>LOW</span> G01, G08, N05 …", "Extra spaces, double spaces, names written in capitals."],
    ]))
    p.append(caution("<p>A score of 99.4 is an average over many tests; a few rows with a grave problem hardly move it. Read the findings, not only the score.</p>"))

    # ---- 6.5 combine
    p.append(sub("Combine tables"))
    p.append(chart("combine_flow", "Combining tables. The preview refuses a join that would silently repeat records."))
    p.append(proc("PR-5 Combine registered tables into one source table",
                  "Join and filter tables you already registered (for example customers and payment terms) into one new source table, before mapping.", "Author",
                  "Two or more registered tables with a column that matches.",
                  steps("Press <b>Combine</b> in the toolbar.",
                        "In <b>Start from</b> choose the first table; every record of it is kept unless a filter or an inner join removes it.",
                        "Press <b>+ Add a table to join</b>. Choose the table, the column on each side and how to compare (ignoring blanks and letter case, or exact).",
                        "Optionally press <b>+ Add a filter</b> (<i>keep records where …</i>).",
                        "Press <b>Preview on my data</b>. Read the counts: records in the first table, rows after joining, and for each join how many records find <b>no partner</b>.",
                        "If the preview is accepted, enter the <b>System name</b>, the <b>Table name</b> and the <b>Primary key column(s)</b> and press <b>Create table</b>."),
                  "<p>A banner reads <i>Created table #N with R rows</i> and links to the Profiler. The new table appears under Sources and is used like any other source; the snapshot records which tables it was built from, so it can be rebuilt.</p>"))
    p.append(img("64_combine_setup.png", "Join set-up: customers joined to a payment-terms table on <code>PAYMENT_TERMS = CODE</code>."))
    p.append(img("65_combine_preview.png", "The preview before anything is created: 1,980 records stay 1,980 rows, and a warning says 1,124 customers find no partner in the lookup table (the data spells <i>NET30</i> where the table says <i>Net 30</i>), so the problem is seen before the table exists."))
    p.append(img("66_combine_created.png", "Creating the table: name, system and key columns."))
    p.append(img("67_combine_blocked.png", "A join to a contacts table would turn 1,980 customers into 2,010 rows (30 repeated). The preview is refused in red and <b>Create table</b> is not offered."))
    p.append(control("<p>A join that would repeat records is <b>refused</b> unless you tick <b>Allow a join that repeats records</b>, in which case the preview says so in plain words. Nothing is written until you press <b>Create table</b>.</p>"))

    # ---- 6.6 mapping
    p.append(sub("Write the mapping"))
    p.append(proc("PR-6 Create a mapping set",
                  "State, per SAP field, which legacy column fills it and how.", "Author",
                  "A registered, profiled table.",
                  steps("Select the table and press <b>Mapping</b> (or double-click <b>Mapping Studio</b> under the table).",
                        "Create rules in one of two ways, or both: <b>Generate AI mapping</b> (6.6.1) or <b>New empty mapping set</b> followed by <b>Add manual rule</b> (6.6.2).",
                        "Complete lookup dictionaries (6.6.3).",
                        "Press <b>Check rules on sample</b> (6.6.4) until it reports that the set can be submitted."),
                  "<p>A DRAFT mapping set with a card per rule, each showing the legacy column, the SAP field and its length, the kind of rule and a sample pass rate.</p>"))
    p.append(img("15_studio_buttons.png", "The Mapping Studio. 1 Generate AI mapping · 2 New empty mapping set · 3 Add manual rule · 4 Check rules on sample · 5 Submit · 6 Approve · 7 Run pipeline."))
    p.append("<h4>6.6.1 Let the AI propose</h4>")
    p.append(chart("ai_mapping", "AI mapping: the model chooses only among SAP fields found by a semantic search, and every proposal is checked by rules and by people."))
    p.append(steps("Press <b>Generate AI mapping</b>. Choose a consistency level if offered.", "The job runs on the server; a progress bar shows the column being examined. You may leave the page.", "Review every proposal: confidence, reason and the tag <i>AI-suggested</i>."))
    p.append(img("16_ai_running.png", "The AI examines one column at a time."))
    p.append(img("17_ai_result.png", "Each proposed rule carries a confidence and the model's name."))
    p.append(img("18_ai_rules.png", "Each proposal states its reason in words, so a person can judge it."))
    p.append(table(["Safeguard", "What it does"], [
        ["Candidate retrieval", "The model may choose only among the closest SAP fields found by meaning (including German names such as <i>Firmenname</i>)."],
        ["Rule validator", "Parses SQL, allows only listed functions, rejects absurd lengths, and marks rules that rewrite values (padding, replacing, translating) <b>NEEDS_REVIEW</b>."],
        ["Value check", "Samples real values: if most are not valid country codes, a country rule is marked for review."],
        ["Colours", "Green: confident (over 85%). Amber: review (60 to 85%). Red: blocked (under 60%). Never auto-approved."],
        ["Human approval", "Nothing takes effect until a second person approves the set."],
    ]))
    p.append(caution("<p><b>What the AI does not do.</b> In the evaluation recorded in <code>docs/ai-evaluation.md</code> it proposed plain copies and chose the right SAP field for 34 of 34 test columns after the German-alias fix. It did not invent the country dictionary, the payment-term codes or the fixed values your business must supply. Confidence percentages did not by themselves separate right from wrong answers; the value check did. On a CPU-only laptop a local 7B model needs minutes per column; an online model is faster if policy allows it.</p>"))
    p.append("<h4>6.6.2 Write the rules yourself</h4>")
    p.append(chart("rule_types", "Choosing the kind of rule."))
    p.append(table(["Kind", "Meaning", "Example"], [
        ["<code>DIRECT_COPY</code>", "Copy the column unchanged", "<code>CUST_NAME</code> → <code>BUT000.NAME_ORG1</code>"],
        ["<code>STATIC_VALUE</code>", "The same fixed value for every row", "<code>'Z001'</code> → <code>BUT000.BU_GROUP</code>"],
        ["<code>VALUE_MAPPING</code>", "Translate through a lookup", "<code>usa, United States → US</code> → <code>ADRC.COUNTRY</code>"],
        ["<code>SQL_EXPRESSION</code>", "A small formula", "<code>upper(left(trim(CUST_NAME), 20))</code> → <code>BU_SORT1</code>"],
    ]))
    p.append(steps("Press <b>New empty mapping set</b>. Its status is DRAFT.", "Press <b>Add manual rule</b>; choose the SAP table and field, the kind, the legacy column and the formula or fixed value (text in quotes). Press <b>Add rule</b>.", "Repeat for every field."))
    p.append(img("19_new_set.png", "A new, empty mapping set in status DRAFT."))
    p.append(img("20_add_rule_form.png", "Adding a rule: the fixed value <code>'Z001'</code> for <code>BUT000.BU_GROUP</code>."))
    p.append(img("21_rules_list.png", "The rules with their SAP field lengths and sample pass rates."))
    p.append(table(["SAP table", "Holds", "Fields used"], [
        ["<code>BUT000</code>", "The business partner", "<code>NAME_ORG1</code> (name), <code>BP_EXT</code>, <code>TYPE</code>, <code>BU_GROUP</code>, <code>BU_SORT1</code>"],
        ["<code>ADRC</code>", "Address", "<code>STREET</code>, <code>CITY1</code>, <code>POST_CODE1</code>, <code>COUNTRY</code>"],
        ["<code>ADR6</code> / <code>ADR2</code>", "E-mail / telephone", "<code>SMTP_ADDR</code> / <code>TEL_NUMBER</code>"],
        ["<code>KNB1</code>", "Accounting data", "<code>ZTERM</code> (payment terms), <code>BUKRS</code> (company code)"],
    ]))
    p.append("<h4>6.6.3 Complete the lookup dictionaries</h4>")
    p.append(steps("On a <code>VALUE_MAPPING</code> rule press <b>Edit</b>.", "In <b>Lookups</b> enter pairs as <code>{\"old value\": \"new value\"}</code>.", "Tick <b>Confirm as reviewed</b> (you state that you read them) and press <b>Save rule</b>."))
    p.append(img("22_lookup_editor.png", "A country dictionary being edited: <code>Great Britain → GB</code>, <code>UK → GB</code>."))
    p.append(note("<p>Matching ignores letter case: <code>USA → US</code> also finds <code>usa</code> and <code>Usa</code>.</p>"))
    p.append("<h4>6.6.4 Check the rules on a sample</h4>")
    p.append(steps("Press <b>Check rules on sample</b>. The rules are applied to the real rows as a dry run; nothing is saved.", "Read the result: whether the set can be submitted, warnings, the pass rate and <b>What to do next</b>."))
    p.append(img("23_preflight.png", "The pre-submission check: can be submitted; one warning (6 mandatory SAP fields have no rule); 90% of 1,980 rows pass every rule."))
    p.append(note("<p>A warning does not prevent submission. The 10% that fail are exactly what the gate will list after the first run (6.9).</p>"))

    # ---- 6.7 approval
    p.append(sub("Submit and approve"))
    p.append(proc("PR-7 Submit a mapping set and approve it",
                  "Obtain an independent review; lock the rules.", "Author submits; a different person approves",
                  "A DRAFT set whose check passed.",
                  steps("As the author press <b>Submit for approval</b>. The status becomes <b>SUBMITTED</b>.",
                        "Change <b>Acting as</b> to the approver's name (in a keyed installation the approver signs in with their own key).",
                        "Read the rules and press <b>Approve</b>."),
                  "<p>Status <b>APPROVED</b>, with the approver's name and time. The rules can no longer be edited.</p>"))
    p.append(img("24_submitted.png", "Submitted: waiting for somebody else."))
    p.append(img("25_approved.png", "Approved by a different user; the rules are locked."))
    p.append(control("<p>An approval by the author is refused (segregation of duties). The approval is part of the audit trail and the certificate.</p>"))

    # ---- 6.8 run
    p.append(sub("Run the pipeline"))
    p.append(chart("run_pipeline", "The run. Every row ends in exactly one place."))
    p.append(proc("PR-8 Run an approved mapping set",
                  "Convert, de-duplicate and validate every row.", "Author or steward",
                  "An APPROVED mapping set.",
                  steps("In the Mapping Studio press <b>Run pipeline</b>.", "Wait; a <b>Governance</b> tab opens when the run ends (seconds for thousands of rows; the scale test processed 1,000,000 rows, see <code>docs/scale-test.md</code>)."),
                  "<p>The Governance page of the new run.</p>"))
    p.append(control("<p>The accounting identity always holds: <i>left out + merged + rejected + valid = rows extracted</i>. A row cannot disappear without a reason.</p>"))

    # ---- 6.9 gate
    p.append(sub("Read the gate and the defects"))
    p.append(proc("PR-9 Interpret the gate",
                  "Decide whether the run may be signed off, and why not if it may not.", "Author, approver, steward",
                  "A finished run.",
                  steps("On the Governance page read the banner: <b>BLOCKED</b> or <b>READY FOR LOAD</b>.", "Scroll to <b>Rule-aggregated defects</b>. Each group gives the rule, severity, row count, examples and the root cause.", "Optionally use <b>Ask about this run</b> (6.17)."),
                  "<p>A clear statement of what blocks the gate and how many rows are affected.</p>"))
    p.append(img("27_gate_blocked.png", "First run: BLOCKED, four critical defects unresolved."))
    p.append(img("28_defects.png", "The defects of the first run; quality score 89.26%."))
    p.append(table(["Severity", "Defect", "Rows", "Cause", "Remedy"], [
        ["<span class='pill red'>CRITICAL</span>", "<code>ERR_MANDATORY_CITY1</code>", "62", "SAP requires a city; none supplied.", "Exclude the rows, or source the city."],
        ["<span class='pill red'>CRITICAL</span>", "<code>ERR_LOOKUP_FAILED_ZTERM</code>", "48", "<code>WHEN-RECEIVED</code> is not in the dictionary.", "Decide its meaning; add it to the lookup."],
        ["<span class='pill red'>CRITICAL</span>", "<code>ERR_LOOKUP_FAILED_COUNTRY</code>", "44", "Atlantis, Narnia, Wakanda.", "Not countries: exclude."],
        ["<span class='pill red'>CRITICAL</span>", "<code>ERR_MANDATORY_COUNTRY</code>", "42", "SAP requires a country; none supplied.", "Exclude the rows."],
        ["<span class='pill amber'>HIGH</span>", "<code>ERR_LENGTH_NAME_ORG1</code>", "14", "Name longer than SAP's 40.", "Shorten in the rule."],
    ]))
    p.append(chart("gate_decision", "Choosing the remedy for a blocked gate."))

    # ---- 6.10 fix
    p.append(sub("Correct the rules and exclude rows"))
    p.append(proc("PR-10 Create version 2 and run again",
                  "Resolve the defects in a new, editable copy; the approved set stays as evidence of run 1.", "Author, then approver",
                  "A run whose gate is BLOCKED.",
                  steps("In the Mapping Studio press <b>Check rules on sample</b> again. Under <b>Needs your approval</b> read each <b>Fix-it</b> card: the situation, the proposed change and how to undo it. Enter any value the card asks for and press <b>Yes, go ahead</b>. A new draft (a copy) is created.",
                        "Open <b>Rows to leave out</b>. Choose a column, a condition and a reason, press <b>Check how many rows</b>, then <b>Leave these rows out</b>.",
                        "Edit rules as needed: add <code>WHEN-RECEIVED → CASH</code> to the payment-term lookup; change the name rule to <code>left(trim(CUST_NAME), 40)</code>.",
                        "Press <b>Check rules on sample</b> until 100% pass; then submit, approve (a different person) and run (PR-7, PR-8)."),
                  "<p>The new run's gate reads <b>READY FOR LOAD</b> with no open critical defects.</p>"))
    p.append(img("29_fix_card.png", "A Fix-it card: SAP requires the company code <code>KNB1.BUKRS</code>; the application asks for the value and will not guess."))
    p.append(img("30_fix_done.png", "A new draft was made: <i>draft #2 (a copy of #1)</i>. The approved set is untouched."))
    p.append(img("31_exclusion_form.png", "One exclusion rule: <i>CITY is blank</i>, with a reason. The application first says how many rows match (62)."))
    p.append(img("32_exclusions_done.png", "Three exclusion rules: 147 of 1,980 rows left out."))
    p.append(img("33_name_rule_edit.png", "Changing a rule from a plain copy to <code>left(trim(CUST_NAME), 40)</code>."))
    p.append(img("34_preflight_v2.png", "The check on version 2: 1,833 rows in scope, 100% pass."))
    p.append(img("35_gate_v2.png", "Version 2: READY FOR LOAD; 0 open critical defects."))
    p.append(img("36_defects_v2.png", "The defects table is empty; quality score 100%."))
    p.append(caution("<p>Leaving out 147 customers (7%) is a business decision. The application makes it visible, recorded and reversible; it does not take it for you. Cutting a name at 40 characters can cut a word in half: it is the quickest fix, not the neatest.</p>"))
    p.append(control("<p>Excluded rows are not deleted. They are kept with the reason and listed on the certificate under <code>EXCLUDED_BY_RULE</code>.</p>"))

    # ---- 6.11 duplicates
    p.append(sub("Review duplicates"))
    p.append(chart("duplicates", "How two rows are judged: safety checks can only veto a merge, never create one."))
    p.append(proc("PR-11 Review duplicate customers",
                  "Make sure one company arrives once, and that two different companies are never merged.", "Author or steward",
                  "A finished run.",
                  steps("Select the run and press <b>Duplicates</b>.", "Read the summary and the lists: merges made on a name alone, <b>possible duplicates</b> awaiting a decision, and decisions already taken.", "For each uncertain pair choose <b>same customer</b> or <b>different</b> and give a reason."),
                  "<p>Decisions are remembered and applied in every later run, with your name and reason.</p>"))
    p.append(table(["Match", "Example", "Outcome"], [
        ["<b>Exact</b>: same tax number, e-mail or phone", "Two rows with tax number <code>AA999999999</code>", "Merged; one survivor, the others listed as not loaded."],
        ["<b>Fuzzy</b>: names and addresses nearly equal", "Müller GmbH, Hauptstr. 5 / Mueller GmbH, Hauptstrasse 5", "Score 0 to 100: high and safe is merged; in between goes to a person."],
        ["<b>Vetoed</b>", "Same name, different tax numbers", "Never merged."],
    ]))
    p.append(img("37_duplicates.png", "The Duplicates page; in this run every match was certain, so the review lists are empty."))
    p.append(img("38_duplicate_merges.png", "Merges made on a name alone."))
    p.append(img("39_duplicate_review.png", "Possible duplicates awaiting a human decision."))
    p.append(note("<p>Accents and German spellings (ü/ue, ß/ss) are ignored when comparing; a very common name such as Müller is never merged on the name alone; a clearly different address sends the pair to a person.</p>"))

    # ---- 6.12 signoff
    p.append(sub("Sign off"))
    p.append(proc("PR-12 Steward sign-off",
                  "Record that a named person has read the evidence and accepts the run for loading.", "Steward",
                  "Gate READY FOR LOAD. You must be an allowed approver and not the author of the mapping.",
                  steps("Open the Governance page of the run and set <b>Acting as</b> to the steward.", "Press <b>Steward sign-off for load</b>."),
                  "<p>A message names the person; the page and the certificate record who and when.</p>"))
    p.append(img("40_gate_buttons.png", "The final controls. 1 Gate · 2 Steward sign-off · 3 Load as · 4 Execute load · 5 Acting as."))
    p.append(img("41_signed_off.png", "Signed off."))
    p.append(control("<p>Approval answers “are the rules acceptable?”; sign-off answers “is the result acceptable and do I take responsibility for loading it?” Two questions, two moments, two records.</p>"))

    # ---- 6.13 deliver
    p.append(sub("Deliver and verify"))
    p.append(chart("delivery", "Delivery: preview, write in batches, count and compare, undo if wrong."))
    p.append(table(["Route", "What happens", "Use when"], [
        ["<b>Migration Cockpit workbook</b>", "An Excel file in the format of SAP's Migration Cockpit, uploaded into SAP by a person.", "The SAP team prefers to load itself."],
        ["<b>SAP sandbox (simulated)</b>", "Each customer is sent to the stand-in gateway; accepted and failed counts are reported.", "Rehearsals."],
        ["<b>SAP live</b>", "Each customer is sent to a real SAP over OData.", "Go-live. Needs the SAP address and a technical user; <b>not</b> exercised in this manual."],
        ["<b>Database target</b> or <b>Firestore</b>", "The finished rows are written to a new table (or collection).", "The new home is not SAP, or to inspect the result with ordinary tools."],
    ]))
    p.append(proc("PR-13 Define a database target (once)",
                  "Describe where data will be written, and prove the account may write there.", "Administrator or steward",
                  "A reachable database and an account with permission to create and fill a table.",
                  steps("Press <b>New Target</b> and choose <b>Database</b>; name it.", "Enter server, port, database, user and password; press <b>Test</b>.", "Choose the schema and run the write test: the wizard writes one test row, reads it back and deletes it.", "Review the summary and press <b>Finish</b>."),
                  "<p>The target appears under <b>Targets</b> in the Explorer.</p>"))
    p.append(img("42_target_kind.png", "New Target, page 1: kind and name."))
    p.append(img("43_target_connection.png", "Page 2: connection; the password is kept only in memory."))
    p.append(img("44_target_write_test.png", "Page 3: the write test proves the account may create and fill tables."))
    p.append(img("45_target_summary.png", "Summary."))
    p.append(proc("PR-14 Deliver to a database target and verify",
                  "Write the finished rows, then prove they arrived unchanged.", "Steward",
                  "A signed-off run and a defined target.",
                  steps("In <b>Deliver to a database target</b> choose the target and enter a <b>new table name</b> (<code>BP_CUSTOMERS</code>).", "Press <b>Show table &amp; SQL</b> to preview the table that will be created.", "Press <b>Deliver</b>.", "Press <b>Verify now</b> whenever you want to re-check the table against the approved result."),
                  "<p>Rows sent equal rows counted in the target, and every cell is read back and compared (25,995 cells in the practice run).</p>"))
    p.append(img("46_delivery_preview.png", "Preview: 1,733 rows into <code>public.bp_customers</code>, with exact column lengths and two bookkeeping columns, <code>legacy_key</code> and <code>mig_run_id</code>."))
    p.append(img("47_delivered.png", "Delivered: 1,733 sent, 1,733 counted (matches), 25,995 cells checked."))
    p.append(img("48_verified.png", "Verified again: <i>still identical</i>. A value changed afterwards would turn this red."))
    p.append(proc("PR-15 Load into SAP (sandbox or live)",
                  "Create business partners in SAP, or rehearse it.", "Steward",
                  "A signed-off run. For live: SAP address and technical user configured.",
                  steps("On the Governance page choose <b>Load as</b> (Migration Cockpit workbook, SAP sandbox, SAP live).", "Press <b>Execute load</b>."),
                  "<p>For the sandbox: <i>1733 loaded, 0 failed</i>, each customer with a business-partner number from the stand-in.</p>"))
    p.append(img("49_sap_sandbox_load.png", "A rehearsal load into the SAP stand-in."))
    p.append(control("<p><b>Create-only.</b> The application never overwrites or merges into existing data. If a table of that name exists it stops and says so.</p>"))
    p.append(caution("<p>Before a real go-live repeat PR-15 against the company's own SAP test client. The stand-in does not prove that real customising, number ranges and authorisations accept the data.</p>"))

    # ---- 6.14 certificate
    p.append(sub("Reconciliation certificate"))
    p.append(proc("PR-16 Produce and read the certificate",
                  "Obtain the proof that every source row is accounted for.", "Steward, auditor",
                  "A run that has been loaded.",
                  steps("Select the run and press <b>Certificate</b>.", "Read the banner, the drop-off accounting and the control checks.", "Download the page as HTML or Markdown if it must be kept outside the application."),
                  "<p><b>RECONCILIATION PASSED</b>, variance 0 rows.</p>"))
    p.append(img("50_certificate.png", "The certificate: extracted 1,980 · transformed 1,833 · duplicate children 100 · valid 1,733 · loaded 1,733 · failed 0."))
    p.append(table(["Reason", "Rows", "Meaning"], [
        ["LOADED", "1,733", "Created in the target."],
        ["EXCLUDED_BY_RULE", "147", "Left out on purpose, with reasons: no city 62, no country 41, not real countries 44."],
        ["DUPLICATE_CHILD", "100", "The same company as another row; merged into the survivor."],
        ["VALIDATION_REJECTED", "0", "Failed SAP's constraints (none: the gate made sure)."],
        ["SAP_LOAD_FAILED", "0", "Sent but refused by the system."],
        ["<b>Total</b>", "<b>1,980</b>", "<b>1,733 + 147 + 100 = 1,980</b>"],
    ]))
    p.append(img("51_certificate_more.png", "Control checks, each PASS: counts agree with the audit trail; author and approver differ; extract equals snapshot; control total verified; rows counted after load; cells read back and equal, with the fingerprint."))
    p.append(img("52_certificate_end.png", "Samples traced from source to SAP number, and who approved the mapping and when."))
    p.append(caution("<p>The banner reads PASSED only when a load really happened. A run that was never loaded (neither into SAP nor the stand-in) shows <i>not passed</i> for that single reason (<code>load_phase_reached</code>).</p>"))

    # ---- 6.15 rollback
    p.append(sub("Roll back a delivery"))
    p.append(proc("PR-17 Roll back one delivery",
                  "Remove exactly the rows one delivery wrote.", "Steward",
                  "A delivery listed on the Governance page.",
                  steps("Find the delivery in the table and press <b>Roll back</b>.", "Enter the reason (it is kept) and confirm."),
                  "<p>A message such as <i>1,733 row(s) removed from public.bp_customers; the table was empty, so it was dropped</i>. The history row reads <i>rolled back by …</i>.</p>"))
    p.append(img("58_rolled_back.png", "Rolled back, with the reason recorded."))
    p.append(control("<p>Every written row carries <code>mig_run_id</code>, so rows of other runs, or rows added by someone else, are never touched.</p>"))

    # ---- 6.16 packs
    p.append(sub("Reuse an approved mapping (content packs)"))
    p.append(chart("packs_flow", "A content pack carries approved rules to the next migration; an import is a draft that a different person must approve."))
    p.append(proc("PR-18 Export and import a content pack",
                  "Reuse rules, lookup dictionaries and leave-out rules from one migration in another, without copying any customer values.", "Author; a different person approves the import",
                  "An approved mapping set (export). A registered table (import).",
                  steps("Press <b>Packs</b>. Under export enter the <b>Mapping set</b> number and an optional <b>Pack name</b>; press <b>Download the pack (.json)</b>.",
                        "To import, enter the table number (<b>Into table</b>), choose the pack file and leave <b>dry run</b> ticked; press <b>Check / import</b>.",
                        "Read what would happen: rules that fit, rules whose columns are missing. Untick <b>dry run</b> and import.",
                        "Open the draft in the Mapping Studio, check it (PR-6.4) and have it approved (PR-7)."),
                  "<p>A DRAFT mapping set in the new table. It is not approved.</p>"))
    p.append(img("68_packs_export.png", "Export: choose the mapping set and give the pack a name."))
    p.append(img("69_packs_dry_run.png", "A dry-run import of mapping set 1 into table 14: 12 rules imported, 0 skipped, 24 lookup entries, 0 exclusions, and the next steps. Imported rules are flagged NEEDS_REVIEW; nothing is written."))
    p.append(control("<p>A pack contains rules and dictionaries only. An import never approves itself.</p>"))

    # ---- 6.17 overview pages
    p.append(sub("Dashboard, tasks, compliance and the assistant"))
    p.append(img("53_dashboard.png", "<b>Dashboard.</b> Tables, mapping sets and runs; per wave the latest run, gate, quality score and valid records, and the trend (89.26 → 100). <b>Compare two runs</b> shows what changed."))
    p.append(img("54_tasks.png", "<b>Tasks.</b> Who fixes what, by when. One task can be created for every open defect group, with an owner chosen by SAP table (for example <code>ADRC=address.owner</code>); a task closes itself when a later run no longer has the defect."))
    p.append(img("55_compliance.png", "<b>Compliance.</b> Each control checked now against this installation, with the standard it serves. It is evidence, not a certificate: human controls (key custody, legal basis) remain with the people who run the system."))
    p.append(img("57_assistant_answer.png", "<b>Ask about this run.</b> The question is answered from the run's own records and the figures are checked against the data (green badge); the table below the answer lists the 147 excluded customers with their reasons. Customer records are not sent to the AI. Note the wording of the sentence: a small local model writes it and it is generic (\"did not meet the filter criteria\"). Rely on the checked table, not on the prose."))

    # ---- 6.18 keys and menus
    p.append(sub("AI keys and the menus"))
    p.append(proc("PR-19 Add or remove an online AI key",
                  "Allow AI mapping through Gemini.", "Administrator",
                  "A Gemini API key and a policy that allows sending column names and a few sample values to an online service.",
                  steps("Press <b>Keys</b> (or the key indicator in the status bar).", "Paste the key (at least 16 characters), optionally label it, press <b>Test key</b>, then add it.", "Remove a key from the same dialog."),
                  "<p>The indicator turns active. Keys live in memory only and are lost when the server restarts.</p>"))
    p.append(img("70_keys.png", "The API key dialog."))
    p.append(img("71_window_menu.png", "The Window menu lists the open pages (the current one is ticked); the other menus offer the same actions as the toolbar."))
    p.append(control("<p>Keys and passwords are never written to a file or returned by the API. Rotate a key that has been pasted anywhere else.</p>"))
    return "".join(p)




# ================================================================================================================ 7. controls
def c_controls(sub) -> str:
    return f"""
{sub("Control framework")}
<p>The table lists what the application <b>enforces</b> (it cannot be bypassed from the user interface) and the evidence it leaves. Further detail is in <code>docs/controls.md</code> and <code>docs/compliance.md</code>;
the <b>Compliance</b> page re-checks the technical controls against the running installation.</p>
{table(["Control", "Enforced by", "Evidence"], [
    ["The legacy data is never changed", "Sources are read only; work uses a snapshot", "Snapshot row count and fingerprint on the certificate"],
    ["The extract is complete", "Control total checked at registration", "Certificate: control total verified"],
    ["Independent review of the rules", "Approval refused for the author", "Approver and time on the certificate; audit log"],
    ["Approved rules cannot drift", "Approved sets are locked; a change makes a new version", "Version history in the Explorer"],
    ["Nothing is loaded with critical defects", "The gate blocks sign-off and load", "Gate state and defect table of each run"],
    ["Every row is accounted for", "Run accounting identity", "Certificate drop-off table (sum equals extracted)"],
    ["Exclusions are deliberate", "Reason required; rows kept", "Exclusion list with reasons"],
    ["A responsible person accepts the result", "Steward sign-off before load", "Name and time recorded"],
    ["Delivery cannot damage existing data", "Create-only; stops if the table exists", "Delivery history"],
    ["Data arrived unchanged", "Row count and every cell read back", "Certificate: cells checked and equal; fingerprint"],
    ["Undo is exact", "Rows tagged with the run number", "Rollback record with reason"],
    ["Secrets are not stored", "Keys and passwords are memory-only", "Compliance page"],
    ["AI cannot act alone", "Candidate-only choice, validator, review, second approval", "AI-suggested tag and model name on each rule"],
])}
{sub("What the controls do not cover")}
<ul>
<li><b>Who the user is.</b> Without <code>MIGRATION_API_KEYS</code> a typed name is trusted. There is no company single sign-on.</li>
<li><b>Whether a business decision is right.</b> The application records excluding 147 customers; it does not judge whether that is acceptable.</li>
<li><b>SAP acceptance.</b> Only the stand-in has been exercised (appendix D).</li>
<li><b>Operating practice.</b> Backup of the workspace, key custody and the legal basis for processing personal data remain with the organisation.</li>
</ul>
"""




# ================================================================================================================ 8. troubleshooting
def c_trouble(sub) -> str:
    return f"""
{sub("Troubleshooting")}
{table(["Symptom", "Likely cause", "Action"], [
    ["A toolbar button is grey", "Nothing is selected in the Explorer.", "Click the table or run first."],
    ["The explorer is empty or the page says <i>Loading…</i>", "The page was opened while the server was down, or the catalogue database is unreachable.", "Check the status bar; start the database; reload with Ctrl+F5."],
    ["After <b>Test</b> everything is in one column, or accents are damaged", "Wrong delimiter or encoding was detected.", "Change <b>Delimiter</b> or <b>Encoding</b> and test again."],
    ["Row count differs from the control total", "Incomplete extract, or wrong number.", "Obtain a new extract or correct the total."],
    ["<b>Approve</b> or <b>Sign off</b> is refused", "You authored the mapping, or you are not an allowed approver.", "Act as a different, authorised person."],
    ["<b>Edit</b> is refused", "The set is approved and locked.", "Use a Fix-it card (makes a copy) or create a new set."],
    ["Gate BLOCKED", "A critical defect is open.", "See 6.9 and 6.10."],
    ["A <b>Combine</b> preview is refused", "The join would repeat records.", "Change the join key, or allow repeats on purpose."],
    ["AI mapping is very slow or returns nothing", "A local model on a small computer is slow; or no AI is configured.", "Write rules by hand, or add an online key under <b>Keys</b>."],
    ["An AI rule is marked NEEDS_REVIEW", "It rewrites values (padding, replacing, translating) or its samples do not fit.", "Read it; edit or delete it."],
    ["<b>Deliver</b> says the table exists", "Delivery is create-only.", "Choose another name, or roll back the earlier delivery."],
    ["<b>Deliver</b> is grey", "No sign-off, or no target defined.", "Sign off; define a target (PR-13)."],
    ["Firestore: quota exceeded", "The free plan allows about 20,000 writes a day.", "Wait, deliver fewer rows, or change plan."],
    ["Certificate says <i>not passed</i>", "No load has happened.", "Execute a load (sandbox or live)."],
    ["A connection test fails for a server you typed", "The host is not in <code>MIGRATION_DB_ALLOWED_HOSTS</code>, or a firewall blocks it.", "Ask the administrator."],
    ["Port busy at start", "Another copy is running.", "Use another port or stop the other copy."],
    ["Docker/PostgreSQL stops answering though the port is open", "The container engine hung.", "Restart Docker (<code>docker desktop restart</code>), then reload the page."],
])}
{sub("Frequently asked questions")}
<dl class="words">
<dt>Can the original data be damaged?</dt><dd>No. It is only read; all work is on a snapshot.</dd>
<dt>Can I use my own file?</dt><dd>Yes. CSV, Excel, JSON, fixed-width and Parquet files, databases (PostgreSQL, SQL Server, Oracle, MySQL), MongoDB, Firestore and cloud storage are supported.</dd>
<dt>Is the AI required?</dt><dd>No. Everything can be done by hand; the AI proposes and a person decides.</dd>
<dt>Is customer data sent to the internet?</dt><dd>Not with the local model. With Gemini, column names and a few sample values are sent for mapping; ask the administrator what policy applies. The assistant never sends customer records.</dd>
<dt>What if I change my mind after delivery?</dt><dd>Roll back (PR-17), correct, and deliver again.</dd>
<dt>What does “sandbox, simulated” in the status bar mean?</dt><dd>The application is connected to the SAP stand-in, not to a real SAP.</dd>
</dl>
"""




# ================================================================================================================ 9. appendices
def c_appendix(sub) -> str:
    return f"""
{sub("Appendix A: quick reference")}
{table(["#", "Task", "Where", "You should see"], [
    ["1", "Register a source", "New Source → wizard → Finish", "Profiler tab with the right row count"],
    ["2", "Profile / assess", "Profiler; Quality → Assess", "Health numbers; findings by severity"],
    ["3", "Combine (optional)", "Combine → Preview → Create table", "A new source table"],
    ["4", "Write rules", "Mapping → Generate AI mapping or Add manual rule", "A card per SAP field with a pass rate"],
    ["5", "Check", "Check rules on sample", "“can be submitted”"],
    ["6", "Submit / approve (someone else)", "Submit for approval; Approve", "SUBMITTED → APPROVED"],
    ["7", "Run", "Run pipeline", "Governance tab opens"],
    ["8", "Read the gate", "Governance", "BLOCKED + defects, or READY FOR LOAD"],
    ["9", "Fix", "Fix-it card; Rows to leave out; Edit rule", "A new DRAFT; repeat 5 to 8"],
    ["10", "Sign off (steward)", "Steward sign-off for load", "“signed off by …”"],
    ["11", "Deliver", "New Target (once) → Deliver; or Load as → Execute load", "Rows sent = rows counted; cells checked"],
    ["12", "Prove", "Certificate", "RECONCILIATION PASSED, variance 0"],
    ["–", "Undo", "Roll back", "“rolled back by …”"],
])}
{sub("Appendix B: defect codes")}
{table(["Pattern", "Meaning", "Usual remedy"], [
    ["<code>ERR_MANDATORY_&lt;FIELD&gt;</code>", "A field SAP requires is empty after mapping.", "Add or correct a rule; supply a value; exclude the rows."],
    ["<code>ERR_LOOKUP_FAILED_&lt;FIELD&gt;</code>", "A value is not in the rule's lookup dictionary.", "Add the value to the lookup, or exclude the rows."],
    ["<code>ERR_LENGTH_&lt;FIELD&gt;</code>", "A value is longer than the SAP field.", "Shorten in the rule, or correct the source."],
])}
<p>Severity: <span class="pill red">CRITICAL</span> blocks the gate; <span class="pill amber">HIGH</span>, MEDIUM and <span class="pill green">LOW</span> are reported without blocking.</p>
{sub("Appendix C: files and scripts")}
{table(["Path", "Purpose"], [
    ["<code>mock_data/legacy_ar_customers.csv</code>", "The 1,980-row practice file."],
    ["<code>docs/user-guide/</code>", "The illustrated, story-style guide for first-time readers (HTML and PDF)."],
    ["<code>docs/user-manual/</code>", "This manual (HTML and PDF)."],
    ["<code>docs/controls.md</code>, <code>docs/compliance.md</code>", "Control detail and the standards each control serves."],
    ["<code>docs/ai-evaluation.md</code>", "How the AI mapping was measured and what it gets wrong."],
    ["<code>docs/scale-test.md</code>", "The million-row test."],
    ["<code>scripts/make_guide_screenshots.py</code>", "Takes the real screenshots by operating the application."],
    ["<code>scripts/guide_diagrams.py</code>", "Draws the flow charts."],
    ["<code>scripts/build_user_manual.py</code>", "Assembles this manual (<code>--pdf</code> also writes the PDF)."],
])}
{sub("Appendix D: what was tested")}
{table(["Area", "Status"], [
    ["Files, profiling, quality rules", "<span class='pill green'>automated tests; used in this manual</span>"],
    ["AI mapping with a local model, and its safeguards", "<span class='pill green'>tested; photographed once (the step takes about 40 minutes on a laptop)</span>. The Gemini path is tested with a fake service."],
    ["Mapping, approval, run, de-duplication, gate, sign-off, certificate, rollback", "<span class='pill green'>automated tests; used in this manual</span>"],
    ["Combine tables, content packs, assistant, tasks, dashboard, compliance", "<span class='pill green'>automated tests and browser tests; used in this manual</span>"],
    ["Delivery to PostgreSQL, SQL Server, Oracle, MySQL, Firestore", "<span class='pill green'>tested against real servers (containers) and a real Firebase project</span>; PostgreSQL used in this manual"],
    ["MongoDB and Firestore as sources", "<span class='pill green'>tested against a MongoDB container and the Firestore emulator</span>; not connected in this manual (the screenshots show the questions the wizard asks)"],
    ["Million-row runs", "<span class='pill green'>tested at 1,000,000 rows</span>"],
    ["<b>SAP: Migration Cockpit workbook, sandbox, live OData</b>", "<span class='pill amber'>against a stand-in only; never a real SAP system</span>"],
    ["Single sign-on", "<span class='pill amber'>not built in; sign-in by API key is supported</span>"],
])}
{sub("Appendix E: how this manual was made")}
<p>The screenshots are unretouched, taken by <code>scripts/make_guide_screenshots.py</code> driving Microsoft Edge through the whole practice migration on a fresh copy of the application and a made-up file. Five pictures of the AI step are the
ones taken for the illustrated guide. The flow charts are drawn by <code>scripts/guide_diagrams.py</code>. Rebuild with <code>python scripts/build_user_manual.py --pdf</code>.</p>
"""




# ================================================================================================================ assembly
def toc_page(pages: dict[str, int] | None) -> str:
    rows = []
    for sid, title, _, subs in S:
        rows.append(("l1", sid, title))
        rows.extend(("l2", sub_id, sub_title) for sub_id, sub_title in subs)
    items = ""
    for kind, ident, title in rows:
        pg = f'<span class="pg">{pages[ident]}</span>' if pages and ident in pages else '<span class="pg"></span>'
        items += f'<li class="{kind}"><a href="#{ident}">{escape(title)}</a><span class="dots"></span>{pg}</li>'
    return f'<section class="tocpage" id="contents"><h2 class="sec" style="margin-top:0">Contents</h2><ol>{items}</ol></section>'


def build(pages: dict[str, int] | None = None) -> str:
    figno[0] = 0
    S.clear()
    # the chapters are built at import; rebuild their figure numbers deterministically by re-running them
    for sid, num, title, lead, fn, pb in CHAPTERS:
        chapter(sid, num, title, lead, fn, pb)
    nav = "".join(f'<a href="#{sid}">{escape(title)}</a>' + "".join(f'<a class="sub" href="#{i}">{escape(t)}</a>' for i, t in subs) for sid, title, _, subs in S)
    body = "".join(html for _, _, html, _ in S)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ERP Migration Accelerator User Manual</title>
<meta name="description" content="User manual of the ERP Migration Accelerator: roles, process, every procedure with real screenshots and flow charts, controls, troubleshooting and appendices.">
<style>{CSS}</style></head>
<body><div class="layout">
<nav class="toc"><h2>Contents</h2>{nav}</nav>
<main>
<div class="cover"><p class="kicker">User Manual</p><h1>ERP Migration Accelerator</h1>
<p>Moving master data from a legacy system into SAP S/4HANA, or into a database or Firestore, under controls that make the result provable.</p>
<table class="docctl"><tr><th>Document</th><td>User Manual</td></tr><tr><th>Version</th><td>{VERSION}</td></tr><tr><th>Date</th><td>{TODAY.strftime("%d %B %Y")}</td></tr>
<tr><th>Applies to</th><td>Application build {git_hash()} (main)</td></tr><tr><th>Audience</th><td>Migration authors, approvers, stewards, administrators and auditors</td></tr>
<tr><th>Practice data</th><td>1,980 invented customers; all screenshots are real</td></tr><tr><th>Classification</th><td>Internal</td></tr>
<tr><th>History</th><td>{VERSION} · {TODAY.strftime("%d %B %Y")} · First issue as a full manual: the practice migration end to end and every other page (sources of all kinds, Combine, Packs, Assistant, Keys).</td></tr></table></div>
{toc_page(pages)}
{body}
<footer>ERP Migration Accelerator User Manual v{VERSION} · built by scripts/build_user_manual.py · screenshots are from a practice run on made-up data.</footer>
</main></div></body></html>"""


CHAPTERS = [(sid, num, title, lead, fn, pb) for (sid, num, title, lead, fn, pb) in [
    ("intro", "1", "Introduction", "What this manual is, who it is for, and how to read it.", c_intro, False),
    ("concepts", "2", "Concepts and architecture", "The ideas the rest of the manual relies on.", c_concepts, True),
    ("roles", "3", "Roles and responsibilities", "Who does what, and what the application prevents.", c_roles, True),
    ("start", "4", "Getting started", "Requirements, installation, the main window and the settings that matter.", c_start, True),
    ("data", "5", "The practice data set", "The file used in the screenshots, and what is wrong with it on purpose.", c_data, False),
    ("procedures", "6", "Procedures", "One procedure per task, in the order of a migration.", c_procedures, True),
    ("controls", "7", "Controls and audit evidence", "What the application enforces and what it leaves behind as proof.", c_controls, True),
    ("trouble", "8", "Troubleshooting and FAQ", "Look up the symptom; each row gives a cause and an action.", c_trouble, True),
    ("appendix", "9", "Appendices", "Quick reference, defect codes, files, test status and provenance.", c_appendix, True),
]]


def render_pdf(html_path: Path, pdf_path: Path) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge")
        page = b.new_page(viewport={"width": 1100, "height": 1400})
        page.emulate_media(media="print")
        page.goto(html_path.as_uri())
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        page.pdf(path=str(pdf_path), format="A4", print_background=True, margin={"top": "16mm", "bottom": "16mm", "left": "13mm", "right": "13mm"}, display_header_footer=True,
                 header_template='<div style="font-size:8px;width:100%;padding:0 13mm;color:#777;display:flex;justify-content:space-between"><span>ERP Migration Accelerator - User Manual</span><span>Version ' + VERSION + '</span></div>',
                 footer_template='<div style="font-size:8px;width:100%;text-align:center;color:#777">Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>')
        b.close()


def page_numbers(pdf_path: Path) -> dict[str, int]:
    """Where each heading landed in the PDF, so the printed contents can carry page numbers (needs pypdf; without it the list has no numbers)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return {}

    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    texts = [norm(pg.extract_text() or "") for pg in PdfReader(str(pdf_path)).pages]
    last_entry = norm(S[-1][3][-1][1])                                                  # the last line of the contents list: the body starts on the page after it
    cur = next((i for i, t in enumerate(texts) if last_entry in t), 0) + 1
    found: dict[str, int] = {}
    for sid, title, _, subs in S:
        for ident, label in [(sid, title)] + subs:
            for i in range(cur, len(texts)):
                if norm(label) in texts[i]:
                    found[ident] = i + 1
                    cur = i                                                             # headings come in order, so the next one cannot be earlier
                    break
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    for name in BORROWED:
        if not (IMAGES / name).is_file() and (GUIDE_IMAGES / name).is_file():
            shutil.copy2(GUIDE_IMAGES / name, IMAGES / name)
    html = build()
    index = OUT / "index.html"
    index.write_text(html, encoding="utf-8")
    print(f"wrote {index} ({len(html) // 1024} KB), {len(S)} chapters, {figno[0]} figures")
    if missing:
        print("MISSING pictures:", ", ".join(sorted(set(missing))))
    if args.pdf:
        pdf = OUT / "ERP-Migration-User-Manual.pdf"
        render_pdf(index, pdf)
        pages = page_numbers(pdf)
        if pages:
            index.write_text(build(pages), encoding="utf-8")
            render_pdf(index, pdf)
        print(f"wrote {pdf} ({pdf.stat().st_size // 1024} KB){'' if pages else '; contents without page numbers (pypdf not installed)'}")


if __name__ == "__main__":
    main()
