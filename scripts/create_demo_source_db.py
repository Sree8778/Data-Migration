"""Create a demo LEGACY database to browse and import through the Add Source wizard.

    python scripts/create_demo_source_db.py [--rows-from mock_data/legacy_ar_customers.csv]

Creates database `legacy_demo` on the dev PostgreSQL server with a READ-ONLY login `legacy_ro` (password `legacy_ro_pw`, demo only):
  ar.customers           1,980 dirty customer rows (the mock extract), primary key cust_id
  ar.customer_contacts   one contact per customer, foreign key to ar.customers
  ar.payment_terms       small code table
  sales.orders / sales.order_lines   orders with a composite-key line table
  ar.v_open_customers    a view
Re-running rebuilds everything. Not part of the product: it only gives the wizard something to connect to.
"""

from __future__ import annotations

import argparse
import csv
import io
import random
from pathlib import Path

import psycopg2
from sqlalchemy.engine import make_url

from src.db.session import get_database_url

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows-from", default=str(ROOT / "mock_data" / "legacy_ar_customers.csv"))
    ap.add_argument("--db", default="legacy_demo")
    args = ap.parse_args()
    url = make_url(get_database_url())
    admin = psycopg2.connect(host=url.host, port=url.port, dbname="postgres", user=url.username, password=url.password)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (args.db,))
    if not cur.fetchone():
        cur.execute(f'CREATE DATABASE "{args.db}"')
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'legacy_ro'")
    if not cur.fetchone():
        cur.execute("CREATE ROLE legacy_ro LOGIN PASSWORD 'legacy_ro_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE")
    admin.close()

    conn = psycopg2.connect(host=url.host, port=url.port, dbname=args.db, user=url.username, password=url.password)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS ar CASCADE; DROP SCHEMA IF EXISTS sales CASCADE; CREATE SCHEMA ar; CREATE SCHEMA sales;")
    cur.execute("""
        CREATE TABLE ar.customers (cust_id integer PRIMARY KEY, cust_name varchar(80) NOT NULL, tax_id varchar(20), email varchar(80), phone varchar(30),
            addr_street varchar(80), city varchar(40), country varchar(40), postal_code varchar(12), payment_terms varchar(20));
        CREATE TABLE ar.customer_contacts (contact_id serial PRIMARY KEY, cust_id integer NOT NULL REFERENCES ar.customers, contact_name varchar(60), role varchar(30));
        CREATE TABLE ar.payment_terms (code varchar(20) PRIMARY KEY, description varchar(60), days integer);
        CREATE TABLE sales.orders (order_id integer PRIMARY KEY, cust_id integer, order_date date, total numeric(14,2), currency char(3));
        CREATE TABLE sales.order_lines (order_id integer, line_no integer, sku varchar(30), qty integer, price numeric(12,2), PRIMARY KEY (order_id, line_no));
    """)
    with open(args.rows_from, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        buf = io.StringIO()
        writer = csv.writer(buf)
        ids = []
        for row in reader:
            ids.append(int(row["CUST_ID"]))
            writer.writerow([row[c] if row[c] != "" else "\\N" for c in ("CUST_ID", "CUST_NAME", "TAX_ID", "EMAIL", "PHONE", "ADDR_STREET", "CITY", "COUNTRY", "POSTAL_CODE", "PAYMENT_TERMS")])
    buf.seek(0)
    cur.copy_expert("COPY ar.customers FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf)
    rng = random.Random(5)
    cur.executemany("INSERT INTO ar.customer_contacts (cust_id, contact_name, role) VALUES (%s, %s, %s)",
                    [(i, f"Contact {i}", rng.choice(["Accounts payable", "Buyer", "Finance director"])) for i in ids])
    cur.executemany("INSERT INTO ar.payment_terms VALUES (%s, %s, %s)", [("NET30", "Net 30 days", 30), ("NET60", "Net 60 days", 60), ("COD", "Cash on delivery", 0), ("NET90", "Net 90 days", 90)])
    orders = [(n, rng.choice(ids), f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}", round(rng.uniform(100, 90000), 2), rng.choice(["EUR", "USD", "GBP"])) for n in range(1, 5001)]
    cur.executemany("INSERT INTO sales.orders VALUES (%s, %s, %s, %s, %s)", orders)
    cur.executemany("INSERT INTO sales.order_lines VALUES (%s, %s, %s, %s, %s)",
                    [(o[0], ln, f"SKU-{rng.randint(1, 400):04d}", rng.randint(1, 50), round(rng.uniform(1, 900), 2)) for o in orders[:2000] for ln in range(1, rng.randint(2, 4))])
    cur.execute("CREATE VIEW ar.v_open_customers AS SELECT cust_id, cust_name, city, country FROM ar.customers WHERE postal_code IS NOT NULL")
    cur.execute("ANALYZE")
    cur.execute(f'GRANT CONNECT ON DATABASE "{args.db}" TO legacy_ro')
    cur.execute("GRANT USAGE ON SCHEMA ar, sales TO legacy_ro; GRANT SELECT ON ALL TABLES IN SCHEMA ar, sales TO legacy_ro;")
    cur.execute("SELECT count(*) FROM ar.customers")
    print(f"database {args.db}: ar.customers = {cur.fetchone()[0]} rows; login legacy_ro / legacy_ro_pw (read-only)")
    conn.close()


if __name__ == "__main__":
    main()
