"""The flow charts of docs/user-guide, drawn as SVG by a tiny layout helper (boxes, diamonds, arrows). Every chart is a function returning an <svg> string, so the guide builder can paste
them into the page. Colours: blue = something the application does, green = a good ending, amber = a person must look/decide, red = stop / refused, grey = the outside world."""

from __future__ import annotations

from html import escape

KINDS = {  # fill, stroke, text
    "step": ("#e8f0fe", "#3b6fd4", "#13294b"),
    "person": ("#fff4d6", "#d49a1a", "#4a3306"),
    "good": ("#e1f5e6", "#2e9d4f", "#0f3d1e"),
    "bad": ("#fde4e4", "#d23b3b", "#5a1010"),
    "world": ("#eef0f3", "#8a93a0", "#2b3138"),
    "ai": ("#efe6fb", "#7a4fd1", "#2e1a57"),
}


class Chart:
    def __init__(self, width: int, height: int, title: str) -> None:
        self.w, self.h, self.title = width, height, title
        self.parts: list[str] = []
        self.nodes: dict[str, tuple[float, float, float, float, str]] = {}

    # ---------------------------------------------------------------- nodes
    def _text(self, cx: float, cy: float, text: str, color: str, size: int = 15, bold_first: bool = True) -> str:
        lines = text.split("\n")
        lh = size + 3
        y0 = cy - (len(lines) - 1) * lh / 2 + size * 0.35
        out = []
        for i, line in enumerate(lines):
            weight = "700" if (i == 0 and bold_first) else "400"
            out.append(f'<text x="{cx}" y="{y0 + i * lh:.1f}" text-anchor="middle" font-size="{size if i == 0 else size - 2}" font-weight="{weight}" fill="{color}">{escape(line)}</text>')
        return "".join(out)

    def box(self, key: str, x: float, y: float, w: float, h: float, text: str, kind: str = "step", size: int = 15) -> None:
        fill, stroke, color = KINDS[kind]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}" stroke-width="2"/>' + self._text(x + w / 2, y + h / 2, text, color, size))
        self.nodes[key] = (x, y, w, h, "box")

    def diamond(self, key: str, cx: float, cy: float, w: float, h: float, text: str, kind: str = "person", size: int = 14) -> None:
        fill, stroke, color = KINDS[kind]
        pts = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
        self.parts.append(f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>' + self._text(cx, cy, text, color, size))
        self.nodes[key] = (cx - w / 2, cy - h / 2, w, h, "diamond")

    def label(self, x: float, y: float, text: str, size: int = 13, color: str = "#4a5565", anchor: str = "middle", bold: bool = False) -> None:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            self.parts.append(f'<text x="{x}" y="{y + i * (size + 3)}" text-anchor="{anchor}" font-size="{size}" fill="{color}"{" font-weight=\"700\"" if bold else ""}>{escape(line)}</text>')

    def band(self, x: float, y: float, w: float, h: float, text: str) -> None:
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="#f7f8fa" stroke="#d5dae1" stroke-width="1.5" stroke-dasharray="6 5"/>')
        self.label(x + 14, y + 22, text, 13, "#5b6573", "start", True)

    # ---------------------------------------------------------------- arrows
    def anchor(self, key: str, side: str) -> tuple[float, float]:
        x, y, w, h, _ = self.nodes[key]
        return {"left": (x, y + h / 2), "right": (x + w, y + h / 2), "top": (x + w / 2, y), "bottom": (x + w / 2, y + h)}[side]

    def arrow(self, a: str, sa: str, b: str, sb: str, text: str = "", color: str = "#55606e", via: str = "auto", dash: bool = False, lx: float = 0, ly: float = 0) -> None:
        (x1, y1), (x2, y2) = self.anchor(a, sa), self.anchor(b, sb)
        if x1 == x2 or y1 == y2:
            d = f"M{x1},{y1} L{x2},{y2}"
        elif via == "h":  # horizontal first
            d = f"M{x1},{y1} L{x2},{y1} L{x2},{y2}"
        elif via == "v":  # vertical first
            d = f"M{x1},{y1} L{x1},{y2} L{x2},{y2}"
        elif sa in ("left", "right"):
            mx = (x1 + x2) / 2
            d = f"M{x1},{y1} L{mx},{y1} L{mx},{y2} L{x2},{y2}"
        else:
            my = (y1 + y2) / 2
            d = f"M{x1},{y1} L{x1},{my} L{x2},{my} L{x2},{y2}"
        self.parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2"{" stroke-dasharray=\"7 5\"" if dash else ""} marker-end="url(#ah{color[1:]})"/>')
        if text:
            self.label((x1 + x2) / 2 + lx, (y1 + y2) / 2 - 7 + ly, text, 13, color)

    def path(self, d: str, text: str = "", tx: float = 0, ty: float = 0, color: str = "#55606e", dash: bool = False) -> None:
        self.parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2"{" stroke-dasharray=\"7 5\"" if dash else ""} marker-end="url(#ah{color[1:]})"/>')
        if text:
            self.label(tx, ty, text, 13, color)

    # ---------------------------------------------------------------- output
    def svg(self) -> str:
        colours = {"55606e", "d23b3b", "2e9d4f", "d49a1a", "3b6fd4"}
        defs = "".join(f'<marker id="ah{c}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#{c}"/></marker>' for c in colours)
        return (f'<svg viewBox="0 0 {self.w} {self.h}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{escape(self.title)}" font-family="Segoe UI, Helvetica, Arial, sans-serif">'
                f'<defs>{defs}</defs>' + "".join(self.parts) + "</svg>")


R, G, A, B = "#d23b3b", "#2e9d4f", "#d49a1a", "#3b6fd4"


# ====================================================================== 1. the big picture
def big_picture() -> str:
    c = Chart(1000, 470, "The whole journey: from the old system to the new one")
    c.box("old", 20, 30, 170, 80, "Old system\nyour customer list", "world")
    c.box("land", 230, 30, 170, 80, "1. Bring it in\nan exact photocopy", "step")
    c.box("prof", 440, 30, 170, 80, "2. Look at it\nprofile + quality check", "step")
    c.box("map", 650, 30, 170, 80, "3. Write the rules\nold word → new word", "ai")
    c.box("appr", 840, 30, 140, 80, "4. A second\nperson approves", "person")
    c.arrow("old", "right", "land", "left"); c.arrow("land", "right", "prof", "left"); c.arrow("prof", "right", "map", "left"); c.arrow("map", "right", "appr", "left")

    c.box("run", 840, 190, 140, 80, "5. Run\nconvert + clean", "step")
    c.arrow("appr", "bottom", "run", "top")
    c.box("gate", 640, 190, 150, 80, "6. The gate\nanything wrong?", "person")
    c.arrow("run", "left", "gate", "right")
    c.box("fix", 440, 190, 150, 80, "7. Fix it\nand run again", "bad")
    c.arrow("gate", "left", "fix", "right", "wrong", R)
    c.path("M515,190 L515,150 L735,150 L735,190", "", 0, 0, R, True)
    c.label(625, 143, "back to step 3 or 5", 12, R)
    c.box("sign", 230, 190, 170, 80, "9. A person\nsigns off", "person")
    c.arrow("gate", "bottom", "sign", "bottom", "", G, via="v")
    c.path("M715,270 L715,310 L315,310 L315,270", "", 0, 0, G)
    c.label(520, 304, "all clear", 13, G)
    c.box("del", 20, 190, 170, 80, "10. Deliver\nput it in the new system", "step")
    c.arrow("sign", "left", "del", "right")
    c.box("chk", 20, 350, 170, 80, "Double-check\ncount + compare values", "step")
    c.arrow("del", "bottom", "chk", "top")
    c.box("cert", 260, 350, 220, 80, "11. Certificate\nthe receipt that proves it", "good")
    c.arrow("chk", "right", "cert", "left")
    c.box("new", 540, 350, 190, 80, "New system\nSAP S/4HANA (or a\ndatabase / Firebase)", "world")
    c.arrow("cert", "right", "new", "left", "", "#55606e", dash=True)
    c.label(880, 400, "Blue = the application works\nAmber = a person decides\nPurple = AI helps (a person checks)", 12, "#5b6573", "middle")
    return c.svg()


# ====================================================================== 2. landing
def landing() -> str:
    c = Chart(960, 250, "What happens when you bring a file in")
    c.box("file", 20, 85, 150, 80, "Your file\n(CSV, Excel …)\nor a database", "world")
    c.box("copy", 220, 85, 170, 80, "Read every row\nexactly as written", "step")
    c.box("snap", 440, 85, 190, 80, "Snapshot\none locked copy, all\nvalues kept as text", "good")
    c.box("count", 680, 20, 250, 70, "Count the rows\nmatches the number you\nexpected?", "person", 14)
    c.box("fp", 680, 150, 250, 80, "Fingerprint\na short code that changes\nif even one letter changes", "step", 14)
    c.arrow("file", "right", "copy", "left"); c.arrow("copy", "right", "snap", "left")
    c.arrow("snap", "right", "count", "left"); c.arrow("snap", "right", "fp", "left")
    c.label(330, 215, "The original is never changed. If anything later looks wrong you can always\ncompare with this untouched copy.", 13, "#4a5565")
    return c.svg()


# ====================================================================== 3. choosing a rule type
def rule_types() -> str:
    c = Chart(960, 560, "How to choose the kind of rule for one new-system field")
    c.box("q0", 340, 10, 280, 56, "One field of the new system\nwhat should go in it?", "person")
    c.diamond("d1", 480, 130, 330, 80, "Is there an old column with\nexactly the right value?", "person")
    c.arrow("q0", "bottom", "d1", "top")
    c.box("copy", 20, 105, 230, 100, "COPY the column\nDIRECT_COPY\nexample: NAME_ORG1 = CUST_NAME", "good", 14)
    c.arrow("d1", "left", "copy", "right", "yes", G)
    c.diamond("d2", 480, 260, 330, 80, "Is it the same fixed word\nfor every customer?", "person")
    c.arrow("d1", "bottom", "d2", "top", "no", R)
    c.box("static", 710, 215, 230, 100, "FIXED value\nSTATIC_VALUE\nexample: BU_GROUP = Z001", "good", 14)
    c.arrow("d2", "right", "static", "left", "yes", G)
    c.diamond("d3", 480, 390, 330, 80, "Does the old column use\nother spellings (USA, usa…)?", "person")
    c.arrow("d2", "bottom", "d3", "top", "no", R)
    c.box("map", 20, 340, 230, 100, "TRANSLATE with a table\nVALUE_MAPPING\nexample: usa, US → US", "good", 14)
    c.arrow("d3", "left", "map", "right", "yes", G)
    c.box("sql", 270, 480, 420, 70, "CALCULATE a formula\nSQL_EXPRESSION   example: upper(left(trim(CUST_NAME),20))", "good", 13)
    c.arrow("d3", "bottom", "sql", "top", "no, it needs a little maths", R)
    c.label(840, 380, "Not sure? Ask the AI to propose\nthe rules, then read each one.\nYou are the boss; it only suggests.", 13, "#5b6573")
    return c.svg()


# ====================================================================== 4. what a run does
def run_pipeline() -> str:
    c = Chart(1000, 430, "What happens to every row during the run")
    c.box("rows", 20, 170, 150, 80, "1,980 rows\nfrom the snapshot", "world")
    c.box("excl", 215, 170, 150, 80, "Leave-out rules\nrows you chose to skip", "person")
    c.box("conv", 410, 170, 150, 80, "Apply your rules\nconvert each field", "step")
    c.box("dedup", 605, 170, 150, 80, "Find twins\nduplicates", "step")
    c.box("val", 800, 170, 170, 80, "Check against SAP\nrules (length, needed…)", "step")
    c.arrow("rows", "right", "excl", "left"); c.arrow("excl", "right", "conv", "left"); c.arrow("conv", "right", "dedup", "left"); c.arrow("dedup", "right", "val", "left")
    c.box("o1", 215, 310, 150, 80, "Left out\nwith the reason", "world")
    c.box("o2", 605, 310, 150, 80, "Merged twins\nkept once, rest listed", "world")
    c.box("o3", 800, 310, 170, 80, "Rejected\nwith the reason", "bad")
    c.box("ok", 410, 30, 360, 70, "Valid rows → these are what gets delivered", "good")
    c.arrow("excl", "bottom", "o1", "top"); c.arrow("dedup", "bottom", "o2", "top"); c.arrow("val", "bottom", "o3", "top", "", R)
    c.arrow("val", "top", "ok", "right", "", G, via="v")
    c.label(500, 415, "The check:  left out + merged + rejected + valid  =  every row you started with.  Nothing can vanish quietly.", 13, "#13294b", bold=True)
    return c.svg()


# ====================================================================== 5. duplicates
def duplicates() -> str:
    c = Chart(1000, 520, "How the application decides two customers are the same company")
    c.box("a", 20, 20, 170, 60, "Two customer rows", "world")
    c.diamond("d1", 330, 50, 230, 80, "Same tax number\nor e-mail or phone?", "person", 13)
    c.arrow("a", "right", "d1", "left")
    c.box("exact", 470, 20, 230, 60, "Exact match\nvery likely the same", "step")
    c.arrow("d1", "right", "exact", "left", "yes", G)
    c.diamond("d2", 330, 175, 230, 80, "Names + addresses\nalmost the same?", "person", 13)
    c.arrow("d1", "bottom", "d2", "top", "no", R)
    c.box("fuzzy", 470, 145, 230, 60, "Fuzzy match\nscore 0–100", "step")
    c.arrow("d2", "right", "fuzzy", "left", "yes", G)
    c.box("none", 20, 145, 170, 60, "Different customers\nboth kept", "good")
    c.arrow("d2", "left", "none", "right", "no", R)
    c.box("safe", 330, 270, 340, 90, "Safety checks (they can only say NO)\n• accents and ß/ss ignored   • very common name → a person looks\n• street clearly different → a person looks\n• two different tax numbers → never merge", "person", 13)
    c.path("M700,50 L715,50 L715,315 L670,315")
    c.path("M585,205 L585,270", "", 0, 0, "#55606e")
    c.box("m1", 40, 410, 260, 80, "High score, passes the checks\nmerge: keep one, list the rest", "good", 14)
    c.box("m2", 370, 410, 260, 80, "Middle score or doubt\nsent to a person to decide", "person", 14)
    c.box("m3", 700, 410, 260, 80, "Tax numbers differ\nnever merged (veto)", "bad", 14)
    c.arrow("safe", "bottom", "m1", "top", "", G, via="v"); c.arrow("safe", "bottom", "m2", "top"); c.arrow("safe", "bottom", "m3", "top", "", R, via="v")
    c.label(865, 100, "Why 'blocking'? Comparing every row with\nevery other would take forever (1 million\nrows = 500 billion pairs). So rows are\nonly compared with others that share\na clue: same name words, same street…", 13, "#5b6573")
    return c.svg()


# ====================================================================== 6. people and the gate
def roles_and_gate() -> str:
    c = Chart(1000, 520, "Who may do what, and the gate that protects the new system")
    c.band(10, 10, 300, 500, "THE AUTHOR  (e.g. Alice)")
    c.band(330, 10, 300, 500, "THE APPROVER  (a different person)")
    c.band(650, 10, 340, 500, "THE APPLICATION")
    c.box("w", 40, 50, 240, 64, "Writes or generates\nthe rules", "ai")
    c.box("s", 40, 150, 240, 64, "Presses 'Submit\nfor approval'", "step")
    c.arrow("w", "bottom", "s", "top")
    c.box("ap", 360, 150, 240, 64, "Reads the rules\npresses 'Approve'", "person")
    c.arrow("s", "right", "ap", "left")
    c.box("lock", 680, 150, 280, 64, "Rules are now locked\nnobody can edit them", "good")
    c.arrow("ap", "right", "lock", "left")
    c.box("run", 680, 250, 280, 64, "Run the pipeline\nconvert, clean, find twins", "step")
    c.arrow("lock", "bottom", "run", "top")
    c.diamond("gate", 820, 380, 260, 100, "GATE: any\nblocking defects?", "person", 14)
    c.arrow("run", "bottom", "gate", "top")
    c.box("fixit", 40, 348, 240, 64, "Fix: change rules,\nleave rows out → new version", "bad")
    c.arrow("gate", "left", "fixit", "right", "BLOCKED: defects found", R, ly=-2)
    c.box("ready", 740, 446, 160, 44, "READY", "good", 14)
    c.arrow("gate", "bottom", "ready", "top", "", G)
    c.box("so", 360, 436, 240, 64, "Reads the proof\npresses 'Sign off'", "person")
    c.arrow("ready", "left", "so", "right", "no defects", G)
    c.label(160, 300, "Author can never approve\nor sign off their own work.", 13, R)
    return c.svg()


# ====================================================================== 7. delivery
def delivery() -> str:
    c = Chart(1000, 410, "Delivery: write, check, and undo if anything is wrong")
    c.box("pre", 20, 40, 170, 80, "Preview\nwhat will be written,\nsafety checks", "step")
    c.box("go", 230, 40, 170, 80, "Write the rows\nin small batches", "step")
    c.box("chk", 440, 40, 170, 80, "Check afterwards\nrow count + values\nmatch the source", "step")
    c.diamond("ok", 710, 80, 180, 100, "Same?", "person")
    c.arrow("pre", "right", "go", "left"); c.arrow("go", "right", "chk", "left"); c.arrow("chk", "right", "ok", "left")
    c.box("cert", 840, 40, 140, 80, "Certificate\nissued", "good")
    c.arrow("ok", "right", "cert", "left", "yes", G)
    c.box("undo", 580, 210, 260, 80, "Take back what was written\nthis run only, nothing else", "bad")
    c.arrow("ok", "bottom", "undo", "top", "no", R)
    c.box("rb", 20, 210, 400, 80, "Later regret? Roll back removes this run's rows\n(each row is tagged with the run number)", "person", 14)
    c.path("M910,120 L910,335 L220,335 L220,290", "later", 560, 328, "#55606e", True)
    c.label(500, 385, "Create-only: the application never overwrites or merges into existing data.\nIf something with the same key already exists, it stops and tells you.", 13, "#4a5565")
    return c.svg()


# ====================================================================== 8. architecture (the manual)
def architecture() -> str:
    c = Chart(1000, 440, "Where the data comes from, what the application does with it, and where it goes")
    c.band(10, 10, 250, 420, "SOURCES (read only)")
    c.band(285, 10, 440, 420, "THE APPLICATION (one server)")
    c.band(750, 10, 240, 420, "TARGETS AND EVIDENCE")
    for i, (key, text) in enumerate([("f", "Files\nCSV · Excel · JSON ·\nfixed-width · Parquet"), ("d", "Databases\nOracle · SQL Server ·\nPostgreSQL · MySQL"),
                                     ("o", "Document stores\nFirestore · MongoDB"), ("s", "Cloud storage\nAzure Blob · S3 · SFTP")]):
        c.box(key, 30, 45 + i * 92, 210, 74, text, "world", 14)
    c.box("land", 305, 45, 190, 74, "Landing\nlocked snapshot, row count\nand fingerprint", "good", 14)
    c.box("cat", 515, 45, 190, 74, "Catalogue (PostgreSQL)\nmappings, runs, approvals,\naudit log", "step", 14)
    c.box("work", 305, 160, 400, 100, "The work steps\nprofile · quality · mapping · exclusions · approval ·\nrun · gate · sign-off · deliver · certificate", "step", 15)
    c.box("help", 305, 300, 400, 100, "Helpers\nAI mapping (Gemini online or a local model) ·\nAssistant (answers from the run's own records) · Setup guide", "ai", 14)
    for k in ("f", "d", "o", "s"):
        c.arrow(k, "right", "land", "left")
    c.arrow("land", "bottom", "work", "top"); c.arrow("cat", "bottom", "work", "top")
    c.arrow("help", "top", "work", "bottom", "", "#7a4fd1", dash=True)
    c.box("sap", 770, 45, 200, 74, "SAP S/4HANA\nBusiness Partner (OData)", "world", 14)
    c.box("db", 770, 137, 200, 74, "A database\nPostgreSQL · SQL Server ·\nOracle · MySQL · Firestore", "world", 14)
    c.box("ev", 770, 229, 200, 74, "Evidence\ncertificate · audit log ·\ndelivery history", "good", 14)
    c.box("pk", 770, 321, 200, 74, "Content packs\napproved rules to reuse\n(no customer values)", "step", 14)
    for k in ("sap", "db", "ev", "pk"):
        c.arrow("work", "right", k, "left")
    return c.svg()


# ====================================================================== 9. AI mapping
def ai_mapping() -> str:
    c = Chart(1000, 330, "How the AI proposes rules, and who checks them")
    c.box("col", 20, 30, 170, 80, "Legacy column\nCUST_NM, FIRMENNAME …", "world")
    c.box("find", 230, 30, 200, 80, "1. Find candidates\nsearch the SAP catalogue\nby meaning: keep 8", "step")
    c.box("ai", 470, 30, 200, 80, "2. The AI chooses\nonly among the 8; writes\na rule + a short reason", "ai")
    c.box("chk", 710, 30, 270, 80, "3. Safety checks\nSQL parsed; only allowed functions;\nrisky rules marked NEEDS REVIEW", "step", 14)
    c.arrow("col", "right", "find", "left"); c.arrow("find", "right", "ai", "left"); c.arrow("ai", "right", "chk", "left")
    c.box("rev", 710, 200, 270, 80, "4. A person reviews\nkeeps, edits or deletes\neach proposed rule", "person")
    c.arrow("chk", "bottom", "rev", "top")
    c.box("sub", 470, 200, 200, 80, "5. Submit for approval\nthe author presses it", "step")
    c.box("app", 230, 200, 200, 80, "6. A second person\napproves: rules locked", "person")
    c.box("go", 20, 200, 170, 80, "Ready to run", "good")
    c.arrow("rev", "left", "sub", "right"); c.arrow("sub", "left", "app", "right"); c.arrow("app", "left", "go", "right", "", G)
    c.label(500, 312, "Nothing the AI writes takes effect until a person has read it and a second person has approved it.", 13, "#4a5565")
    return c.svg()


# ====================================================================== 10. life of a mapping set
def mapping_lifecycle() -> str:
    c = Chart(1000, 300, "The life of a set of rules (a mapping set)")
    c.box("dr", 20, 50, 190, 80, "DRAFT\nanyone may edit", "step")
    c.box("su", 270, 50, 190, 80, "SUBMITTED\nwaiting for a second person", "person", 14)
    c.box("ap", 520, 50, 190, 80, "APPROVED\nlocked: cannot be edited", "good")
    c.box("ru", 770, 50, 210, 80, "RUNS\nuse exactly these rules", "step")
    c.arrow("dr", "right", "su", "left", "submit"); c.arrow("su", "right", "ap", "left", "approve"); c.arrow("ap", "right", "ru", "left")
    c.box("cp", 520, 190, 190, 70, "New editable copy\nversion + 1", "bad")
    c.arrow("ap", "bottom", "cp", "top", "needs a change?", R, lx=56)
    c.path("M520,225 L115,225 L115,130", "back to draft", 300, 217, R)
    c.label(500, 285, "The person who submitted can never approve; the approver can never be the author.", 13, "#4a5565")
    return c.svg()


# ====================================================================== 11. combine tables
def combine_flow() -> str:
    c = Chart(1000, 400, "Combining tables: preview first, refuse silent duplicates")
    c.box("a", 20, 30, 200, 76, "1. First table\nevery record is kept", "step")
    c.box("b", 260, 30, 220, 76, "2. Add tables to join\nmatch columns, ignoring\nblanks and upper/lower case", "step", 14)
    c.box("f", 520, 30, 200, 76, "3. Filters (optional)\nkeep records where …", "step")
    c.box("p", 760, 30, 220, 76, "4. Preview on my data\nnothing is written yet", "step")
    c.arrow("a", "right", "b", "left"); c.arrow("b", "right", "f", "left"); c.arrow("f", "right", "p", "left")
    c.diamond("q", 870, 215, 230, 110, "Would any record\nrepeat?", "person")
    c.arrow("p", "bottom", "q", "top")
    c.box("no", 470, 175, 240, 80, "Refused\nnothing is created: change the\njoin, or allow repeats on purpose", "bad", 14)
    c.arrow("q", "left", "no", "right", "yes", R)
    c.box("cr", 700, 310, 280, 70, "5. Create the table\nname, system and key columns", "good")
    c.arrow("q", "bottom", "cr", "top", "no", G)
    c.box("use", 380, 310, 260, 70, "A new source table\nprofile, map and run it as usual", "world", 14)
    c.arrow("cr", "left", "use", "right")
    return c.svg()


# ====================================================================== 12. content packs
def packs_flow() -> str:
    c = Chart(1000, 300, "Reusing approved rules in the next migration with a content pack")
    c.box("m", 20, 40, 190, 80, "Approved mapping set\nfrom this migration", "good")
    c.box("e", 250, 40, 210, 80, "Export the pack\nrules, lookup tables,\nleave-out rules", "step")
    c.box("j", 500, 40, 200, 80, "pack.json\nno customer values\ninside", "world")
    c.box("i", 740, 40, 240, 80, "Import into a new table\ndry run first: shows what\nwould fit, writes nothing", "step", 14)
    c.arrow("m", "right", "e", "left"); c.arrow("e", "right", "j", "left"); c.arrow("j", "right", "i", "left")
    c.box("d", 740, 180, 240, 80, "A DRAFT mapping set\nrules whose columns are\nmissing are listed", "step", 14)
    c.box("a", 470, 180, 220, 80, "A different person\nreviews and approves", "person")
    c.box("r", 230, 180, 190, 80, "Run as usual", "good")
    c.arrow("i", "bottom", "d", "top"); c.arrow("d", "left", "a", "right"); c.arrow("a", "left", "r", "right", "", G)
    return c.svg()


# ====================================================================== 13. what to do when the gate blocks
def gate_decision() -> str:
    c = Chart(1000, 425, "The gate is blocked: which remedy fits which defect")
    c.box("g", 20, 165, 170, 80, "Gate: BLOCKED\ndefects were found", "bad")
    c.diamond("q", 320, 205, 200, 110, "What kind of\ndefect is it?", "person")
    c.arrow("g", "right", "q", "left")
    c.box("b1", 470, 30, 260, 80, "Wrong or missing value\nchange the rule, or add the\nvalue to the lookup", "step", 14)
    c.box("b2", 470, 165, 260, 80, "A record that must not move\nleave it out, with a reason\n(the reason is recorded)", "person", 14)
    c.box("b3", 470, 300, 260, 80, "Twin records\nreview the proposed merges\nand keep one survivor", "step", 14)
    c.arrow("q", "top", "b1", "left", via="v"); c.arrow("q", "right", "b2", "left"); c.arrow("q", "bottom", "b3", "left", via="v")
    c.box("e", 780, 165, 200, 80, "New version\nsubmit · approve ·\nrun again", "step")
    c.arrow("b1", "right", "e", "top", via="h"); c.arrow("b2", "right", "e", "left"); c.arrow("b3", "right", "e", "bottom", via="h")
    c.label(500, 408, "Repeat until the gate shows READY, then a person signs off.", 13, "#4a5565")
    return c.svg()


ALL = {"big_picture": big_picture, "landing": landing, "rule_types": rule_types, "run_pipeline": run_pipeline, "duplicates": duplicates, "roles_and_gate": roles_and_gate, "delivery": delivery,
       "architecture": architecture, "ai_mapping": ai_mapping, "mapping_lifecycle": mapping_lifecycle, "combine_flow": combine_flow, "packs_flow": packs_flow, "gate_decision": gate_decision}

if __name__ == "__main__":  # write each chart as a stand-alone file to look at it
    from pathlib import Path
    out = Path(__file__).resolve().parent.parent / "docs" / "user-guide" / "diagrams"
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in ALL.items():
        (out / f"{name}.svg").write_text(fn(), encoding="utf-8")
        print("wrote", name)
