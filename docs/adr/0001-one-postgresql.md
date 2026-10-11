# ADR-0001: One PostgreSQL for spatial, time-series, vector and relational data

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

The platform stores four kinds of data:

- **Relational:** wells, wellbores, reports, extracted facts, provenance, alerts, users.
- **Spatial:** surface locations, trajectories, field outlines.
- **Time-series:** real-time drilling channels (ROP, WOB, SPP, ECD) at up to 1 Hz per well.
- **Vector:** embeddings of facts and document passages for retrieval.

The obvious split is four stores: Postgres, a GIS store, a time-series DB (InfluxDB), and a
vector DB (Qdrant, Weaviate). Every answer we give joins across these: "show NPT events within
50 m TVD of the casing shoe on wells in this block, with the report page they came from."
Across four stores that join lives in application code, with four backup regimes, four auth
models and no transaction spanning them.

The team is small. Every extra stateful service is paid for in operations for the life of the
project.

## Decision

Use one PostgreSQL cluster with extensions:

- **PostGIS** for spatial types and indexes.
- **TimescaleDB** hypertables for channels, with compression and continuous aggregates.
- **pgvector** for embeddings (HNSW indexes).

Dev and CI use the `timescale/timescaledb-ha` image, which ships all three. Keycloak keeps its
own database in the same cluster. Only large binary objects (scans, raw files) live outside,
in S3-compatible object storage, referenced by key from Postgres.

## Consequences

**Good**

- Cross-kind questions are one SQL query, in one transaction, under one set of row-level
  permissions.
- Provenance is a foreign key, not a cross-system reference that can dangle.
- One backup, one restore drill, one connection pool, one thing to monitor.

**Bad**

- Vector search at very large scale (hundreds of millions of vectors) is slower than dedicated
  engines. Expected corpus is well below that.
- Time-series ingest at many thousands of wells at high frequency could outgrow one node.
  Timescale multi-node or offloading raw channels to object storage are the exits.
- Some TimescaleDB features (compression, continuous aggregates) are under the Timescale
  License, not Apache 2.0. See [Licences](../licences.md).
- Extension upgrades must be coordinated with Postgres major upgrades.

**Revisit when** vector count passes ~50 M, sustained channel ingest passes ~50 k rows/s, or a
query class cannot meet its latency target after indexing.
