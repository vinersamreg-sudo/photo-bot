# Delivery Process

## Текущий owner-only MAX deploy

Если Environment secret `MAX_BOT_TOKEN` существует, workflow передаёт его по stdin в `/opt/photo-bot/.env` и выставляет `MAX_TRANSPORT_MODE=polling`. Handlers включаются (`MAX_POLL_OBSERVE_ONLY=false`) только при наличии отдельного `MAX_OWNER_USER_ID`; он также передаётся по stdin и записывается как `MAX_OWNER_USER_IDS`. Без owner secret deploy очищает allowlist и принудительно включает observe-only. Затем workflow выполняет `max-check`, устанавливает `/etc/systemd/system/photo-bot.service`, включает autostart и ожидает полноценный readiness. Значения secrets не попадают в shell arguments или отчёт.

MAX требует доверия цепочке Минцифры для `platform-api2.max.ru`. Репозиторий хранит публичный root из официального `https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt`; приложение указывает его только MAX API client через `MAX_CA_BUNDLE`. Системный trust store не расширяется, TLS verification не отключается, сертификат защищён тестом DER SHA-256.

Если MAX secret отсутствует, deploy кода разрешён, но mode принудительно остаётся `disabled`, unit останавливается и readiness MAX не заявляется. Прямой background-start и cron watchdog не используются. Изменение systemd ограничено отдельными разрешёнными deployment-командами; само приложение не имеет root/sudo.

## Обычный выпуск

1. Изменить код и документацию в локальном репозитории.
2. Выполнить `python scripts/scan_secrets.py`, unit-тесты, healthcheck и `pip check`.
3. Просмотреть diff и убедиться, что TripDay и секреты не затронуты.
4. Commit и push в `main` запускают workflow `Test and deploy`.
5. Job `test` использует Python 3.12. Job `deploy` работает только после него и через Environment `production`.
6. Перед rsync безопасно останавливается только PID `photo-bot`; rsync обновляет `/opt/photo-bot`, сохраняя `.env`, `venv`, `data`, `logs`, `temp` и исключая отдельный `site/`.
7. На сервере устанавливаются зависимости, применяются идемпотентные SQLite migrations, повторяются тесты и healthcheck, фиксируется SHA, а при наличии `OPENAI_API_KEY` проверяются авторизация и модель. Runtime запускается только при `MAX_TRANSPORT_MODE=polling` и непустом token; disabled mode не оставляет idle-процесс.

Ручной повтор: GitHub Actions → `Test and deploy` → `Run workflow`.

## Секреты

Инфраструктурные секреты: `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_PORT`, `HETZNER_SSH_PRIVATE_KEY`. Application/API secrets: `OPENAI_API_KEY`, подтверждённый `MAX_BOT_TOKEN` и закрытый `MAX_OWNER_USER_ID`; для будущего Webhook потребуется `MAX_WEBHOOK_SECRET`. Значения передаются deploy через stdin, никогда не являются shell argument и не выводятся.

## Откат

Откат — новый commit, возвращающий нужное состояние, и обычный deploy. Runtime state не удаляется. Перед изменением формата данных требуется миграция с резервной копией и проверяемым обратным путём; сейчас миграций данных нет.

## Аварийный доступ

Использовать `vineradmin` по персональному SSH-ключу. `photoapp` не получает `sudo`. Root оставлен только для key-based аварийного доступа до отдельного решения об окончательном отключении. Не редактировать код вручную; диагностика допустима, исправление возвращается через Git.

## Проверка выпуска

Проверить зелёный Actions run, соответствие `data/deployed_commit.txt` SHA коммита, healthcheck, отсутствие секретов в логах и, когда ключи настроены, `openai-check`/`max-check`. Systemd template устанавливает администратор из `ops/photo-bot.service` только после готовности transport mode; unit работает под `photoapp`, читает `/opt/photo-bot/.env`, имеет restart-on-failure и graceful SIGTERM.
