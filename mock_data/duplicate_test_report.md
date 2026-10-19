# Duplicate detection test report

11 files with known, injected duplicates (seed 7); full pipeline via the API with a deterministic mapping; engine clusters compared with the answer key.

| Scenario | Rows | Injected dups | Clusters | Children removed | False merges | Look-alikes merged | Golden record | Seconds |
|---|---|---|---|---|---|---|---|---|
| Exact copies (every field identical, new customer number) | 340 | 40 | 40 | 40 | 0 | 0/0 | - | 1.8 |
| Same tax id in different formats, company renamed and moved | 340 | 40 | 40 | 40 | 0 | 0/0 | - | 1.4 |
| Fuzzy name duplicates (no tax id, same address) | 520 | 120 | 120 | 120 | 0 | 0/0 | - | 1.4 |
| Same company, address written / recorded differently (name identical) | 640 | 320 | 160 | 160 | 0 | 0/0 | - | 1.8 |
| Name typo together with an address difference | 160 | 60 | 60 | 60 | 0 | 0/0 | - | 1.0 |
| E-mail / phone only duplicates - DEFAULT dedupe config | 360 | 60 | 60 | 60 | 0 | 0/0 | - | 1.2 |
| E-mail / phone only duplicates - explicit dedupe config with e-mail + phone keys | 360 | 60 | 60 | 60 | 0 | 0/0 | - | 1.2 |
| Look-alikes that are DIFFERENT entities and must never merge | 200 | 0 | 0 | 0 | 0 | 0/100 | - | 1.2 |
| Groups of three + golden-record choice (most complete record must survive) | 120 | 80 | 40 | 80 | 0 | 0/0 | 40/40 | 1.4 |
| The whole extract delivered twice (every record appears again under a new number) | 400 | 200 | 200 | 200 | 0 | 0/0 | - | 1.4 |
| Scale: 10,000 distinct customers + mixed duplicates | 11,000 | 1000 | 1000 | 1000 | 0 | 0/0 | - | 11.1 |

## Recall by duplicate type (duplicates found / injected)

| Scenario | Duplicate type | Found | Injected | Recall |
|---|---|---|---|---|
| exact_copies | exact-copy | 40 | 40 | 100% |
| tax_id | tax-id-reformatted | 40 | 40 | 100% |
| name_variants | legal-suffix-punctuation | 30 | 30 | 100% |
| name_variants | case-and-spacing | 30 | 30 | 100% |
| name_variants | transposed-letters | 30 | 30 | 100% |
| name_variants | missing-letter | 30 | 30 | 100% |
| address_variants | street-abbreviated (St, Ave) | 40 | 40 | 100% |
| address_variants | street-typo | 40 | 40 | 100% |
| address_variants | postal-code-blank | 40 | 40 | 100% |
| address_variants | postal-code-differs (should NOT merge) | 0 | 40 | 0% |
| address_variants | house-number-differs (should NOT merge) | 0 | 40 | 0% |
| address_variants | street-blank | 40 | 40 | 100% |
| address_variants | city-blank (fails mandatory check) | 0 | 40 | 0% |
| address_variants | whole-address-blank (fails mandatory check) | 0 | 40 | 0% |
| name_typo_and_address | name-typo + street-abbreviated | 30 | 30 | 100% |
| name_typo_and_address | name-typo + postal-blank | 30 | 30 | 100% |
| email_phone_default | email-only | 30 | 30 | 100% |
| email_phone_default | phone-only | 30 | 30 | 100% |
| email_phone_explicit | email-only | 30 | 30 | 100% |
| email_phone_explicit | phone-only | 30 | 30 | 100% |
| chains_and_master | triple-member | 80 | 80 | 100% |
| doubled_file | exact-copy | 200 | 200 | 100% |
| scale | exact-copy | 250 | 250 | 100% |
| scale | tax-id-reformatted | 250 | 250 | 100% |
| scale | legal-suffix-punctuation | 250 | 250 | 100% |
| scale | case-and-spacing | 250 | 250 | 100% |

## Notes

- **tax_id**: Only the tax id links these; names and addresses are different.
- **address_variants**: A postal code or house number that one record lacks is missing data and does not block a merge; a DIFFERENT number does. Blank-city records fail the mandatory-field check and are excluded from deduplication until fixed (by design).
- **email_phone_default**: The default config now also keys on e-mail and phone columns when the source has them.
- **email_phone_explicit**: The default config now also keys on e-mail and phone columns when the source has them.
- **lookalikes**: 25 pairs each: 'Plant 1' vs 'Plant 2'; same name in two cities; one word different; same street, different house number.

## File-level checks

- Same file uploaded again (renamed): table id unchanged (31 -> 31), tables in catalog 38 -> 38, SHA-256 identical, rows 340, warnings: none
- Same CUST_ID used twice in one file (2 keys repeated), primary key declared: HTTP 422: primary key (CUST_ID) does not identify rows uniquely: 2 key value(s) are used by more than one row, e.g. 500007 (2 rows), 500052 (2 rows)

## Hard expectation failures

- none
