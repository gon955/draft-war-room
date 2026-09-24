-- Runs once, when the postgres container initialises an empty data directory.
--
-- The suite drops and recreates every table, so it must never point at the
-- development database. warroom/tests/conftest.py enforces that by refusing any
-- database whose name does not end in '_test' — so this one has to exist before
-- `pytest` can run at all.
CREATE DATABASE warroom_test;
