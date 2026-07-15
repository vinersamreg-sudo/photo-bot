# Current State

Состояние на 15.07.2026.

## Реализовано

- минимальный Python-каркас и `.env`-конфигурация;
- filesystem healthcheck для `data`, `logs`, `temp`;
- OpenAI client check без генерации: авторизация и видимость `gpt-image-2`;
- безопасная классификация API-ошибок и редактирование ключа в логах;
- signal-aware idle run-loop и PID-скрипты;
- unit-тесты и проверка deploy policy;
- secret scan отслеживаемых Git-файлов;
- Hetzner VPS, пользователи `vineradmin`/`photoapp`, venv Python 3.12;
- SSH hardening, UFW и fail2ban;
- GitHub Environment `production` с Hetzner deploy secrets;
- автоматический rsync deploy в `/opt/photo-bot`;
- первый deploy коммита `d80f7e0` успешно прошёл через GitHub Actions;
- устаревшие REG.RU repository secrets удалены после подтверждения миграции.
- production-процесс `photo-bot` запущен; встроенный OpenAI check прочитал ключ из `/opt/photo-bot/.env`, получил HTTP 200 и подтвердил модель `gpt-image-2`;
- проведено отдельное публичное исследование MAX; продуктовые выводы перенесены в стратегию без смешивания исходников и данных проектов.
- завершён безопасный mystery shopping восьми выбранных конкурентов MAX: подтверждены реальные onboarding/paywall, цены, четыре бесплатных результата, correction и юридические паттерны; сырьё и screenshots остались в отдельном проекте;
- сформирован продуктовый blueprint с минимальным сценарием, pricing-формулами и политиками retry/correction/refund/storage/delete.

## Не реализовано

Интеграция с мессенджером, приём/обработка фото, вызов image generation/edit endpoint, UI, база операций, платежи, баланс, очередь, systemd-сервис, мониторинг и backup данных.

## Известные ограничения

- transport-интеграция с MAX ещё не спроектирована и не реализована;
- тарифы требуют измерения all-in себестоимости собственного `gpt-image-2`: конкурентные цены подтверждены, но не определяют нашу маржу или качество;
- Hetzner Cloud Firewall не настроен; внешний доступ сейчас ограничивает UFW.

## Проверенное окружение

Сервер отвечает из Германии, имеет доступ к `api.openai.com` по HTTPS, а запрос без ключа возвращает ожидаемый `401`. Публично слушает только SSH, failed systemd units отсутствуют, свободно около 35 GB. После системных обновлений выполнена контролируемая перезагрузка; key-based root access и состояние systemd повторно проверены.
