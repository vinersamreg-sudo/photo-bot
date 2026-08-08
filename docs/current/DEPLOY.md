# Deploy

## Workflow

The backend release workflow is `.github/workflows/deploy.yml`. Pull requests run
the fast T1 profiles and never deploy. Pushes to the approved branch and manual
dispatches run the full release gate and synchronize application code to
`/opt/photo-bot`. The static site has its own workflow.

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
5. Wait for the existing workflow; do not start a duplicate run.
   The one-time `activate_gemini=true` manual input applies the approved Gemini
   flags; later deploys preserve an explicit environment rollback.
6. Workflow requires idle production before stopping the service.
7. Synchronize code while preserving mutable paths.
8. Install/check dependencies and run migrations.
9. Install/restart one hardened systemd service.
10. Before the application workflow, a root operator installs the exact Ravuna
    payment/return nginx routes with `ops/deploy_nginx_ravuna_payment.sh`; the
    workflow verifies the nginx-owned route marker before stopping the service.
    This marker is deliberately independent of the backend SHA, so routes can be
    published safely while the previous backend still serves the established flow.
11. Run health, migration, ledger, storage and runtime audits.
12. Record exact deployed SHA only after successful health checks.
13. Verify ResultURL transport without creating payment state.

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
- prove restore when the risk warrants it;
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
