# Google Firebase (Firestore) as a source

Firestore, the document database behind Firebase, can be a **source** (read only). It is not offered as a target, and the connector has no write path at all.

## Connect (Add Source wizard > Google Firebase (Firestore))

| Field | What to enter |
|---|---|
| Project ID | Firebase console > Project settings > Project ID (filled in when you load the key file) |
| Service-account key | Press **Load key file** (or paste the content). Google Cloud console > IAM & Admin > Service accounts > Keys > Add key > JSON. Give that account **only** the *Cloud Datastore Viewer* role. The key is read in your browser and kept in the server's memory only (or name a server environment variable: *Key variable*). It is never written to disk, logged or shown again. Delete the key when the migration is done. |
| Firestore database | Only if the project has more than one; default `(default)` |
| Server / Port | Leave empty for Google. `localhost` + a port selects the Firestore **emulator** (no key needed) |

## How documents become rows

One document = one row; the row count is the control total. The document ID is the column `_id` (the primary key, kept as text, so `00123` keeps its zeros). Nested maps become dotted
columns (`address.city`, to 4 levels); arrays are kept whole as JSON text; a missing field and a null are both an empty cell. Timestamps become ISO text in UTC (microseconds: Firestore's
nanoseconds are cut), a GeoPoint becomes `<field>.latitude` / `<field>.longitude`, a document reference becomes its path, bytes become hex. The columns are the union of the fields of all documents.

## Limits and costs

* **Top-level collections only.** A sub-collection is not followed.
* Firestore **bills one read per document**: extracting N documents costs about N reads plus a few for the count. The free plan allows 50,000 reads a day; a quota error says so.
* A collection that changes *during* the extract can differ from the count taken before it; the landing rejects that (rows read must equal the count).
* More than 500 distinct field names in a collection means it is not table-shaped; the extract stops and says so.
* A field literally called `_id` is refused (that column holds the document ID); so are two fields that would give the same column name.

## Tests

* Unit tests need nothing: `python -m pytest tests/test_real_firestore.py`
* Against the official emulator: `docker compose -f docker-compose.servers.yml --profile firestore up -d --wait` then the same command (3,004 documents, every type above, the full import through the API).
* In a browser: `python scripts/e2e_browser_wizard_firestore.py --base http://localhost:8000` (emulator running).
* Against a **real** project (writes a few documents into a collection of its own and removes them; use a scratch project and a key with the *Cloud Datastore User* role):
  `$env:FIREBASE_TEST_KEY_FILE="C:\path\key.json"; $env:FIREBASE_TEST_PROJECT="my-scratch-project"; python -m pytest tests/test_real_firestore.py -k real_project`

## Verified against a real project

Run on 2026-10-24 against a real Firebase scratch project (a Firestore database in Native mode, a service-account key from the Firebase console):

* `tests/test_real_firestore.py -k real_project`: connect, list, count, extract and read back exactly (leading zeros, umlauts, timestamps, nulls), and a damaged key refused without repeating it.
* `scripts/e2e_browser_wizard_firestore.py --key-file <key.json>`: in the browser, the key file loaded through the wizard, 1,501 documents seeded (more than the 1,000 a page holds), imported with the row-count check matching, profiled; the key appears nowhere in any response.
  Run it with extra collections in the project too: the setup guide ticks every customer-looking collection it suggests, and the test unticks the ones that are not its own.

Both clean up after themselves (only collections named `e2e_customers_*` or `erp_migration_test_*` are ever removed). Delete the key in the Google Cloud console when you are done.
