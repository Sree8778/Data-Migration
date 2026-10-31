# Migration controls: what the system checks, where it is recorded, what it does not cover

Written for the people who run and audit a migration with this application.

## 1. Before a mapping reaches an approver

| Control | What it does | Where you see it | Setting |
|---|---|---|---|
| **Pinned model** | The AI model that answered is recorded on every rule (`alias -> concrete version` when the vendor reports it). A model alias such as `gemini-flash-latest` is reported as *unpinned*: the vendor can repoint it, so a re-run may differ. | Rule card ("model ..."), pre-submission check | `GEMINI_MODEL` / `OLLAMA_MODEL` to a versioned name |
| **Stability vote** | The model is asked N times per column; the most common answer is kept and a column whose answers differ is flagged *unstable* with the losing answers. A local model at temperature 0 is asked once. | Studio: "AI runs per column", review flags | `MIGRATION_AI_CONSISTENCY_RUNS` (1-5, default 2) |
| **Catalog evidence** | A plain copy is flagged when the source is longer than the SAP field, or its sample values are not keys of the field's check table (country *names* into a field that holds ISO codes). | Review flags on the rule | - |
| **Pre-submission dry run** | The mapping runs on a reproducible sample of the real rows through the real transformation and validation (nothing is written to the run tables). Every rule gets a measured pass rate; lookup rules list the legacy values that have no entry. Submission is refused when a lookup table is empty or a rule fails most of the sample. | Studio: "Check rules on sample"; 422 `PREFLIGHT_BLOCKED` on submit | `MIGRATION_PREFLIGHT` = `enforce` (default) / `warn` / `off` |
| **Placeholders** | Placeholder rules for fields the legacy system never had fail validation by design until Data Construction fills them: reported, never blocking. | Pre-submission check | - |

The AI only proposes. It is not in the per-row data path: the approved rules are plain code.

## 2. Which rows are migrated

**Rows to leave out** (Studio panel): deleted-flag records, test and dummy customers. A rule is a column, an operator (equals, is one of, is blank, starts with, contains, regex), the value(s), and a mandatory reason. The data suggests candidates (a set deletion flag, names that say TEST or DUMMY); nothing is applied until a person accepts it. Rules can only change while the mapping set is a draft.
Excluded rows are removed before the mapping runs and kept in `excluded.parquet` with the rule and reason. The certificate shows them as their own line (`EXCLUDED_BY_RULE`) so extracted = excluded + transformed + ... still has zero variance.

## 3. Proof after a delivery into a database

* **Row count** read back inside the delivery's own transaction.
* **Values**: every cell is read back before the commit and compared with what was sent (a content fingerprint per column). A delivery with the right number of rows but altered values (a character set that cannot hold an umlaut, a value cut short) is refused and rolled back, naming the columns. `verify_values=false` skips it, and the certificate then says it was not compared.
* **Re-verify at any time** (Governance: "Verify now", `POST /api/pipeline/verify-db/{run}`): compares the live table with the run's validated payload; catches edits after delivery.
* **Rollback** (`POST /api/pipeline/rollback-db/{run}`, reason required): deletes exactly this run's rows in one transaction, counts checked, and drops the table if nothing else is in it. Refused when the table no longer holds what was delivered.

## 4. Delta runs

Every run is validated against the whole population. A **delta** compares a run's valid records with an earlier run's, on the SAP target fields: new, changed, unchanged, removed (with the reason: now rejected / now a duplicate / no longer in the source). A **merge** delivery (`mode: merge`, `baseline_run`) writes only new and changed records into the table the baseline delivered, and with `delete_removed` deletes the removed ones, in one transaction that checks counts and values and keeps a copy of every replaced or deleted row. Rolling back a merge restores those rows exactly.

## 5. The live SAP system

No real SAP system was reachable while building this, so the live path is **tested against a strict stand-in** (basic auth, CSRF token with its session cookie, token expiry mid-batch, SAP error structure, `$metadata`), not against SAP itself. Before the first load run `python scripts/verify_live_sap.py` (or `POST /api/targets/diagnose-sap`) against your system: connection, credentials, the CSRF/session handshake, **every field name and length this application sends against the system's own `$metadata`**, and read access. `--write-test --yes-create-one-test-partner` creates one clearly marked partner, reads it back, and checks that SAP's rejection of an invalid payload is understood.

## 5b. The AI assistant

The assistant proposes; it never applies, and every proposal is checked by code that does not trust it.

* **Suggest a rule** (Studio: "Suggest rule" next to an unmapped column, `POST /api/mapping/{set}/suggest-rule`): for a column the first pass missed. The model is asked several times and the most common answer kept; your optional hint ("this is the customer account group") steers the **retrieval** of candidate SAP fields as well as the prompt, because a model can only choose among the fields it is shown; targets that already have a rule are not offered; the answer goes through the same guardrails as every AI rule and is **measured on your file** (how many rows already hold valid keys of the target's SAP check table, how many fit its length). Nothing is saved until you accept, and an accepted suggestion still needs a human review.
* **Draft lookups** (rule editor: "Draft lookups with AI"): the most frequent legacy values are translated without the model where they already are valid SAP keys or are on a built-in synonym list; the rest go to the model together with the list of allowed keys, and **a key that is not on that list is discarded** however sure the model sounded. Entries are pre-ticked only when every run agreed and the model was sure; you tick, optionally correct a key (it must exist in the SAP list), and apply. Each approved entry records who approved it and which model drafted it. Possible only where the system knows the SAP key list of the target's check table (countries, languages, currencies, Incoterms, payment-term keys of the demo set); for your own account groups or any list the system does not know, no draft is possible because it could not be checked. Case variants (`USA`, `usa`) are one entry, because lookups ignore case.
* **Ask in plain English** (Studio and Governance: "Ask about this mapping / run", `POST /api/assistant/ask`): a model only *chooses* which read-only tools to run (run summary, defects, find records, column facts, mapping rules, delta); the server runs them against the real data with a fixed set of operators and column names checked against the real file; customer records are shown to you in tables and **are never sent to the model**; the model words the answer from the facts only, and **every number in the wording is checked against the facts**: if one is not there the wording is dropped and the facts are stated plainly. If the model cannot be reached a keyword router picks the tools and the facts are stated plainly. A lookup that ignores what the question says is missing ("no country") is narrowed to that field and the answer says so.

Limits: the built-in key lists are not an export from your SAP system (`CsvDomainProvider` loads real exports); the assistant answers about one run or mapping at a time; the model can still choose the wrong tool, which is why the tools used and their results are always shown with the answer.

## 6. What is not covered (read before relying on it)

* **SAP updates.** A delta run can update a *database* target. Loading a second run into SAP would create the partners again: SAP update support is not built, so load into SAP once per population.
* **Rolling back a load into SAP** is done in SAP; the rollback here is for database targets.
* **Delta duplicates across runs**: a record that duplicates one delivered by the baseline is handled by running deduplication on the whole population, but an already-delivered record that later turns out to be a duplicate child is reported as *removed*, not unloaded unless `delete_removed` is chosen.
* Live SAP behaviour (number ranges, conversion exits, language keys: the payload sends `Language` as the one-letter SAP key) is only verified by the diagnostics on your system.

## 7. Access control, audit trail, privacy and data quality

Who may do what (six roles, one policy table), the tamper-evident audit trail, automatic personal-data classification, retention / erasure / legal hold, the data quality
rule library and the live compliance report are described in [compliance.md](compliance.md), with the standard each control serves and what it cannot do for you. How
the product relates to the market, and what was learned from it, is in [learning-from-syniti.md](learning-from-syniti.md).
