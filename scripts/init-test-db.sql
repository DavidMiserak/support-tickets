-- Creates the isolated test database on first volume initialization.
-- (docker-entrypoint-initdb.d runs this only when the data directory is empty.)
CREATE DATABASE ticketsupport_test;
