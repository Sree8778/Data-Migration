# How well does the AI work? An evaluation against known answers

Written for the product owner and whoever maintains the AI parts. It records what was measured, what was wrong, what was changed, and what this does *not* prove.

## What was measured

`scripts/evaluate_ai.py` runs against a throwaway copy of the application (own database and workspace, never your data) and scores the AI against answers written in the
script. It can be rerun at any time:

    python scripts/evaluate_ai.py --base http://127.0.0.1:8768 --legacy path/to/messy_customers.csv

| Area | How it is scored |
|---|---|
| **Setup guide** (no model) | 24 plain-language phrasings: the connection details it should pick out, and the ones it must leave alone (questions, passwords, a hostile `; DROP TABLE`); 6 cases of "should it question this value" |
| **AI mapping** (Gemini) | Which SAP field it chooses for 17 tidy legacy columns and 15 cryptic SAP-style ones (KUNNR, SPRAS, KTOKD...): correct / wrong / missed |
| **Lookup drafts** (Gemini) | The translations for 42 messy country spellings and 6 payment terms. What matters most: *nothing wrong may be pre-ticked as certain* |
| **Assistant** (Gemini) | The valid and rejected counts, the biggest cause, and refusing a prompt injection, an off-topic question and SQL |
| **Fixes** | Every proposal is about a real defect; no constant for customer data; every written entry is right; the result improves as the fix says |

## What was wrong, and what changed

| Finding | Effect before | Change |
|---|---|---|
| The guide acted on questions: "what port does oracle use?" chose Oracle | The wizard switched provider on a question | A question is not an instruction; only explicit `host=...` forms count inside one |
| "which account should I use" filled the user as "should"; `db=sales` after `host=db1` read as database "1"; "users" gave user "s" | Wrong values in the form | Keywords must end where the word ends; verbs are never values; "is called X" is understood; "localhost" is read |
| 4 of 32 columns left unmapped (the partner key, language, account group) | Missing rules | The SAP and everyday aliases (KUNNR, SPRAS, KTOKD, STCEG...) are now part of each field's description |
| After the better descriptions: a customer extract got **supplier** fields (`LFM1.WAERS`, `LFB1.ZTERM`) | 2 wrong fields in SAP | The mapper decides whether the source is a customer or supplier table and does not offer the other side's tables at all; a "Source is" selector overrides it |
| `2/10 NET 30` -> `NT3D` pre-ticked as certain | A guessed meaning loaded as fact | Anything whose meaning is defined in the target system (cash-discount terms) is proposed but never pre-ticked |

Each is covered by a regression test in `tests/test_ai_quality.py`.

## Result of the last run

* guide: 30 of 30 · mapping: 32 of 32 columns correct (0 wrong, 0 missed) · country lookups: 34 pre-ticked and right, 0 pre-ticked and wrong, 5 junk values correctly left for a person ·
  payment terms: 4 right, 0 wrong, 2 left for a person (the discount term, and "Net 45", which SAP has no key for) · assistant: every figure and refusal right ·
  fixes: every proposal about a real defect, no constant offered for customer data, every written entry right.
* 59 of 59 checks; nothing that would have damaged data.

## What this does not show

* **One model, one dataset.** Gemini (flash) on one 21,460-row customer extract and one small cryptic file. A different language, a different legacy system, or the local Ollama model
  (not evaluated here; it is slow on CPU) can behave differently. Run the script on your own extract before trusting a mapping.
* **The answer key is a person's judgement** (mine). Where the right answer is genuinely arguable, the script prints the case ("judge by hand") instead of scoring it.
* **Small samples.** Seventeen and fifteen columns, two lookup columns, five assistant questions. A 100% here is "no fault found", not "no fault exists".
* **SAP itself.** Whether `NT30` or `1000` exists in *your* SAP system is not known to the application: the key lists are built in. The checks against your real system happen only when you load.
* **Free-text answers** (the wording of the assistant) are checked only for figures; the sentences themselves are not graded.

## Second evaluation: the AI against a person's mapping on three file shapes (Gemini, `scripts/compare_ai_vs_manual.py`)

Three files, scored against a person's answer key: the English practice file (10 columns), a German SAP-style file (`KUNDE_NR`, `FIRMENNAME`, `STRASSE`, `PLZ` ...; 14 scored columns) and a Firestore-style file with nested names (`address.city`, `contact.email`; 10 columns).

| | Field right | Wrong | Missed |
|---|---|---|---|
| Before the fixes | 31 of 34 | 0 | 3 (German file) |
| After the fixes | 34 of 34 | 0 | 0 |

What was found, and what changed:

| Finding | Change |
|---|---|
| `FIRMENNAME` (company name), `ZUSATZ` (name line 2) and `UST_IDNR` (VAT number) got no suggestion: the retrieval shortlist never contained `NAME_ORG1`, `NAME_ORG2` or `TAXNUM`, so the model could only say "no match" and the most important mandatory field stayed unmapped | German SAP-style words are spelled out beside the column name in the retrieval query (`ABBREVIATIONS`), and the field descriptions carry the German names |
| `PLZ` was proposed as `LPAD(CAST(PLZ AS VARCHAR), 5, '0')` with a confident reason and no review flag: it would turn the valid 4-digit codes of Austria and Switzerland (`1010`, `8001`) into wrong codes | An AI expression that pads, replaces or translates characters is now flagged "needs review" with the reason; a plain cast, trim, case change or cut is not |

Regression tests: `tests/test_ai_quality.py` (12 German column names must have the right field among the candidates; which expressions count as changing the values; the padded postal code is flagged, a cast is not).

Still true: one model, small samples (about 400 rows for the German and Firestore-style files), answer key written by a person; the local Ollama model was only tried on the English file (9 of 10, `COUNTRY` sent to the bank-country field, caught by the review flag).
