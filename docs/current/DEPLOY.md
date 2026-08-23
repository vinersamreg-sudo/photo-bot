# Deploy

## Workflow

`.github/workflows/ci.yml` is the canonical main-application CI. Pull requests to
`main` and pushes to `main` run tests only. It has no production environment,
production secrets, SSH or deployment steps.

`.github/workflows/deploy.yml` is manual-only. Its required `target_sha` must be a
full commit SHA in canonical `origin/main` ancestry and must already have a
successful `Canonical release gate` check for that exact SHA. The validation job
has no production environment or secrets. Only the gated deployment job may use
the `production` environment and SSH.

Site testing remains in `.github/workflows/site.yml`; it is also CI-only on pull
requests and pushes. Site production deployment is separately manual in
`.github/workflows/site-deploy.yml` with the same exact-SHA ancestry and CI gate.
Changing or fast-forwarding `main` therefore cannot deploy the application or
site by itself.

Content Studio is a separate runtime and release procedure using
`ops/deploy_ravuna_content_studio.sh`, `/opt/ravuna-content/current/REVISION` and
`ravuna-content-publisher.timer`. Main-bot deployment must not deploy, restart or
rewrite Content Studio.

## Canonical history contract

- `main` is the canonical release history for both deployed runtime lineages.
- A deployed SHA may be behind `main`; equality with the current head is not
  required.
- Absence of a deployed SHA from canonical `main` ancestry is an operational
  error.
- Production deployment uses the explicit validated `target_sha`, never the
  workflow source SHA implicitly.
- Force-push and destructive history repair are prohibited.

## Preservation contract

Deployment excludes and preserves:

- `.env`;
- `venv/`;
- `data/`;
- `logs/`;
- `temp/`;
- the separately deployed live site.

Missing settings receive safe defaults through `ensure_env`. Stable product and
compatibility settings may be written explicitly through `set_env`. Operating
state flags must not be forcibly reset by an ordinary deploy.

## Fail-closed contract

A new VPS or empty `.env` must start with public access and payment/webhook
disabled, observe-only enabled, provider disabled and Robokassa sandbox/unapproved.
Production secrets alone must not silently make a fresh server public.

## Release sequence

1. Confirm scope and clean staging list.
2. Run the required targeted profile during development.
3. Run `python scripts/test_release.py` once.
4. Commit one coherent change and push once.
5. Wait for `Ravuna CI / Canonical release gate` on the exact release SHA; do not
   start a duplicate run.
6. Start the manual deploy workflow with that full `target_sha`. The optional
   one-time `activate_gemini=true` input applies the approved Gemini flags; later
   deploys preserve an explicit environment rollback.
7. Workflow revalidates canonical ancestry and exact-SHA CI before entering the
   production environment, then requires idle production before stopping the
   service.
8. Synchronize code while preserving mutable paths.
9. Install/check dependencies and run migrations.
10. Install/restart one hardened systemd service.
11. Before the application workflow, a root operator installs the exact Ravuna
    payment/return nginx routes with `ops/deploy_nginx_ravuna_payment.sh`; the
    workflow verifies the nginx-owned route marker before stopping the service.
    This marker is deliberately independent of the backend SHA, so routes can be
    published safely while the previous backend still serves the established flow.
12. Run health, migration, ledger, storage and runtime audits.
13. Record the exact validated `target_sha` only after successful health checks.
14. Verify ResultURL transport without creating payment state.

## Production snapshot

Before any deploy that can affect runtime behavior, capture:

- current deployed SHA;
- public access and observe-only;
- payment/provider/webhook/refund flags;
- Robokassa mode/approval;
- OpenAI image request guard;
- pilot controls;
- systemd/MainPID/restarts;
- SQLite/processing/orphan status.

After deploy, compare exact values. Do not compare against a generic “safe” closed
profile; compare against the real pre-deploy snapshot.

## Database and storage changes

For migrations, storage layout, retention or original delivery changes:

- require T3;
- verify a fresh encrypted backup;
- verify both the SQLite artifact and encrypted DB + private-storage recovery bundle;
- prove restore in a separate synthetic/temporary root when the risk warrants it;
- test migrations from the supported prior schema;
- verify no negative credits, duplicate entitlements or stale processing;
- verify orphan candidates before and after.

## Rollback

Rollback code only after preserving evidence and confirming database compatibility.
Never `git reset --hard` production state. Restore source from a known artifact/SHA,
retain `.env` and mutable data, restart the single service and run the same health
gates. Schema rollback requires a separate migration/restore plan.

## When deploy is unnecessary

Do not deploy for archive-only documentation, local diagnostics or test-runner
ergonomics unless runtime/deploy behavior changed or the user explicitly requests
deployment. CI evidence is still required when the task asks for commit/push.

Current operating flags are documented only in [Production](PRODUCTION.md).
