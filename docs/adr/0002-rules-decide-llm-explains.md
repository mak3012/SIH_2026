# ADR-0002: Rules decide, LLM explains

- **Status:** Accepted
- **Date:** 2026-09-30

## Context

Alerts drive actions on a live well: a driller may stop, circulate or change mud weight because
of one. An alert that fires when it should not erodes trust. An alert that does not fire when it
should is a well-control risk.

LLMs are good at reading context and writing clear explanations. They are not deterministic,
cannot be unit-tested against every input, and can produce confident, wrong output. They are
also hard to audit after the fact: the same input may not reproduce the same decision.

## Decision

- **Rules decide.** Every alert is raised, suppressed, escalated or cleared by deterministic,
  versioned, unit-tested rules over measured data (for example: ECD within 0.02 sg of the
  LOT/FIT equivalent for 60 s). A rule's output is a pure function of its inputs and version.
- **The LLM explains.** The LLM may summarise why a rule fired, gather related facts, and
  suggest what an engineer should check. Its output is labelled as generated, cites the facts
  it used, and is stored beside the alert, never in place of it.
- The LLM has no write path to alert state. This is enforced by the API, not by prompting.
- The same split applies to extracted facts: validation rules (units, depth ordering,
  MD ≥ TVD) accept or reject a value; the LLM may propose values from a scan but cannot mark
  them validated.

## Consequences

**Good**

- Every alert is reproducible from data plus rule version. Audits and incident reviews work.
- Rules can be tested with ordinary unit tests, including edge cases from past incidents.
- If the LLM is down or slow, alerts still fire on time; only explanations are missing.

**Bad**

- Rules must be written and maintained by people with domain knowledge. Coverage grows slowly.
- Some patterns an LLM might catch will be missed until someone writes a rule for them.
  Mitigation: the LLM may *suggest* candidate rules for human review.

**Revisit** only with evidence that a learned model beats rules on recall *and* false-alarm rate
on held-out incidents, and even then as a rule input, not a replacement for the decision.
