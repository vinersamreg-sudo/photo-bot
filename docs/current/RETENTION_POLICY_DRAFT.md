# Retention 14/60 — draft; do not publish or activate

Current public evidence, 12 November 2026: [offer 1.4 §34, §38](https://ravuna.ru/legal/offer.html),
[privacy 1.1 §14](https://ravuna.ru/legal/privacy.html), [consent §6](https://ravuna.ru/legal/personal-data.html),
[terms](https://ravuna.ru/legal/terms.html) and the FAQ describe demo/source up to
30 days, paid work up to 180 days, trash up to 30 days. The code defaults are
30/180/30; configuration can override these defaults. A 108-day public or
repository rule was not found. This is not a finding about all historic terms.

## Coordinated publication draft

Use a new revision for each changed page; assign the actual effective date at
publication. Preserve the previous immutable page versions and the related
evidence. Do not put this draft in `site/public` or represent it as current law
or a new consent.

**Offer, retention section:** «Для новых работ по этой редакции бесплатные
результаты и необходимые для них изображения хранятся 14 дней, оплаченные —
60 дней. Срок считается с готовности результата; если связанная оплата или
первая подтверждённая выдача оригинала произошли позднее, отсчёт начинается
с более позднего события. Повторное скачивание не продлевает срок.
При незавершённой оплате, обработке, выдаче или споре
нужные файлы сохраняются до разрешения ситуации. Для прежних работ и заказов
сохраняются условия, действовавшие при оформлении; новые сроки их не сокращают.
Удаление фотографий по сроку не прекращает действие неиспользованных пакетов
и не удаляет баланс, платежи, чеки и обязательные записи учёта».

**Privacy, retention section:** «У новых работ бесплатные результаты и связанные
исходники и превью хранятся 14 дней, оплаченные — 60 дней по правилам оферты.
Общие исходники сохраняются, пока нужны другой доступной или защищённой работе.
Если пользователь удалил работу, она скрывается и помещается в корзину на срок
до 30 дней; файлы, которые ещё нужны для исполнения обязательств, не удаляются
досрочно. Резервные копии не видны в галерее и заменяются по отдельному
расписанию. После аварийного восстановления удалённые работы не возвращаются
в публичный доступ автоматически. Платёжные и обязательные учётные данные
хранятся отдельно от срока хранения фото».

**Правила и FAQ, включая структурированный FAQ:** «Для новых работ:
бесплатные — 14 дней, оплаченные — 60 дней.
Прежние условия сохраняются. Скачайте важные изображения до истечения срока».
В разделе удаления сохранить описанную корзину и отсылку к оферте.

**Форма согласия, §6:** заменить только описание практических сроков согласованным
текстом политики. Срок действия согласия, его отзыв и юридическое основание
обработки не меняются этим PR. До публикации отдельно проверить, требуется ли
повторное предъявление или отдельное действие пользователя; не считать
установку флага доказательством согласия. При необходимости подтверждение
добавляется до активации. Отдельно проверить обещания о физическом удалении
и сроки существования защищённых recovery-точек.

## Storage enforcement

Schema 17 records terms for each new gallery item and each newly created
payment order, only when the explicit timezone-qualified effective timestamp
is active. It never backfills existing records. Old orders retain their
original identity on reuse, refresh and duplicate callback. A work which
consumes an unproven/older paid lot or original entitlement is held even
if the work itself is new. The same applies to unclear lineage, uncertain
delivery or unknown versions of the terms. Expiry for a proven result uses
successful completion, actual funding reservation, attributed order and
first confirmed delivery timestamps; a re-download or unrelated purchase does
not reset its clock.
Known referral grants for new work are free unless the result also has
attributable paid funding. Unresolved legacy cases are counted, not guessed.

The planner keeps in-flight/payment/context/trash/shared-path guards.
Support cases under dispute must be reconciled and represented in the
account-scoped `retention_holds` table before execution; no dispute text or
personal data belongs in the planner or its output. The hold covers all work
of the account until the case is independently closed. Cleanup is isolated
from gallery display, MAX, provider cleanup and financial audit; the fourth
result never triggers deletion. Execution is capped at ten, only under an
active effective timestamp and a separate cleanup flag. Duration values are
stored on the work/order; SQLite rejects updates and replacements of pinned
rows. A later verified payment/delivery can extend protection. Schema 17
blocks whole-work purge even with no stamped rows and switches OFF.
Opening/favouriting does
not reset the retention clock. Missing files are counted; no fabricated
photos or grants are created. Orders, grants, balances and unused credits
are not changed by the planner.

## Release gates

Use `scripts/rehearse_retention_migration.py --help` on an isolated verified
schema-16 restore, with separate private scratch. It compares existing tables,
financial records and linked media checksums, verifies repeated initialization,
simulates process termination immediately before and after commit and asserts
that disabled cleanup cannot modify the copy. Run it locally only. Restore into
a separate directory or volume; never substitute an older backup over the
working database to undo a migration after new payments or uploads.

1. Separate GO: confirm legal wording, notice/acceptance requirements, exact
   effective instant, evidence of the currently published pages and deployment
   of matching MAX copy. Do not publish this draft or activate retention from CI.
2. Separate GO: rehearsal of 16→17 on a verified restored copy, backup, staging,
   and deployment with both retention switches inactive. Preserve the historical
   cohort unchanged; configure the actual effective instant only after proof of
   the matching public terms. Start with the cleanup flag still OFF.
3. Separate GO: aggregate read-only dry-run, sample evidence for free/paid,
   verify obligations/finance/files and independently reconcile open disputes
   which are not represented in the app, then authorize first <=10 deletions.
   Never use the legacy cleanup to sidestep an unresolved obligation.
   No proof for old work means no automatic deletion, not proof of perpetual
   retention. Quantify the held cohort and establish a separate lawful disposition.
4. Keep independent verified recovery available before authorizing any
   execution. The independent recovery candidate is not a dependency of
   schema 17 and does not replace the current Hetzner backups.
