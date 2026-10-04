#!/bin/sh
# Creates the isolated database used by automated test runs.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE "${POSTGRES_TEST_DB:-migration_catalog_test}";
EOSQL
