# Sprint template

Use this for a new task or handoff. Keep it short; link to current documents rather
than pasting repository history.

## Objective

One concrete, measurable outcome.

## Evidence / problem

- What was observed.
- Where it was observed.
- What is confirmed versus assumed.

## Scope

- Files/modules/surfaces allowed.
- Explicitly excluded areas.

## Production baseline

- Relevant current SHA and runtime flags.
- Whether production mutation is allowed.
- Exact state restoration requirement.

## Test tier

Choose T0, T1, T2, T3 or T4 from `AGENTS.md`.

## External-operation budget

- Payments: none / exact count.
- OpenAI image requests: none / exact count.
- MAX real actions: none / bounded scenario.
- Browser/cabinet changes: none / exact allowed action.

## Acceptance criteria

- Observable behavior.
- Required tests/status.
- Required final production state.

## Delivery

- Commit/push/deploy authorization.
- Required report format.

## Example

**Objective:** expired MAX callback shows a visible recovery message.

**Scope:** `app/max_application.py` and its focused tests; no payment/provider work.

**Baseline:** public production remains unchanged.

**Tier:** T1.

**External budget:** zero MAX, payment and OpenAI requests.

**Acceptance:** focused MAX tests, Python compile and `git diff --check` pass; no
silent callback path remains.

**Delivery:** local changes only, no commit/deploy.
