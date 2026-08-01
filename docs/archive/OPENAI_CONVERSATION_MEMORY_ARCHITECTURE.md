# Optional OpenAI provider context architecture

## Инвариант

Ravuna хранит память работы сама, а OpenAI conversation используется как
дополнительный контекст для последовательных правок.

Источник истины: SQLite + private Storage + `GalleryVersion` lineage +
`SceneIntent/EditPlan`. Provider IDs можно удалить, сбросить или полностью
отключить без потери локальной истории.

## Feature gates

Conversational path доступен только когда одновременно включены:

```dotenv
OPENAI_CONVERSATION_MEMORY_ENABLED=true
OPENAI_RESPONSES_IMAGE_ENABLED=true
OPENAI_CONVERSATION_RETENTION_ENABLED=true
```

Любой выключенный флаг оставляет текущий `/v1/images/edits`. Обычный production
deploy принудительно возвращает все три флага в `false`.

## Два режима provider

1. `STATELESS_IMAGE_EDIT`: проверенный `client.images.edit`, `gpt-image-2`.
2. `CONVERSATIONAL_IMAGE_EDIT`: `client.responses.create` +
   `image_generation(action=edit, model=gpt-image-2)`.

MAX и Gallery UI не знают о `previous_response_id`. Выбор выполняют
`ProviderContextService` и `ContextAwareImageProvider`.

## Цепочки

### Initial

- При выключенных флагах: stateless Images API.
- При явно включённых трёх флагах: новый локальный provider context и Response
  depth 1.
- Результат становится `GalleryVersion` только после Storage, watermark и
  успешной delivery.

### Correction

- Parent проверяется внутри GalleryItem текущего пользователя.
- Вход — private original выбранного parent.
- Состояние — merged `EditPlan`; prompt — полный English technical prompt.
- `previous_response_id` берётся только из выбранной parent version.
- Новый response ID записывается в новую version после delivery.

### Repeat

Repeat всегда stateless. Он использует тот же input и effective intent, создаёт
альтернативную ветку и не загрязняет provider chain основной версии.

## Branching

`provider_contexts.last_response_id` — диагностическое summary, а не parent.
Настоящий parent хранится в `gallery_versions.provider_response_id`.

```text
v1(resp-1)
├─ v2(resp-2, previous=resp-1)
│  └─ v3(resp-3, previous=resp-2)
└─ v4(resp-4, previous=resp-1)
```

Если parent не имеет response ID, Correction выполняется stateless. При depth
limit создаётся новая Responses chain от parent original. Idle/deleted/broken
context даёт stateless fallback.

## Fallback

Timeout, connection/rate-limit, invalid previous response, 5xx или неожиданная
response schema приводят к одному вызову проверенного stateless adapter. SDK
может выполнить собственные настроенные HTTP retries внутри каждого вызова.
Policy rejection и insufficient quota не запускают второй image request.

Fallback сохраняет полный локальный `EditPlan`, parent original и prompt. Demo
quota списывается один раз только после успешной delivery. До неё GalleryVersion
не создаётся.

## Данные

`provider_contexts` хранит минимальное состояние работы: provider/model,
conversation/last response IDs, status, depth, reset/fallback counters,
created/updated/last-used/expiry/delete timestamps и error class.

Каждая attempt/version хранит provider mode, response/parent/conversation/context
IDs, depth, fallback reason, HTTP status, request ID, duration, usage и hashes
effective prompt/SceneIntent. Изображения остаются только в private Storage.

## Ограничения

Контекст улучшает смысловую последовательность, но не гарантирует identity или
pixel preservation. Польза, latency и стоимость не объявляются подтверждёнными
до controlled comparison.
