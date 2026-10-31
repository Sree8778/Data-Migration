# Learning from Syniti: what the published material shows, and what we took from it

Written for the product owner and the engineers who decide what to build next.

## What this is, and what it is not

This is a study of **publicly available material** about Syniti (vendor and SAP product pages, press releases, a Syniti conference deck, partner and analyst
write-ups, practitioner reviews and job descriptions). We had no access to the product, and we did not decompile, probe or copy it. What follows is therefore the
*method and feature set* Syniti publishes about itself, which is what it is reasonable to learn from, and not its internals. Vendor and partner figures
("99% match accuracy", "80% less code", "500+ rules") are marketing claims and are quoted as such. Where a question could not be answered from public material,
it is marked *unknown*.

## What Syniti is, according to its own material

* **Syniti Knowledge Platform (SKP)**: modules *Migrate* (the cloud version of Advanced Data Migration, ADM), *Quality*, *Match*, *Replicate* (change-data-capture
  replication), *Catalog & Governance*, *Master Data Management*; *Rapid Data Governance* is a no-code accelerator for SAP MDG. SAP resells ADM as **SAP Advanced Data
  Migration and Management by Syniti** and positions it for S/4HANA and RISE projects. A competitor's page says Syniti is now part of Capgemini (December 2024); unverified.
* **Method** (Syniti conference deck, 2022): **Prepare → Extract → Profile → Design → Map → Construct → Transform → Load**, inside a longer journey of *Prepare, Migrate,
  Scale, Evolve, Retire*. Its four stated principles are **orchestration** (every stakeholder in one system: system integrators, business users, IT, the data team),
  **automation** (generated code), **simulation** (mock loads before the real one) and **visibility** (dashboards per stage). "Much more than an ETL tool."
* **Reuse**: "all metadata and every decision about data rules, policies, team members, roles, mappings, ETL code" stays available for the next migration, quality or
  governance project. Pre-built content: a partner document claims **65+ migration objects and 500+ data-quality rules**, plus accelerators for sources such as SAP ECC.
* **Quality and control**: a library of **pre-load validation rules** plus custom rules; **pre- and post-load comparison** that highlights discrepancies; dashboards for
  management; "active metadata scanning" that flags and suggests corrections; **data construction** web forms in which business owners fill in what the legacy system
  lacks, with automated rules alerting owners and stewards; matching and harmonisation with configurable match rules and survivorship.
* **Practitioner feedback** (reviews, job ads): powerful once configured, but complex, a steep learning curve, and consultants are often needed to set it up.

## Parity, honestly

| Capability | Syniti (published) | This application before | Now | Remaining gap |
|---|---|---|---|---|
| Prepare / assess the legacy data | Data assessment, 500+ DQ rules (claimed) | Profiler only | **Rule library (35+ rules, 7 dimensions) with per-column roles, country-aware checks, masked examples, stored history** | Fewer rules; no SAP-object-specific packs beyond business partner |
| Extract | Many sources, any target | Files, 4 SQL databases, MongoDB, Google Firebase (Firestore), S3 / Azure / SFTP | unchanged | No SAP ECC / BW extractors; no change-data-capture |
| Profile | Yes | Yes | Yes, and personal data is now classified automatically | |
| Design / map | Pre-built mappings, generated code (up to 80%, claimed) | AI-assisted mapping with measured guardrails, manual rules, lookups drafted by AI | **Business partner now includes person names, e-mail / telephone / fax, tax numbers, bank details and supplier (vendor) data, in the catalog, the validation, the Cockpit workbook, the SAP payload and the database delivery** | One business object (business partner, customer and supplier side) instead of dozens |
| Construct (business owners fill gaps) | Web forms, rules alert owners | Excel template and upload, placeholder rules | **Stewardship tasks: owner, due date, priority, status, notes; one task per defect group routed by SAP table; overdue to the dashboard; a defect task closes itself when a later run no longer has the defect; optional webhook for e-mail / chat bridges** | Excel rather than web forms for the data entry itself |
| Transform | Generated ETL | Single compiled DuckDB query, per-cell lineage | control characters are now defects, not data | |
| Match / deduplicate | Configurable rules, survivorship | Hybrid exact + fuzzy, golden record | **Three outcomes (merge / possible duplicate / different), tax-number veto, match-strength labels, a review workflow whose decisions stick across runs (merge / different), survivorship (`fill_blanks`: a merged record inherits blank fields from its best duplicate), bounded work (100k rows: 228 s → 11 s)** | Survivorship is fill-the-blanks only, not a rule per field |
| Pre-load validation | Rule library, custom rules | SAP-spec validation, pre-submission dry run | unchanged | |
| Simulation (mock loads) | Cycles | `wave_name` on a run | **Cycle comparison: what was resolved, what is new, what got worse** | No cutover plan or scheduler |
| Visibility | Dashboards per stage | Governance page per run | **Dashboard page and API: waves, trends, data quality, tasks, what needs attention; cycle comparison** | |
| Post-load reconciliation | Pre/post comparison | Certificate; value-level read-back; verify; rollback | interrupted deliveries recoverable | SAP side only as good as the stand-in |
| Governance and audit | Workflow, audit capabilities | Segregation of duties, gate, sign-off | **Roles, tamper-evident audit trail, retention / erasure / legal hold, compliance report** | |
| Reuse across projects | Metadata and decisions retained | Mapping sets and lookups are kept | **Content packs: an approved mapping with its lookups and exclusions exported as a checksummed file, imported as a DRAFT with a dry run, guardrails on every rule, rules that do not fit skipped and listed, everything flagged for review** | No shared library of packs; no versioned upgrade of an imported pack |
| Replication (CDC) | Syniti Data Replication | none | none | Out of scope |
| MDG accelerator | Rapid Data Governance | none | none | Out of scope |

## What we adopted, and why

1. **Assess before you map** (their *Prepare* and *Profile*): a migration is decided by what is wrong with the legacy data, so the first thing the business should see is a
   rule-by-rule list of what is wrong, in the language of the data quality dimensions, with a hint what to do about each finding. It is re-runnable, which is what makes
   it *monitoring*: the score and the findings are compared with the previous assessment.
2. **Simulate and see the direction** (their *simulation* and *visibility*): mock loads are only useful if you can say what the second changed relative to the first.
3. **Do not decide for the business on a guess** (their match rules and clerical review, and the record-linkage literature): the review band and the match-strength label.
4. **Make the controls part of the product** (their "full audit capabilities", "workflow and approval"): roles, an audit trail that cannot be quietly edited, erasure with
   evidence kept, and a report that checks all of it live.

## What we deliberately did not copy

* **The size of the platform.** Reviewers describe it as heavy: configuration needs consultants. This application's advantage is that one person can run a migration:
  keep it light, keep the defaults safe, and make every control visible on one screen.
* **Generated code as the central idea.** Here the AI proposes a mapping and *code that does not trust it* checks it against the real data; the rules that run are plain,
  reviewable SQL. That is a different trade: less automation, more explainability for an auditor.
* **Cloud-hosted processing.** Data stays where you run the application.

## What would close the remaining distance, in order of value

1. **More business objects** (material, G/L account, and so on). Business partner now covers customer and supplier data with 60 target fields across 13 tables (tax numbers,
   e-mail, telephone, fax, bank details, person names, vendor company-code and purchasing data), carried all the way to the Cockpit workbook, the OData deep insert and the
   database delivery. What is still missing inside the business partner: credit data, contact persons, partner relationships, several addresses or several tax numbers or bank
   accounts per partner (one of each is delivered), and an IBAN check digit test (the shape is checked, not the checksum).
2. **Run it against a real SAP system** and the real database servers (the latter now runs through Docker, see the test log), which no amount of design can replace.
3. **A shared library of content packs** with versions, so an improvement to a pack can be offered to the projects that imported it.
4. **Survivorship per field** (most recent, longest, source priority) instead of fill-the-blanks.
5. **Data entry for owners in the browser** (forms with the rules applied as they type) instead of an Excel round trip.

## Sources

* [Syniti: The Data Journey to SAP S/4HANA (conference deck, 2022)](https://assets.dm.ux.sap.com/webinars/2021-12-31-sap-btp-customer-value-network-global/pdfs/syniti_the_data_journey_to_sap_s4hana.pdf)
* [Help Net Security: Syniti Migrate optimizes data migration for enterprise customers](https://www.helpnetsecurity.com/2022/05/05/syniti-migrate/)
* [IT Brief: Syniti updates Knowledge Platform with cloud-based version](https://itbrief.co.nz/story/syniti-updates-knowledge-platform-with-cloud-based-version)
* [DBTA: New updates to the Syniti Knowledge Platform](https://www.dbta.com/Editorial/News-Flashes/New-Updates-to-the-Syniti-Knowledge-Platform-Enable-Faster-Lower-Risk-Data-Migrations-152774.aspx)
* [BigDATAwire: Syniti Match and Syniti Replicate on SAP Store](https://bigdatawire.com/this-just-in/syniti-match-and-syniti-replicate-now-available-on-sap-store)
* [SAPinsider: Syniti, SAP ADMM, migration and data governance](https://sapinsider.org/syniti-sap-admm-migration-data-governance/)
* [Deloitte: clean data accelerator (65+ objects, 500+ rules, claimed)](https://www.deloitte.com/content/dam/assets-zone2/be/en/docs/alliances/deloitte-clean-data.pdf)
* [Deloitte: Rapid Data Governance guide](https://www.deloitte.com/content/dam/assets-zone2/be/en/docs/alliances/dgoal-rdg-2022-deloitte-be-uide-en.pdf)
* [Gartner Peer Insights: Syniti Knowledge Platform reviews](https://www.gartner.com/reviews/product/syniti-knowledge-platform)
* [PeerSpot: Syniti Data Quality review](https://www.peerspot.com/product_reviews/syniti-data-quality-review-5372276-by-gagan-yadav)
* [DBTA / Datanami on Syniti Data Replication](https://www.datanami.com/this-just-in/syniti-announces-low-touch-data-replication-solution-with-free-trial/)
* [Data Ladder: comparison page (competitor; treat as biased)](https://dataladder.com/whitepapers/why-data-ladder-is-the-best-syniti-alternative/)
