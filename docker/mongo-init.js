// Runs once on a fresh volume: a database the tests fill, and a read-only login like the one a customer would give the migration.
db = db.getSiblingDB("legacy_docs");
db.createUser({ user: "legacy_ro", pwd: "Mongo_ro_Test1", roles: [{ role: "read", db: "legacy_docs" }] });
