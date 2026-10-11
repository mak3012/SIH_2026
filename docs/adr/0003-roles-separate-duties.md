# ADR-0003: Roles separate duties

- **Status:** Accepted
- **Date:** 2026-10-08

## Context

The first realm had three roles: `viewer`, `engineer` (every write) and `admin` (everything).
One engineer could select the offsets that drive a roadmap, acknowledge and shelve the alerts
it produces, and accept the extracted values both rest on. An administrator could do all of
that too. The people who use the platform have different jobs (planners before spud, wellsite
engineers and superintendents on and above the rig, data stewards for record quality), and an
insider who wants to hide or invent a hazard needs several of those powers at once.

## Decision

- Five roles that match the jobs: `planner`, `wellsite_engineer`, `superintendent`,
  `data_steward`, `admin`; plus `edge_sync` for rig-box service accounts.
- Roles grant **permissions** (`plan`, `operate`, `review`, `admin`, `sync`), defined once in
  `api.auth.PERMISSIONS` and mirrored by the web app only to hide buttons.
- No role holds both `review` and anything that produces data to be reviewed; `admin` holds
  no data permission at all and sees no wells without licence roles.
- Reads need any application role; what is read is still limited by licence blocks and JVs
  (row-level security), which stay orthogonal to roles.
- A person who needs two jobs gets two roles, visibly, in Keycloak.

## Consequences

**Good**

- Hiding or inventing a hazard needs two people; every step is in the audit log with its actor.
- An administrator's account is not a master key to the data.
- Each screen shows only what its user can do.

**Bad**

- Small teams must assign several roles to one person, which weakens the separation; Keycloak
  admin events make that visible but do not prevent it.
- Every new endpoint must name its permission; a test per role checks the mapping.
