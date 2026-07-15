# MAX Transport Audit

Актуально на 15.07.2026. Документ фиксирует только проверенные факты до реализации live transport.

## Где искался прежний бот

Проверены:

- локальные Git-репозитории в `Documents`, `OneDrive/Документы`, Desktop и Downloads;
- все репозитории GitHub владельца `vinersamreg-sudo`;
- рабочий каталог `photo-bot`, Hetzner `/opt` и прежний REG.RU `data/apps`;
- предыдущие задания проекта и имена переменных в доступных `.env` без чтения значений.

Отдельный исходный проект MAX-бота «Лайви» не найден. У владельца есть только `photo-bot` и отдельный `tripday-agent`; TripDay не относится к этому проекту. Первое задание на создание `photo-bot` прямо запрещало реализацию MAX-интеграции. На production нет `MAX_BOT_TOKEN`, MAX transport или другого процесса бота. Следовательно, существующий код команд, оферты, баланса и старого интерфейса отсутствует; `app/max_adapter.py` является transport-независимым контрактом, а `python -m app.main run` — idle-заглушкой.

Наличие зарегистрированного профиля чат-бота в MAX и его токена пока не подтверждено: в доступной конфигурации токена нет, а авторизованный интерфейс MAX для бизнеса во время аудита не был доступен программно. Нельзя утверждать, что старый бот реально получает события.

## Проверенный официальный контракт

Источники:

- <https://dev.max.ru/docs-api>
- <https://dev.max.ru/docs-api/objects/Update>
- <https://dev.max.ru/docs-api/methods/GET/updates>
- <https://dev.max.ru/docs-api/methods/POST/subscriptions>
- <https://dev.max.ru/docs-api/methods/POST/uploads>
- <https://dev.max.ru/docs-api/methods/POST/messages>
- <https://dev.max.ru/docs-api/methods/PUT/messages>
- <https://dev.max.ru/docs-api/methods/DELETE/messages>
- <https://dev.max.ru/docs-api/methods/POST/answers>
- проверенный MAX форк Python SDK: `max-messenger/max-botapi-python`, audit SHA `4a286baa06d38f2db9f7244d94f89508743f8fee`.

Подтверждено:

- актуальный API base URL — `https://platform-api2.max.ru`; токен передаётся только заголовком `Authorization`;
- события имеют типы `bot_started`, `message_created`, `message_callback` и другие варианты `Update`;
- `message_created` содержит `message.sender.user_id`, `message.recipient.chat_id`, `message.body.mid`, text и attachments;
- image attachment содержит `payload.photo_id`, `payload.token`, `payload.url`; скачивание выполняется по URL, полученному в событии;
- callback содержит `callback.callback_id`, payload и user; ответ на callback отправляется через `POST /answers`;
- сообщение отправляется по `user_id` или `chat_id`; inline keyboard является attachment с callback payload;
- изображение отправляется в три шага: `POST /uploads?type=image`, multipart upload по возвращённому URL, затем `POST /messages` с image token;
- `type=photo` больше не поддерживается; используется `type=image`;
- изображения API: JPG/JPEG/PNG/GIF/TIFF/BMP/HEIC, максимум 50 МБ и 7680×7680; входной WEBP принимается приложением только если реальный MAX event вернёт поддерживаемое image-вложение с валидным содержимым;
- отправка произвольного файла поддерживается как `type=file`, но original в demo-flow не отправляется;
- API позволяет редактировать сообщения через `PUT /messages` и удалять через `DELETE /messages`;
- общий предел — 30 запросов в секунду; после upload возможен `attachment.not.ready`, для которого нужен ограниченный backoff;
- Long Polling использует `marker`, `timeout` 0–90 и список `types`; без marker возвращается только последнее обновление;
- MAX разрешает Long Polling для разработки/тестирования, но для production требует Webhook; одновременно оба режима не работают;
- Webhook доступен только по HTTPS на порту 443, должен вернуть 200 не позднее 30 секунд и проверять `X-Max-Bot-Api-Secret`; MAX выполняет до 10 retry и снимает подписку после длительной недоступности.

## Idempotency и delivery

Глобальный `update_id` официальным объектом не заявлен. Для `message_created` стабильным ключом служит `message.body.mid`, для callback — `callback.callback_id`. Long Polling marker является durable checkpoint получения, но не заменяет обработанную event-запись. Webhook retry требует отдельной SQLite-дедупликации до выполнения побочного эффекта.

Успешный HTTP-ответ `POST /messages` подтверждает принятие сообщения API и возвращает `Message`; именно это является доступной transport delivery boundary. Если upload или send завершились ошибкой, domain delivery callback возвращает false, и demo quota не расходуется.

## Выбранная архитектура

Выбран вариант C: единое приложение `photo-bot` с отдельным тонким модулем MAX transport.

Причины:

- оба слоя пишутся на Python, отдельного стабильного transport-проекта нет;
- HTTP-микросервис добавил бы authentication, deployment и failure boundary без пользы для одного процесса;
- transport только получает/нормализует Update, скачивает/загружает media, отправляет/редактирует сообщения и подтверждает delivery;
- quota, watermark, generation, gallery, payment guards и retention остаются в существующих services.

Для первой закрытой проверки допустим временный single-instance Long Polling runner. Он не считается production-ready transport. Целевой production mode — Webhook после появления домена, TLS endpoint на 443, webhook secret и разрешённого firewall ingress.

Python SDK не принимается как обязательная runtime-зависимость: репозиторий MAX сам называет его неофициальным форком, а прямой небольшой HTTP boundary проще зафиксировать fake-тестами. Модели SDK использованы только для сверки полей фактического контракта.

## Блокеры live smoke

- отсутствует `MAX_BOT_TOKEN` в GitHub Environment и production `.env`;
- не подтверждены username/id зарегистрированного тестового бота и тестовый пользователь;
- для production Webhook нет выделенного домена, TLS endpoint, `MAX_WEBHOOK_SECRET` и открытого 443;
- текущий UFW разрешает только SSH;
- юридические тексты остаются техническим draft.

До устранения этих блокеров unit/fake vertical slice можно реализовать полностью, но нельзя заявлять успешную MAX-интеграцию или выполнять реальный smoke.
