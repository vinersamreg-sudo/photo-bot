# OpenAI conversation memory audit

Дата проверки: 2026-07-19. Источники — только официальная документация OpenAI.

## Краткий вывод

Текущий проверенный production-путь Pixora — `POST /v1/images/edits` с
`gpt-image-2`. OpenAI Responses API официально поддерживает image generation/edit
как встроенный tool, входное изображение, последовательные image turns и
продолжение через `previous_response_id`. Image tool позволяет явно выбрать
`gpt-image-2`, качество, размер и формат результата.

Это позволяет построить экспериментальную цепочку правок, но не доказывает, что
она лучше сохраняет лицо, позу, фон или композицию. Это проверяется только
контролируемым визуальным сравнением. До него production остаётся на Images API.

## Что подтверждено официально

| Вопрос | Ответ |
|---|---|
| `gpt-image-2` через Responses | Да. Модель указывает Responses среди поддерживаемых endpoints; image-generation tool имеет параметр `model`. |
| Image generation/edit | Да. Tool поддерживает `action=generate`, `edit` и `auto`. |
| Входное изображение | Да. Responses принимает `input_image` как URL/data URL или File ID. |
| `previous_response_id` | Да. Следующий Response можно связать с предыдущим без ручной пересылки всех текстовых turns. |
| Conversations API | Да. Conversation можно создать и удалить; items сохраняются в conversation. |
| Несколько image turns | Да. Официальный image guide показывает multi-turn image editing. |
| Tool output image | Да. Результат находится в `image_generation_call.result` как base64. |
| Параметры | Доступны `quality`, `size`, `output_format`, `output_compression`, `background`, `moderation` и выбор image model; применимость отдельных значений зависит от модели. |
| Удаление Response | Да, отдельным DELETE endpoint. |
| Удаление Conversation | Да, отдельным DELETE endpoint. |
| Сохранение Response | Responses сохраняются по умолчанию; обычный срок application state — не менее 30 дней. `store=false` отключает сохранение Response object. |
| Сохранение Conversation | Conversation и связанные с ней items хранятся до удаления и не ограничены обычным 30-дневным TTL Response. |
| Стоимость контекста | Предыдущие input tokens повторно тарифицируются как input даже при использовании `previous_response_id`. Image generation оплачивается отдельно; основной Responses model добавляет text/image input/output token cost. |
| Повторная отправка всей истории | Не обязательна при `previous_response_id` или Conversation, но прошлый контекст всё равно учитывается и тарифицируется. |

## Отличия от `/v1/images/edits`

- Images API — более прямой и уже проверенный Pixora путь одной правки.
- Responses добавляет основной reasoning model и image-generation tool; это
  дополнительная задержка, стоимость и поверхность отказов.
- Image tool можно закрепить на `gpt-image-2`, но orchestration всё равно выполняет
  отдельный Responses model.
- Timeout и retry являются настройками HTTP/SDK клиента, а не семантикой image
  memory.
- Request ID, HTTP status, duration и usage следует снимать с raw SDK response;
  фактическую полноту полей надо подтвердить smoke-тестом.
- Модерация применяется к входам и выходам в обоих вариантах.

## Хранение и privacy

`previous_response_id` требует сохранённого Response, поэтому экспериментальный
адаптер явно использует `store=true`. Это означает provider-side application
state. Pixora хранит только минимальные provider IDs, usage, duration и тип
fallback; raw Response, data URL, image bytes, ключи и signed URLs в БД и логи не
попадают.

`/v1/conversations` и conversation items хранятся до удаления и не являются
ZDR-совместимыми endpoints. В MVP Pixora не использует одну удалённую Conversation
как источник ветвления: сохраняется `provider_response_id` каждой
`GalleryVersion`, а parent выбирается из локальной lineage. Gateway создания и
удаления Conversation существует для отдельного будущего эксперимента, но
операционный MVP использует `previous_response_id`.

## Что является гипотезой

- Контекст уменьшит смысловой drift в последовательных Correction.
- Короткие ссылки вроде «эти же скалы» будут выполняться стабильнее.
- Дополнительный reasoning turn не ухудшит identity и не создаст новых
  артефактов.
- Польза превысит дополнительную стоимость и latency.

## Что нельзя гарантировать

- pixel-perfect сохранение лица, позы, одежды, фона или композиции;
- отсутствие visual drift;
- одинаковый результат двух повторов;
- полное восстановление удалённой/истёкшей provider chain;
- фиксированный latency, usage или стоимость;
- что Conversation «помнит фотографию» как человек.

## Что не поддерживается выбранным MVP

- единая линейная Conversation для всего дерева GalleryVersion;
- автоматическое доверие `last_response_id` вместо parent lineage;
- выполнение команд из model output;
- хранение полного пользовательского диалога у provider;
- включение памяти для owner или pilot без отдельного разрешения.

## Что требует controlled smoke

1. Фактическая схема `image_generation_call` для выбранных production models.
2. Parity `quality=medium`, `size=1024x1024`, PNG и `gpt-image-2`.
3. Identity, фон, одежда, композиция и артефакты на тех же входах.
4. HTTP status, request ID, usage, latency и стоимость обоих путей.
5. Поведение invalid/expired `previous_response_id` и однократного fallback.
6. Удаление Responses и фактическое подтверждение cleanup API.

Реальные image requests в рамках реализации: **0**.

## Официальные источники

- [Image generation guide](https://developers.openai.com/api/docs/guides/image-generation)
- [Conversation state guide](https://developers.openai.com/api/docs/guides/conversation-state)
- [GPT Image 2](https://developers.openai.com/api/docs/models/gpt-image-2)
- [Create a Response](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Delete a Response](https://developers.openai.com/api/reference/resources/responses/methods/delete)
- [Create a Conversation](https://developers.openai.com/api/reference/resources/conversations/methods/create)
- [Delete a Conversation](https://developers.openai.com/api/reference/resources/conversations/methods/delete)
- [Your data](https://developers.openai.com/api/docs/guides/your-data)
- [API pricing](https://developers.openai.com/api/docs/pricing)
