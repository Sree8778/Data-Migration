# Source format comparison

Reference: `customers_utf8.csv` (1980 rows, 10 columns). Control total (1980 rows) sent with every file except BAD-truncated-silent.

| Format | File | KB | Format detected | Rows | Control total | Content fingerprint | Verdict |
|---|---|---|---|---|---|---|---|
| csv-utf8 | customers_utf8.csv | 236 | delimited | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| csv-lf-endings | customers_lf.csv | 234 | delimited | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| csv-utf8-bom | customers_bom.csv | 236 | delimited | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| semicolon-cp1252 | customers_semicolon.txt | 236 | delimited | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| pipe-gzip | customers_pipe.psv.gz | 77 | delimited | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| tsv-utf16 | customers_tab.tsv | 473 | delimited | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| csv-in-zip | customers.zip | 78 | delimited | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| excel | customers.xlsx | 164 | excel | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| excel-banner-rows | customers_banner.xlsx | 165 | excel | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| csv-banner-rows | customers_banner.csv | 236 | delimited | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| json-array | customers.json | 525 | json | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| ndjson | customers.ndjson | 523 | json | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| fixed-width | customers.dat | 440 | fixed | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| parquet | customers.parquet | 121 | parquet | 1980 | MATCH | `cc5b403783be` | IDENTICAL |
| BAD-truncated-silent | bad_truncated.csv | 236 | delimited | 1973 | NOT_PROVIDED | `8f50c5115102` | DIFFERS (as expected) |
| BAD-one-value-changed | bad_changed.csv | 236 | delimited | 1980 | MATCH | `8875910164a8` | DIFFERS (as expected) |
| BAD-control-total | bad_control.csv | 236 | - | - | - | - | REJECTED (as expected) |

## Details

- **fixed-width**: warning: fixed-width values are right-trimmed (padding is not data); leading spaces are kept
- **BAD-truncated-silent**: content differs - row count 1973 vs reference 1980; profile differs in: CUST_ID, TAX_ID, EMAIL, PHONE, ADDR_STREET, CITY, COUNTRY, PAYMENT_TERMS
- **BAD-one-value-changed**: content differs - row 501, column EMAIL: 'tampered@example.com' vs reference 'accounts@xifalo500.example.com'; profile differs in: EMAIL
- **BAD-control-total**: control total mismatch: 1973 row(s) were read but the source system reported 1980

## Reference profile (all identical formats share this)

| Column | Type | Nulls | Distinct | Min len | Max len |
|---|---|---|---|---|---|
| CUST_ID | BIGINT | 0 | 1980 | 6 | 6 |
| CUST_NAME | VARCHAR | 0 | 1920 | 14 | 70 |
| TAX_ID | VARCHAR | 537 | 1423 | 11 | 11 |
| EMAIL | VARCHAR | 440 | 1520 | 28 | 33 |
| PHONE | VARCHAR | 373 | 1587 | 14 | 15 |
| ADDR_STREET | VARCHAR | 0 | 1940 | 15 | 26 |
| CITY | VARCHAR | 62 | 33 | 4 | 9 |
| COUNTRY | VARCHAR | 42 | 22 | 2 | 14 |
| POSTAL_CODE | VARCHAR | 121 | 1790 | 5 | 8 |
| PAYMENT_TERMS | VARCHAR | 262 | 7 | 3 | 13 |
