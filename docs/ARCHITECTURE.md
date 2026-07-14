# Architecture

## Текущая схема

`GitHub main → GitHub Actions → SSH/rsync → /opt/photo-bot → Python 3.12 venv → OpenAI API`.

CI запускает unit-тесты, healthcheck, `pip check` и secret scan. Deploy выполняется непривилегированным `photoapp`, синхронизирует только код и сохраняет `.env`, `venv/`, `data/`, `logs/`, `temp/`. После проверки SHA фиксируется в `data/deployed_commit.txt`.

## Компоненты

- configuration: environment variables и `.env` через `app/config.py`;
- OpenAI boundary: создание клиента, классификация безопасных ошибок и проверка модели в `app/openai_client.py`;
- runtime CLI: health, API-check и idle run-loop в `app/main.py`;
- filesystem state: локальные каталоги `data`, `logs`, `temp`;
- operations: скрипты в `scripts/` и GitHub Actions.

## Целевая схема MVP

Один Python-процесс принимает события мессенджера, валидирует фото и команду, создаёт локальную операцию, вызывает OpenAI image API, сохраняет минимальные метаданные и отправляет результат. Для малой нагрузки достаточно SQLite и последовательной/ограниченно-параллельной обработки внутри одного приложения.

Границы модулей: transport adapter, use-case/service, OpenAI image gateway, repository для операций/баланса, storage policy, observability. Внешние интеграции должны быть заменяемыми и покрываться тестами через fake-клиенты.

## Безопасность и эксплуатация

Секреты поступают только из окружения; логи редактируют известные значения ключей; пользовательский ввод и файлы имеют ограничения; временные файлы удаляются; операции получают correlation ID без персональных данных. Сервис не запускается от root. Systemd появится при реализации реального обработчика.

## Ограничения масштаба

2 GB RAM требуют ограничить размер изображений и число одновременных задач. Redis/Celery, отдельное хранилище и дополнительные узлы вводятся только после измерения очереди, памяти и времени обработки.
