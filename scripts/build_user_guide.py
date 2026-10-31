"""Builds docs/user-guide/index.html (and, with --pdf, docs/user-guide/ERP-Migration-Guide.pdf): a step-by-step guide to the application that a school child can follow.

    python scripts/make_guide_screenshots.py ...   (the real screenshots, docs/user-guide/images)
    python scripts/build_user_guide.py [--pdf]

The flow charts come from scripts/guide_diagrams.py and are pasted into the page as SVG; the pictures of the application are the real screenshots. A picture that is missing is
reported and left out, so the guide can be rebuilt while the screenshots are being retaken.
"""

from __future__ import annotations

import argparse
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import guide_diagrams as D  # noqa: E402

OUT = ROOT / "docs" / "user-guide"
IMAGES = OUT / "images"
missing: list[str] = []

CSS = """
:root { --ink:#1b2333; --muted:#566074; --line:#dfe3ea; --bg:#f6f7fb; --card:#fff; --brand:#4f46e5; --brand-soft:#eceafd; --ok:#1f8a4c; --ok-soft:#e3f5ea; --warn:#b7791f; --warn-soft:#fff4d6;
        --bad:#c53030; --bad-soft:#fde8e8; --tip:#2563eb; --tip-soft:#e6efff; }
@media (prefers-color-scheme: dark) { :root:not([data-print]) { --ink:#e8ecf4; --muted:#a3adc0; --line:#2c3446; --bg:#111623; --card:#171d2d; --brand:#8b83ff; --brand-soft:#252a52; --ok:#5fd18d; --ok-soft:#173326;
        --warn:#f0b957; --warn-soft:#3a2f14; --bad:#ff8585; --bad-soft:#3b1b1b; --tip:#7fb0ff; --tip-soft:#192c4d; } }
* { box-sizing:border-box; }
html { scroll-behavior:smooth; scroll-padding-top:16px; }
body { margin:0; background:var(--bg); color:var(--ink); font:17px/1.65 "Segoe UI", system-ui, -apple-system, Helvetica, Arial, sans-serif; }
.layout { display:grid; grid-template-columns:270px minmax(0,1fr); max-width:1280px; margin:0 auto; }
nav.toc { position:sticky; top:0; align-self:start; height:100vh; overflow:auto; padding:22px 14px 30px 20px; border-right:1px solid var(--line); font-size:14.5px; }
nav.toc h2 { margin:0 0 10px; font-size:13px; letter-spacing:.09em; text-transform:uppercase; color:var(--muted); }
nav.toc a { display:block; padding:5px 8px; border-radius:7px; color:var(--ink); text-decoration:none; line-height:1.35; }
nav.toc a:hover { background:var(--brand-soft); }
nav.toc a.sub { padding-left:20px; color:var(--muted); font-size:13.5px; }
main { padding:34px 38px 80px; min-width:0; }
.cover { background:linear-gradient(135deg,#4f46e5,#7c3aed); color:#fff; border-radius:22px; padding:44px 40px; margin-bottom:30px; }
.cover h1 { margin:0 0 10px; font-size:40px; line-height:1.15; }
.cover p { margin:6px 0; font-size:19px; opacity:.95; max-width:720px; }
.cover .meta { margin-top:20px; font-size:14px; opacity:.85; }
h2.sec { font-size:30px; margin:62px 0 6px; padding-top:10px; line-height:1.2; }
h2.sec .num { display:inline-flex; align-items:center; justify-content:center; width:46px; height:46px; margin-right:12px; border-radius:50%; background:var(--brand); color:#fff; font-size:23px; vertical-align:middle; }
h3 { font-size:21px; margin:34px 0 6px; }
p.lead { font-size:19px; color:var(--muted); margin:4px 0 18px; }
.box { border-radius:14px; padding:14px 18px; margin:18px 0; border:1px solid var(--line); background:var(--card); }
.box > :first-child { margin-top:0; } .box > :last-child { margin-bottom:0; }
.box .tag { display:inline-block; font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; margin-bottom:4px; }
.box.think { background:var(--brand-soft); border-color:transparent; } .box.think .tag { color:var(--brand); }
.box.see { background:var(--ok-soft); border-color:transparent; } .box.see .tag { color:var(--ok); }
.box.careful { background:var(--warn-soft); border-color:transparent; } .box.careful .tag { color:var(--warn); }
.box.stop { background:var(--bad-soft); border-color:transparent; } .box.stop .tag { color:var(--bad); }
.box.try { background:var(--tip-soft); border-color:transparent; } .box.try .tag { color:var(--tip); }
figure { margin:22px 0; background:var(--card); border:1px solid var(--line); border-radius:14px; overflow:hidden; box-shadow:0 2px 10px rgba(20,30,60,.06); break-inside:avoid; }
figure img { display:block; width:100%; height:auto; }
figcaption { padding:10px 16px 12px; font-size:15px; color:var(--muted); border-top:1px solid var(--line); }
figcaption b { color:var(--ink); }
.diagram { margin:24px 0; background:#fff; border:1px solid var(--line); border-radius:14px; padding:14px 10px 6px; break-inside:avoid; }
.diagram svg { display:block; width:100%; height:auto; }
.diagram .cap { text-align:center; font-size:14.5px; color:#566074; padding:4px 10px 10px; }
.steps { counter-reset:s; list-style:none; padding:0; margin:16px 0; }
.steps li { counter-increment:s; position:relative; padding:4px 0 10px 48px; }
.steps li::before { content:counter(s); position:absolute; left:0; top:2px; width:32px; height:32px; border-radius:50%; background:var(--brand); color:#fff; font-weight:700; text-align:center; line-height:32px; font-size:16px; }
table.t { width:100%; border-collapse:collapse; margin:16px 0; font-size:15.5px; background:var(--card); border-radius:12px; overflow:hidden; border:1px solid var(--line); }
table.t th, table.t td { text-align:left; vertical-align:top; padding:9px 12px; border-bottom:1px solid var(--line); }
table.t th { background:var(--brand-soft); font-size:13px; letter-spacing:.05em; text-transform:uppercase; color:var(--muted); }
table.t tr:last-child td { border-bottom:0; }
.tw { overflow-x:auto; margin:16px 0; } .tw table.t { margin:0; }
code, .mono { overflow-wrap:break-word; font-family:Consolas, "Cascadia Mono", monospace; font-size:.92em; background:var(--brand-soft); padding:1px 6px; border-radius:6px; }
pre { background:#161b2b; color:#e6ebf7; padding:14px 16px; border-radius:12px; overflow:auto; font-size:14px; line-height:1.5; }
pre code { background:none; padding:0; overflow-wrap:normal; }
dl.words { display:grid; grid-template-columns:minmax(150px,230px) 1fr; gap:0; border:1px solid var(--line); border-radius:12px; overflow:hidden; background:var(--card); }
dl.words dt { font-weight:700; padding:10px 14px; border-bottom:1px solid var(--line); background:var(--brand-soft); }
dl.words dd { margin:0; padding:10px 14px; border-bottom:1px solid var(--line); }
dl.words dt:last-of-type, dl.words dd:last-of-type { border-bottom:0; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:16px; } .two > * { min-width:0; }
.pill { display:inline-block; padding:1px 10px; border-radius:999px; font-size:13.5px; font-weight:600; }
.pill.red { background:var(--bad-soft); color:var(--bad); } .pill.green { background:var(--ok-soft); color:var(--ok); } .pill.amber { background:var(--warn-soft); color:var(--warn); }
footer { margin-top:70px; padding-top:20px; border-top:1px solid var(--line); color:var(--muted); font-size:14px; }
@media (max-width: 980px) { .layout { grid-template-columns:1fr; } nav.toc { position:static; height:auto; border-right:0; border-bottom:1px solid var(--line); } main { padding:22px 16px 60px; } .two { grid-template-columns:1fr; }
        .cover h1 { font-size:30px; } dl.words { grid-template-columns:1fr; } }
@media print {
  :root { --bg:#fff; } body { font-size:12.4px; line-height:1.5; background:#fff; } nav.toc { display:none; } .layout { display:block; max-width:none; } main { padding:0; }
  .cover { border-radius:10px; padding:26px 26px; -webkit-print-color-adjust:exact; print-color-adjust:exact; } .cover h1 { font-size:30px; } h2.sec { font-size:22px; margin-top:26px; break-after:avoid; page-break-after:avoid; } h2.sec .num { width:34px; height:34px; font-size:17px; }
  h3 { break-after:avoid; page-break-after:avoid; font-size:16px; } .box, figure, .diagram, table.t tr { break-inside:avoid; page-break-inside:avoid; } figure { box-shadow:none; }
  .box, .pill, table.t th, dl.words dt, .steps li::before, code { -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  .sec-break { break-before:page; page-break-before:always; } a { color:inherit; text-decoration:none; }
}
"""


def img(name: str, caption: str, alt: str = "") -> str:
    path = IMAGES / name
    if not path.is_file():
        missing.append(name)
        return f'<div class="box careful"><span class="tag">Picture missing</span><p>{escape(name)}</p></div>'
    return f'<figure><img src="images/{name}" alt="{escape(alt or caption)}" loading="lazy"><figcaption>{caption}</figcaption></figure>'


def chart(name: str, caption: str) -> str:
    return f'<div class="diagram">{D.ALL[name]()}<div class="cap">{caption}</div></div>'


def box(kind: str, title: str, body: str) -> str:
    return f'<div class="box {kind}"><span class="tag">{escape(title)}</span>{body}</div>'


def think(body: str) -> str:
    return box("think", "Think of it like this", body)


def see(body: str) -> str:
    return box("see", "What you should see", body)


def careful(body: str) -> str:
    return box("careful", "Be careful", body)


def tryit(body: str) -> str:
    return box("try", "Try it", body)


def why(body: str) -> str:
    return box("think", "Why does this matter?", body)


def steps(*items: str) -> str:
    return '<ol class="steps">' + "".join(f"<li>{i}</li>" for i in items) + "</ol>"


def table(head: list[str], rows: list[list[str]]) -> str:
    return '<div class="tw"><table class="t"><thead><tr>' + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</tbody></table></div>"


def section(sid: str, num: str, title: str, lead: str, body: str, page_break: bool = False) -> tuple[str, str, str]:
    head = f'<h2 class="sec{" sec-break" if page_break else ""}" id="{sid}">{f"<span class=num>{num}</span>" if num else ""}{title}</h2><p class="lead">{lead}</p>'
    return sid, f"{num + '. ' if num else ''}{title}", head + body


S: list[tuple[str, str, str]] = []

# ============================================================================================ 0. start here
S.append(section("start-here", "", "Start here", "What this guide is, who it is for, and what you will do.", f"""
<p>Imagine a school with an <b>old paper register</b> of every student. The school gets a brand-new computer system, and somebody has to <b>copy every student into it</b> without losing one,
without typing a name wrong, and without putting the same student in twice.</p>
<p>That is exactly the job of this application. Only the "students" are <b>customers of a company</b>, the "old register" is an <b>old computer system</b> (or a spreadsheet),
and the "new computer system" is <b>SAP S/4HANA</b>, a very large program that many big companies use to run their business.</p>
<p>This guide walks you through a whole move, <b>from the very first click to the final receipt</b>, using a practice file of 1,980 made-up customers. You do not need to know anything about computers
to follow it. Every new word is explained the first time it appears, and every step shows <b>a real picture taken from the application</b>.</p>
{tryit("<p>Read once from top to bottom, then do it yourself with the practice file <code>mock_data/legacy_ar_customers.csv</code>. A grown-up who looks after the computer can start the application for you: the last chapter, <a href='#grownups'>For the grown-up</a>, tells them how.</p>")}
{box("careful", "Two honest things before we start", "<ul><li><b>The pictures are real</b>, taken by a robot that used the application exactly like a person. Only the red numbered circles were added, to point at things.</li><li><b>SAP itself was not used.</b> The application can talk to a real SAP, but for this guide it talked to a <b>practice SAP stand-in</b> (a pretend SAP that behaves like the real one). Where you see \"SAP\" in a picture, that is the stand-in. The chapter on delivery says what that means.</li></ul>")}
<div class="two">
{box("think", "How long does it take?", "<p>Reading: about 40 minutes. Doing the whole practice move: about 30 minutes, plus a wait of a few minutes while the AI helper thinks, if you let it.</p>")}
{box("think", "What do I need to know first?", "<p>Nothing. If you can use a web browser and fill in a form, you can follow this guide.</p>")}
</div>
"""))

# ============================================================================================ 1. the story
S.append(section("story", "", "The story: moving house", "If you remember one idea from this guide, remember this one.", f"""
<p>Moving a company's data is just like <b>moving house</b>. Every part of the application matches something you already know:</p>
{table(["In moving house …", "In this application …", "What it is"], [
    ["The <b>old house</b>", "The <b>legacy system</b> (the old computer system or file)", "Where the data lives today. We only <i>look</i> at it; we never change it."],
    ["Each <b>box</b> you pack", "A <b>row</b> (one customer)", "One customer = one box."],
    ["The <b>labels</b> on a box: \"kitchen\", \"books\"", "The <b>columns</b> (name, city, country …)", "The kinds of facts we keep about each customer."],
    ["<b>Looking through</b> the boxes before packing", "<b>Profiling</b> and the <b>quality check</b>", "Counting, spotting broken things, finding blanks."],
    ["The <b>list</b>: \"kitchen things go in the new kitchen\"", "The <b>mapping</b> (a set of <b>rules</b>)", "Says which old column goes into which new field, and how to change it on the way."],
    ["A friend <b>double-checks</b> your list", "<b>Approval</b> by a second person", "Nobody marks their own homework."],
    ["The <b>moving van</b>", "The <b>run</b> (the pipeline)", "Converts, cleans and checks every customer."],
    ["The <b>inspector</b> at the new door", "The <b>gate</b> (governance)", "Says \"stop, a box is broken\" or \"all clear\"."],
    ["You <b>sign</b> the delivery note", "The <b>sign-off</b>", "A named person takes responsibility."],
    ["The <b>new house</b>", "<b>SAP S/4HANA</b> (or a database, or Firebase)", "Where the customers end up."],
    ["The <b>receipt</b> listing everything delivered", "The <b>reconciliation certificate</b>", "Proof that nothing was lost or added."],
    ["\"Oops, put it all back!\"", "<b>Rollback</b>", "Takes back exactly what one delivery put in."],
])}
{think("<p>A good move is not fast. It is <b>provable</b>. At the end you can show anyone a receipt that says: \"We started with 1,980 customers. 147 were left out on purpose, for these reasons. 100 were the same company twice. The other 1,733 arrived, and we compared every single value.\"</p>")}
"""))

# ============================================================================================ 2. big picture
S.append(section("big-picture", "", "The big picture", "Eleven steps, one picture. The numbers match the chapters. Come back to this page whenever you feel lost.", f"""
{chart("big_picture", "The whole journey. Blue boxes are done by the application, amber boxes need a person, the purple box is where AI helps (and a person still checks).")}
{table(["Chapter", "Step", "In one sentence", "Who"], [
    ["1", "<a href='#s1'>Bring it in</a>", "Make an exact, locked copy of the old data so we never touch the original.", "You"],
    ["2", "<a href='#s2'>Look at it</a>", "Count, measure and find the problems before they hurt.", "You"],
    ["3", "<a href='#s3'>Write the rules</a>", "Say which old column becomes which new field. The AI can propose; you decide.", "You (+ AI)"],
    ["4", "<a href='#s4'>Approve</a>", "A <i>second</i> person reads the rules and says yes.", "Another person"],
    ["5", "<a href='#s5'>Run</a>", "The application converts, cleans, de-duplicates and checks every customer.", "Application"],
    ["6", "<a href='#s6'>The gate</a>", "It tells you what is still wrong. Red = stop. Green = go.", "Application"],
    ["7", "<a href='#s7'>Fix</a>", "Make a new version of the rules that fixes the problems, then repeat steps 4–6.", "You"],
    ["8", "<a href='#s8'>Twins</a>", "Part of the run: the same company entered twice is recognised and merged.", "Application"],
    ["9", "<a href='#s9'>Sign off</a>", "A named person says \"I accept this\".", "A steward"],
    ["10", "<a href='#s10'>Deliver</a>", "Put the customers into the new system and check them afterwards.", "A steward"],
    ["11", "<a href='#s11'>Certificate</a>", "The receipt that proves everything is accounted for.", "Application"],
])}
"""))

# ============================================================================================ 3. words
S.append(section("words", "", "Words you will meet", "Skim this list now. Come back when a word surprises you.", f"""
<dl class="words">
<dt>Row</dt><dd>One line of data. In our file, one customer.</dd>
<dt>Column</dt><dd>One kind of fact, like <code>CITY</code> or <code>COUNTRY</code>. A table is rows and columns, like a spreadsheet.</dd>
<dt>Table</dt><dd>A named grid of rows and columns. Our practice table is called <code>AR_CUSTOMERS</code> ("AR" = accounts receivable: the customers who owe the company money).</dd>
<dt>Key</dt><dd>The column that is <b>different for every row</b>, like a student number. Here: <code>CUST_ID</code>. Without a key you cannot say <i>which</i> customer a problem belongs to.</dd>
<dt>Control total</dt><dd>The number of rows you <b>expect</b>. You type it in; the application counts and complains if the two numbers differ. (Like counting boxes before the van leaves.)</dd>
<dt>Source / legacy</dt><dd>The old system or file that you are moving <i>from</i>.</dd>
<dt>Target</dt><dd>The new place you are moving <i>to</i>: SAP, a database, or Firebase.</dd>
<dt>Snapshot</dt><dd>A locked copy of the source, taken once. Everything afterwards works on the snapshot, so the original can never be damaged.</dd>
<dt>Profile</dt><dd>A medical check-up for data: how many rows, how many blanks, how many different values, how long the values are.</dd>
<dt>Blank (null)</dt><dd>A cell with nothing in it.</dd>
<dt>Duplicate</dt><dd>The same customer entered twice (or more).</dd>
<dt>Business partner (BP)</dt><dd>SAP's word for a customer or supplier. We are creating <b>business partners</b>.</dd>
<dt>Mapping / rule</dt><dd>A <b>mapping</b> is the whole list; a <b>rule</b> is one line: "put this old column into that new field, like this".</dd>
<dt>Lookup table</dt><dd>A translation dictionary inside a rule: <code>USA → US</code>, <code>Deutschland → DE</code>. SAP wants two-letter country codes; people wrote anything.</dd>
<dt>Mandatory field</dt><dd>A field SAP insists on. Leave it empty and SAP refuses the customer.</dd>
<dt>Approval</dt><dd>A second person says the rules are fine. After that the rules are <b>locked</b>.</dd>
<dt>Run / pipeline</dt><dd>One complete pass of all customers through the rules and the checks.</dd>
<dt>Defect</dt><dd>A problem found by the run, grouped by kind. <span class="pill red">CRITICAL</span> blocks the gate; <span class="pill amber">HIGH</span> and lower are warnings.</dd>
<dt>Gate</dt><dd>The inspector. It is <span class="pill red">BLOCKED</span> while a critical defect exists and <span class="pill green">READY FOR LOAD</span> when none is left.</dd>
<dt>Sign-off</dt><dd>A named steward presses a button that says "I accept this run". It is recorded forever.</dd>
<dt>Delivery / load</dt><dd>Writing the finished customers into the new system.</dd>
<dt>Certificate</dt><dd>The receipt: every row accounted for, every check listed, who approved, who signed off.</dd>
<dt>Rollback</dt><dd>Taking back exactly what one delivery wrote, and nothing else.</dd>
<dt>Fingerprint (hash)</dt><dd>A short code computed from the data. Change one letter in a million rows and the code changes. Used to prove "this is the same data".</dd>
</dl>
"""))

# ============================================================================================ 4. the screen
S.append(section("screen", "", "Meet the screen", "Everything happens in one window. Learn its seven parts and you will never be lost.", f"""
{img("02_home_tour.png", "<b>The home screen.</b> 1 Menu bar · 2 Toolbar (the big buttons) · 3 Explorer (the tree on the left) · 4 Tabs · 5 Work area · 6 Status bar · 7 \"Acting as\".")}
{table(["#", "Part", "What it is for"], [
    ["1", "<b>Menu bar</b>", "File, View, Tools, Window, Help: the usual menus."],
    ["2", "<b>Toolbar</b>", "The big buttons: <b>New Source</b> (bring data in), <b>New Target</b> (say where it goes), <b>Profiler</b>, <b>Quality</b>, <b>Mapping</b>, <b>Governance</b>, <b>Duplicates</b>, <b>Certificate</b>, and more. Some are <span class='pill amber'>greyed out</span> until they make sense: you cannot ask for a certificate before there is a run."],
    ["3", "<b>Explorer</b>", "A tree of everything: <b>Sources</b> (your tables), <b>Targets</b>, <b>Runs</b>, <b>Settings</b>. <b>Click</b> an item to pick it; the toolbar buttons then work on it. <b>Double-click</b> to open it."],
    ["4", "<b>Tabs</b>", "Every page opens in a tab, like a web browser, so you can jump back and forth."],
    ["5", "<b>Work area</b>", "The page itself. The first one is the Start Page with a progress checklist."],
    ["6", "<b>Status bar</b>", "Is the database OK? Which SAP is connected (here: <i>sandbox, simulated</i>)? Which AI is active (here: <i>ollama</i>, a model on this computer)?"],
    ["7", "<b>Acting as</b>", "<b>Who you are.</b> Every action is recorded under this name. In this practice set-up you type a name; in a real company people sign in and the name comes from their account."],
])}
{careful("<p><b>Pick first, then press.</b> If a toolbar button is grey, you have not picked the table (or the run) in the Explorer yet. This is the most common reason for \"nothing happens\".</p>")}
{img("56_help.png", "<b>The Start Page.</b> A checklist of the journey (all six boxes are ticked here because the practice move is finished) and, on the right, the <b>Guide</b>: a helper that explains the next step and answers questions.")}
"""))

# ============================================================================================ 5. practice file
S.append(section("practice-file", "", "Our practice file", "Meet the messy customer list we are going to move.", f"""
<p>The file is <code>legacy_ar_customers.csv</code>. A <b>CSV</b> is the simplest kind of spreadsheet file: plain text, one row per line, values separated by commas. It has <b>1,980 rows</b> and <b>10 columns</b>.
All names are invented; no real person is in it.</p>
<pre><code>CUST_ID,CUST_NAME,TAX_ID,EMAIL,PHONE,ADDR_STREET,CITY,COUNTRY,POSTAL_CODE,PAYMENT_TERMS
100000,Penene Labs LLC,US974844773,accounts@penene0.example.com,+33-315-7664202,258 Birch Road 0,Miami,usa,24514,NET60
100001,Nomili Labs Ltd,,accounts@nomili1.example.com,+91-996-9238128,408 River Road 1,,India,714169,COD
100002,Jujukama Motors Inc,CA430312277,accounts@jujukama2.example.com,+44-924-7743569,444 Pine Road 2,Toronto,Canada,K8H 8H9,NET60</code></pre>
<p>Look at the second row: the <code>CITY</code> is empty. Look at the first: the country is written <code>usa</code> in small letters. <b>That is real life.</b> The file is messy on purpose, in the ways real files are messy:</p>
{table(["What is wrong", "How many", "Why SAP minds"], [
    ["<b>City is blank</b>", "62 rows", "SAP needs a city to post a letter."],
    ["<b>Country is blank</b>", "42 rows", "SAP cannot choose the address and tax rules without it."],
    ["<b>Countries that do not exist</b>: Atlantis, Narnia, Wakanda", "44 rows", "Test rows that slipped into the old system."],
    ["<b>Same country, many spellings</b>: USA, usa, US, United States; Germany, Deutschland, DE; UK, Great Britain, GB …", "most rows", "SAP wants the two-letter code, exactly: <code>US</code>, <code>DE</code>, <code>GB</code>."],
    ["<b>Payment terms spelled 7 ways</b>: COD, Net 30, NET30, NET 60, NET60, NET90, and a strange <code>WHEN-RECEIVED</code> (50 rows)", "~1,700 rows", "SAP has fixed payment-term <i>codes</i>. A human must decide what <code>WHEN-RECEIVED</code> means."],
    ["<b>Names longer than 40 letters</b>", "14 rows", "The SAP field holds 40 characters. Longer names would be cut off or refused."],
    ["<b>The same company twice</b> (same tax number)", "about 100 rows", "SAP should hold each customer once."],
    ["Blank e-mail / phone / tax number", "440 / 373 / 537", "Not an error: some customers simply have none."],
])}
"""))

# ============================================================================================ 6. step 1 import
S.append(section("s1", "1", "Bring the data in", "Make a locked copy of the old data. The original is never touched.", f"""
{chart("landing", "What \"bringing it in\" does. The copy is the only thing the rest of the application uses.")}
{think("<p>Before a builder changes your house, they photocopy the plans. If anything goes wrong, the original plans are safe. The <b>snapshot</b> is that photocopy.</p>")}
<h3>Press <i>New Source</i></h3>
{steps("In the toolbar press <b>New Source</b>. A <b>wizard</b> opens: a little guide with five pages that you click through with <b>Next</b>.",
       "<b>Give the source a name</b> (we use <code>LEGACY_ERP</code>). It is the name of the old <i>system</i>; later one system can hold many tables. Leave the type on \"Extract file\" and the provider on <i>Auto-detect</i>.")}
{img("03_wizard_provider.png", "<b>Page 1 of the wizard: name the source.</b> On the right, the <b>Setup guide</b> explains each page in plain words and can answer questions.")}
{steps("Press <b>Next</b>, then <b>Choose File</b> and pick <code>legacy_ar_customers.csv</code>. Press <b>Test</b>.")}
{img("04_wizard_file.png", "<b>Page 2: choose the file.</b> The application worked out by itself that this is a delimited file, encoded utf-8, separated by commas, with 10 columns.")}
{see("<p>A green line: <i>Test succeeded: format delimited, encoding utf-8, delimiter , - 10 columns</i>. If it says everything landed in one column, or the accents look wrong, change the <b>Delimiter</b> or <b>Encoding</b> and press <b>Test</b> again.</p>")}
{steps("Press <b>Next</b>. Now <b>describe the table</b>: type a name (<code>AR_CUSTOMERS</code>), tick the <b>key</b> column (<code>CUST_ID</code>) and type the <b>control total</b> you expect (<code>1980</code>).")}
{img("05_wizard_table.png", "<b>Page 3: name the table, tick the key, type the control total.</b>")}
{why("<p>The key lets every later problem say <i>\"customer 100451 has a name that is too long\"</i> instead of <i>\"a name somewhere\"</i>. The control total protects you from a half-finished export: if your colleague sent 1,500 rows instead of 1,980, the application stops you right here.</p>")}
{steps("Press <b>Next</b>. Page 4 asks what to do at the end. <b>Tick \"Profile the table now\"</b> and leave the others empty.")}
{img("06_wizard_actions.png", "<b>Page 4: actions.</b> Profiling is ticked, so the check-up starts by itself.")}
{steps("Press <b>Next</b> to read the summary, then <b>Finish</b>.")}
{img("07_wizard_summary.png", "<b>Page 5: the summary</b> of what is about to happen.")}
{see("<p>The wizard closes, the rows are read and counted, the snapshot is made, and a new tab, <b>Profiler</b>, opens. A message says <i>Source LEGACY_ERP / AR_CUSTOMERS registered: 1,980 rows, 10 columns</i>.</p>")}
""", page_break=True))

# ============================================================================================ 7. step 2 look
S.append(section("s2", "2", "Look at the data", "Two check-ups: the <i>profile</i> (what is in it?) and the <i>quality check</i> (what is wrong with it?).", f"""
<h3>The profile</h3>
<p>The Profiler page opens by itself. It is a medical check-up for the table.</p>
{img("08_profile.png", "<b>The Profiler.</b> At the top: 1,980 rows, 10 columns, an average <i>health</i> of 89.3 out of 100, 0 completely identical rows, 3 columns that could serve as a key. Below: one line per column.")}
{table(["Column of the report", "What it tells you"], [
    ["<b>Type</b>", "What kind of thing is in the column: whole numbers (BIGINT), text (VARCHAR) …"],
    ["<b>Null ratio</b>", "The share of <b>blank</b> cells. <code>TAX_ID</code> is 27.1% blank, <code>CITY</code> 3.1%, <code>CUST_ID</code> 0%."],
    ["<b>Distinct</b>", "How many <i>different</i> values. <code>CUST_ID</code> is 100% distinct (every customer has their own number, as a key should). <code>COUNTRY</code> has 22 different spellings."],
    ["<b>Length</b>", "The shortest and longest value. <code>CUST_NAME</code> runs from 14 to <b>70</b> characters: SAP only takes 40."],
    ["<b>Top values</b>", "The most common values and how often. Country: <code>USA</code> 188 times, <code>usa</code> 183 times, <code>US</code> 180 times: the same country, three spellings."],
    ["<b>Sample (masked)</b>", "A few examples, with letters shown as <code>A</code>/<code>a</code> and digits as <code>9</code>, so personal data is not shown to everyone."],
    ["<b>Health</b>", "A score from 0 to 100. Green is good; amber (like <code>TAX_ID</code>, 73) is worth a look."],
])}
{img("10_profile_duplicates.png", "<b>Duplicates and keys.</b> <code>CUST_ID</code> is unique, so it can be the key. The same <i>name</i> occurs 60 times (60 surplus rows), the same <i>street</i> 40 times: a first hint of duplicate customers.")}
<h3>The quality check</h3>
<p>The profile measures. The <b>quality check</b> <i>judges</i>: it runs a library of tests ("is this a real country?", "is this tax number used by several customers?") and lists what it found, worst first.</p>
{steps("Select the table in the Explorer and press <b>Quality</b> in the toolbar.", "Press <b>Assess data quality now</b>.")}
{img("12_quality_result.png", "<b>The quality result.</b> 110 tests were run. Score 99.4. 8 findings, 2 of them HIGH.")}
{img("13_quality_findings.png", "<b>The findings table.</b> Each line says which test failed, on which column, how many rows, an example, and <b>what to do</b>.")}
{table(["Finding", "In plain words"], [
    ["<span class='pill red'>HIGH</span> T03 Tax number used by several partners", "120 customers share a tax number with another customer. Duplicates, branches, or a typing mistake: somebody has to decide."],
    ["<span class='pill red'>HIGH</span> C01 Not a country", "44 rows say Wakanda, Narnia or Atlantis."],
    ["<span class='pill amber'>MEDIUM</span> E03 E-mail shared by several partners", "A shared mailbox (info@…) is fine; one person's address on several customers usually is not."],
    ["<span class='pill green'>LOW</span> C02 Country written as a name, not a code", "SAP wants <code>US</code>, not <code>United States</code>: it needs a lookup table."],
    ["<span class='pill green'>LOW</span> G01 / G08 / N05 …", "Extra spaces, double spaces, names written in capitals only."],
])}
{careful("<p><b>A score of 99.4 does not mean \"all fine\".</b> The score is an average over many tests, so a few rows with a big problem hardly move it. Always read the <i>findings</i>, not just the score.</p>")}
"""))

# ============================================================================================ 8. step 3 mapping
S.append(section("s3", "3", "Write the rules (the mapping)", "Tell the application which old column becomes which new field, and how to change it on the way.", f"""
{think("<p>You are packing for a new house. The <b>mapping</b> is your packing list: <i>\"things from the old kitchen drawer go into the new kitchen drawer; the old box labelled 'books' goes into the new study.\"</i> Each line of that list is a <b>rule</b>.</p>")}
<h3>What the new house looks like</h3>
<p>SAP keeps one customer in <b>several drawers</b> (tables), each holding one kind of fact:</p>
{table(["SAP table", "Holds", "Example field"], [
    ["<code>BUT000</code>", "The customer in general", "<code>NAME_ORG1</code> (the name), <code>BU_GROUP</code> (the customer group)"],
    ["<code>ADRC</code>", "The address", "<code>STREET</code>, <code>CITY1</code>, <code>POST_CODE1</code>, <code>COUNTRY</code>"],
    ["<code>ADR6</code>", "E-mail", "<code>SMTP_ADDR</code>"],
    ["<code>ADR2</code>", "Telephone", "<code>TEL_NUMBER</code>"],
    ["<code>KNB1</code>", "Data for the accounting department", "<code>ZTERM</code> (payment terms)"],
])}
<p>A field name is written <code>TABLE.FIELD</code>, for example <code>ADRC.CITY1</code>. A star (<code>*</code>) after it, as in <code>BUT000.NAME_ORG1 *</code>, means SAP <b>insists</b> on it (mandatory).</p>
<h3>The four kinds of rule</h3>
{chart("rule_types", "How to choose the kind of rule: just ask the questions from the top.")}
{table(["Kind", "Meaning", "Example from our file"], [
    ["<code>DIRECT_COPY</code>", "Copy the column as it is", "<code>CUST_NAME</code> → <code>BUT000.NAME_ORG1</code>"],
    ["<code>STATIC_VALUE</code>", "The same fixed value for every customer", "<code>'Z001'</code> → <code>BUT000.BU_GROUP</code> (everyone is in customer group Z001)"],
    ["<code>VALUE_MAPPING</code>", "Translate through a lookup dictionary", "<code>usa, USA, United States → US</code> into <code>ADRC.COUNTRY</code>"],
    ["<code>SQL_EXPRESSION</code>", "A small formula", "<code>upper(left(trim(CUST_NAME), 20))</code> → <code>BU_SORT1</code> (the search key: first 20 letters, capitals, no spaces at the ends)"],
])}
<h3>The Mapping Studio</h3>
<p>Select the table and press <b>Mapping</b>. This is the workshop where rules are made.</p>
{img("15_studio_buttons.png", "<b>The Mapping Studio, tour of the buttons.</b> 1 Generate AI mapping · 2 New empty mapping set · 3 Add manual rule · 4 Check rules on sample · 5 Submit for approval · 6 Approve · 7 Run pipeline. Left: the legacy columns. Right: the SAP fields and their rules.")}
<h3>Way 1: let the AI propose</h3>
<p>Press <b>Generate AI mapping</b>. An AI model reads the column names and a few example values and suggests, for each column, the SAP field it fits best, <b>with a percentage of confidence and its reason</b>.</p>
{img("16_ai_running.png", "<b>The AI at work.</b> It examines one column at a time and shows a progress bar; you can leave the page, and the job keeps running on the server.")}
{img("17_ai_result.png", "<b>The AI's proposal.</b> Every rule carries a confidence (61%, 97%, 78% …), the tag <i>AI-suggested</i> and the name of the model.")}
{img("18_ai_rules.png", "<b>The reasons.</b> Each suggestion explains itself in words, so a person can judge it instead of trusting it blindly.")}
{box("careful", "How well did the AI do? An honest answer", "<ul><li>In this guide the AI was a <b>small model running on the same computer</b>, so nothing left it. It took <b>about 40 minutes</b> for 10 columns. An online AI service is usually much faster, if your company allows it.</li><li>It chose a target field for all 10 columns and got <b>9 of 10 right</b>. The miss: <code>COUNTRY</code> was sent to <code>ADRC.BANKS</code> (the country <i>of a bank</i>) instead of <code>ADRC.COUNTRY</code>, with 70% confidence, the same as two correct rules and higher than the correct 61% for the customer number. The percentage did <b>not</b> separate right from wrong. What caught it was a separate check that only 2 of 5 sample values were valid country codes, which marked the rule <i>needs review</i>.</li><li>It proposed <b>10 rules, all plain copies</b>. It did <b>not</b> invent the country dictionary, the payment-term codes, or the fixed values SAP needs (customer group, type). Those depend on <i>your business</i>, and no AI can guess them.</li><li>Colours: green = confident (over 85%), amber = needs review (60–85%), red = blocked (under 60%). A rule the AI is unsure about is <b>never</b> approved automatically.</li></ul><p><b>So: the AI saves typing, not thinking.</b> Treat it like a keen helper who is new to your company.</p>")}
<h3>Way 2: write the rules yourself</h3>
<p>We will write our own rules, because we know the business. Press <b>New empty mapping set</b>: a blank page for a new version of the packing list.</p>
{img("19_new_set.png", "<b>A new, empty mapping set.</b> Its status is <b>DRAFT</b>: it can still be changed. The panels below stay empty until rules exist.")}
{steps("Press <b>Add manual rule</b>.", "Fill the little form: the SAP <b>table</b>, the SAP <b>field</b>, the <b>kind</b> of rule, the legacy <b>column</b>, and the <b>formula or value</b> if the kind needs one.", "Press <b>Add rule</b>. Repeat for every field.")}
{img("20_add_rule_form.png", "<b>Adding a rule:</b> here, the fixed value <code>'Z001'</code> for <code>BUT000.BU_GROUP</code>. Fixed text goes in quotes.")}
<p>Our twelve rules, in the order we typed them:</p>
{table(["SAP field", "Kind", "From", "Note"], [
    ["BUT000.NAME_ORG1", "DIRECT_COPY", "CUST_NAME", "the name"], ["BUT000.BP_EXT", "DIRECT_COPY", "CUST_ID", "keeps the old customer number"], ["BUT000.TYPE", "STATIC_VALUE", "'2'", "2 = organisation"],
    ["BUT000.BU_GROUP", "STATIC_VALUE", "'Z001'", "customer group"], ["BUT000.BU_SORT1", "SQL_EXPRESSION", "upper(left(trim(CUST_NAME), 20))", "search key"],
    ["ADRC.STREET", "DIRECT_COPY", "ADDR_STREET", ""], ["ADRC.CITY1", "DIRECT_COPY", "CITY", ""], ["ADRC.POST_CODE1", "DIRECT_COPY", "POSTAL_CODE", ""],
    ["ADRC.COUNTRY", "VALUE_MAPPING", "COUNTRY", "needs a dictionary"], ["ADR6.SMTP_ADDR", "DIRECT_COPY", "EMAIL", ""], ["ADR2.TEL_NUMBER", "DIRECT_COPY", "PHONE", ""],
    ["KNB1.ZTERM", "VALUE_MAPPING", "PAYMENT_TERMS", "needs a dictionary"],
])}
{img("21_rules_list.png", "<b>The rules, listed.</b> Each card shows the legacy column, the SAP field and its length (<code>CHAR(40)</code>), the kind of rule and a <b>sample result</b> such as <i>sample: 99.3% pass</i>.")}
<h3>Teach the dictionaries</h3>
<p>The two <code>VALUE_MAPPING</code> rules start empty ("0 lookup values"). Press <b>Edit</b> on the rule, type the dictionary in the <b>Lookups</b> box as <code>{{"old value": "new value"}}</code>, tick <b>Confirm as reviewed</b> (you are promising you read it), and press <b>Save rule</b>.</p>
{img("22_lookup_editor.png", "<b>Editing a rule's dictionary.</b> Here part of the country dictionary: <code>Great Britain → GB</code>, <code>UK → GB</code> …")}
{tryit("<p><b>Capital letters do not matter in a lookup.</b> You only need <code>USA → US</code>; <code>usa</code> and <code>Usa</code> are found too. That is why our country dictionary has fewer lines than the file has spellings.</p>")}
<h3>Check the rules before anybody is asked to approve them</h3>
<p>Press <b>Check rules on sample</b>. The application <b>tries the rules on the real rows</b> (a dry run, nothing is saved) and reports.</p>
{img("23_preflight.png", "<b>The pre-submission check.</b> Green <i>can be submitted</i>; one amber warning. The dry run covered 1,980 of 1,980 rows and <b>90% passed every rule</b>. The box <i>What to do next</i> lists the next three steps.")}
{see("<p>The amber warning says <i>6 mandatory SAP fields have no rule and will be empty</i>. A warning does not stop you. We will deal with it in step 7. The remaining 10% that did not pass is exactly what the gate will show us in a moment.</p>")}
"""))

# ============================================================================================ 9. step 4 approval
S.append(section("s4", "4", "A second person approves", "Nobody marks their own homework.", f"""
{chart("roles_and_gate", "Who may do what. The author writes and submits; a different person approves; the gate and the sign-off come later.")}
<p>Press <b>Submit for approval</b>. The status changes from DRAFT to <b>SUBMITTED</b>, and the rules can no longer be edited by accident.</p>
{img("24_submitted.png", "<b>Submitted.</b> The set waits for somebody else. The <b>Approve</b> button is now available, but not to the person who made the rules.")}
{steps("Change <b>Acting as</b> (top right) to the name of a <b>different person</b>. In real life that person would sign in with their own account.", "Read the rules. Press <b>Approve</b>.")}
{img("25_approved.png", "<b>Approved, by <code>guide.steward</code></b>, and recorded. The rules are now locked. Pressing Approve as the same person who wrote the rules is refused: that is called <b>segregation of duties</b>.")}
{why("<p>If one person could write the rules, approve them and deliver them, one mistake (or one dishonest person) could damage the company's customer list with nobody noticing. Two pairs of eyes is the oldest safety rule there is.</p>")}
"""))

# ============================================================================================ 10. step 5 run
S.append(section("s5", "5", "Run it", "The moving van drives. Every customer goes through the rules and the checks.", f"""
{chart("run_pipeline", "What happens to every row. At the end every row is accounted for in exactly one place.")}
<p>Press <b>Run pipeline</b>. Behind the scenes, in order:</p>
<ol>
<li><b>Leave out</b> the rows you told the application to skip (we have not yet).</li>
<li><b>Apply your rules</b> to every row: copy, calculate, translate.</li>
<li><b>Find twins</b>: customers that are the same company entered twice are merged into one.</li>
<li><b>Check against SAP's rules</b>: is every mandatory field filled, is every value short enough, is every code a real code?</li>
</ol>
<p>The run takes seconds for a file this size (minutes for a million rows). When it ends, a new tab, the <b>Governance</b> page, opens by itself.</p>
{careful("<p>The count <b>always</b> adds up: <i>left out + merged + rejected + valid = every row you started with</i>. A row can never quietly disappear. You will see that sum again on the certificate.</p>")}
"""))

# ============================================================================================ 11. step 6 gate
S.append(section("s6", "6", "The gate says STOP", "The first run is blocked. That is not a failure: finding problems is the point.", f"""
{img("27_gate_blocked.png", "<b>The Governance page.</b> The red banner says <b>BLOCKED</b>: 4 critical defects are unresolved.")}
<p>Scroll down to <b>Rule-aggregated defects</b>. A defect here is a <b>group</b>: all the rows that broke the same rule are counted together, with a few example customer numbers and the <b>root cause</b> in plain words.</p>
{img("28_defects.png", "<b>The defects of the first run.</b> Quality score 89.26%. Four CRITICAL groups and one HIGH.")}
{table(["Severity", "Defect", "Rows", "What it really means", "What to do"], [
    ["<span class='pill red'>CRITICAL</span>", "<code>ERR_MANDATORY_CITY1</code>", "62", "SAP insists on a city; these customers have none.", "Leave these rows out (or find the city in another source)."],
    ["<span class='pill red'>CRITICAL</span>", "<code>ERR_LOOKUP_FAILED_ZTERM</code>", "48", "The payment term is <code>WHEN-RECEIVED</code>, which our dictionary does not know.", "Decide what it means and add it to the dictionary."],
    ["<span class='pill red'>CRITICAL</span>", "<code>ERR_LOOKUP_FAILED_COUNTRY</code>", "44", "Wakanda, Narnia and Atlantis are not in the country dictionary.", "They are not countries: leave these rows out."],
    ["<span class='pill red'>CRITICAL</span>", "<code>ERR_MANDATORY_COUNTRY</code>", "42", "SAP insists on a country; these rows have none.", "Leave these rows out."],
    ["<span class='pill amber'>HIGH</span>", "<code>ERR_LENGTH_NAME_ORG1</code>", "14", "The name is longer than the 40 letters SAP stores.", "Shorten the name in the rule."],
])}
{think("<p>The gate is like the inspector who walks into the new house with the delivery list and says <i>\"Fourteen boxes are too big for the door, forty-eight have a label I cannot read, and a hundred and some have no address at all. I am not letting this in yet.\"</i> Better to hear it here than after the boxes are inside.</p>")}
{tryit("<p>The page has an <b>Ask about this run</b> box. You can type, or press one of the suggestions, such as <i>Why were records rejected?</i> or <i>Which records were left out, and why?</i>. The answer is built from the real numbers, and every figure in it is checked against the data; if a figure cannot be checked, plain facts are shown instead. Customer records are never sent to the AI.</p>")}
"""))

# ============================================================================================ 12. step 7 fix
S.append(section("s7", "7", "Fix it: make version 2", "An approved set is locked, so a fix goes into a <b>new editable copy</b>. Then the same road again: approve, run, gate.", f"""
<p>The rules were approved, so they are <b>locked</b>. You cannot edit a locked set; you make a <b>new version</b>, a copy that you may change. The easiest way is a <b>Fix it</b> button.</p>
<h3>7a. Press a Fix-it button (and say yes)</h3>
<p>Back in the Mapping Studio, press <b>Check rules on sample</b> again. Under the check appear cards headed <b>Needs your approval</b>. Each card says <b>the situation</b>, <b>what the application proposes</b>, and how to <b>undo</b> it. Nothing happens until you say yes.</p>
{img("29_fix_card.png", "<b>A Fix-it card.</b> SAP requires the company code <code>KNB1.BUKRS</code> and nothing fills it. The card asks <i>you</i> for the value: it will not guess. The little arrow opens \"Exactly what will change, what will not, and how to undo it\".")}
<p>We typed <code>1000</code> (an example company code; in real life your SAP team tells you the right one) and pressed <b>Yes, go ahead</b>.</p>
{img("30_fix_done.png", "<b>A new draft was made.</b> The message says <i>Made draft #2 (a copy of #1) and applied the fix there</i>. The old, approved set is untouched: it still describes exactly what run 1 did.")}
<h3>7b. Leave out rows that cannot be migrated</h3>
<p>Open <b>Rows to leave out</b>. Choose a column, a condition and a reason, press <b>Check how many rows</b>, then <b>Leave these rows out</b>. The rows are <b>not deleted</b>: they are kept in a separate file with the reason, and counted on the certificate.</p>
{img("31_exclusion_form.png", "<b>One exclusion rule:</b> <i>CITY is blank</i>, reason \"No city: SAP needs one to post a letter\". The application first says how many rows match (62).")}
{img("32_exclusions_done.png", "<b>Three rules, 147 rows left out of 1,980.</b> City blank (62), country blank (41 more, since some were already out), and the three countries that do not exist (44).")}
{careful("<p>Leaving out 147 customers (7%) is a <b>business decision</b>, not a technical one. Somebody should ask: <i>do we call those customers, or fix the data in the old system?</i> The application makes the decision <b>visible and reversible</b>; it does not make it for you.</p>")}
<h3>7c. Teach the dictionary a new word, and shorten the long names</h3>
<p>We decided that <code>WHEN-RECEIVED</code> means <code>CASH</code> and added it to the payment-term dictionary (<b>Edit</b> the rule, add the pair, <b>Save rule</b>). Then we changed the name rule from a plain copy to a formula that keeps only the first 40 letters.</p>
{img("33_name_rule_edit.png", "<b>Changing a rule's kind.</b> <code>NAME_ORG1</code> is now a <code>SQL_EXPRESSION</code>: <code>left(trim(CUST_NAME), 40)</code>.")}
{careful("<p>Cutting a name at 40 letters can cut a word in half. It is the fastest fix, not the prettiest. In a real project somebody would look at the 14 names and shorten them properly.</p>")}
<h3>7d. Check again, approve again, run again</h3>
{img("34_preflight_v2.png", "<b>The check on version 2.</b> The dry run covers 1,833 rows (the 147 are out) and <b>100% pass every rule</b>.")}
<p>Then the same road as before: <b>Submit</b> (as the author), <b>Approve</b> (as a different person), <b>Run pipeline</b>.</p>
{img("35_gate_v2.png", "<b>The gate is open.</b> <b>READY FOR LOAD</b> - mapping approved, run succeeded, validated: yes, open critical defects: 0.")}
{img("36_defects_v2.png", "<b>The defects table is empty</b> and the quality score is 100%. (It still says \"Nothing to show\", which is exactly what we wanted.)")}
"""))

# ============================================================================================ 13. step 8 duplicates
S.append(section("s8", "8", "Twins: finding duplicates", "The same company entered twice should arrive once.", f"""
<p>During the run the application looked for <b>twins</b>: two rows that are really the same customer. It does this in two ways, and it is deliberately <b>careful</b>, because merging two <i>different</i> companies is much worse than keeping one company twice.</p>
{chart("duplicates", "How two rows are judged. The safety checks can only say NO, never YES.")}
{table(["Kind of match", "Example", "What happens"], [
    ["<b>Exact</b>: same tax number, e-mail or phone", "Two rows with the tax number <code>AA999999999</code>", "Merged: one survivor, the other kept in a list."],
    ["<b>Fuzzy</b>: names and addresses almost the same", "\"Müller GmbH, Hauptstr. 5\" and \"Mueller GmbH, Hauptstrasse 5\"", "A similarity score from 0 to 100. High and safe: merged. In between: sent to a person."],
    ["<b>Vetoed</b>", "Same name but two <i>different</i> tax numbers", "Never merged."],
])}
<p>Accents and German spellings (<code>ü / ue</code>, <code>ß / ss</code>) are ignored when comparing, a very common name such as \"Müller\" is never merged on the name alone, and an address that is clearly different sends the pair to a person.</p>
<h3>What happened in our run</h3>
<p>The certificate (next but one chapter) shows <b>DUPLICATE_CHILD: 100</b>: 100 rows were recognised as the same company as another row (for example, the same tax number) and merged, keeping 1,733 customers. The first row is the <b>survivor</b>, loaded into SAP; the others are listed, with their customer numbers, as <i>intentionally not loaded</i>.</p>
{img("37_duplicates.png", "<b>The Duplicates page.</b> It lists only the <i>uncertain</i> cases (\"possible duplicates\"), merges made on a name alone, and the decisions people took. In this run everything was certain, so the lists are empty.")}
{tryit("<p>When there <i>are</i> uncertain pairs, you decide for each: <b>same customer</b> or <b>different</b>. Your decision is remembered and used in every later run, with your name and your reason.</p>")}
"""))

# ============================================================================================ 14. step 9 sign-off
S.append(section("s9", "9", "Sign off", "A named person says: \"I have read the proof. I accept this run.\"", f"""
<p>With the gate green, the toolbar of the Governance page holds the final buttons.</p>
{img("40_gate_buttons.png", "<b>The final buttons.</b> 1 The gate banner (green) · 2 <b>Steward sign-off for load</b> · 3 <b>Load as …</b> (choose where it goes) · 4 <b>Execute load</b> · 5 <b>Acting as</b>: the person who signs off must be allowed to, and must not be the author of the rules.")}
{steps("Make sure <b>Acting as</b> shows the steward.", "Press <b>Steward sign-off for load</b>.")}
{img("41_signed_off.png", "<b>Signed off.</b> The message names the person; the page records who and when.")}
{why("<p>Approval says \"the <i>rules</i> are fine\". Sign-off says \"the <i>result</i> of running them is fine, and I take responsibility for putting it in the live system\". They are two different questions, and two different moments.</p>")}
"""))

# ============================================================================================ 15. step 10 deliver
S.append(section("s10", "10", "Deliver it", "Put the customers into the new system, and then check, row by row, that they really arrived.", f"""
<p>There are <b>three ways</b> out, chosen in the <b>Load as</b> list (and a fourth, a database, in its own panel):</p>
{table(["Way", "What happens", "When to use it"], [
    ["<b>Migration Cockpit workbook</b>", "The application builds an <b>Excel file</b> in the exact format of SAP's own import tool (the <i>Migration Cockpit</i>). A person uploads it into SAP.", "The classic route when the SAP team prefers to load themselves."],
    ["<b>SAP sandbox (simulated)</b>", "The application sends every customer to a <b>practice SAP stand-in</b> and reports how many were accepted.", "Rehearsals. This is what the guide used."],
    ["<b>SAP live</b>", "The application sends every customer to a <b>real SAP</b> over its web interface (OData).", "The real thing. It needs an SAP address and a technical user; it was <b>not</b> exercised in this guide."],
    ["<b>A database target</b> (PostgreSQL, SQL Server, Oracle, MySQL) or <b>Google Firebase</b>", "The finished customers are written into a <b>new table</b> (Firebase: a new collection).", "When the new home is not SAP, or to look at the result with ordinary tools."],
])}
{chart("delivery", "A delivery: preview, write in small batches, count and compare afterwards, and undo if anything is wrong.")}
<h3>10a. Say where it goes: New Target</h3>
<p>For a database, first describe it once. Press <b>New Target</b> and choose <b>Database</b>.</p>
{img("42_target_kind.png", "<b>New Target, page 1:</b> choose the kind of target and give it a name.")}
{img("43_target_connection.png", "<b>Page 2: connection.</b> Server, port, database, user and password. The password is typed into a masked box and kept <b>only in the application's memory</b>; it is never written to disk. Press <b>Test</b>: the wizard really connects.")}
{img("44_target_write_test.png", "<b>Page 3: prove the account may write.</b> Choose the schema (the folder inside the database) and press the test: the wizard writes one test row, reads it back and deletes it. If that fails, delivery is refused.")}
{img("45_target_summary.png", "<b>Page 5: summary,</b> then <b>Finish</b>. The target now appears under <b>Targets</b> in the Explorer.")}
<h3>10b. Preview, then deliver</h3>
{steps("In the <b>Deliver to a database target</b> panel, choose the target and type a <b>new table name</b> (we used <code>BP_CUSTOMERS</code>).", "Press <b>Show table &amp; SQL</b> to see exactly what will be created.", "Press <b>Deliver</b>.")}
{img("46_delivery_preview.png", "<b>The preview.</b> 1,733 rows will go into <code>public.bp_customers</code>. The table's columns, with the exact lengths, are shown, including two bookkeeping columns: <code>legacy_key</code> (the old customer number) and <code>mig_run_id</code> (which run wrote the row).")}
{img("47_delivered.png", "<b>Delivered.</b> 1,733 rows sent, 1,733 counted in the target (<i>matches</i>), and <b>25,995 cells checked</b>: after writing, every value was read back and compared with what was sent.")}
{img("48_verified.png", "<b>Verified again.</b> Pressing <b>Verify now</b> re-reads the table and compares it with the approved result. Somebody who changed a value afterwards would be caught here (<i>still identical</i> turns red).")}
{careful("<p><b>Create-only.</b> The application never overwrites or merges into data that is already there. If a table of that name exists, it stops and tells you; choose a new name, or roll back the earlier delivery first.</p>")}
<h3>10c. Rehearse the SAP load</h3>
<p>Choose <b>SAP sandbox (simulated)</b> in <b>Load as</b> and press <b>Execute load</b>.</p>
{img("49_sap_sandbox_load.png", "<b>A simulated SAP load.</b> The message: <i>1733 loaded, 0 failed</i>. Each customer received a business-partner number from the stand-in.")}
{box("careful", "What was and was not tested", "<p>The SAP side is a stand-in that follows SAP's published interface. It proves that the application builds the right messages and handles success and failure. It does <b>not</b> prove that a particular company's real SAP will accept them. Before a real go-live, run the same steps against that company's SAP test system (client).</p>")}
"""))

# ============================================================================================ 16. step 11 certificate
S.append(section("s11", "11", "The certificate: the receipt", "Every customer, accounted for.", f"""
<p>Press <b>Certificate</b> in the toolbar (or the button on the Governance page).</p>
{img("50_certificate.png", "<b>The reconciliation certificate.</b> Green banner: <b>RECONCILIATION PASSED</b> - variance 0 rows. Top: extracted 1,980 · transformed OK 1,833 · duplicate children 100 · rejected 0 · valid 1,733 · loaded 1,733 · failed 0.")}
<p>The heart of it is the <b>drop-off accounting</b>: where did every one of the 1,980 customers go?</p>
{table(["Reason", "Rows", "Plain words"], [
    ["LOADED", "1,733", "Created in the new system."],
    ["EXCLUDED_BY_RULE", "147", "Left out on purpose, with the exact reasons (no city 62, no country 41, not real countries 44)."],
    ["DUPLICATE_CHILD", "100", "The same company as another row; merged into the survivor."],
    ["VALIDATION_REJECTED", "0", "Failed SAP's checks. (None: the gate made sure.)"],
    ["SAP_LOAD_FAILED", "0", "Sent but refused by the system. (None.)"],
    ["<b>Sum</b>", "<b>1,980</b>", "<b>1,733 + 147 + 100 = 1,980. Every row accounted for.</b>"],
])}
{img("51_certificate_more.png", "<b>Control checks, one line each, all PASS:</b> counts agree between the run and the audit trail; the author and the approver were different people; the extract matches the snapshot; the source's control total was verified; <b>1,733 rows counted in the database after the load</b>; <b>25,995 cells read back and equal</b>, with the fingerprint; verified again later. At the bottom: a sample of customers traced from source to SAP number, and who approved the mapping and when.")}
{why("<p>When an auditor, a boss or a customer asks \"how do you know nothing was lost?\", you do not say \"trust me\". You hand them this page. It can also be downloaded as an HTML page or a Markdown file.</p>")}
{careful("<p>The banner reads <b>PASSED</b> only when the load really happened. If you finish a run and have not yet loaded anywhere (not into SAP, and not into the SAP stand-in), the certificate honestly says <i>not passed</i> for that one reason (<code>load_phase_reached</code>). Loading into a plain database also counts as a delivery line, but the SAP load phase is a separate check.</p>")}
"""))

# ============================================================================================ 17. undo
S.append(section("s12", "12", "Oops: taking a delivery back", "Exactly what one delivery wrote, and nothing else.", f"""
<p>Every row the application writes carries the number of the run that wrote it (<code>mig_run_id</code>). That is what makes a precise undo possible.</p>
{steps("On the Governance page, find the delivery in the table.", "Press <b>Roll back</b>.", "Type the <b>reason</b> (it is kept in the record) and confirm.")}
{img("58_rolled_back.png", "<b>Rolled back.</b> The message: <i>1,733 row(s) removed from public.bp_customers; the table was empty, so it was dropped</i>. The row in the table now reads <i>rolled back by guide.steward</i> with the reason.")}
<p>Rows that belong to <b>other</b> runs, or that someone else added, are not touched. If the table is empty afterwards, the table itself is removed.</p>
"""))

# ============================================================================================ 18. extras
S.append(section("extras", "", "The other buttons", "Things you do not need for your first move, but will want soon.", f"""
{img("53_dashboard.png", "<b>Dashboard.</b> The programme at a glance: tables, mapping sets, runs, and for each <i>wave</i> the latest run, its gate, quality score, valid records and the trend (89.26 → 100). \"Compare two runs\" shows what changed between them.")}
{img("54_tasks.png", "<b>Tasks.</b> Who fixes what, by when. One task can be made for every open defect group, with an owner chosen from the SAP table involved (for example <code>ADRC=address.owner</code>). A task closes itself when a later run no longer has the defect.")}
{img("55_compliance.png", "<b>Compliance.</b> Each control the application relies on, checked now against this installation, with the standard it serves. It is evidence, not a certificate: the human controls (who holds the keys, the legal basis) stay with the people who run it.")}
{table(["Button", "What it is for"], [
    ["<b>Sources</b>", "A list of every table you brought in."],
    ["<b>Combine</b>", "Join and filter tables you have already brought in into one new source table, before mapping."],
    ["<b>Packs</b>", "Save an approved mapping (its rules, lookup dictionaries and leave-out rules, never any customer values) and reuse it in the next migration. An import makes a <i>draft</i>; somebody independent still approves it."],
    ["<b>Keys</b>", "Add or remove the keys for the online AI service (Gemini). They are kept in memory only; the application never writes them to disk."],
    ["<b>Help</b>", "The Start Page with the progress checklist and the Guide helper."],
    ["<b>Setup guide</b> (in every wizard)", "A chat panel on the right that explains each page in plain words and can fill in a field if you tell it the server, database or user."],
])}
"""))

# ============================================================================================ 19. cheat sheet
S.append(section("cheat", "", "One-page cheat sheet", "Print this page and stick it next to your screen.", f"""
{table(["#", "Do this", "Button", "You should see"], [
    ["1", "Bring the file in", "New Source → wizard → Finish", "Profiler tab with the right row count"],
    ["2", "Look at it", "Profiler, then Quality → Assess", "Health numbers; findings, worst first"],
    ["3", "Write the rules", "Mapping → Generate AI mapping <i>or</i> New empty mapping set + Add manual rule", "A card for every SAP field, with a sample pass rate"],
    ["4", "Check them", "Check rules on sample", "\"can be submitted\" and a pass rate"],
    ["5", "Submit", "Submit for approval", "Status SUBMITTED"],
    ["6", "Approve <b>(someone else)</b>", "Acting as → other name → Approve", "Status APPROVED"],
    ["7", "Run", "Run pipeline", "Governance tab opens"],
    ["8", "Read the gate", "Governance", "BLOCKED + defects <i>or</i> READY FOR LOAD"],
    ["9", "Fix", "Check rules on sample → Fix-it card; Rows to leave out; Edit rule", "A new DRAFT; repeat 4–8"],
    ["10", "Sign off <b>(steward)</b>", "Steward sign-off for load", "\"signed off by …\""],
    ["11", "Deliver", "New Target (once) → Deliver, or Load as → Execute load", "rows sent = rows counted; \"cells checked\""],
    ["12", "Prove", "Certificate", "RECONCILIATION PASSED, 0 variance"],
    ["–", "Undo", "Roll back", "\"rolled back by …\""],
])}
<div class="two">
{box("see", "Three golden rules", "<ol><li>Pick first, then press.</li><li>Never approve your own work.</li><li>Read the findings, not just the score.</li></ol>")}
{box("careful", "Three things the application will never do", "<ol><li>Change your original data.</li><li>Overwrite what is already in the target.</li><li>Let a row vanish without saying why.</li></ol>")}
</div>
""", page_break=True))

# ============================================================================================ 20. troubleshooting
S.append(section("trouble", "", "When something goes wrong", "Look for your problem in the left column.", f"""
{table(["What you see", "Probably because", "What to do"], [
    ["A toolbar button is <b>greyed out</b>", "You have not picked a table (or a run) in the Explorer.", "<b>Click</b> the table (or the run) in the Explorer first."],
    ["After <i>Test</i> everything is in <b>one column</b>, or accents look odd", "The delimiter or the encoding was detected wrongly.", "Change <b>Delimiter</b> or <b>Encoding</b> in the wizard and press Test again."],
    ["The wizard says the <b>row count differs</b> from the control total", "The file is incomplete, or the number you typed is wrong.", "Ask for a fresh export, or correct the control total."],
    ["<b>Approve</b> is refused", "You are the person who made the rules.", "Change <b>Acting as</b> to somebody else."],
    ["<b>Fix-it</b> or <b>Edit</b> is refused", "The set is approved and locked.", "Use a Fix-it button (it makes a copy), or create a new mapping set."],
    ["The gate is <b>BLOCKED</b>", "A critical defect is open.", "Read the defect table: each line says the cause. Fix, make a new version, run again."],
    ["The AI is very <b>slow</b> or <b>gives nothing</b>", "A local model on a small computer is slow; or no AI is configured.", "Write the rules by hand (always possible), or add a key under <b>Keys</b> for an online AI service."],
    ["<b>Deliver</b> says the table already exists", "Delivery is create-only.", "Choose another table name, or roll back the earlier delivery."],
    ["<b>Deliver</b> is greyed", "No sign-off yet, or no target.", "Sign off first; add a target with <b>New Target</b>."],
    ["Firebase says <b>quota exceeded</b>", "The free plan allows about 20,000 writes a day.", "Wait a day, deliver fewer rows, or use a paid plan."],
    ["The certificate says <b>not passed</b>", "The SAP load has not happened (only a check is red).", "Run <b>Execute load</b> (sandbox or live), or accept that this run was a database-only delivery."],
    ["The <b>port is busy</b> when the application starts", "Another copy is already running.", "Use another port, or stop the other copy."],
])}
<h3>Questions people ask</h3>
<dl class="words">
<dt>Can I break the old data?</dt><dd>No. The application only reads it, and works on a snapshot.</dd>
<dt>Can I use my own file?</dt><dd>Yes: <b>New Source</b>, choose your file. Excel, JSON, fixed-width and Parquet files work too, as do databases (PostgreSQL, SQL Server, Oracle, MySQL, MongoDB, Firebase) and cloud storage.</dd>
<dt>Do I have to use the AI?</dt><dd>No. Everything can be done by hand. The AI proposes, a person decides.</dd>
<dt>Is my customers' data sent to the internet?</dt><dd>Not by default: the AI in this guide ran on the same computer. With an online AI service, ask your administrator what is sent. Passwords and keys are kept in memory only.</dd>
<dt>What if I change my mind after delivery?</dt><dd>Roll back (chapter 12), fix, and deliver again.</dd>
<dt>What does \"SAP sandbox (simulated)\" in the status bar mean?</dt><dd>The application is connected to the practice stand-in, not to a real SAP.</dd>
</dl>
"""))

# ============================================================================================ 21. grown-up
S.append(section("grownups", "", "For the grown-up", "How to start the application, what it needs, and what was and was not tested.", f"""
<p>The application is a <b>web application</b> (Python, FastAPI) with a catalogue in <b>PostgreSQL</b> and a working folder on disk. Everything below is done once, by whoever looks after the computer.</p>
<h3>Start it</h3>
<pre><code># 1. a PostgreSQL database for the catalogue (Docker is the quickest)
docker compose up -d postgres

# 2. Python packages
python -m venv .venv
.venv\\Scripts\\python -m pip install -r requirements.txt

# 3. tell it where things are (a file called .env in the project folder; the port is POSTGRES_PORT, 5432 unless you changed it)
DATABASE_URL=postgresql+psycopg2://migration:&lt;password&gt;@localhost:5432/migration_catalog
MIGRATION_WORKSPACE=workspace

# 4. create the tables and the SAP field catalogue
.venv\\Scripts\\python -m alembic upgrade head
.venv\\Scripts\\python -m src.seeds.seed_sap_catalog

# 5. start the application
.venv\\Scripts\\python -m uvicorn --factory src.api.main:create_app --host 127.0.0.1 --port 8000
# then open http://127.0.0.1:8000 in a browser</code></pre>
<h3>Settings that matter</h3>
{table(["Setting", "What it does"], [
    ["<code>LLM_PROVIDER</code>", "<code>ollama</code> (default: a model on this computer, <code>OLLAMA_MODEL</code>, default <code>qwen2.5-coder:7b</code>), <code>gemini</code> (online; keys are added in the <b>Keys</b> panel and kept in memory only), or <code>ollama-only</code>."],
    ["<code>MIGRATION_REQUIRE_AUTH</code>", "<code>true</code> refuses to start unless <code>MIGRATION_API_KEYS</code> is set. <b>Without keys, anybody can type any name into \"Acting as\"</b>, which is fine for a practice session and wrong for real data."],
    ["<code>MIGRATION_WORKSPACE</code>", "The folder for snapshots and run results. Back it up, and treat it as confidential: it holds copies of customer data."],
    ["<code>MIGRATION_AUTO_SEED</code>", "Seeds the SAP field catalogue automatically at start (default on)."],
    ["<code>MIGRATION_APPROVERS</code>", "Who may approve and sign off. Default: anybody except the person who made the mapping."],
    ["<code>MIGRATION_DB_ALLOWED_HOSTS</code>", "Which servers the application may connect to. Default: any, because a user types the address. <b>Set this in production.</b>"],
])}
<h3>What was tested, and what was not</h3>
{table(["Area", "Status"], [
    ["Files (CSV, Excel, JSON, fixed-width, Parquet), profiling, 179 quality rules", "<span class='pill green'>automated tests, and used in this guide</span>"],
    ["AI mapping (local model) and the safeguards around it", "<span class='pill green'>tested; used in this guide</span>. The online AI (Gemini) path is tested with a fake service, not in this guide."],
    ["Mapping rules, approval, run, de-duplication, gate, sign-off, certificate", "<span class='pill green'>automated tests, and used in this guide</span>"],
    ["Delivery into PostgreSQL, SQL Server, Oracle, MySQL, Firebase (Firestore), with rollback", "<span class='pill green'>tested against real servers (containers) and a real Firebase project</span>; PostgreSQL used in this guide"],
    ["Million-row runs", "<span class='pill green'>tested at 1,000,000 rows</span> (see <code>docs/scale-test.md</code>)"],
    ["<b>SAP: Migration Cockpit workbook, sandbox, live OData</b>", "<span class='pill amber'>tested against a <b>stand-in</b> SAP only</span>. Never against a real SAP system. Do this before a go-live."],
    ["Single sign-on, per-user accounts", "<span class='pill amber'>sign-in by API key is supported (<code>MIGRATION_API_KEYS</code>); a company single-sign-on is not built in</span>"],
])}
<h3>How these pictures were made</h3>
<p>A script, <code>scripts/make_guide_screenshots.py</code>, drives a browser through the whole practice move on a fresh copy of the application and photographs each screen; <code>scripts/guide_diagrams.py</code> draws the flow charts; <code>scripts/build_user_guide.py</code> assembles this page. So the guide can be rebuilt whenever the application changes.</p>
""", page_break=True))


def build() -> str:
    toc = "".join(f'<a href="#{sid}">{escape(title)}</a>' for sid, title, _ in S)
    body = "".join(html for _, _, html in S)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Move-House Guide: ERP Migration Accelerator</title>
<meta name="description" content="A step-by-step guide, with real screenshots and flow charts, to moving customer data from an old system into SAP with the ERP Migration Accelerator.">
<style>{CSS}</style></head>
<body><div class="layout">
<nav class="toc"><h2>Contents</h2>{toc}</nav>
<main>
<div class="cover"><h1>The Move-House Guide</h1><p>How to move a company's customer list from an old computer system into SAP: step by step, with real pictures, and a receipt at the end that proves nothing was lost.</p>
<p>Written so that a school child can follow it.</p><div class="meta">ERP Migration Accelerator · practice file: 1,980 made-up customers · every screenshot is real</div></div>
{body}
<footer>ERP Migration Accelerator user guide · built by scripts/build_user_guide.py · screenshots from a practice run on made-up data.</footer>
</main></div></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    html = build()
    (OUT / "index.html").write_text(html, encoding="utf-8")
    print(f"wrote {OUT / 'index.html'} ({len(html) // 1024} KB), {len(S)} sections")
    if missing:
        print("MISSING pictures:", ", ".join(missing))
    if args.pdf:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(channel="msedge")
            page = b.new_page(viewport={"width": 1100, "height": 1400})
            page.emulate_media(media="print")
            page.goto((OUT / "index.html").as_uri())
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            pdf = OUT / "ERP-Migration-Guide.pdf"
            page.pdf(path=str(pdf), format="A4", print_background=True, margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"}, display_header_footer=True,
                     header_template="<span></span>", footer_template='<div style="font-size:8px;width:100%;text-align:center;color:#777">The Move-House Guide - page <span class="pageNumber"></span> of <span class="totalPages"></span></div>')
            b.close()
        print(f"wrote {pdf} ({pdf.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
