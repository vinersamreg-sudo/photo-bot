# Project Bible

> Дополнение 16.07.2026: Pixora использует typed hybrid-processing architecture.
> Real backgrounds должны идти через licensed local composite, enhancement — через
> local Pillow, а generative provider применяется только к подходящим modes.
> Composite/segmentation/AI finishing остаются выключенными до checklist из
> `REAL_BACKGROUND_PIPELINE.md`. Новые `PROCESSING_MODES_*`,
> `SEGMENTATION_EVALUATION.md` и `BACKGROUND_ASSET_POLICY.md` являются частью wiki.

Публичный бренд продукта утверждён: **Pixora**. Техническое имя репозитория и backend остаётся `photo-bot`; продуктовая стратегия продажи понравившегося результата не меняется.

Актуально на 15 июля 2026 года. Это главный источник истины проекта; при конфликте других документов сначала обновляется решение здесь.

## Идентичность и цель

`photo-bot` — техническое имя личной AI-фотостудии пользователя. Публичный бренд — Pixora. Продукт продаёт не модель, генерации или кредиты, а конкретную понравившуюся фотографию без watermark, её историю версий и простой возврат к прежним работам.

Основное обещание: «Сначала посмотрите реальный результат. Платите только если он вам понравился». После первой покупки пользователь должен понимать: «Все мои фотографии теперь живут здесь».

Demo MVP закрепляет одну исходную фотографию за одним пользователем MAX, создаёт до пяти успешно доставленных уменьшенных результатов с крупным watermark «ОБРАЗЕЦ» и хранит оригиналы приватно. Пользователь платит за разблокировку конкретного уже увиденного результата. Реальный MAX transport авторизован; application handlers защищены обязательным owner allowlist, а без owner secret production остаётся в observe-only. Live `/start`/upload/generation E2E ещё должен быть подтверждён, эквайринг не подключён.

Каждый пользователь имеет одну Gallery. Одна работа (`gallery_item`) содержит source и историю `gallery_versions`; original всегда принадлежит конкретной версии. Повтор и correction создают новую версию той же работы. Коллекции, tags, favorites, current best, preferences, recent и trash являются частью доменной модели, но реальные MAX-экраны пока не реализованы.

## Источники истины

- код: GitHub `vinersamreg-sudo/photo-bot`, ветка `main`;
- рабочая копия: `C:\Users\viner\Documents\Codex\photo-bot`;
- production-код: `/opt/photo-bot`, только результат CI/CD;
- состояние реализации: `docs/CURRENT_STATE.md`;
- принятые решения: `docs/DECISIONS.md`;
- последовательность развития: `docs/ROADMAP.md`.

## Production

- Hetzner Cloud VPS `ai-prod-01`, Nuremberg, Germany;
- Ubuntu 24.04 LTS, x86_64, 1 vCPU, 2 GB RAM, 40 GB disk;
- публичный вход только SSH `22/tcp`;
- администратор `vineradmin`, вход по персональному ключу, `sudo`;
- приложение/деплой `photoapp`, отдельный ключ, без `sudo`;
- корень `/opt/photo-bot`, владелец `photoapp:photoapp`;
- venv `/opt/photo-bot/venv`, Python 3.12;
- UFW: deny incoming, allow outgoing, SSH only;
- fail2ban: jail `sshd`;
- SSH: password и keyboard-interactive отключены, root только по ключу;
- отдельный Hetzner Cloud Firewall пока не настроен; применён один ясный host-level слой UFW.

## Секреты

Production Environment GitHub использует `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_SSH_PORT`, `HETZNER_SSH_PRIVATE_KEY`, `OPENAI_API_KEY` и `MAX_BOT_TOKEN`. Для Webhook потребуется отдельный `MAX_WEBHOOK_SECRET`. Значения секретов не документируются и доставляются в production безопасным deploy-контуром.

Приватные ключи и `.env` запрещено выводить, коммитить, пересылать в аргументах команд или сохранять в документации. Деплой передаёт OpenAI-ключ через stdin и записывает `.env` с правами `600`.

## Обязательные границы

- TripDay полностью вне области проекта; любые его файлы, процессы, настройки и deploy запрещено затрагивать.
- Никаких proxy, VPN или обходов региональных ограничений.
- Не вводить Docker, Redis, Celery, Kubernetes и микросервисы без подтверждённой необходимости.
- Не создавать постоянно работающий сервис для idle-каркаса. `run` в disabled mode завершается; systemd-шаблон устанавливается только после выбора работающего transport mode.
- Long Polling MAX допустим только для разработки и закрытого smoke. Production mode — Webhook по HTTPS:443 с проверкой secret.
- Использовать утверждённый публичный бренд Pixora; не придумывать параллельные названия.
- Не обещать функциональность, которой нет в `CURRENT_STATE.md`.
- Не отправлять original до подтверждённого paid status и не считать техническую ошибку успешной итерацией.
- Бесплатные расходы всегда ограничиваются пользовательской квотой, concurrency и глобальным дневным бюджетом.
- Gallery API не раскрывает original заблокированной версии.
- Preferences помогают навигации и персонализации интерфейса, но не используются для скрытого обучения моделей.
- Soft delete предшествует физической очистке; retention и purge управляются конфигурацией и проверяемой задачей.

## Критерий production-ready для каждого изменения

Тесты, secret scan, `pip check` и healthcheck зелёные; deploy ограничен `/opt/photo-bot`; runtime-файлы сохранены; SHA записан в `data/deployed_commit.txt`; при наличии ключа OpenAI проверка авторизации и модели успешна; документация отражает реальность.
