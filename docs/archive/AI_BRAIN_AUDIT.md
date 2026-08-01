# Аудит AI-цепочки Pixora до внедрения AI Brain

Дата фиксации: 2026-07-16. Аудируемый commit: `e64fdbe0b0a4f0ba26cb8375eadf79e4e41ea102`.

## Доказательство production-версии

Последний успешный production workflow `29487661265` завершился на commit `e64fdbe0b0a4f0ba26cb8375eadf79e4e41ea102`. Deploy job записывает `GITHUB_SHA` в `/opt/photo-bot/data/deployed_commit.txt` только после remote tests, healthcheck, проверки единственного PID и перезапуска systemd. Более поздних успешных или запущенных deploy workflow нет. Прямое чтение marker по SSH из локальной среды недоступно: локальный SSH-ключ не авторизован для пользователя `photoapp`. Поэтому SHA подтверждён последним завершённым deployment run и его атомарным deploy-контрактом; post-deploy проверка должна дополнительно прочитать marker на сервере.

## Фактический provider request

До изменений production использует:

- provider/model: OpenAI `gpt-image-2`;
- SDK method: `client.images.edit`, HTTP endpoint `POST /v1/images/edits`;
- входных изображений: одно;
- `quality`: `low`, значение жёстко задано в `app/image_service.py`;
- `size`: `1024x1024`, значение жёстко задано в `app/image_provider.py`;
- `input_fidelity`: не передаётся. Для `gpt-image-2` это правильно: модель всегда обрабатывает image input с high fidelity и API не разрешает менять этот параметр;
- `output_format`: `png`;
- timeout: 300 секунд из `GENERATION_TIMEOUT_SECONDS`;
- retries: OpenAI SDK настроен на максимум 2 автоматических retry; до изменений attempt telemetry всегда записывала `0`, даже если SDK повторял запрос, потому что использовался только parsed response;
- preview: provider PNG сохраняется как private original, затем создаётся watermarked JPEG/WebP preview размером до 1024 px; production default для JPEG — quality 82.

OpenAI описывает `quality=low` как режим быстрых draft/thumbnail/iteration, а `medium` и `high` — как варианты для более финального результата. Это не доказывает, что вся размытость вызвана только quality, но делает `low` неподходящим безусловным production default для коммерческого фоторедактора.

Официальные источники:

- https://developers.openai.com/api/docs/models/gpt-image-2
- https://developers.openai.com/api/docs/guides/image-generation

## Как строился prompt

### Initial/custom

Пользовательский текст передавался provider без нормализации, структурирования и системных preservation rules.

### Готовый сценарий

Русский `Scenario.prompt_template` форматировался с исходной пользовательской фразой. Правила сохранения личности присутствовали только в отдельных шаблонах сценариев и были непоследовательны.

### Correction

`GalleryService.compose_prompt()` брал строку `parent.effective_prompt` и добавлял новую строку `Исправление: <новый текст>`. Отрицания, конфликтующие ограничения, цель исправления и уже успешные изменения не представлялись отдельными данными.

### Repeat

При наличии parent повторно использовалась строка `parent.effective_prompt`. Текст кнопки создавал техническую фразу `Другой вариант`, но при parent она фактически отбрасывалась.

## Что сохранялось

- `generation_attempts.prompt`: текущий пользовательский текст;
- `correction_prompt`: только новый correction text;
- `effective_prompt`: provider prompt или строковая цепочка `старый effective_prompt + Исправление`;
- `parent_version_id`: выбранная версия для correction/repeat;
- `gallery_versions.source_path`: путь, записанный в attempt;
- отдельного сериализованного intent, parser version и provider prompt не было.

## Критический дефект lineage

Несмотря на корректный `parent_version_id`, `DemoService.generate()` всегда устанавливал `source_path` из `demo_sessions.source_file_path` и именно этот файл передавал в `images.edit`. Поэтому Correction версии 2 или 3 начиналась с исходной загруженной фотографии, а не с private original выбранной успешной версии. `current_best` и `parent_version_id` влияли только на текст и историю, но не на image input.

Следствия:

1. модель не видела уже созданные скалы, одежду и позу;
2. фраза «фон всё равно размыт» приходила вместе с текстовым описанием старой цели, но с исходным изображением;
3. для исполнения модели было естественно заново сгенерировать фон, а не локально улучшить существующий;
4. каждое новое исправление увеличивало противоречивую русскую строку, не устанавливая запрет на замену фона;
5. `quality=low` и preview JPEG quality 82 дополнительно снижали наблюдаемую детализацию, но не являлись единственной первопричиной.

## Вывод до изменений

Gallery lineage хранилась, но не исполнялась provider-слоем. Pixora имела историю строк, а не память намерения. Для исправления нужны одновременно: типизированный EditPlan, детерминированный разбор отрицаний, merge ограничений, отдельный technical prompt и выбор image input по выбранной успешной версии.
