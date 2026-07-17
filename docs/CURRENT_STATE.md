# Current State

Актуально на 17.07.2026 после production deploy этапа closed-pilot hardening.

## Подтверждено в production

- commit `7deb6c0` развёрнут workflow `29589482028`; production marker совпал с полным Git SHA;
- `photo-bot.service` и MAX polling здоровы, Python 3.12, один MainPID и single-instance lock;
- `MAX_POLL_OBSERVE_ONLY=true`, owner allowlist настроен, pilot limit 0, пользовательские handlers выключены;
- OpenAI auth/model check зелёный для `gpt-image-2`; router/composite/segmentation выключены;
- SQLite migration v6 и `PRAGMA quick_check=ok`; активных/stale processing нет;
- encrypted backup workflow `29590129626` создал online snapshot, проверил restore на VPS, скопировал только encrypted artifact off-site и независимо восстановил его на GitHub runner;
- off-site artifact хранится 14 дней; migration v6 и quick_check подтверждены после расшифровки;
- cleanup удалил 3 подтверждённых orphan-файла (1 256 651 байт), после чего remaining orphan/temp count равен 0;
- `launch-status --strict`: operational readiness `true`, public launch readiness `false`, observe-only сохранён.

Изменения статического сайта не публикуются автоматически; production сайта этим backend deploy не менялся.

## Реализовано и проверено тестами

- direct MAX flow `/start → photo → text → result`, optional «Идеи»;
- short UX, exact CTA order, quota hidden except last/exhausted;
- correction/repeat lineage, Gallery cards/history/current best/favorite/delete;
- exact invalid/size/timeout/network/quota/policy/delivery/budget errors;
- one status message, edit-on-success/error, crash recovery without duplicate provider replay;
- migration v6 privacy-minimal `product_events` without prompt/image/platform identity;
- owner + ordered pilot allowlist with fail-closed 0/5/10/20 activation;
- encrypted backup/restore/off-site/retention and scoped dry-run/execute cleanup;
- privacy-safe `python -m app.main launch-status`;
- site copy aligned with the bot and its current payment/model limitations.

Local suite: 153 pytest checks, 145 unittest checks, 26 subtests and 8 site tests. No real OpenAI image request was made by this sprint at this stage.

## Ещё не подтверждено live

- owner E2E: new generation, correction, repeat + Gallery + delete;
- пять последовательных полных успешных owner E2E;
- фактические latency/cost/error/delivery metrics нового UX;
- readiness for five external pilot users.

## Не готово

Эквайринг, verified payment callback/refunds, окончательные legal documents/operator details, support process, public deep link/site launch, 10/20-user evidence и public scale. Long polling допустим для малого allowlisted pilot, но не для сотен публичных пользователей.
