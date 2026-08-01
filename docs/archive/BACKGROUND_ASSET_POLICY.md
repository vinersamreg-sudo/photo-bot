# Background asset policy

Ravuna не скачивает случайные фотографии из интернета и не принимает URL как
production asset. Каждый фон существует только как локальный файл рядом с
`catalog.json` и обязан иметь доказуемую commercial provenance.

## Обязательные поля

ID, title/category/tags, location/orientation/aspect ratio, lighting/time/weather,
horizon, source type/reference, license type/record, commercial-use flag,
attribution flag, file SHA-256, relative filename, active flag и creation time.

Разрешены только legacy-идентификатор `pixora_owned`, `commercial_license`, `purchased`,
`user_provided`, `synthetic_test` и соответствующие source types. Абсолютные пути,
`..`, symlink, неизвестная license, отсутствие record, запрет commercial use или
checksum mismatch останавливают загрузку всего каталога.

## Операционный процесс

1. Получить оригинал по договору/покупке/собственной съёмке.
2. Сохранить invoice/license/release вне публичного каталога и указать стабильную
   ссылку на запись в `license_record`.
3. Удалить EXIF/личные данные, проверить model/property releases.
4. Посчитать SHA-256, заполнить metadata, оставить `active=false`.
5. Выполнить юридическую и визуальную проверку, затем активировать.
6. Запустить `python scripts/check_asset_licenses.py` в CI и production.

Текущий production catalog валиден, но содержит **0 активных assets**. Синтетический
benchmark asset создаётся только во временной директории и не является продуктовым.
