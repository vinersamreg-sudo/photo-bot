# Processing modes architecture

> Status 17.07.2026: experimental and dormant. Pixora v1 production has
> `PROCESSING_MODE_ROUTER_ENABLED=false` and sends image edits only to OpenAI
> `gpt-image-2`. Reconsider this architecture only after closed-pilot data.

## Контракт

`EditPlan` описывает намерение, `ModeRouter` выбирает технологию, а
`ProcessingPlan` является неизменяемым execution contract. Он содержит mode,
обоснование и confidence, fallback, asset source/id/checksum, mask strategy,
provider/model, pipeline version и preservation flags.

| Mode | Назначение | Исполнитель | Внешний image call |
|---|---|---|---:|
| `AI_GENERATION` | вымышленные/явно AI-сцены | OpenAI `images.edit` | 1 |
| `REAL_BACKGROUND_COMPOSITE` | реальная локация из каталога | segmentation + Pillow | 0 |
| `LOCAL_AI_EDIT` | одежда, поза, объекты, локальные изменения | OpenAI `images.edit` | 1 |
| `ENHANCEMENT` | консервативная резкость/контраст | Pillow | 0 |
| `RESTORATION` | повреждения и старые фотографии | OpenAI `images.edit` | 1 |

## Маршрутизация

Реальная категория (`rocky_mountains`, `forest`, `office` и т. п.) выбирает
composite. Он исполним только при одновременно включённом feature flag, подходящем
активном asset и доступном segmenter. Без них router не подменяет запрос AI-фоном:
план требует подтверждения/нового выбора и останавливается fail-closed. Явная фраза
про AI-фон либо фантазийная сцена выбирает generation. Restoration и enhancement
имеют отдельные deterministic ветки. Остальные локальные изменения идут в
`LOCAL_AI_EDIT`.

Correction наследует asset lineage родительской версии и меняет только затронутые
поля. Repeat копирует весь родительский processing plan. Любая версия хранит
`parent_version_id`, `processing_plan_json`, mode/provider/model/pipeline и asset
metadata. Старые записи backfill-ятся как legacy plan, поэтому схема обратно
совместима.

## Fallback policy

Fallback записывается как возможность, но не исполняется молча. Переключение
`REAL_BACKGROUND_COMPOSITE → AI_GENERATION` меняет природу результата и требует
явного пользовательского выбора. Техническая ошибка mask не может создавать
GalleryVersion или расходовать demo quota. AI finishing после local composite
запрещён до отдельной visual validation masked preservation.

## Feature flags

Безопасный production baseline:

```text
PROCESSING_MODE_ROUTER_ENABLED=false
REAL_BACKGROUND_COMPOSITE_ENABLED=false
ALLOW_AI_BACKGROUND_FALLBACK=false
SEGMENTATION_BACKEND=disabled
LOCAL_AI_FINISHING_ENABLED=false
```

Включение composite требует полного checklist из `REAL_BACKGROUND_PIPELINE.md`.
