# ERP Migration Accelerator

Moves customer master data out of a legacy system and into **SAP S/4HANA as Business Partners**, with every step checked, approved by a second person and written down as evidence.

You give it a legacy extract (a file, a database table, MongoDB or Firestore). It profiles the data, finds the quality problems and duplicates, maps the columns to SAP fields (with optional AI help), runs the migration, stops at a validation gate, and, once a person has signed off, delivers the result and produces a reconciliation certificate. A delivery into a database can be rolled back.

> **Status: the SAP side has only been tested against a stand-in gateway, never a real SAP system.** The stand-in is strict (basic auth, CSRF token and session cookie, token expiry, SAP error structure, `$metadata`), but live SAP behaviour is only verified by the diagnostics described under [Before the first live SAP load](#before-the-first-live-sap-load).

## What it does

```
land → profile → quality → mapping → exclusions → approval → run → gate → sign-off → deliver → certificate
```

| Stage | What happens |
|---|---|
| **Land** | Files (CSV, TSV, JSON, NDJSON, XLSX, ZIP, gzip, fixed-width and more), databases (PostgreSQL, MySQL, SQL Server, Oracle), cloud storage (S3, Azure Blob, SFTP), MongoDB and Firestore (read-only). Several tables can be combined into one source. |
| **Profile** | Per-column facts, personal-data classification, duplicate candidates. |
| **Quality** | A rule library measures data quality dimensions such as completeness, validity, uniqueness and consistency; each finding becomes a defect with a suggested fix. |
| **Mapping** | Columns are mapped to SAP Business Partner fields by rules (copy, lookup, format, constant, expression). The AI only *proposes*; it is not in the per-row data path. A pre-submission dry run measures each rule on a sample of the real rows. |
| **Exclusions** | Test and deleted records are left out by explicit rules, each with a mandatory reason, and remain visible on the certificate. |
| **Approval** | The author of a mapping cannot approve it. Approvers are configurable. |
| **Run and gate** | The whole population is validated; the gate blocks on critical defects. |
| **Sign-off, deliver** | A person signs off, then the data is delivered to SAP (OData) or to a database table, with row counts and values read back. |
| **Certificate** | Extracted = excluded + transformed + rejected + duplicates, with zero variance, plus who did what and when. |
| **Rollback / delta** | A database delivery is undone by run id. A delta run compares two runs and merges only new and changed records. |

Also included: an AI assistant that answers questions about a run in plain English (every number in its answer is checked against the facts), compliance packs and a live compliance report, a tamper-evident audit trail, role-based access control, stewardship tasks, and a programme dashboard.

## Tech stack

FastAPI · DuckDB (data processing) · PostgreSQL (catalog, run history, audit) · SQLAlchemy and Alembic · sentence-transformers and RapidFuzz (duplicate matching) · Gemini or a local Ollama model (optional AI). Plain HTML, CSS and JavaScript for the web UI.

## Getting started

Requirements: Python 3.13 (the version the project is developed on), Docker (for the PostgreSQL catalog), and optionally a Gemini API key or a local Ollama.

```bash
# 1. Python environment
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

# 2. PostgreSQL catalog (container erp_migration_pg)
docker compose up -d --wait

# 3. Create the tables and load the SAP Business Partner catalog
python -m alembic upgrade head
python -m src.seeds.seed_sap_catalog

# 4. Start the application
python -m uvicorn --factory src.api.main:create_app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> for the web UI. The interactive API documentation is at <http://127.0.0.1:8000/docs>.

By default the app connects to `postgresql+psycopg2://migration:migration_dev_pw@localhost:5432/migration_catalog` (the credentials in `docker-compose.yml`, which are for local development only). If port 5432 is taken, set `POSTGRES_PORT` before `docker compose up` and start the app with the same value, or point `DATABASE_URL` at any PostgreSQL database. A `.env` file in the project root is read at start-up; real environment variables win over it.

### Optional: AI help

The application works without a model; the AI features (mapping suggestions, lookup drafts, the assistant) are then unavailable.

| Provider | Setup |
|---|---|
| **Gemini** | `LLM_PROVIDER=gemini` and `GEMINI_API_KEYS=key1,key2` (several keys rotate when one hits its quota). Keys can also be added in the UI; they are held in memory unless `MIGRATION_GEMINI_KEYS_FILE` is set. Set `GEMINI_MODEL` to a versioned model name so results are reproducible. |
| **Ollama** (default) | Run Ollama locally; `OLLAMA_BASE_URL` (default `http://localhost:11434/v1`) and `OLLAMA_MODEL` (default `qwen2.5-coder:7b`). |

Never commit API keys. The `.gitignore` excludes `.env*`, `gemini_keys*.json` and Firebase service-account files.

### Try it with practice data

`mock_data/` holds generated legacy files (clean, messy, duplicate-heavy, and in many file formats) together with answer keys. Chapter 5 of the [User Manual](docs/user-manual/ERP-Migration-User-Manual.pdf) walks through a complete migration with them, screenshot by screenshot.

## Configuration

All settings are optional environment variables (see `src/api/settings.py` for the full list and exact semantics).

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Catalog database (see above) |
| `MIGRATION_WORKSPACE` | Where uploads and run artefacts are kept (default `./workspace`) |
| `MIGRATION_API_KEYS` | `key:user:role+role,...`. When set, every request needs `X-API-Key` and identity comes from the key. Keys may be given as `sha256=<hex>` hashes. |
| `MIGRATION_REQUIRE_AUTH` | `true` refuses to start without API keys |
| `MIGRATION_APPROVERS` | Users allowed to approve mappings and sign off loads |
| `MIGRATION_AUDIT_HMAC_KEY` | Secret that keys the audit-trail hash chain |
| `MIGRATION_RETENTION_DAYS` | Personal data of a run is due for erasure after this many days |
| `MIGRATION_DB_ALLOWED_HOSTS` | Hosts the server may connect to as a source or target |
| `MIGRATION_PREFLIGHT` | `enforce` (default), `warn` or `off`: the pre-submission dry run |
| `MIGRATION_MAX_UPLOAD_MB` | Upload size limit (default 500) |

**Without API keys the app runs in dev-header mode**, where anyone can name themselves in the `X-User` header. That is fine on your own machine and nowhere else. For a shared installation set the keys, `MIGRATION_REQUIRE_AUTH=true`, the approvers and the audit key, and put TLS in front (the app speaks plain HTTP and trusts `X-Forwarded-Proto`). Details and the standard each control serves are in [docs/compliance.md](docs/compliance.md).

## Roles

Six roles share one policy table, for example mapper, approver, loader and auditor. Author and approver of a mapping are always different people. See chapter 3 of the User Manual.

## Tests

```bash
python -m pytest
```

Tests use their own database, `migration_catalog_test`, created on demand. **Run only one pytest session at a time**: the sessions share that database, and two at once deadlock.

Tests that need real servers (MySQL, SQL Server, Oracle, MongoDB, storage, TLS) skip themselves when the server is unreachable. To run them, start one profile at a time (SQL Server and Oracle each want about 2 GB):

```bash
docker compose -f docker-compose.servers.yml --profile mysql up -d --wait
python -m pytest tests/test_real_servers.py -k mysql
docker compose -f docker-compose.servers.yml --profile mysql down -v
```

`scripts/` also contains browser end-to-end scripts (`e2e_browser_*.py`, Playwright), an AI evaluation (`evaluate_ai.py`) and a one-million-row scale test (`scale_test.py`); results are in [docs/ai-evaluation.md](docs/ai-evaluation.md) and [docs/scale-test.md](docs/scale-test.md).

## Before the first live SAP load

No real SAP system was available while building this. Before loading anything into one, run the diagnostics against it:

```bash
python scripts/verify_live_sap.py
```

It checks the connection, credentials, the CSRF and session handshake, every field name and length this application sends against the system's own `$metadata`, and read access. Adding `--write-test --yes-create-one-test-partner` creates one clearly marked partner and reads it back. The same check is available as `POST /api/targets/diagnose-sap`.

Known limits (full list in [docs/controls.md](docs/controls.md)): SAP update support is not built, so load into SAP once per population; rolling back a load into SAP is done in SAP; the built-in SAP key lists are not an export from your system.

## Project layout

```
src/
  api/           FastAPI app, routes, web UI (templates, static JS/CSS)
  services/      Profiling, quality, mapping, transformation, dedup, validation, delivery, audit, compliance
  connectors/    Database, storage, MongoDB and Firestore sources and targets
  schemas/       Pydantic models
  db/            SQLAlchemy models and sessions
  seeds/         SAP Business Partner catalog
migrations/      Alembic migrations
tests/           Test suite (including a strict SAP gateway stand-in)
scripts/         E2E browser scripts, evaluations, demo and documentation builders
mock_data/       Practice data sets and answer keys
docs/            Manuals and technical notes
```

## Documentation

| Document | For |
|---|---|
| [User Manual (PDF)](docs/user-manual/ERP-Migration-User-Manual.pdf) · [HTML](docs/user-manual/index.html) | Anyone running a migration: roles, procedures with screenshots and flow charts, controls, troubleshooting |
| [Illustrated Guide (PDF)](docs/user-guide/ERP-Migration-Guide.pdf) · [HTML](docs/user-guide/index.html) | A plain-language walk-through for first-time users |
| [docs/controls.md](docs/controls.md) | What the system checks, where it is recorded, what it does not cover |
| [docs/compliance.md](docs/compliance.md) | Control mapping to standards, how to switch the controls on |
| [docs/ai-evaluation.md](docs/ai-evaluation.md) | How well the AI works, measured against known answers |
| [docs/scale-test.md](docs/scale-test.md) | One-million-row test |
| [docs/firestore.md](docs/firestore.md) | Firestore as a source |
| [docs/learning-from-syniti.md](docs/learning-from-syniti.md) | How the product relates to the market |

To regenerate the manuals: `python scripts/build_user_manual.py --pdf` (and `scripts/build_user_guide.py --pdf`). Fresh screenshots come from `scripts/make_guide_screenshots.py`, which must run against a throwaway app with its own databases, never your real one.

## License

No license file is included; all rights are reserved by the author unless one is added.
