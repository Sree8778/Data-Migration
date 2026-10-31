# Compliance: what the application does, what you still have to do

Written for the people who run this application and answer for it to auditors, a data protection officer and security.

**This is a technical control mapping, not a certification.** It says which control of which standard each feature serves, where to see it working, and what the
application cannot do for you. `GET /api/compliance/report` checks the technical controls against the running installation, so the claims below are not just
prose: a control that is off or broken shows as WARN or FAIL there.

## 1. How to switch it all on

```
MIGRATION_API_KEYS=sha256=<hex of key1>:alice:mapper+loader,sha256=<hex of key2>:bob:approver,sha256=<hex of key3>:carol:auditor
MIGRATION_REQUIRE_AUTH=true            # refuse to start without keys
MIGRATION_APPROVERS=bob                # who may approve mappings and sign off loads
MIGRATION_AUDIT_HMAC_KEY=<secret>      # keeps the audit chain from being forged by whoever can edit the file
MIGRATION_RETENTION_DAYS=90            # personal data of a run is due for erasure after this
MIGRATION_DB_ALLOWED_HOSTS=...         # which database hosts the server may connect to
MIGRATION_CONNECTION_ADMINS=...        # who may create database connections
MIGRATION_MAINTENANCE_HOURS=24         # run the retention sweep (really erases) and overdue-task reminders every 24 hours
MIGRATION_NOTIFY_WEBHOOK=https://...   # told when a stewardship task is created, assigned, closed or overdue
```

Put TLS in front of the application (the application speaks plain HTTP and trusts `X-Forwarded-Proto`). Without keys the application runs in *dev-header mode*, in
which anybody can name themselves in `X-User`; it logs a warning at start-up and the compliance report fails `AC-1`.

## 2. Control mapping

| Area | Control in the application | Standard it serves | Where to see it |
|---|---|---|---|
| Authentication | API keys (optionally held as `sha256=` hashes); a key decides identity, `X-User` is ignored; failed log-ins are throttled per address | ISO/IEC 27001:2022 A.5.15, A.8.5 · NIST SP 800-53 IA-2, IA-5 · OWASP API2:2023 | `AC-1`, `AC-3` |
| Least privilege | Six roles (`viewer`, `mapper`, `approver`, `loader`, `auditor`, `admin`); one policy table decides every state-changing route; a route nobody listed is admin-only; a test fails when a route is added without an entry | NIST AC-6 · SOX ITGC logical access · OWASP API5:2023 (function-level authorisation) | `AC-2`, `tests/test_security_compliance.py` |
| Segregation of duties | The creator of a mapping set can never approve it (service **and** database constraint); approving, signing off and loading are three different roles; named approvers (`MIGRATION_APPROVERS`) | ISO A.5.3 · NIST AC-5 · SOX segregation of duties | `AC-4` |
| Change control | A mapping is dry-run on real rows before it can be submitted; APPROVED mapping sets are immutable (change = new version); the governance gate blocks a load while critical defects are open or no sign-off exists | SOX ITGC change management · ISO A.8.32 | `CC-1` |
| Audit trail | Every state-changing call, every refusal (401/403/429) and every download of a report, with who, what, when, outcome, request id; **hash-chained and append-only** so that changing, removing or inserting an entry is detected; optionally keyed (HMAC); high-risk actions (approve, sign-off, load, rollback, recover, keys, connections, erasure) are written **before** they run and do not run if the trail cannot be written | ISO A.8.15 · NIST AU-2, AU-3, AU-9 · SOX · ALCOA+ (attributable, contemporaneous) | `AU-1..3`, `GET /api/compliance/audit-log`, `/verify`, `/head` |
| No data in the trail | Entries hold identifiers and outcomes, never request bodies or record values | GDPR art. 5(1)(c), 25 | `test_the_trail_never_holds_request_bodies` |
| Resource consumption | Per-caller rate limit (a tenth for heavy calls), JSON body cap, upload cap, bounded DuckDB memory, bounded fuzzy-match blocks | OWASP API4:2023 · ISO A.8.6 | `AC-6` |
| Response hygiene | `Cache-Control: no-store` on every API response, `X-Request-ID`, CSP, `nosniff`, `frame-ancestors`, HSTS behind TLS | OWASP ASVS · GDPR art. 32 | any response header |
| Data integrity | The landed source is checksummed (SHA-256 of the original), row counts are controlled against the source system's total, the run records exactly which snapshot it read; the database delivery is all-or-nothing, counts **and values** are read back before commit, `verify` repeats it later, `rollback` undoes it, and an interrupted delivery is recorded before it starts and settled by `recover-db` | ALCOA+ (original, accurate, complete, consistent) · SOX completeness and accuracy | `DI-1..3`, reconciliation certificate |
| Data quality | A rule library (35+ rules in 7 dimensions) assesses a landed table before mapping; every assessment is kept, the trend is the monitoring; pre-load validation against the SAP specification; deduplication with a review band (below) | ISO 8000 · ISO/IEC 25012 · DAMA-DMBOK dimensions | `/api/quality/*`, `/api/dashboard` |
| Personal data | Source columns are classified as personal data **automatically** (by name and by value shape: e-mail, IBAN, card number with Luhn check, phone, national id); samples are shape-masked in the catalog, on screen and before any AI prompt; a classification is only ever raised | GDPR art. 25, 32 · ISO A.5.12, A.5.34 | `PV-1` |
| Storage limitation | Retention period; sweep (dry run first); erasure of a run's record values (files, cell lineage, exclusion examples) and of a landed table (copy, original, catalog samples) while **keeping the evidence** (counts, rules, hashes, sign-offs, delivery records, certificate) and recording who erased what and why | GDPR art. 5(1)(e), 17 · ISO A.8.10 | `PV-2..4`, `POST /api/compliance/runs/{id}/erase` |
| Legal hold | A hold on a run stops erasure by hand and by the sweep until released | e-discovery, SOX record retention | `.../hold`, `.../release` |
| Stewardship | Findings become tasks with an owner, a due date, a priority and a status; every change keeps who and when; defect tasks close themselves when a later run no longer has the defect; people's decisions about possible duplicates are recorded, attributed and applied by every later run; reuse is through checksummed content packs that import as an unapproved draft | SOX ITGC (accountability for remediation), ALCOA+ (attributable) · ISO/IEC 27001:2022 A.5.2 | `/api/tasks`, `/api/duplicates`, `/api/packs` |
| Record of processing | The technical part of an art. 30 record: categories of personal data, purpose, recipients, storage, retention, measures | GDPR art. 30 | `GET /api/compliance/processing-record` |
| AI governance | AI only proposes; every proposal passes code that does not trust it (SQL guardrails, key lists, measured on the real data); a person approves; model identity is recorded; the assistant's numbers are checked against the facts | GDPR art. 22 spirit · ISO/IEC 42001 | `PV-5`, `docs/controls.md` §1, §5b |
| Outbound connections | Allow-list of database hosts, role (source/target), connection administrators, passwords in memory only or from an environment variable | OWASP API7:2023 (SSRF) · ISO A.8.20 | `AC-5` |

## 3. Deduplication, and why nothing is dropped on a guess

Record linkage theory (Fellegi and Sunter, 1969) has three outcomes for a pair, not two: *link*, *non-link* and *possible link*, the last going to clerical review.
Commercial duplicate checks work the same way (SAP's own check has strong / medium / weak strictness; matching products have an auto-link and a clerical-review
threshold). A tool that merges and drops on a single threshold loses customers silently, and a lost customer is a completeness failure.

* score at or above `fuzzy_threshold` (default 0.88): merged; the child goes to `duplicates.parquet`, never deleted;
* from `review_threshold` (0.80) up to that: a **possible duplicate**, **not merged**, both records load, listed in `possible_duplicates.parquet`;
* a different tax number is a **veto**: two legal entities are never merged because their names look alike;
* every merge is labelled `dedup_match_strength`: `EXACT` (a shared key), `NAME_ADDRESS`, or `NAME_ONLY` (no address to confirm it: check these);
* a record that failed transformation can be recognised as a duplicate of a good one (no fixing needed) but never becomes the master of a group that has a good member;
* oversized look-alike groups are compared with their nearest neighbours instead of all-against-all, and the report says so.

## 4. What the application cannot do for you

* **Anchor the audit trail.** A hash chain proves nothing was *changed*; only a copy of the chain's head outside the application proves nothing was *cut off*.
  Copy `GET /api/compliance/audit-log/head` to a SIEM or WORM store on a schedule and check with `verify?expected_seq=&expected_hash=`.
* **Hold the keys.** `MIGRATION_AUDIT_HMAC_KEY`, API keys and database passwords belong in a secrets manager, not in the workspace.
* **Be the lawful basis.** Purpose limitation, lawful basis (art. 6), the DPIA (art. 35), transfers outside the EEA, contact details of controller and DPO are the
  controller's to document; the processing record lists them as open.
* **Encrypt at rest.** The workspace and PostgreSQL are plain files and a plain database: use disk or volume encryption and PostgreSQL TDE or an encrypted volume.
* **Be notified on its own.** The webhook only tells a bridge you provide (e-mail, chat); the application sends no mail itself.
* **Run in more than one process.** Jobs, throttling counters and the interrupted-run clean-up assume one server process per workspace.
* **Prove the live SAP path.** It has been tested against a strict stand-in, never against SAP; run `scripts/verify_live_sap.py` on your system.
* **Replace organisational controls:** joiners/movers/leavers, key rotation, backup and restore tests, incident response, supplier assessment.

## 5. Sources consulted

Standards and guidance were read through public summaries and vendor explanations; check wording against the standard before quoting it to an auditor.

* Fellegi, Sunter: *A Theory for Record Linkage* (1969) and the three-way decision with a clerical-review band: [summary](https://pubs.dbs.uni-leipzig.de/dc/node/742), [lecture notes](https://cs.cornell.edu/~shmat/courses/cs6434/fellegi-sunter.pdf)
* Blocking and filtering for entity resolution: [Papadakis et al., survey](https://arxiv.org/pdf/1905.06167), [end-to-end survey](https://arxiv.org/pdf/1905.06397)
* Duplicate check strictness in SAP, and cleansing before migration: [SAP knowledge base](https://userapps.support.sap.com/sap/support/knowledge/en/2586842), [CDQ whitepaper](https://www.cdq.com/sites/default/files/2023-12/Whitepaper_Data_Deduplication_for_S4HANA_LinkedIn.pdf)
* Auto-link and clerical-review thresholds in master data matching: [Stibo Systems](https://doc.stibosystems.com/doc/version/2025.2/web/content/mtchlnkmrg/match_action/match_and_link/match_and_link.html)
* ISO/IEC 27001:2022 A.8.15 logging: [ISMS.online](https://www.isms.online/iso-27001/annex-a-2022/8-15-logging-2022/), NIST SP 800-53 AU-9 / AU-10 / AU-3: [Coralogix guide](https://coralogix.com/guides/nist-sp-800-53-audit-logging-au-controls/)
* OWASP API Security Top 10 (2023): [ISACA review](https://www.isaca.org/resources/news-and-trends/industry-news/2023/reviewing-the-2023-owasp-api-top-10), [Rapid7](https://www.rapid7.com/blog/post/2023/06/08/owasp-top-10-api-security-risks-2023/)
* ALCOA+ data integrity attributes (the MHRA GxP data integrity guidance of 2018 is the formal source): [CASRAI glossary](https://casrai.org/dictionary/term/alcoa-plus-data-integrity), [CASRAI GxP page](https://casrai.org/gxp/alcoa)
* GDPR storage limitation, minimisation, records of processing: [Legiscope](https://www.legiscope.com/blog/gdpr-data-minimisation-storage-limitation-official.html), Article 5 text: [gdpr-text.com](https://gdpr-text.com/de/?p=4152)
* Migration reconciliation and segregation of duties as ITGC themes: [BDO](https://www.bdo.com/insights/assurance/the-critical-role-of-risk-and-control-assessments-in-erp-system-implementations), [reconciliation controls](https://www.hyperbots.com/glossary/erp-reconciliation-controls)
