"""NARRATED SCREEN RECORDING: drives a visible Edge window through a full migration of a LARGE, complex dataset with a spoken voice-over.

    python scripts/generate_complex_data.py --rows 60000 --out mock_data/large --seed 7      # once
    python scripts/record_demo.py [--data mock_data/large] [--out mock_data/recordings] [--tests-summary "..."] [--pace 1.0]

What it produces in --out:
    migration_demo.mp4          the screen recording (the page, 1600x900) with the voice-over mixed in
    migration_demo_voice.wav    the voice-over alone
    migration_demo.srt          subtitles with the same text and timing
    migration_demo_script.md    the voice-over as a readable script

The voice is the local Windows speech engine (System.Speech): nothing is sent to an online service. Each sentence is spoken out loud while the
demo runs, and a sentence never starts before the previous one has finished, so the picture waits for the explanation and not the other way
round. During the long waits (AI mapping, landing, the pipeline) extra explanatory sentences are spoken. The narrated numbers (rows, rules,
defects, the reconciliation figures) are read from the real run, not typed in. Needs a Gemini key in the server for the AI mapping step and
the demo legacy database (scripts/create_demo_source_db.py) for the database-connection step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import wave
import winsound
from pathlib import Path

import httpx
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
ap.add_argument("--data", default=str(ROOT / "mock_data" / "large"))
ap.add_argument("--out", default=str(ROOT / "mock_data" / "recordings"))
ap.add_argument("--pace", type=float, default=1.0)
ap.add_argument("--voice", default="Microsoft Zira Desktop")
ap.add_argument("--rate", type=int, default=0, help="speech rate, -10 (slow) to 10 (fast)")
ap.add_argument("--tests-summary", default="", help="one or two sentences about the test results, spoken at the end")
ap.add_argument("--pg-port", default="5433")
ap.add_argument("--no-db-step", action="store_true", help="skip the database-connection part")
ap.add_argument("--no-fast-forward", action="store_true", help="keep the waiting stretches at normal speed")
args = ap.parse_args()
BASE, PACE = args.base, args.pace
DATA, OUT = Path(args.data), Path(args.out)
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "voice").mkdir(exist_ok=True)
CSV = DATA / "customers_complex.csv"
KEY = json.loads((DATA / "answer_key.json").read_text(encoding="utf-8"))
ROWS = int(KEY["rows"])
SIZE_MB = CSV.stat().st_size / 1_048_576
SYSTEM, TABLE = "SAP_ECC_REC_" + time.strftime("%H%M"), "KNA1_LARGE"
TOTAL_STEPS = 19
step_no = 0
api = httpx.Client(base_url=BASE, headers={"X-User": "demo.author"}, timeout=120)


# ====================================================================== the voice
class Narrator:
    """Speaks sentences with the local Windows voice, one at a time, and remembers when each one started (for the sound track)."""

    PS = r"""
Add-Type -AssemblyName System.Speech
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('%VOICE%'); $s.Rate = %RATE%
[Console]::Out.WriteLine('ready')
while (($l = [Console]::In.ReadLine()) -ne $null) {
  $i = $l.IndexOf('|')
  $s.SetOutputToWaveFile($l.Substring(0, $i)); $s.Speak($l.Substring($i + 1)); $s.SetOutputToNull()
  [Console]::Out.WriteLine('ok')
}
"""

    def __init__(self) -> None:
        script = OUT / "voice" / "tts.ps1"
        script.write_text(self.PS.replace("%VOICE%", args.voice).replace("%RATE%", str(args.rate)), encoding="utf-8")
        self.proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        assert self.proc.stdout.readline().strip() == "ready", "speech engine did not start"
        self.t0 = time.monotonic()
        self.busy_until = 0.0
        self.events: list[tuple[float, float, Path, str]] = []          # (start, duration, wav, text)
        self.marks: list[float] = []                                    # when each step caption went up

    def _wav(self, text: str) -> tuple[Path, float]:
        path = OUT / "voice" / (hashlib.sha1(f"{args.voice}|{args.rate}|{text}".encode()).hexdigest()[:16] + ".wav")
        if not path.is_file():
            self.proc.stdin.write(f"{path}|{text}\n")
            self.proc.stdin.flush()
            assert self.proc.stdout.readline().strip() == "ok"
        with wave.open(str(path)) as w:
            return path, w.getnframes() / w.getframerate()

    def prepare(self, *texts: str) -> None:
        for t in texts:
            self._wav(t)

    def idle(self) -> None:
        """Block until the sentence being spoken has finished."""
        wait = self.busy_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def speak(self, text: str) -> float:
        """Say `text` after the previous sentence; returns its duration. Does not wait for it to finish."""
        path, seconds = self._wav(text)
        self.idle()
        start = time.monotonic()
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        self.busy_until = start + seconds + 0.45
        self.events.append((start - self.t0, seconds, path, text))
        print(f"   [voice @{start - self.t0:6.1f}s] {text}", flush=True)
        return seconds

    def close(self) -> None:
        self.idle()
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()


voice: Narrator


def pause(seconds: float = 1.2) -> None:
    time.sleep(seconds * PACE)


def say(page: Page, title: str, detail: str, *narration: str) -> None:
    """Wait for the previous explanation to finish, show this step's caption, and start explaining it."""
    global step_no
    voice.idle()
    step_no += 1
    voice.marks.append(time.monotonic() - voice.t0)
    print(f"[step {step_no}/{TOTAL_STEPS} @{voice.marks[-1]:.1f}s] {title} - {detail}", flush=True)
    page.evaluate("""([n, total, text, detail]) => {
        let b = document.getElementById('demo-caption');
        if (!b) {
            b = document.createElement('div'); b.id = 'demo-caption';
            const s = b.style;
            s.position = 'fixed'; s.top = '0'; s.left = '50%'; s.transform = 'translateX(-50%)'; s.zIndex = '99999'; s.maxWidth = '70vw';
            s.background = 'rgba(30,27,75,.94)'; s.color = '#fff'; s.padding = '10px 22px'; s.borderRadius = '0 0 10px 10px';
            s.font = '600 16px "Segoe UI", system-ui, sans-serif'; s.boxShadow = '0 6px 22px rgba(0,0,0,.35)'; s.textAlign = 'center'; s.pointerEvents = 'none';
            document.body.appendChild(b);
        }
        b.innerHTML = '';
        const head = document.createElement('div'); head.textContent = 'Step ' + n + ' of ' + total + ': ' + text; b.appendChild(head);
        if (detail) { const d = document.createElement('div'); d.textContent = detail; d.style.fontWeight = '400'; d.style.fontSize = '13px'; d.style.opacity = '.85'; d.style.marginTop = '3px'; b.appendChild(d); }
    }""", [step_no, TOTAL_STEPS, title, detail])
    for sentence in narration:
        voice.speak(sentence)
    pause(0.6)


def wait_narrated(locator, lines: list[str], timeout_s: float, state: str = "visible") -> None:
    """Wait for `locator` while explaining what is going on; the sentences are spoken in order, one per slice of the wait."""
    deadline = time.monotonic() + timeout_s
    queue = list(lines)
    while True:
        slice_s = 2.5
        if queue:
            slice_s = voice.speak(queue.pop(0)) + 0.5
        try:
            locator.wait_for(state=state, timeout=max(200, int(slice_s * 1000)))
            return
        except PlaywrightTimeout:
            if time.monotonic() > deadline:
                raise


def point(loc, seconds: float = 0.9):
    """Outline an element for a moment so the viewer sees what is about to be used (Playwright's own highlight leaves its selector labels on screen)."""
    try:
        loc.scroll_into_view_if_needed(timeout=5000)
        loc.first.evaluate("el => { el.dataset.hl = el.style.outline; el.style.outline = '3px solid #f59e0b'; el.style.outlineOffset = '2px'; }", timeout=2000)
    except Exception:  # noqa: BLE001
        pass
    pause(seconds)
    try:
        loc.first.evaluate("el => { el.style.outline = el.dataset.hl || ''; }", timeout=1500)
    except Exception:  # noqa: BLE001
        pass
    return loc


def frame(page: Page):
    return page.frame_locator("iframe.active")


def act_as(page: Page, user: str) -> None:
    box = page.locator("#actor")
    point(box, 0.4)
    box.fill(user)
    box.press("Tab")
    pause(0.6)


def words(code: str) -> str:
    """ERR_MANDATORY_CITY1 -> 'mandatory city1'"""
    return re.sub(r"^(ERR|WARN)_", "", code).replace("_", " ").lower()


def latest_run(mapping_set_id: int | None = None) -> int:
    runs = api.get("/api/runs").json()
    if mapping_set_id:
        runs = [r for r in runs if r["mapping_set_id"] == mapping_set_id]
    return max(r["run_id"] for r in runs)


# ====================================================================== the recording
voice = Narrator()
errors: list[str] = []
result: dict = {}
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=False, slow_mo=int(180 * PACE), args=["--window-position=0,0"])
    ctx = browser.new_context(viewport={"width": 1600, "height": 900}, record_video_dir=str(OUT / "raw"), record_video_size={"width": 1600, "height": 900})
    page = ctx.new_page()
    voice.t0 = time.monotonic()                                  # the video starts with the page: every sentence is placed relative to now
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("localStorage.setItem('migration.user', 'demo.author'); localStorage.removeItem('migration.console.tabs');")
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    page.wait_for_timeout(1500)

    # -------------------------------------------------------------- 1. the console
    say(page, "The Management Center", "Explorer on the left, tabs for each task, status bar below.",
        f"Welcome. This is the E R P data migration accelerator. In this recording we migrate a large and messy legacy customer file, {ROWS:,} rows, into the S A P Business Partner model.",
        "An A I assistant proposes the field mapping, a second person has to approve it, every field is validated, and at the end the system produces a reconciliation certificate.",
        "On the left is the explorer, with every source system and every target. The tabs along the top are the working areas, and the bar at the bottom shows who you are acting as.")
    point(page.locator("#explorer"), 1.2)
    point(page.locator(".iconbar"), 1.2)
    point(page.locator(".statusbar"), 1.0)

    # -------------------------------------------------------------- 2. a real database as the source
    if not args.no_db_step:
        say(page, "A source does not have to be a file", "The same wizard connects to real database servers with a read-only login.",
            "Sources do not have to be files. The same wizard connects to a real PostgreSQL server, using a read only login.")
        nxt = page.locator("#wz-next")
        point(page.get_by_role("button", name="New Source")).click()
        page.locator("#wizard[open]").wait_for()
        point(page.locator("#s-name"), 0.4).type("LEGACY_ERP_PG_DEMO", delay=60)
        page.select_option("#s-kind", "postgresql")
        pause(0.8)
        nxt.click()
        page.fill("#c-host", "localhost")
        page.fill("#c-port", args.pg_port)
        page.fill("#c-database", "legacy_demo")
        page.fill("#c-user", "legacy_ro")
        page.fill("#c-password", "legacy_ro_pw")
        pause(1.0)
        point(page.locator("#c-test"), 0.6).click()
        page.locator("#c-status.ok").wait_for(timeout=60000)
        voice.speak("The connection test proves the credentials and lists the schemas this login is allowed to see. The password is held in memory only and is never written to disk.")
        point(page.locator("#c-status"), 1.5)
        nxt.click()
        page.locator("#d-tree .wz-schema").first.wait_for(timeout=60000)
        voice.speak("Here is the schema browser. We open a schema, and click a table to see its columns, data types and primary key, before deciding what to extract.")
        page.locator('[data-toggle="ar"]').click()
        page.locator('#d-tree .tbl-name[data-table="customers"]').wait_for()
        pause(0.8)
        page.locator('#d-tree .tbl-name[data-table="customers"]').click()
        page.locator("#d-cols", has_text="primary key").wait_for()
        point(page.locator("#d-cols"), 2.0)
        voice.speak("The same connector family also covers Oracle, S Q L Server and MySQL, plus Azure Blob storage, S three, and S F T P. We tested each of those against real running servers.")
        voice.idle()
        voice.speak("This recording uses a file as its source, so I cancel here. Cancelling also removes the connection that was saved just for browsing.")
        point(page.locator("#wz-cancel"), 0.8).click()
        page.locator("#wizard[open]").wait_for(state="detached")
        voice.idle()
    else:
        step_no += 1

    # -------------------------------------------------------------- 3. the file source
    say(page, "Add the legacy file", f"A {SIZE_MB:.1f} MB Windows-1252 export of {ROWS:,} customers, 27 cryptic SAP-style columns.",
        f"Now the main source: an extract file from the legacy E R P. It is {SIZE_MB:.0f} megabytes, with {ROWS:,} customers and twenty seven cryptic columns, written in the Windows twelve fifty two encoding with semicolons as delimiters.")
    point(page.get_by_role("button", name="New Source")).click()
    page.locator("#wizard[open]").wait_for()
    pause(0.8)
    point(page.locator("#s-name"), 0.5).type(SYSTEM, delay=60)
    pause(0.6)
    say(page, "The provider: an extract file", "Databases, Azure, S3 and SFTP are in this list too.",
        "We give the source system a name and choose the provider. An extract file is selected here, and you can see the databases and the cloud storage options in the same list.")
    point(page.locator("#s-kind"), 1.0)
    page.locator("#wz-next").click()
    pause(0.8)

    say(page, "Choose the file: the format is detected, not configured", "Watch the encoding, the delimiter and the column count appear.",
        "Now we choose the file. Nothing has to be configured by hand.")
    point(page.locator("#p-file"), 0.6)
    page.set_input_files("#p-file", str(CSV))
    wait_narrated(page.locator("#p-status.ok"), ["The wizard is reading the start of the file to work out its encoding, its delimiter and its columns."], 180)
    detected = page.locator("#p-status").inner_text()
    columns = (re.search(r"(\d+) columns", detected) or [None, "27"])[1]
    print("   detected:", detected, flush=True)
    voice.speak(f"It detected the format, the encoding and the delimiter by itself, and found {columns} columns.")
    point(page.locator("#p-status"), 1.5)
    point(page.locator("#p-delimiter"), 0.8)
    point(page.locator("#p-encoding"), 0.8)
    page.locator("#wz-next").click()
    pause(0.8)

    say(page, "Name the table, pick the key, add the control total", f"{ROWS:,} rows are reported by the source system: the file is rejected if the count differs.",
        f"We name the table, mark the customer number as the key, and enter the control total. The source system says it exported {ROWS:,} rows. If the landed row count differs, the import is rejected, so nothing can be lost silently.")
    name = page.locator("#t-name")
    name.fill("")
    name.type(TABLE, delay=60)
    point(page.locator('.t-key[data-col="KUNNR"]'), 0.8).check()
    point(page.locator("#t-expected"), 0.5).fill(str(ROWS))
    pause(1.2)
    page.locator("#wz-next").click()
    pause(0.6)
    point(page.locator("#a-profile"), 0.6)
    page.uncheck("#a-studio")                                    # the studio is opened by hand a little later
    page.locator("#wz-next").click()
    pause(0.8)
    say(page, "Review the summary and finish", "The file is landed as a snapshot with a SHA-256, the rows are counted, the table is registered and profiled.",
        "The summary shows what will happen. The file is landed as an all text snapshot, so no value is converted or damaged on the way in, and it gets a S H A two fifty six fingerprint as proof of what was received.")
    point(page.locator("#wz-body"), 1.5)
    page.locator("#wz-next").click()
    wait_narrated(page.locator("#wizard[open]"),
                  ["Every row is now being counted against the control total, and the table is registered in the catalogue.",
                   "Then the profiler reads every column of every row. With this many rows it takes a little while."], 600, state="detached")
    page.locator("#tabstrip .tab", has_text="Profiler").wait_for(timeout=180000)

    # -------------------------------------------------------------- 4. the profile
    say(page, "The profile: what is really in this file", f"Null ratios, distinct counts, lengths, top values, health scores and flags for all {columns} columns.",
        f"This is the profile of what is really in the file: null ratios, distinct counts, lengths, top values and a health score for each of the {columns} columns.",
        "This is where you see the blank cities, the many spellings of each country and the names that are too long, before any mapping work starts.")
    f = frame(page)
    f.locator("#summary .stat").first.wait_for(timeout=180000)
    point(f.locator("#summary"), 2.0)
    f.locator("#columns table").wait_for()
    point(f.locator("#columns"), 2.0)
    f.locator("#columns").evaluate("el => el.scrollIntoView({block: 'start'})")
    pause(2.5)

    # -------------------------------------------------------------- 5. AI mapping
    say(page, "Open the Mapping Studio and let the AI propose a mapping", "Gemini reads the cryptic names plus sample values and maps them to SAP fields.",
        "Now the A I part. We open the mapping studio and ask the assistant for a first mapping.")
    node = page.locator(f'#tree li[data-id="sys:{SYSTEM}"] li[data-id^="t"] > .node').first
    point(node, 0.6).click()
    page.get_by_role("button", name="Mapping", exact=True).click()
    page.locator("#tabstrip .tab", has_text="Mapping").wait_for()
    f = frame(page)
    f.locator("#mapping-select").wait_for()
    pause(1.0)
    point(f.locator("#generate"), 0.8).click()
    f.locator("#job:not(.hidden)").wait_for(timeout=30000)
    point(f.locator("#job"), 0.5)
    wait_narrated(f.locator("#toast.show", has_text="rules proposed"),
                  ["Gemini reads each cryptic legacy column name, such as K U N N R, N A M E one or O R T zero one, together with real sample values from the data, and proposes the matching S A P target field.",
                   "The sample values matter. A column that holds postal codes, or one that holds tax numbers, is recognised by what is in it, not only by its name.",
                   "It makes one model call per column, so this takes about a minute.",
                   "Every proposal comes back with a confidence score, and the weaker ones are flagged for a human to review. The assistant proposes, but it never approves."], 420)
    toast = f.locator("#toast").inner_text()
    proposed = (re.search(r"(\d+) rules? proposed", toast) or [None, "the"])[1]
    print("   ", toast, flush=True)
    pause(1.5)
    f.locator(".rule").first.wait_for()
    say(page, "The AI's proposal, with confidence and review flags", "Every rule shows its target SAP field, rule type, confidence and whether a human must review it.",
        f"{proposed} rules were proposed. Each one shows its target table and field, the rule type, the confidence, and whether a person must review it.")
    point(f.locator("#rules"), 1.5)
    f.locator("#rules").evaluate("el => el.scrollIntoView({block: 'start'})")
    pause(2.0)

    # -------------------------------------------------------------- 6. the steward completes it
    say(page, "The steward completes the mapping", "Lookups for countries and payment terms, plus defaults for the mandatory fields the legacy system never had.",
        "Next, a data steward finishes the mapping. The legacy system wrote countries in many ways, like Deutschland, Germany and D E. So we attach a lookup table that turns every spelling into the S A P country code, and another one for the payment terms.")

    def edit_rule(target_text: str, lookups: dict) -> bool:
        rule = f.locator(".rule", has_text=target_text).first
        if rule.count() == 0:
            return False
        point(rule, 0.6)
        rule.locator(".edit").click()
        pause(0.6)
        rule.locator('select[name="rule_type"]').select_option("VALUE_MAPPING")
        rule.locator('textarea[name="lookups"]').fill(json.dumps(lookups, ensure_ascii=False))
        pause(1.0)
        rule.locator('input[name="confirm"]').check()
        rule.get_by_role("button", name="Save rule").click()
        f.locator("#toast.show").wait_for()
        pause(1.0)
        return True

    edited = [edit_rule("ADRC.COUNTRY", KEY["lookups"]["country"]), edit_rule("KNB1.ZTERM", KEY["lookups"]["terms"])]
    # The assistant chooses the rule type column by column and does not always choose the same one: the currency column is sometimes a plain copy
    # and sometimes a value mapping with an empty lookup table, which rejects every single row. It is fixed when, and only when, it is the latter.
    waers = f.locator(".rule", has_text="KNVV.WAERS").first
    if waers.count() and "lookups are not defined" in waers.inner_text().lower():
        voice.speak("Notice the currency column. The assistant chose a lookup for it but left the lookup table empty, and the review flag on the rule says so. "
                    "Left like that, every record would be rejected, so the steward adds the currency translations too.")
        edited.append(edit_rule("KNVV.WAERS", KEY["lookups"]["currency"]))
    print("   lookups edited (country, payment terms, currency):", edited, flush=True)
    voice.speak("Then we add fixed default values for the mandatory business partner fields that the legacy system never had, the partner group and the partner type.")
    for column, logic in (("BU_GROUP", "'BP01'"), ("TYPE", "'2'")):
        if f.locator(".rule", has_text=f"BUT000.{column}").count():
            continue
        if f.locator("#add-rule.hidden").count():                # the toggle flips the panel, so click it only when the panel is closed
            point(f.locator("#add-rule-toggle"), 0.5).click()
        form = f.locator("#add-rule-form")
        form.wait_for(state="visible", timeout=10000)
        form.locator('input[name="target_table"]').fill("BUT000")
        form.locator('input[name="target_column"]').fill(column)
        form.locator('select[name="rule_type"]').select_option("STATIC_VALUE")
        form.locator('input[name="transformation_logic"]').fill(logic)
        pause(0.8)
        form.get_by_role("button", name="Add rule").click()
        f.locator(".rule", has_text=f"BUT000.{column}").first.wait_for()
        pause(0.8)

    # -------------------------------------------------------------- 7. four-eyes approval
    say(page, "Submit the mapping for approval", "demo.author submits it. Nothing can run against the live target until a second person approves.",
        "The author now submits the mapping for approval. Until a second person approves it, nothing can run.")
    point(f.locator("#submit-mapping"), 0.8).click()
    f.locator("#mapping-status", has_text="SUBMITTED").wait_for()
    pause(1.0)
    say(page, "A different person approves: now acting as demo.steward", "The approval records the approver's name and the time; the creator of a mapping set can never approve it.",
        "Now we act as a different person, the steward. The system records the approver's name and the time, and it refuses approval from whoever created the mapping. This is the four eyes principle, enforced by the software and not by good intentions.")
    act_as(page, "demo.steward")
    point(f.locator("#approve-mapping"), 0.8).click()
    f.locator("#mapping-status", has_text="APPROVED").wait_for()
    pause(1.2)

    # -------------------------------------------------------------- 8. the pipeline
    say(page, "Run the pipeline", "Transform with the approved rules, find duplicates, validate every field against the SAP catalog, record defects.",
        f"The mapping is approved, so we run the pipeline on all {ROWS:,} rows.")
    mapping_set_id = int(re.sub(r"\D", "", f.locator("#mapping-select").evaluate("el => el.value")) or 0)
    point(f.locator("#run-pipeline"), 0.8).click()
    wait_narrated(page.locator("#tabstrip .tab", has_text="Governance"),
                  ["First the approved rules transform every legacy row into S A P Business Partner records, across the general data, address and company code tables.",
                   "Then the duplicate engine looks for the same customer entered more than once. It compares names, addresses, tax numbers, phone numbers and e mails, and it keeps look alikes apart, so two different companies in the same street are not merged.",
                   "Finally every field is validated against the S A P catalogue: its length, whether it is mandatory, and whether its value exists in the lookup tables. Each failure is recorded as a defect.",
                   "All of this is done in a bounded amount of memory, so a file like this one does not need a big server."], 1500)
    f = frame(page)
    f.locator("#gate").wait_for(timeout=120000)
    pause(1.5)
    run_id = latest_run(mapping_set_id or None)
    recon = api.get(f"/api/reconciliation/{run_id}").json()["report"]
    defects = api.get(f"/api/defects/{run_id}").json()
    valid_rows = recon["counts"].get("valid") or 0
    if valid_rows < 0.5 * ROWS:                                   # a recording in which most rows fail for one avoidable reason would mislead
        worst = sorted(defects["defects"], key=lambda d: -d["violation_count"])[:3]
        raise RuntimeError(f"only {valid_rows:,} of {ROWS:,} rows are valid after the run; worst defects: " + "; ".join(f"{d['rule_signature']} {d['violation_count']:,}" for d in worst))
    result.update(run_id=run_id, counts=recon["counts"], variance=recon["variance"], balanced=recon["balanced"], open_critical=defects["open_critical"])

    # -------------------------------------------------------------- 9. the gate and the defects
    state = defects["readiness"]["state"]
    top = sorted(defects["defects"], key=lambda d: -d["violation_count"])[:3]
    top_words = "; ".join(f"{words(d['rule_signature'])}, {d['violation_count']:,} records" for d in top)
    say(page, f"The governance gate says {state}, and exactly why", "A real extract has real problems: blank cities and countries, unknown lookup values, names longer than SAP allows.",
        f"The governance gate says {state.lower()}, and it says exactly why. There are {defects['open_critical']} critical defect groups open, so nothing may be loaded into S A P. That is the control working as designed: bad data is stopped before it reaches the target.")
    point(f.locator("#gate"), 3.0)
    f.locator("#defects table").wait_for()
    say(page, "Defects, grouped by rule", "Each group names the SAP field, how many records fail, sample keys and a plain-language root cause.",
        f"The defects are grouped by rule. The largest groups are: {top_words}.",
        "Each group names the S A P field, shows how many records fail, lists sample record keys so someone can look them up, and gives the likely root cause in plain language.")
    f.locator("#defects").evaluate("el => el.scrollIntoView({block: 'start'})")
    point(f.locator("#defects"), 3.0)
    print("   gate:", f.locator("#gate").inner_text().replace("\n", " | ")[:200], flush=True)

    # -------------------------------------------------------------- 10. data construction + certificate
    say(page, "Data Construction: values the legacy system never had", "Download the template, fill the gaps (like the mandatory reconciliation account), upload, and the run re-validates.",
        "Some defects are not errors in the data, but values the legacy system never held, like the reconciliation account. The data construction tab gives the business a template to download, fill in, and upload, and the run is validated again.")
    point(f.locator("#tab-construction"), 0.8).click()
    pause(2.0)
    point(f.locator("#tab-defects"), 0.5).click()
    pause(0.8)
    c = recon["counts"]
    say(page, "The reconciliation certificate: every record accounted for", "Extracted, transformed, duplicates removed, rejected, valid: the numbers must add up with zero variance.",
        f"Finally the reconciliation certificate. The system extracted {c['extracted']:,} records. {c['transformed_ok']:,} were transformed. "
        f"{(c['duplicate_children'] or 0):,} were removed as duplicates, {(c['validation_rejected'] or 0):,} were rejected by validation, and {(c['valid'] or 0):,} are valid.",
        f"The accounted rows add up to the extracted rows with a variance of {recon['variance']}. " + ("The certificate is balanced: not one record is unexplained." if recon["balanced"] else "The certificate is not balanced."))
    point(page.get_by_role("button", name="Certificate", exact=True), 0.8).click()
    page.locator("#tabstrip .tab", has_text="Certificate").wait_for()
    f = frame(page)
    pause(3.0)
    point(f.locator("body"), 3.0)

    # -------------------------------------------------------------- 11. close
    closing = ["That was a complete migration run: the source landed and verified, mapped with the A I assistant, approved by a second person, validated, deduplicated, blocked by the gate where it had to be, and closed with a certificate."]
    if args.tests_summary:
        closing.append("And these are the results of the real tests that were run before this recording. " + args.tests_summary)
    say(page, "That was a complete migration run", "Source landed and verified, AI-mapped, approved by a second person, validated, duplicates found, gate enforced, certificate produced.", *closing)
    page.get_by_role("button", name="Start", exact=True).click()
    pause(3)
    voice.idle()
    pause(1.5)
    video_path = Path(page.video.path())
    ctx.close()                                                   # the video file is finalised when the context closes
    browser.close()
voice.close()

# ====================================================================== the sound track, the fast-forwards and the final file
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_recording import build  # noqa: E402

final = build(voice.events, video_path, OUT, marks=voice.marks, fast_forward=not args.no_fast_forward)

print(f"\nsteps run: {step_no} (planned {TOTAL_STEPS}); sentences spoken: {len(voice.events)}")
print("run:", json.dumps(result))
print("console errors:", [e for e in errors if "422" not in e], flush=True)
print(f"VIDEO: {final}")
print("DEMO COMPLETE")
