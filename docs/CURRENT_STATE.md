# Current State

Актуально на 17.07.2026 после повторной owner-only visual validation.

## Подтверждено в production

- deployed commit `0f91627d5bd532da24e21c49c2047545a43044f6`;
- `photo-bot.service` и MAX polling здоровы; `MAX_POLL_OBSERVE_ONLY=true`;
- owner allowlist настроен, pilot limit 0, пользовательские handlers выключены;
- OpenAI `gpt-image-2` — единственный production image provider; router/composite/segmentation выключены;
- SQLite migration v6, `PRAGMA quick_check=ok`, pending/processing attempts 0, processing dialogs 0, processing GalleryVersions 0;
- owner dialog восстановлен в `main_menu`;
- encrypted backup/restore/off-site lifecycle подтверждён; public launch readiness остаётся false.

## Реальный owner E2E

- выполнено ровно 5/5 разрешённых production image requests через MAX;
- все 5 attempts завершились `succeeded`, все previews доставлены с watermark;
- созданы GalleryVersions 11–15; quota списана ровно по одной попытке на доставленную версию;
- проверены три Correction, Repeat, Gallery, History, Favorite и Current best;
- средняя provider duration 63.824 с; внутренний cost reserve 50 RUB;
- identity оставалась узнаваемой; recolor куртки и correction резкости сработали;
- остаётся visual drift: Repeat заметно изменил композицию, а первый background edit не дал требуемую резкость сразу;
- разговорный parser fallback в повторной серии не проверялся из-за жёсткого лимита; semantic parser не добавлен.

Полный отчёт: `docs/AI_BRAIN_VISUAL_VALIDATION.md`. Redacted production evidence собирается workflow `owner-e2e-audit.yml` без owner ID, токенов и приватных путей.

## Исправлено по итогам аудита

- добавлен read-only owner E2E exporter и workflow с проверкой observe-only/processing/SQLite/orphans;
- добавлен regression на точный переход Gallery history «Предыдущая»;
- исправлен ложный orphan: session `metadata.json` теперь считается referenced и не удаляется maintenance;
- после deploy повторный аудит подтвердил orphan private files 0.

Local/CI suite: 147 unittest tests, secret scan и production healthcheck зелёные.

## Не готово

Эквайринг, verified payment callback/refunds, окончательные legal documents/operator details, support process, public deep link/site launch, внешний пилот 5 пользователей и 10/20-user evidence. Long polling допустим для малого allowlisted pilot, но не для сотен публичных пользователей. До платного публичного запуска нужен visual quality gate для identity/scene drift.
