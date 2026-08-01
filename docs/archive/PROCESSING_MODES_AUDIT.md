# Processing modes audit

Дата аудита: 16.07.2026. Область: production-код Pixora, без реальных image requests.

## Исходное состояние

До этого спринта любой сценарий в итоге вызывал один и тот же `ImageProvider.edit`.
Структурированный `EditPlan` и английский prompt уже уменьшали неоднозначность, но
технология выполнения не зависела от задачи. Поэтому просьба поставить человека на
реальный фон могла привести к сгенерированному пейзажу, а простое повышение резкости —
к дорогому generative edit с риском изменения личности.

Production API contract подтверждён по коду и безопасному credential check:
`POST /v1/images/edits`, модель `gpt-image-2`, `quality=medium`, `size=1024x1024`,
PNG, timeout 300 секунд, максимум 2 SDK retry. Для `gpt-image-2` приложение не
отправляет `input_fidelity`: высокая fidelity применяется моделью автоматически.
Реальных запросов в рамках этого спринта: **0**.

Официальная документация OpenAI подтверждает, что Image API поддерживает edits,
mask и несколько входных изображений. При этом mask — guidance, а не гарантия
пиксельной геометрии, и для multiple inputs относится к первому изображению. Это
не заменяет локальный deterministic composite, когда фон должен быть именно
лицензированным фото: [Image generation guide](https://developers.openai.com/api/docs/guides/image-generation),
[Images API reference](https://developers.openai.com/api/reference/resources/images).

## Найденные риски

- одна технология для разных классов задач;
- стоимость OpenAI даже для локально решаемого enhancement;
- отсутствие доказуемой provenance у фоновых изображений;
- невозможность гарантировать неизменность реального фона после generative edit;
- неполный lineage: не было mode, asset, mask strategy и pipeline version;
- fallback мог бы незаметно изменить обещанный пользователю тип результата;
- без segmentation gate плохая mask могла создать версию и испортить quota UX.

## Результат

Введены пять typed modes: `AI_GENERATION`, `REAL_BACKGROUND_COMPOSITE`,
`LOCAL_AI_EDIT`, `ENHANCEMENT`, `RESTORATION`. Deterministic router строит
сериализуемый `ProcessingPlan`; выбранный mode и lineage сохраняются в attempt и
GalleryVersion. Локальные режимы не вызывают OpenAI. Отсутствие безопасного asset
или segmentation backend теперь блокирует операцию до provider/attempt, а ошибка
сегментации не списывает quota и не создаёт GalleryVersion.

## Честный вывод

Архитектурный дефект устранён, но `REAL_BACKGROUND_COMPOSITE` ещё не готов к
публичному включению: production-каталог содержит 0 активных фото, rembg/model не
установлены на VPS и owner-only visual validation нового pipeline не проводилась.
Поэтому deploy обязан оставить этот режим выключенным.
