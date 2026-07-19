# Privacy and data flow

## Поток одного запроса

```text
пользователь MAX
  -> MAX Bot API (MAX user/chat ID, фото, инструкция)
  -> photo-bot на Hetzner, Германия
  -> private storage + SQLite
  -> OpenAI image processing (фото + technical prompt)
  -> private original
  -> server-side watermarked preview
  -> MAX Bot API -> пользователь
```

После запуска оплаты отдельный поток добавляет Robokassa: order ID, сумма, статус, callback и refund events. Полные реквизиты карты в Pixora не поступают.

## Что сохраняет Pixora

- opaque MAX user/chat relation;
- исходное фото, prompt, result и version lineage;
- legal consent version/time;
- demo quota, Gallery, favorites, collections и current best;
- минимальные operational/payment events без отдельной копии фото, полного prompt, токенов или банковских реквизитов.

## Сроки

- demo/source/result — 30 дней;
- paid GalleryItem — 180 дней;
- trash — 30 дней;
- temp — 24 часа;
- provider context — отключён в production; если будет включён отдельно, default 30 дней;
- финансовый audit хранится отдельно от изображений и требует финального юридического срока.

## Третьи стороны и местонахождение

- MAX — transport;
- Hetzner, Германия — runtime/private storage;
- OpenAI — image provider;
- GitHub — зашифрованный off-site backup artifact;
- Robokassa — payment/fiscal flow после отдельного запуска.

Эта схема включает трансграничную обработку. До публичного коммерческого запуска нужны юридическая проверка основания/уведомлений/локализации и подтверждение текстов политики и согласия.

## Удаление и безопасность

Работа удаляется сначала логически, затем maintenance cleanup физически после trash TTL. Cleanup dry-run по умолчанию, не следует symlink и запускается после подтверждённой backup copy. Секреты находятся только в `/opt/photo-bot/.env` mode 600 и GitHub Environment, сайт публикует только `site/public`.

## Публичные документы

- `/legal/privacy.html`;
- `/legal/personal-data.html`;
- `/legal/offer.html`;
- `/legal/payment-refund.html`;
- `/legal/terms.html`;
- `/contacts.html`.

Engineering не заменяет юридическую экспертизу. Подтверждённый ИНН и рабочий контакт владельца пока отсутствуют в безопасных источниках и не должны быть выдуманы.
