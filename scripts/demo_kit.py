"""The on-screen parts of the live demos, built so that NOTHING covers the application:

* the explanation (step number, what is happening, and the evidence read from outside the application) is shown in a SEPARATE window docked as a column at the RIGHT of the
  screen, not as a bar over the page: the application keeps the full height of the screen and every button stays visible and clickable;
* the element about to be used gets a thin orange OUTLINE (no fill, no overlay), and a small glowing cursor glides to it.

Used by the demo scripts that drive a visible Edge window with Playwright.
"""

from __future__ import annotations

import ctypes
import json
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

CAPTION_WIDTH = 500           # the column at the right of the screen that holds the explanation window

# the cursor is a popover, so it sits in the browser's top layer ABOVE an open dialog (the wizards are modal dialogs and would otherwise hide it)
GLOW_JS = """
(() => {
  if (window.__glowReady) return; window.__glowReady = true;
  const make = () => {
    if (document.getElementById('demo-glow')) return document.getElementById('demo-glow');
    const g = document.createElement('div'); g.id = 'demo-glow';
    if ('popover' in g) g.setAttribute('popover', 'manual');
    const s = g.style;
    s.position = 'fixed'; s.inset = 'auto'; s.left = '-100px'; s.top = '-100px'; s.width = '34px'; s.height = '34px'; s.margin = '-17px 0 0 -17px'; s.padding = '0'; s.border = '0';
    s.overflow = 'visible'; s.borderRadius = '50%'; s.zIndex = '2147483647'; s.pointerEvents = 'none'; s.transition = 'left .55s ease, top .55s ease, transform .15s ease';
    s.background = 'radial-gradient(circle, rgba(255,214,10,.95) 0%, rgba(255,160,0,.55) 45%, rgba(255,120,0,0) 72%)'; s.boxShadow = '0 0 22px 10px rgba(255,190,0,.55)';
    document.documentElement.appendChild(g);
    return g;
  };
  const top = (g) => {                        // keep it above whatever dialog is open now
    if (!g.showPopover) return;
    try {
      const open = g.matches(':popover-open');
      if (!open) g.showPopover();
      else if (document.querySelector('dialog[open]')) { g.hidePopover(); g.showPopover(); }
    } catch (e) { /* a page without popovers keeps the plain fixed element */ }
  };
  window.__glowTo = (x, y) => { const g = make(); top(g); g.style.left = x + 'px'; g.style.top = y + 'px'; };
  window.__glowClick = () => { const g = document.getElementById('demo-glow'); if (!g) return; g.style.transform = 'scale(1.9)'; setTimeout(() => { g.style.transform = 'scale(1)'; }, 180); };
})();
"""

# the person acting is set once, by the top page only: an init script also runs in every iframe (each console tab) and would reset the acting person on every tab load
USER_JS = ("if (window.top === window && !sessionStorage.getItem('demo.started')) { sessionStorage.setItem('demo.started', '1'); "
           "localStorage.setItem('migration.user', 'demo.author'); localStorage.removeItem('migration.console.tabs'); }")

CAPTION_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Demo narration</title><style>
html, body { margin: 0; height: 100%; background: #1e1b4b; color: #fff; font-family: "Segoe UI", system-ui, sans-serif; overflow: hidden; }
#wrap { display: flex; flex-direction: column; gap: 14px; height: 100%; padding: 18px 20px; box-sizing: border-box; }
#step { font-size: 12px; letter-spacing: .1em; text-transform: uppercase; color: #fbbf24; font-weight: 700; }
#title { font-size: 23px; font-weight: 700; line-height: 1.2; margin-top: -8px; }
#detail { font-size: 15px; opacity: .9; line-height: 1.45; }
#evidence { flex: 1; min-height: 0; background: #0f172a; border: 2px solid #fbbf24; border-radius: 8px; padding: 8px 12px; overflow: auto; font: 12.5px/1.45 Consolas, monospace; display: none; }
#evidence h4 { margin: 0 0 6px; color: #fbbf24; font: 700 13px "Segoe UI", sans-serif; }
#evidence div { white-space: pre-wrap; }
</style></head><body><div id="wrap"><div id="step"></div><div id="title"></div><div id="detail"></div><div id="evidence"></div></div>
<script>
window.setCaption = (n, total, text, detail) => { document.getElementById('step').textContent = 'Step ' + n + ' of ' + total; document.getElementById('title').textContent = text; document.getElementById('detail').textContent = detail || ''; document.title = 'Step ' + n + ': ' + text; };
window.setEvidence = (title, lines) => { const e = document.getElementById('evidence'); e.innerHTML = ''; const h = document.createElement('h4'); h.textContent = title; e.appendChild(h);
  for (const l of lines) { const d = document.createElement('div'); d.textContent = l; e.appendChild(d); } e.style.display = 'block'; };
window.clearEvidence = () => { const e = document.getElementById('evidence'); e.style.display = 'none'; e.innerHTML = ''; };
</script></body></html>"""


def screen_size() -> tuple[int, int]:
    """The primary screen in the units Edge's window bounds use."""
    try:
        user32 = ctypes.windll.user32
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    except Exception:  # noqa: BLE001
        return 1536, 960


class Kit:
    def __init__(self, pace: float = 1.0, total_steps: int = 0, stop_after: int = 0, narration: dict[int, dict] | None = None) -> None:
        self.pace, self.total, self.stop_after, self.step_no = pace, total_steps, stop_after, 0
        self.caption: Page | None = None
        # narration: {step: {text, audio, duration, cues}} (see demo_narration.py). A step waits for the previous step's voice to finish, so speech and screen stay together;
        # the moment every step began is kept, for the video that is assembled from the recording.
        self.narration = narration or {}
        self.timeline: list[dict] = []
        self._voice_until = 0.0

    def pause(self, seconds: float = 1.2) -> None:
        time.sleep(seconds * self.pace)

    @staticmethod
    def _place(context: Any, page: Page, left: int, top: int, width: int, height: int) -> None:
        cdp = context.new_cdp_session(page)
        window = cdp.send("Browser.getWindowForTarget")
        cdp.send("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "normal", "left": left, "top": top, "width": width, "height": height}})

    def open(self, playwright: Any, headless: bool):
        """-> (browser, page, console errors). Visible: the application window fills the screen except a column at the right, where the explanation window is docked."""
        browser = playwright.chromium.launch(channel="msedge", headless=headless, slow_mo=0 if headless else int(120 * self.pace))
        errors: list[str] = []
        if headless:
            ctx = browser.new_context(viewport={"width": 1500, "height": 950})
        else:
            width, height = screen_size()
            cap_ctx = browser.new_context(no_viewport=True)                  # the explanation window first, so the application window is the one in front
            self.caption = cap_ctx.new_page()
            self._place(cap_ctx, self.caption, width - CAPTION_WIDTH, 0, CAPTION_WIDTH, height)
            self.caption.set_content(CAPTION_HTML)
            ctx = browser.new_context(no_viewport=True)
        page = ctx.new_page()
        if not headless:
            self._place(ctx, page, 0, 0, width - CAPTION_WIDTH, height)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept("Rehearsal: the demo is finished") if d.type == "prompt" else d.accept())
        page.add_init_script(GLOW_JS)
        page.add_init_script(USER_JS)
        return browser, page, errors

    def say(self, page: Page, text: str, detail: str = "") -> None:
        """The explanation of the step: in the narration window (and printed). With a voice-over the step first waits until the previous step has been spoken."""
        wait = self._voice_until - time.time()
        if wait > 0:
            time.sleep(wait)
        self.step_no += 1
        now = time.time()
        spoken = self.narration.get(self.step_no)
        if spoken:
            self._voice_until = now + spoken["duration"] + 0.7
        self.timeline.append({"step": self.step_no, "epoch": now, "title": text, "detail": detail, **({"spoken": spoken["text"], "audio": spoken["audio"], "duration": spoken["duration"],
                                                                                                         "cues": spoken["cues"]} if spoken else {})})
        print(f"[step {self.step_no}/{self.total}] {text}" + (f" - {detail}" if detail else ""), flush=True)
        if self.caption is not None:
            self.caption.evaluate("([n, total, text, detail]) => window.setCaption(n, total, text, detail)", [self.step_no, self.total, text, detail])
        self.pause(1.8)
        if self.stop_after and self.step_no >= self.stop_after:
            print("stopped after step", self.step_no, flush=True)
            raise SystemExit(0)

    def finish(self) -> None:
        """Wait for the last sentence to be spoken."""
        wait = self._voice_until - time.time()
        if wait > 0:
            time.sleep(wait)

    def write_timeline(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.timeline, indent=1), encoding="utf-8")

    @staticmethod
    def _outline(loc, on: bool) -> None:
        try:
            loc.evaluate("""(el, on) => {
                if (on) { el.__demo = [el.style.outline, el.style.outlineOffset]; el.style.outline = '3px solid #f59e0b'; el.style.outlineOffset = '2px'; }
                else if (el.__demo) { el.style.outline = el.__demo[0]; el.style.outlineOffset = el.__demo[1]; el.__demo = null; }
            }""", on, timeout=2000)
        except Exception:  # noqa: BLE001
            pass

    def point(self, page: Page, loc, seconds: float = 0.8):
        """Glide the glowing cursor to the element, move the real mouse there too (hover states show), and outline the element thinly while it is the subject."""
        try:
            loc.scroll_into_view_if_needed(timeout=5000)
            box = loc.bounding_box()
            if box:
                x, y = box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 24)
                page.evaluate("([x, y]) => window.__glowTo && window.__glowTo(x, y)", [x, y])
                page.mouse.move(x, y, steps=12)
        except Exception:  # noqa: BLE001
            pass
        self._outline(loc, True)
        self.pause(seconds)
        self._outline(loc, False)
        return loc

    def click(self, page: Page, loc, seconds: float = 0.6, **kw) -> None:
        self.point(page, loc, seconds)
        page.evaluate("() => window.__glowClick && window.__glowClick()")
        loc.click(**kw)

    def type_into(self, page: Page, loc, text: str, delay: int = 55) -> None:
        self.point(page, loc, 0.4)
        loc.fill("")
        loc.type(text, delay=int(delay * min(self.pace, 1.5)))

    def act_as(self, page: Page, user: str) -> None:
        box = page.locator("#actor")
        self.point(page, box, 0.4)
        box.fill(user)
        box.press("Tab")
        self.pause(0.6)

    @staticmethod
    def frame(page: Page):
        return page.frame_locator("iframe.active")

    def banner(self, page: Page, title: str, lines: list[str]) -> None:
        """Evidence from outside the application (rows read from the database): in the narration window, never over the page."""
        if self.caption is not None:
            self.caption.evaluate("([title, lines]) => window.setEvidence(title, lines)", [title, lines])
        else:
            print("   evidence:", title, lines[:6], flush=True)

    def clear_banner(self, page: Page) -> None:
        if self.caption is not None:
            self.caption.evaluate("() => window.clearEvidence()")
