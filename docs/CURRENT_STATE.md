# Current State

Актуально на 17.07.2026 до финального deploy этого спринта.

## Реализовано локально

- direct MAX flow `/start → photo → text → result`, optional «Идеи»;
- short UX, exact CTA order, quota hidden except last/exhausted;
- correction/repeat lineage, Gallery cards/history/current best/favorite/delete;
- exact invalid/size/timeout/network/quota/policy/delivery/budget errors;
- one status message, edit-on-success/error, restart recovery without quota debit;
- `gpt-image-2` production guard and disabled experimental processing router;
- migration v6 `product_events` without prompt/image/platform identity;
- owner + ordered pilot allowlist with 0/5/10/20 activation;
- encrypted SQLite backup, restore-test, off-site workflow, retention;
- dry-run/execute cleanup for retention, temp and orphan files;
- privacy-safe `python -m app.main launch-status`;
- site copy aligned with the bot and model limitations.

Local suite: 153 pytest checks, 145 unittest checks and 26 subtests; no real OpenAI image request was made by this sprint at this stage.

## Последнее фактически проверенное production состояние до изменений

Commit `0def471`: `photo-bot.service` active, MAX polling healthy, `observe_only=true`, owner allowlist configured, SQLite quick_check `ok`, migration v5, OpenAI auth/model check green for `gpt-image-2`, zero stale processing temp orphans. Production had router enabled while composite/segmentation were disabled; this contradiction is removed in the pending deploy.

## Не подтверждено до deploy/E2E

- production migration v6 and current UX;
- first scheduled/manual encrypted backup artifact and independent restore;
- production cleanup report and strict launch status;
- three owner E2E paths and their real image request count/latency;
- readiness for five users.

## Не готово

Эквайринг, verified payment callback/refunds, окончательные legal documents/operator details, support process, public deep link/site launch, 10/20-user evidence, public scale. Long polling is accepted for a small allowlisted pilot, not for hundreds of public users.
