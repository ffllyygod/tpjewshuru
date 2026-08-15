-- Runs once, on first local Postgres container init (docker-entrypoint-initdb.d).
-- Creates a second, isolated database for SuperTokens' own internal tables —
-- kept separate from our app schema (customers, orders, ...), same pattern
-- used on Railway (see JOURNAL.md).
CREATE DATABASE supertokens;
