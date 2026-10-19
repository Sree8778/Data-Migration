# Complex dataset - end-to-end report

## 1. Landing and profiling

- File: 3.7 MB, 16,600 rows x 27 columns; detected **delimited**, encoding **cp1252** (not valid UTF-8, assumed Windows-1252), delimiter **;**
- Rows landed: 16,600 (control total MATCH) in 1.6s; multi-line quoted fields and embedded delimiters parsed correctly
- Null counts equal the answer key in **27/27** columns

## 2. AI mapping (Gemini)

- 18 columns have a target in the catalog; **15 mapped correctly**, 1 to a different target {'KUNNR': 'KNB1.KUNNR'}, **2 missed** (KTOKD, SPRAS); 0 mapped although the catalog has no target (-). Took 81517s.

## 3. Steward corrections applied to a copy of the AI mapping (AI set 128 -> working set 129)

- KUNNR retargeted to BUT000.BP_EXT (the legacy key, not the SAP customer number)
- ZTERM: 10 payment-term lookups
- WAERS changed from DIRECT_COPY to VALUE_MAPPING (23 spellings of 8 currencies) with lookups
- added SPRAS -> ADRC.LANGU (VALUE_MAPPING)
- added KTOKD -> BUT000.BU_GROUP (VALUE_MAPPING)
- LAND1: 55 lookups supplied for the AI's value mapping
- BUT000.TYPE derived from the account group (person '1' / organisation '2')

## 4. Pipeline runs

| Run | Dedupe config | Extracted | Transform failed | Dup children removed | Valid | Defective | Quality % | Readiness | Seconds |
|---|---|---|---|---|---|---|---|---|---|
| 116 | default | 16,600 | 2,196 | 858 | 12,692 | 3,050 | 80.6 | BLOCKED | 32 |
| 117 | explicit | 16,600 | 2,196 | 858 | 12,691 | 3,051 | 80.6 | BLOCKED | 38 |

## 5. Deduplication vs the answer key

(Rows that fail the mandatory/lookup checks are excluded from deduplication by design; they are listed separately.)

| Duplicate type | Injected | Found - default config | Found - explicit keys | Missed only because a row was excluded (explicit) |
|---|---|---|---|---|
| case-and-spacing | 100 | 87 (87%) | 87 (87%) | 13 |
| email-only | 100 | 86 (86%) | 86 (86%) | 14 |
| exact-copy | 150 | 130 (87%) | 130 (87%) | 20 |
| legal-suffix-punctuation | 200 | 165 (82%) | 165 (82%) | 35 |
| phone-only | 100 | 87 (87%) | 87 (87%) | 13 |
| street-abbreviated | 100 | 85 (85%) | 85 (85%) | 15 |
| tax-id-reformatted | 150 | 133 (89%) | 133 (89%) | 17 |
| typo-plus-postal-blank | 100 | 85 (85%) | 85 (85%) | 15 |

- Different entities merged by mistake: default **0** clusters, explicit **0** clusters; look-alike pairs merged (of 300): default **0**, explicit **0**

## 6. Defect detection vs the answer key (explicit run, duplicate children excluded)

| Defect | In the file | Flagged by the app | Missed | False alarms |
|---|---|---|---|---|
| blank_city | 495 | 495 | 0 | 0 |
| blank_country | 324 | 324 | 0 | 0 |
| unknown_country | 338 | 338 | 0 | 0 |
| unknown_terms | 433 | 433 | 0 | 0 |
| name_over_40 | 291 | 291 | 0 | 0 |
| unknown_account_group | 157 | 157 | 0 | 0 |
| unknown_currency | 300 | 300 | 0 | 0 |
| language_not_in_domain | 1000 | 1000 | 0 | 0 |

## 7. Records that reach the 'valid' output although the legacy system said they should not migrate

- Flagged for deletion in the legacy system (LOEVM = X): **360** of 461 ended up valid
- Obvious test / dummy records: **55** of 70 ended up valid
- Invalid e-mail addresses: 261 of 337 valid (no e-mail target field in this catalog, so not checked)

## 8. Output values in the valid records (checked against the source row)

| Check | Rows checked | Wrong |
|---|---|---|
| BP_EXT = KUNNR (10-digit, leading zeros kept) | 12,691 | 0 |
| TYPE = '1' for persons, '2' otherwise | 12,691 | 0 |
| POST_CODE1 keeps leading zeros | 12,691 | 0 |
| NAME_ORG1 keeps umlauts / accents | 12,691 | 0 |
| COUNTRY converted to the ISO code | 12,691 | 0 |
| ZTERM converted to the SAP key | 12,691 | 0 |
| VKORG padded to 4 characters | 12,691 | 0 |

## 9. Reconciliation and governance (explicit run)

- Every source row accounted for: **True** (extracted 16600, variance 0)
- Checks: run_counter_loaded_matches_lineage=ok, run_counter_failed_matches_transform_plus_load=ok, every_loaded_record_has_sap_number=ok, sap_numbers_are_unique=ok, load_phase_reached=NO, mapping_approved_with_segregation_of_duties=NO, validation_report_supplied=ok, validation_input_equals_extract=ok, validation_split_is_consistent=ok, load_attempts_do_not_exceed_valid_records=ok, extract_matches_landed_snapshot=ok, source_control_total_verified=ok
- Readiness gate: {'run_id': 117, 'mapping_set_id': 129, 'state': 'BLOCKED', 'mapping_status': 'DRAFT', 'run_status': 'SUCCEEDED', 'validated': True, 'open_critical_defects': 6, 'open_defects': 8, 'reasons': ['mapping set 129 is DRAFT; it must be APPROVED', '6 CRITICAL defect(s) unresolved: ERR_MANDATORY_CITY1 (495), ERR_LOOKUP_FAILED_ZTERM (433), ERR_LOOKUP_FAILED_COUNTRY (338), ERR_MANDATORY_COUNTRY (324), ERR_LOOKUP_FAILED_WAERS (300), ERR_LOOKUP_FAILED_BU_GROUP (157)']}

## Findings

- AI mapping missed 2 of 18 mappable columns: KTOKD, SPRAS
- 360 deletion-flagged and 55 test records pass through as valid: there is no row-exclusion rule
