# Full-pipeline format comparison

AI mapping by Gemini (`gemini-flash-latest`) on the reference table; the same mapping replicated onto each format's table.

## Mapping proposed by Gemini for the reference

| Source column | Outcome | Target | Rule | Review |
|---|---|---|---|---|
| CUST_ID | NO_MATCH | . |  |   |
| CUST_NAME | SAVED | BUT000.NAME_ORG1 | DIRECT_COPY | OK  |
| TAX_ID | NO_MATCH | . |  |   |
| EMAIL | NO_MATCH | . |  |   |
| PHONE | NO_MATCH | . |  |   |
| ADDR_STREET | SAVED | ADRC.STREET | DIRECT_COPY | OK  |
| CITY | SAVED | ADRC.CITY1 | DIRECT_COPY | OK  |
| COUNTRY | SAVED | ADRC.COUNTRY | VALUE_MAPPING | NEEDS_REVIEW value lookups are not defined yet |
| POSTAL_CODE | SAVED | ADRC.POST_CODE1 | DIRECT_COPY | OK  |
| PAYMENT_TERMS | SAVED | KNB1.ZTERM | VALUE_MAPPING | NEEDS_REVIEW value lookups are not defined yet |

## Pipeline result per format

| Format | Run | Extracted | Valid | Defective | Dup children | Quality % | Recon balanced | Recon passed | Valid-output fingerprint | Seconds | vs reference |
|---|---|---|---|---|---|---|---|---|---|---|---|
| csv-utf8 | 39 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 5.4 | reference |
| csv-lf-endings | 40 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.7 | IDENTICAL |
| csv-utf8-bom | 41 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.7 | IDENTICAL |
| semicolon-cp1252 | 42 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.6 | IDENTICAL |
| pipe-gzip | 43 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.6 | IDENTICAL |
| tsv-utf16 | 44 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.5 | IDENTICAL |
| csv-in-zip | 45 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.5 | IDENTICAL |
| excel | 46 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.5 | IDENTICAL |
| excel-banner-rows | 47 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.4 | IDENTICAL |
| csv-banner-rows | 48 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.6 | IDENTICAL |
| json-array | 49 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.5 | IDENTICAL |
| ndjson | 50 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.7 | IDENTICAL |
| fixed-width | 51 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `2c6a7c572c0c` | 2.4 | DIFFERENT: valid_fp |
| parquet | 52 | 1980 | 1544 | 372 | 64 | 80.58 | True | False | `b18751580e29` | 2.6 | IDENTICAL |

## Reference run detail

- Dedup: {'duplicate_clusters': 64, 'duplicate_children': 64, 'surviving_records': 1544, 'exact_match_clusters': 34, 'fuzzy_match_clusters': 30}
- Readiness: BLOCKED
- Failures by check: {"ADRC.COUNTRY|LOOKUP_FAILED|CRITICAL": 216, "ADRC.CITY1|MANDATORY|CRITICAL": 62, "KNB1.ZTERM|LOOKUP_FAILED|CRITICAL": 50, "ADRC.COUNTRY|MANDATORY|CRITICAL": 42, "BUT000.NAME_ORG1|LENGTH|HIGH": 16}
- Reconciliation checks: {"run_counter_loaded_matches_lineage": true, "run_counter_failed_matches_transform_plus_load": true, "every_loaded_record_has_sap_number": true, "sap_numbers_are_unique": true, "load_phase_reached": false, "mapping_approved_with_segregation_of_duties": false, "validation_report_supplied": true, "validation_input_equals_extract": true, "validation_split_is_consistent": true, "load_attempts_do_not_exceed_valid_records": true, "extract_matches_landed_snapshot": true, "source_control_total_verified": true}

## Is the AI mapping repeatable across formats?

- excel: same as reference
- fixed-width: DIFFERENT - [('POSTAL_CODE', 'ADRC', 'POST_CODE1', 'DIRECT_COPY'), ('POSTAL_CODE', 'ADRC', 'POST_CODE1', 'SQL_EXPRESSION')]
- json-array: same as reference
