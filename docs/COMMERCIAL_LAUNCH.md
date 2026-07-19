# Commercial Launch Runbook

## Current verdict

The codebase contains a sandbox-capable, exact-version payment architecture, but real sales remain disabled. This is a commercial MVP candidate for closed technical validation, not permission to accept money.

The official site is also a launch candidate, not evidence of commercial readiness. Before Robokassa moderation or any payment, DNS and trusted HTTPS must work publicly, the seller's confirmed INN and contact must be published, every legal page must receive specialist review, and `PIXORA_SITE_DEPLOY_ENABLED` may be enabled only after the site runbook gate passes.

## Gate A — owner sandbox

1. Keep pilot limit 0 and enable handlers for owner only.
2. Configure Robokassa test merchant and public HTTPS ResultURL.
3. Create one 49 RUB sandbox order for one version.
4. Confirm valid callback, exact-version unlock, original delivery and repeat delivery.
5. Replay callback and verify no double unlock/delivery.
6. Simulate MAX delivery failure and verify `delivery_pending` recovery.
7. Prepare a refund; exercise provider refund only when operation key and Password3 are confirmed.
8. Restore observe-only and all payment flags off; run backup, restore, cleanup and health report.

## Gate B — first real 49 RUB payment

Requires owner approval, verified merchant cabinet, fiscal/legal approval, HTTPS/proxy/security controls, support availability and a documented rollback. Enable production only for the bounded transaction, reconcile provider cabinet/SQLite/receipt/original, then decide whether to keep or disable it. This task does not authorize that transaction.

## Gate C — five-user pilot

Invite exactly the first five secret allowlist entries. Payments may remain off; pilot can validate generation and support separately. Stop on payment/data loss, original leakage, stuck processing, repeated delivery failure, failed restore, uncontrolled cost or abuse incident.

## Daily checks

```bash
python -m app.main health-report --online
python -m app.main pilot-status
python -m app.main payment-status
python -m app.main storage-status
python -m app.main backup-status
python -m app.main cleanup-status
python -m app.main cost-status
```

Economics are estimates until reconciled. `ROBOKASSA_COMMISSION_PERCENT=0` means unknown/not configured, not zero real commission.

## Legal/fiscal checklist

Obtain specialist approval for operator identity and contacts, offer, privacy and personal-data consent, external AI processing disclosure, photo/depicted-person rights, minors, cross-border/data-location position, retention/deletion, cancellation/refund/failed delivery, consumer claims, fiscal receipt/tax/nomenclature, support and accounting retention. Do not invent or publish final legal text from engineering documentation.
