# AI repository context

Перед изменениями прочитать `docs/PROJECT_BIBLE.md`, `docs/CURRENT_STATE.md`,
`docs/DECISIONS.md` и релевантный профильный документ. Это краткая передача
контекста новому Codex-чату; при конфликте приоритет у Project Bible и ADR.

## Неподвижные границы

- Публичный бренд — **Pixora**, техническое имя backend/repository — `photo-bot`.
- Source of truth: `C:\Users\viner\Documents\Codex\photo-bot`, ветка `main`.
- Production: Hetzner `ai-prod-01`, `/opt/photo-bot`, `photoapp`, Python 3.12,
  systemd `photo-bot.service`; deploy только GitHub Actions.
- TripDay не связан с проектом; не читать и не изменять.
- Секреты и user IDs не печатать, не логировать и не коммитить.
- Не добавлять Docker/Redis/Celery/Kubernetes/proxy без отдельного ADR.
- Production по умолчанию `MAX_POLL_OBSERVE_ONLY=true`; owner allowlist сохранять.
- Не выполнять реальные OpenAI image requests без явного разрешения и лимита.

## Текущее состояние — 16.07.2026

Работает direct MAX flow `/start → фото → текст → preview`, watermark, Gallery,
versions, corrections/repeat, favorites, collections/search, TTL/rate/budget guards,
SQLite, MAX polling owner-only/observe-only, OpenAI `images.edit`, CI/CD и сайт.
Payment остаётся placeholder, публичный webhook/массовый запуск не готовы.

AI Brain v3 строит provider-neutral structured EditPlan и English-only technical
prompt. Hybrid layer маршрутизирует в `AI_GENERATION`,
`REAL_BACKGROUND_COMPOSITE`, `LOCAL_AI_EDIT`, `ENHANCEMENT`, `RESTORATION`.
ProcessingPlan и asset/mask/provider lineage сохраняются в migration v5 и Gallery.
Correction наследует выбранную parent version; Repeat наследует полный plan.

Local enhancement/composite не вызывают OpenAI. Real composite fail-closed:
никаких случайных internet assets и silent AI fallback. Production catalog сейчас
валиден, но пуст; rembg/model не установлены. Поэтому flags обязаны оставаться:

```text
PROCESSING_MODE_ROUTER_ENABLED=true
REAL_BACKGROUND_COMPOSITE_ENABLED=false
ALLOW_AI_BACKGROUND_FALLBACK=false
SEGMENTATION_BACKEND=disabled
LOCAL_AI_FINISHING_ENABLED=false
```

## Проверки и следующий gate

Полный suite на момент спринта: 131 тест после deploy-policy regression; actual
OpenAI image requests в hybrid sprint: 0. Offline fixed-mask 1024 benchmark
проверяет только Pillow composite/enhancement, не rembg quality/RSS.

До включения real backgrounds нужны лицензированные assets с records/checksums,
pinned ONNX source/license/SHA, VPS CPU/RSS/timeout benchmark и отдельная owner-only
visual validation. Читайте:

- `docs/PROCESSING_MODES_AUDIT.md`;
- `docs/PROCESSING_MODES_ARCHITECTURE.md`;
- `docs/SEGMENTATION_EVALUATION.md`;
- `docs/BACKGROUND_ASSET_POLICY.md`;
- `docs/REAL_BACKGROUND_PIPELINE.md`.
