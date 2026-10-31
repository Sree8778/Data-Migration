"""Take the REAL screenshots of the application for docs/user-guide: the robot uses the application exactly as a person would, on the sample file mock_data/legacy_ar_customers.csv,
and photographs each screen. Nothing is drawn by hand; the only additions are the red numbered rings ("callouts") that point at parts of a screen.

    python scripts/make_guide_screenshots.py --base http://127.0.0.1:8771 --stage import,quality,...      (a FRESH copy of the application: it creates sources, mappings, runs)

Stages are run one after the other and each continues from what the previous one left in the application, so a stage can be repeated by starting a fresh application and running
the stages again from the start. The images go to docs/user-guide/images.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from demo_kit import USER_JS  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8771")
ap.add_argument("--stage", default="import")
ap.add_argument("--out", default=str(ROOT / "docs" / "user-guide" / "images"))
ap.add_argument("--csv", default=str(ROOT / "mock_data" / "legacy_ar_customers.csv"))
ap.add_argument("--visible", action="store_true")
args = ap.parse_args()
BASE, OUT, CSV = args.base, Path(args.out), Path(args.csv)
OUT.mkdir(parents=True, exist_ok=True)
SYSTEM, TABLE, KEY = "LEGACY_ERP", "AR_CUSTOMERS", "CUST_ID"
COPY_ID = 2             # the editable copy the fix button made
MAPPING_ID = 1          # the hand-made mapping set (the AI try is another set)
api = httpx.Client(base_url=BASE + "/api", timeout=600, headers={"X-User": "guide.author"})

CALLOUT_JS = """([items]) => {
  for (const it of items) {
    const mk = (css) => { const d = document.createElement('div'); d.className = 'guide-callout'; if ('popover' in d) d.setAttribute('popover', 'manual'); const s = d.style;
      s.position = 'fixed'; s.inset = 'auto'; s.margin = '0'; s.padding = '0'; s.border = '0'; s.background = 'transparent'; s.overflow = 'visible'; s.pointerEvents = 'none'; s.zIndex = '2147483647';
      Object.assign(s, css); document.documentElement.appendChild(d); try { d.showPopover(); } catch (e) {} return d; };
    mk({ left: (it.x - 4) + 'px', top: (it.y - 4) + 'px', width: (it.w + 8) + 'px', height: (it.h + 8) + 'px', border: '3px solid #e11d48', borderRadius: '8px', boxSizing: 'border-box' });
    const dot = mk({ left: Math.max(2, it.x - 18) + 'px', top: Math.max(2, it.y - 18) + 'px', width: '30px', height: '30px', borderRadius: '50%', background: '#e11d48', color: '#fff', font: '700 17px "Segoe UI", sans-serif',
      textAlign: 'center', lineHeight: '30px', boxShadow: '0 2px 6px rgba(0,0,0,.4)' });
    dot.textContent = it.n;
  }
}"""


class Shooter:
    def __init__(self) -> None:
        self.taken: list[str] = []

    def open(self, p):
        browser = p.chromium.launch(channel="msedge", headless=not args.visible)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1.25)
        page = ctx.new_page()
        page.add_init_script(USER_JS.replace("demo.author", "guide.author"))
        page.on("dialog", lambda d: d.accept("Rolled back while preparing the guide") if d.type == "prompt" else d.accept())
        return browser, page

    def shot(self, page: Page, name: str, clip=None) -> None:
        path = OUT / f"{name}.png"
        page.screenshot(path=str(path), **({"clip": clip} if clip else {}))
        self.taken.append(name)
        print(f"  [{name}.png] {path.stat().st_size // 1024} KB", flush=True)

    def callouts(self, page: Page, items: list[tuple[str, object]]) -> None:
        """Red numbered rings around parts of the screen. items: (number, locator)."""
        boxes = []
        for n, loc in items:
            box = loc.first.bounding_box()
            if box:
                boxes.append({"n": n, "x": box["x"], "y": box["y"], "w": box["width"], "h": box["height"]})
        page.evaluate(CALLOUT_JS, [boxes])

    @staticmethod
    def clear(page: Page) -> None:
        page.evaluate("() => document.querySelectorAll('.guide-callout').forEach((e) => e.remove())")


def frame(page: Page):
    return page.frame_locator("iframe.active")


def settle(page: Page, ms: int = 600) -> None:
    page.wait_for_timeout(ms)


# ====================================================================== the stages
def stage_import(g: Shooter, page: Page) -> None:
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    settle(page, 1500)
    g.shot(page, "01_home")
    g.callouts(page, [("1", page.locator(".menubar")), ("2", page.locator(".iconbar")), ("3", page.locator("#explorer")), ("4", page.locator("#tabstrip")),
                      ("5", page.locator("#tabbody")), ("6", page.locator(".statusbar")), ("7", page.locator("#actor"))])
    g.shot(page, "02_home_tour")
    g.clear(page)

    page.get_by_role("button", name="New Source").click()
    page.locator("#wizard[open]").wait_for()
    page.fill("#s-name", SYSTEM)
    settle(page)
    g.shot(page, "03_wizard_provider")
    page.locator("#wz-next").click()
    page.set_input_files("#p-file", str(CSV))
    page.locator("#p-status.ok").wait_for(timeout=60000)
    settle(page, 900)
    g.shot(page, "04_wizard_file")
    page.locator("#wz-next").click()
    name = page.locator("#t-name")
    name.fill(TABLE)
    page.locator(f'.t-key[data-col="{KEY}"]').check()
    page.locator("#t-expected").fill("1980")
    settle(page)
    g.shot(page, "05_wizard_table")
    page.locator("#wz-next").click()
    page.uncheck("#a-studio")
    settle(page)
    g.shot(page, "06_wizard_actions")
    page.locator("#wz-next").click()
    settle(page)
    g.shot(page, "07_wizard_summary")
    page.locator("#wz-next").click()
    page.locator("#wizard[open]").wait_for(state="detached", timeout=120000)
    page.locator("#tabstrip .tab", has_text="Profiler").wait_for(timeout=60000)
    f = frame(page)
    f.locator("#summary .stat").first.wait_for(timeout=60000)
    f.locator("#columns table").wait_for()
    settle(page, 1500)
    g.shot(page, "08_profile")
    f.locator("#columns").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 700)
    g.shot(page, "09_profile_columns")
    f.locator("#dupes").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 700)
    g.shot(page, "10_profile_duplicates")


def stage_quality(g: Shooter, page: Page) -> None:
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    settle(page, 1200)
    open_table(page)
    page.get_by_role("button", name="Quality", exact=True).click()
    page.locator("#tabstrip .tab", has_text="Quality").wait_for()
    f = frame(page)
    f.locator("#assess").wait_for()
    settle(page, 800)
    g.shot(page, "11_quality_before")
    f.locator("#assess").click()
    f.locator("#summary .stat, #summary table, #summary .card").first.wait_for(timeout=300000)
    settle(page, 2500)
    g.shot(page, "12_quality_result")
    f.locator("#findings").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 700)
    g.shot(page, "13_quality_findings")


def act_as(page: Page, user: str) -> None:
    box = page.locator("#actor")
    if box.input_value() != user:
        box.fill(user)
        box.press("Tab")
        settle(page, 600)


def open_table(page: Page) -> None:
    node = page.locator(f'#tree li[data-id="sys:{SYSTEM}"] li[data-id^="t"] > .node').first
    if not node.is_visible():
        page.locator(f'#tree li[data-id="sys:{SYSTEM}"] > .node .twist').first.click()
    node.click()


def open_studio(page: Page):
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    settle(page, 1200)
    open_table(page)
    page.get_by_role("button", name="Mapping", exact=True).click()
    page.locator("#tabstrip .tab", has_text="Mapping").wait_for()
    f = frame(page)
    f.locator("#mapping-select").wait_for()
    settle(page, 900)
    return f


def stage_ai(g: Shooter, page: Page) -> None:
    f = open_studio(page)
    g.shot(page, "14_studio_empty")
    g.callouts(page, [("1", f.locator("#generate")), ("2", f.locator("#new-set")), ("3", f.locator("#add-rule-toggle")), ("4", f.locator("#preflight")),
                      ("5", f.locator("#submit-mapping")), ("6", f.locator("#approve-mapping")), ("7", f.locator("#run-pipeline"))])
    g.shot(page, "15_studio_buttons")
    g.clear(page)
    f.locator("#consistency").select_option("1")
    started = time.time()
    f.locator("#generate").click()
    f.locator("#job:not(.hidden)").wait_for(timeout=60000)
    page.wait_for_timeout(25000)
    g.shot(page, "16_ai_running")
    f.locator("#toast.show", has_text="rules proposed").wait_for(timeout=40 * 60 * 1000)
    print(f"  the local model took {time.time() - started:.0f} s", flush=True)
    f.locator(".rule").first.wait_for()
    settle(page, 2000)
    g.shot(page, "17_ai_result")
    f.locator("#rules").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 700)
    g.shot(page, "18_ai_rules")


COUNTRY_LK = {"USA": "US", "US": "US", "United States": "US", "Germany": "DE", "Deutschland": "DE", "DE": "DE", "Japan": "JP", "France": "FR", "FR": "FR", "India": "IN", "IN": "IN",
              "Canada": "CA", "CA": "CA", "Spain": "ES", "United Kingdom": "GB", "Great Britain": "GB", "GB": "GB", "UK": "GB"}
TERMS_LK = {"COD": "CASH", "Net 30": "NT30", "NET30": "NT30", "NET 60": "NT60", "NET60": "NT60", "NET90": "NT90"}
# (SAP table, SAP field, kind of rule, legacy column, formula / fixed value)
RULES = [
    ("BUT000", "NAME_ORG1", "DIRECT_COPY", "CUST_NAME", ""), ("BUT000", "BP_EXT", "DIRECT_COPY", "CUST_ID", ""),
    ("BUT000", "TYPE", "STATIC_VALUE", "", "'2'"), ("BUT000", "BU_GROUP", "STATIC_VALUE", "", "'Z001'"),
    ("BUT000", "BU_SORT1", "SQL_EXPRESSION", "CUST_NAME", "upper(left(trim(CUST_NAME), 20))"),
    ("ADRC", "STREET", "DIRECT_COPY", "ADDR_STREET", ""), ("ADRC", "CITY1", "DIRECT_COPY", "CITY", ""), ("ADRC", "POST_CODE1", "DIRECT_COPY", "POSTAL_CODE", ""),
    ("ADRC", "COUNTRY", "VALUE_MAPPING", "COUNTRY", ""), ("ADR6", "SMTP_ADDR", "DIRECT_COPY", "EMAIL", ""), ("ADR2", "TEL_NUMBER", "DIRECT_COPY", "PHONE", ""),
    ("KNB1", "ZTERM", "VALUE_MAPPING", "PAYMENT_TERMS", ""),
]


def add_rule(f, rule, page: Page | None = None, name: str = "") -> None:
    table, column, rtype, source, logic = rule
    if f.locator("#add-rule.hidden").count():
        f.locator("#add-rule-toggle").click()
    form = f.locator("#add-rule-form")
    form.wait_for(state="visible", timeout=10000)
    form.locator('input[name="target_table"]').fill(table)
    form.locator('input[name="target_column"]').fill(column)
    form.locator('select[name="rule_type"]').select_option(rtype)
    form.locator('input[name="source_column"]').fill(source)
    form.locator('input[name="transformation_logic"]').fill(logic)
    if page is not None and name:
        settle(page, 400)
        shooter_ref[0].shot(page, name)
    form.get_by_role("button", name="Add rule").click()
    f.locator(".rule", has_text=f"{table}.{column}").first.wait_for(timeout=15000)


shooter_ref: list = [None]


def stage_manual(g: Shooter, page: Page) -> None:
    shooter_ref[0] = g
    f = open_studio(page)
    f.locator("#new-set").click()
    f.locator("#mapping-status", has_text="DRAFT").wait_for(timeout=30000)
    settle(page, 800)
    g.shot(page, "19_new_set")
    for n, rule in enumerate(RULES):
        add_rule(f, rule, page, "20_add_rule_form" if n == 3 else "")
    settle(page, 800)
    g.shot(page, "21_rules_list")
    for column, lookups in (("COUNTRY", COUNTRY_LK), ("ZTERM", TERMS_LK)):
        rule = f.locator(".rule", has_text=f".{column}").first
        rule.locator(".edit").click()
        rule.locator('select[name="rule_type"]').select_option("VALUE_MAPPING")
        rule.locator('textarea[name="lookups"]').fill(json.dumps(lookups, indent=1))
        rule.locator('input[name="confirm"]').check()
        settle(page, 500)
        if column == "COUNTRY":
            rule.scroll_into_view_if_needed()
            g.shot(page, "22_lookup_editor")
        rule.get_by_role("button", name="Save rule").click()
        f.locator("#toast.show").wait_for()
        settle(page, 1000)
    stage_preflight(g, page, f)


def stage_preflight(g: Shooter, page: Page, f=None) -> None:
    f = f or open_studio(page)
    if f.locator("#mapping-select").input_value() != str(MAPPING_ID):
        f.locator("#mapping-select").select_option(str(MAPPING_ID))
        settle(page, 1200)
    f.locator("#preflight").click()
    f.locator("#preflight-panel:not(.hidden)").wait_for(timeout=180000)
    settle(page, 2000)
    f.locator("#preflight-panel").scroll_into_view_if_needed()
    g.shot(page, "23_preflight")


def stage_run(g: Shooter, page: Page) -> None:
    f = frame(page)
    f.locator("#submit-mapping").scroll_into_view_if_needed()
    f.locator("#submit-mapping").click()
    f.locator("#mapping-status", has_text="SUBMITTED").wait_for(timeout=30000)
    f.locator("#mapping-status").scroll_into_view_if_needed()
    settle(page, 900)
    g.shot(page, "24_submitted")
    box = page.locator("#actor")
    box.fill("guide.steward")
    box.press("Tab")
    settle(page, 700)
    f.locator("#approve-mapping").click()
    f.locator("#mapping-status", has_text="APPROVED").wait_for(timeout=30000)
    settle(page, 900)
    g.shot(page, "25_approved")
    f.locator("#run-pipeline").click()
    try:                                              # a small file can be done before the progress bar is drawn
        f.locator("#job:not(.hidden)").wait_for(timeout=4000)
        g.shot(page, "26_running")
    except Exception:
        print("  (the run was too quick to photograph its progress bar)", flush=True)
    page.locator("#tabstrip .tab", has_text="Governance").wait_for(timeout=600000)
    gf = frame(page)
    gf.locator("#gate").wait_for(timeout=60000)
    gf.locator("#defects table").first.wait_for(timeout=60000)
    settle(page, 1500)
    g.shot(page, "27_gate_blocked")
    gf.locator("#defects").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 700)
    g.shot(page, "28_defects")


def stage_fix(g: Shooter, page: Page) -> None:
    """The approved mapping met the gate and was BLOCKED. An approved mapping cannot be edited, so the fix button makes an editable copy; the copy gets the leave-out rules, the extra
    lookup and the shorter name, and goes through approval again."""
    f = open_studio(page)
    act_as(page, "guide.author")
    f.locator("#mapping-select").select_option(str(MAPPING_ID))
    settle(page, 1200)
    f.locator("#preflight").click()
    f.locator("#preflight-panel .fixes .fix").first.wait_for(timeout=180000)
    settle(page, 1500)
    card = f.locator("#preflight-panel .fixes .fix", has_text="KNB1.BUKRS").first
    card.wait_for(timeout=30000)
    card.locator("details summary").click()
    settle(page, 400)
    card.evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 900)
    g.shot(page, "29_fix_card")
    card.locator(".fix-value").fill("1000")
    card.locator(".fix-yes").click()
    f.locator("#mapping-status", has_text="DRAFT").wait_for(timeout=60000)
    settle(page, 2500)
    g.shot(page, "30_fix_done")
    print(f"  the editable copy is mapping set {f.locator('#mapping-select').input_value()}", flush=True)


def stage_v2(g: Shooter, page: Page) -> None:
    f = open_studio(page)
    f.locator("#mapping-select").select_option(str(COPY_ID))
    f.locator("#mapping-status", has_text="DRAFT").wait_for(timeout=30000)
    settle(page, 1200)
    f.locator("#exclusions summary").click() if not f.locator("#exclusions").evaluate("el => el.open") else None
    settle(page, 500)
    for column, op, values, reason, shot in (("CITY", "is_blank", "", "No city: SAP needs one to post a letter", "31_exclusion_form"),
                                             ("COUNTRY", "is_blank", "", "No country: SAP cannot pick the tax and address rules", None),
                                             ("COUNTRY", "in", "Atlantis, Narnia, Wakanda", "Not real countries: test rows that slipped into the old system", None)):
        f.locator("#excl-column").select_option(column)
        f.locator("#excl-operator").select_option(op)
        if values:
            f.locator("#excl-value").fill(values)
        f.locator("#excl-reason").fill(reason)
        f.locator("#excl-check").click()
        f.locator("#excl-preview", has_text="row(s) match").wait_for(timeout=30000)
        if shot:
            f.locator("#exclusions").scroll_into_view_if_needed()
            settle(page, 500)
            g.shot(page, shot)
        f.locator("#excl-add").click()
        f.locator("#excl-rules", has_text=reason[:20]).wait_for(timeout=30000)
        settle(page, 700)
    f.locator("#exclusions").scroll_into_view_if_needed()
    g.shot(page, "32_exclusions_done")

    terms = dict(TERMS_LK, **{"WHEN-RECEIVED": "CASH"})
    rule = f.locator(".rule", has_text=".ZTERM").first
    rule.scroll_into_view_if_needed()
    rule.locator(".edit").click()
    rule.locator('textarea[name="lookups"]').fill(json.dumps(terms, indent=1))
    rule.locator('input[name="confirm"]').check()
    rule.get_by_role("button", name="Save rule").click()
    f.locator("#toast.show").wait_for()
    settle(page, 1200)
    name_rule = f.locator(".rule", has_text=".NAME_ORG1").first
    name_rule.locator(".edit").click()
    name_rule.locator('select[name="rule_type"]').select_option("SQL_EXPRESSION")
    name_rule.locator('textarea[name="logic"]').fill("left(trim(CUST_NAME), 40)")
    name_rule.locator('input[name="confirm"]').check()
    settle(page, 500)
    name_rule.scroll_into_view_if_needed()
    g.shot(page, "33_name_rule_edit")
    name_rule.get_by_role("button", name="Save rule").click()
    f.locator("#toast.show").wait_for()
    settle(page, 1200)
    f.locator("#preflight").click()
    f.locator("#preflight-panel:not(.hidden)").wait_for(timeout=180000)
    settle(page, 2000)
    f.locator("#preflight-panel").scroll_into_view_if_needed()
    g.shot(page, "34_preflight_v2")


def open_governance(page: Page, run_id: int = 0):
    """The Governance page of a run (the newest one unless told otherwise): the run is picked in the explorer, as a person would."""
    run_id = run_id or max(r["run_id"] for r in api.get("/runs").json())
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    settle(page, 1200)
    node = page.locator(f'#tree li[data-id="r{run_id}"] > .node')
    if not node.is_visible():
        page.locator('#tree li[data-id="runs"] > .node .twist').first.click()
    node.click()                                       # a click picks the run; the toolbar button opens its page
    page.get_by_role("button", name="Governance", exact=True).click()
    page.locator("#tabstrip .tab", has_text=f"Governance - run {run_id}").wait_for()
    gf = frame(page)
    gf.locator("#gate").wait_for(timeout=60000)
    settle(page, 1500)
    return gf


def stage_run2(g: Shooter, page: Page) -> None:
    """The corrected copy: submitted by the author, approved by somebody else, run again."""
    f = open_studio(page)
    act_as(page, "guide.author")
    f.locator("#mapping-select").select_option(str(COPY_ID))
    f.locator("#mapping-status", has_text="DRAFT").wait_for(timeout=30000)
    f.locator("#submit-mapping").click()
    f.locator("#mapping-status", has_text="SUBMITTED").wait_for(timeout=30000)
    act_as(page, "guide.steward")
    f.locator("#approve-mapping").click()
    f.locator("#mapping-status", has_text="APPROVED").wait_for(timeout=30000)
    f.locator("#run-pipeline").click()
    page.locator('#tabstrip .tab[aria-selected="true"]', has_text="Governance").wait_for(timeout=600000)
    stage_gate2(g, page)


def stage_gate2(g: Shooter, page: Page) -> None:
    gf = open_governance(page)
    gf.locator("#gate").wait_for(timeout=60000)
    gf.locator("#defects").wait_for(timeout=60000)
    settle(page, 2500)
    print("  gate:", gf.locator("#gate").inner_text().replace(chr(10), " | ")[:300], flush=True)
    g.shot(page, "35_gate_v2")
    gf.locator("#defects").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 700)
    g.shot(page, "36_defects_v2")


def stage_dups(g: Shooter, page: Page) -> None:
    open_governance(page)
    page.get_by_role("button", name="Duplicates", exact=True).click()
    page.locator("#tabstrip .tab", has_text="Duplicates").wait_for()
    df = frame(page)
    df.locator("#summary").wait_for(timeout=60000)
    settle(page, 2500)
    g.shot(page, "37_duplicates")
    df.locator("#merges").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 800)
    g.shot(page, "38_duplicate_merges")
    df.locator("#possible").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 800)
    g.shot(page, "39_duplicate_review")


def stage_signoff(g: Shooter, page: Page) -> None:
    gf = open_governance(page)
    act_as(page, "guide.steward")
    g.callouts(page, [("1", gf.locator("#gate")), ("2", gf.locator("#sign-off")), ("3", gf.locator("#load-mode")), ("4", gf.locator("#load")), ("5", page.locator("#actor"))])
    g.shot(page, "40_gate_buttons")
    g.clear(page)
    gf.locator("#sign-off").click()
    gf.locator("#toast.show", has_text="signed off by").wait_for(timeout=30000)
    settle(page, 1500)
    g.shot(page, "41_signed_off")


def pg_settings() -> dict[str, str]:
    from urllib.parse import urlparse
    dsn = ""
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            dsn = line.split("=", 1)[1].strip()
    u = urlparse(dsn.replace("postgresql+psycopg2", "postgresql"))
    return {"host": u.hostname or "localhost", "port": str(u.port or 5432), "user": u.username or "", "password": u.password or ""}


TARGET_DB, TARGET_NAME, TARGET_TABLE = "guide_target2", "NEW_ERP_DATABASE", "BP_CUSTOMERS"


def stage_target(g: Shooter, page: Page) -> None:
    pg = pg_settings()
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    settle(page, 1200)
    page.get_by_role("button", name="New Target").click()
    page.locator("#wizard[open]").wait_for()
    page.select_option("#g-type", "db")
    page.fill("#g-name", TARGET_NAME)
    settle(page, 600)
    g.shot(page, "42_target_kind")
    page.locator("#wz-next").click()
    page.fill("#c-host", pg["host"])
    page.fill("#c-port", pg["port"])
    page.fill("#c-database", TARGET_DB)
    page.fill("#c-user", pg["user"])
    page.fill("#c-password", pg["password"])
    page.locator("#c-test").click()
    page.locator("#c-status.ok").wait_for(timeout=60000)
    settle(page, 700)
    g.shot(page, "43_target_connection")
    page.locator("#wz-next").click()
    page.locator("#t-schema option", has_text="public").wait_for(state="attached")
    page.select_option("#t-schema", "public")
    page.locator("#t-test").click()
    page.locator("#t-status.ok").wait_for(timeout=60000)
    settle(page, 700)
    g.shot(page, "44_target_write_test")
    page.locator("#wz-next").click()
    page.locator("#wz-next").click()
    settle(page, 500)
    g.shot(page, "45_target_summary")
    page.locator("#wz-next").click()
    page.locator("#wizard[open]").wait_for(state="detached", timeout=60000)
    settle(page, 1000)


def stage_deliver(g: Shooter, page: Page) -> None:
    gf = open_governance(page)
    act_as(page, "guide.steward")
    gf.locator("#db-conn option", has_text=TARGET_NAME).wait_for(state="attached", timeout=30000)
    gf.locator("#db-conn").select_option(label=gf.locator("#db-conn option", has_text=TARGET_NAME).first.inner_text())
    gf.locator("#db-table").fill(TARGET_TABLE)
    gf.locator("#db-preview").click()
    gf.locator("#db-ddl:not(.hidden)").wait_for(timeout=60000)
    settle(page, 1200)
    gf.locator("#db-delivery").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 500)
    g.shot(page, "46_delivery_preview")
    gf.locator("#db-load").click()
    gf.locator("#db-history table").wait_for(timeout=600000)
    settle(page, 1500)
    gf.locator("#db-delivery").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 500)
    g.shot(page, "47_delivered")
    gf.locator(".db-verify").first.click()
    gf.locator("#toast.show", has_text="identical").wait_for(timeout=300000)
    settle(page, 1500)
    g.shot(page, "48_verified")
    gf.locator("#load-mode").select_option("sap:sandbox")      # the other way out: the SAP stand-in gateway
    gf.locator("#load").click()
    gf.locator("#toast.show", has_text="SAP load").wait_for(timeout=300000)
    settle(page, 1200)
    g.shot(page, "49_sap_sandbox_load")


def stage_cert(g: Shooter, page: Page) -> None:
    open_governance(page)
    page.get_by_role("button", name="Certificate", exact=True).click()
    page.locator("#tabstrip .tab", has_text="Certificate").wait_for()
    cf = frame(page)
    settle(page, 3000)
    g.shot(page, "50_certificate")
    cf.locator("body").evaluate("el => window.scrollTo(0, 900)")
    settle(page, 800)
    g.shot(page, "51_certificate_more")
    cf.locator("body").evaluate("el => window.scrollTo(0, 99999)")
    settle(page, 800)
    g.shot(page, "52_certificate_end")


def stage_extras(g: Shooter, page: Page) -> None:
    page.goto(BASE + "/")
    page.locator("#tree .node").first.wait_for()
    settle(page, 1200)
    for button, shot in (("Dashboard", "53_dashboard"), ("Tasks", "54_tasks"), ("Compliance", "55_compliance"), ("Help", "56_help")):
        try:
            page.get_by_role("button", name=button, exact=True).click()
            settle(page, 3500)
            g.shot(page, shot)
        except Exception as e:  # noqa: BLE001 - an extra picture, not the story
            print(f"  ({shot} skipped: {str(e)[:80]})", flush=True)
    stage_assistant(g, page)


def stage_assistant(g: Shooter, page: Page) -> None:
    gf = open_governance(page)
    gf.locator("#ask-question").fill("Which records were left out, and why?")
    gf.locator("#ask-go").click()
    for _ in range(120):                                  # the answer replaces the "Looking it up" note
        text = gf.locator("#ask-result").inner_text()
        if text.strip() and "Looking it up" not in text:
            break
        page.wait_for_timeout(1000)
    settle(page, 2000)
    gf.locator("#assistant").evaluate("el => el.scrollIntoView({block: 'start'})")
    settle(page, 500)
    g.shot(page, "57_assistant_answer")


def stage_rollback(g: Shooter, page: Page) -> None:
    gf = open_governance(page)
    act_as(page, "guide.steward")
    gf.locator("#db-delivery").evaluate("el => el.scrollIntoView({block: 'start'})")
    gf.locator(".db-rollback").first.click()
    gf.locator("#db-history .badge", has_text="rolled back").first.wait_for(timeout=300000)
    settle(page, 1500)
    g.shot(page, "58_rolled_back")


STAGES = {"import": stage_import, "quality": stage_quality, "ai": stage_ai, "manual": stage_manual, "preflight": stage_preflight, "run": stage_run, "fix": stage_fix, "v2": stage_v2, "run2": stage_run2, "gate2": stage_gate2, "dups": stage_dups, "signoff": stage_signoff, "target": stage_target,
          "deliver": stage_deliver, "cert": stage_cert, "extras": stage_extras, "assistant": stage_assistant, "rollback": stage_rollback}

if __name__ == "__main__":
    shooter = Shooter()
    with sync_playwright() as p:
        browser, page = shooter.open(p)
        for stage in args.stage.split(","):
            print(f"== stage {stage}", flush=True)
            STAGES[stage](shooter, page)
        browser.close()
    print(f"{len(shooter.taken)} screenshots in {OUT}")
