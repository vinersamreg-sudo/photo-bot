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

An ordinary deploy requires an already provisioned, healthy polling installation.
It never creates, parses/reformats or writes `.env`, even to add missing keys.
`scripts/deploy_env_guard.py` captures SHA256, mode, UID and GID into a private
staging snapshot, then verifies them before sync, after sync, before the completion
marker and on exit (including failed deployments). No credential values enter
that snapshot. Missing/empty/symlink environments fail closed without repair.

Credential rotation, provider activation, owner/pilot configuration, dialog resets
and test grants are separate operator actions, not deploy inputs. Fresh provisioning
is also separate: defaults may initialize only a genuinely fresh environment;
an existing environment must never be completed or normalized by deployment.

Rsync also protects the separately deployed Content Studio and Admin Journal
code/entry points, site tree and retention unit files. Main-bot deploy does not
install, enable, stop or restart their services/timers. Existing shared `venv/`
packages are not installed/upgraded by this workflow. Requirements must match;
dependency-changing releases require separately approved provisioning before deploy.

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
6. Start the manual deploy workflow with only the full `target_sha`.
7. Workflow revalidates canonical ancestry and exact-SHA CI before entering the
   production environment. `scripts/build_deploy_artifact.py` uses `git archive`
   with `core.autocrlf=false`/`core.eol=lf`, verifies every member against its Git
   blob and emits a checksum manifest. Untracked/dirty Windows bytes and `.git`
   never enter the artifact. Unsupported links, private tracked data, export
   substitutions and any byte mismatch fail closed. The VPS verifies the extracted
   artifact under a unique private `/var/tmp/ravuna-deploy-*` directory.
8. Capture live health/configuration, stage the target database code, create a consistent SQLite
   copy, migrate only that copy to the code-declared schema version, and require
   `quick_check=ok`, foreign-key integrity and unchanged commerce fingerprints.
9. Run the explicit main-bot production preflight **in isolated staging before
   stopping the service**, using the existing venv, `APP_ENV=test`, a staging
   `BASE_DIR` and `MAX_TRANSPORT_MODE=disabled`. Changed requirements or missing
   target dependencies fail before live changes; provision dependency upgrades separately.
   This preflight does not discover Content
    Studio or repository-policy tests and does not require `.git`; canonical CI
   still runs the complete release suite. Health unit tests model systemd explicitly;
   actual systemd/PID/lock/polling checks remain in live pre/post health gates.
10. Require zero processing/dialog work and reserved credits, create and verify a
    fresh SQLite backup, stop only main-bot, and sync the verified artifact while
    protecting mutable/separate-runtime paths. Check dependencies and migrate the
    live database; require unchanged commerce fingerprints, schema and integrity.
11. Install the main-bot unit and execute **exactly one** restart on a successful
    deploy. Require readiness, a new PID, one runtime, zero automatic restarts and
    MAX connectivity and a successful polling response newer than the restart.
    No duplicate poller or second restart is launched as a test.

The root-owned deploy allowlist remains `ops/photo-bot-deploy.sudoers`; any
allowlist change requires a separate root operation and `visudo -cf` validation.
This workflow neither changes the allowlist nor installs retention/admin units.
12. Before the application workflow, a root operator installs the exact Ravuna
    payment/return nginx routes with `ops/deploy_nginx_ravuna_payment.sh`; the
    workflow verifies the nginx-owned route marker before stopping the service.
    This marker is deliberately independent of the backend SHA, so routes can be
    published safely while the previous backend still serves the established flow.
13. Run health, migration, ledger, storage and runtime audits.
14. Record the exact validated `target_sha` only after successful health checks.
15. Verify ResultURL transport without creating payment state.

No automatic rollback is performed on failure. Preserve the private staging
evidence and report whether the service was stopped or code/migration already
applied; recover only through an explicitly authorized, schema-compatible procedure.
Staging evidence is not uploaded to GitHub and is not automatically deleted (its
SQLite copy contains private data); remove it later under the operator retention policy.

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
- derive the expected schema from `app.database.CURRENT_SCHEMA_VERSION`; backup
  and deploy checks fail closed for a newer or otherwise unsupported schema;
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
