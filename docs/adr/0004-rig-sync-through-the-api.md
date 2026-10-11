# ADR-0004: Rig boxes sync through the API, not database replication

- **Status:** Accepted
- **Date:** 2026-10-08

## Context

A rig box must work for days without a link (VSAT drops, bandwidth is scarce) and then catch
up. Two directions: headquarters' records for the well (offsets, documents, roadmap, detector
bank) go down; the rig's alerts, acknowledgements, shelves, voice notes and audit trail go up.
PostgreSQL logical replication tolerates outages (slots keep WAL) and needs no code.

## Decision

Sync through the API with an outbox, and package the downward data:

- **Down:** a hazard package (`tar.gz`) built under row-level security for the rig's licences,
  loaded by upsert. Rows keep headquarters' ids.
- **Up:** triggers queue changed rows in `edge_outbox`; the agent sends batches to
  `POST /edge/sync` with the rig's audit entries. Headquarters maps rig ids to its own
  (`edge_record`), so batches are idempotent, and checks the rig's audit chain entry by entry.
- Rig-made rows take ids from 2^50 up, so they never collide with packaged rows.

Logical replication was rejected because:

- Publications are table-wide (row filters cannot express "what this account may see"), and a
  subscription bypasses row-level security; the API path keeps RLS on both directions.
- The rig's audit chain cannot be replicated into headquarters' chain (both assign ids and
  hashes); through the API it is verified and embedded instead.
- Replication slots on a box offline for days hold WAL without bound on small disks.
- A replication connection over VSAT is a database port exposed between sites; the API is
  already TLS, authenticated and audited.

## Consequences

**Good**

- The same authorization and audit as any user; a stolen box can be cut off in Keycloak.
- Re-sending is always safe; a failed sync leaves the outbox and watermark untouched.
- Edits made on a box while offline are caught by the audit-chain check at the next sync.

**Bad**

- Code to maintain (`api/edge.py`); new rig-made tables must be added to the outbox and upload.
- Deletions at headquarters do not reach the rig; full-rate time series does not go up (the
  rig's WITSML store already replicates to the real-time centre).
