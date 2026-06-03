-- Creates the isolated test database on first volume initialization.
-- docker-entrypoint-initdb.d runs this script ONLY when the Postgres data
-- directory is empty (fresh volume), so duplicate-database errors cannot
-- occur in normal Docker Compose usage.
--
-- Do NOT run this file manually against a running Postgres instance; use
-- the idempotent guard in the Makefile instead:  make test-db
CREATE DATABASE ticketsupport_test;
