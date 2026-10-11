-- Runs once, on first start of an empty data volume.
-- The application schema (extensions included) is owned by Alembic: apps/api/migrations.

-- Keycloak keeps its own database in the same cluster (ADR-0001).
CREATE DATABASE keycloak;
