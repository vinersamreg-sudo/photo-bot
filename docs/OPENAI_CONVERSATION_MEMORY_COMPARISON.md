# Owner-only controlled comparison runbook

Статус: подготовлено, **не выполнено**. Требуется отдельное разрешение владельца
на реальные OpenAI image requests.

## Бюджет

Максимум 4 image requests, одна безопасная синтетическая фотография взрослого:

1. Stateless: заменить фон на реалистичные скалистые горы.
2. Stateless Correction: одежда для хайкинга; лицо и фон не менять.
3. Conversational: повторить request 1 в новой работе.
4. Conversational Correction: сохранить скалы и одежду, сделать только фон резче.

Никаких автоматических retries сверх SDK policy и единственного stateless
fallback. Если fallback создаёт риск пятого image request, тест останавливается.

## Safety gates

- owner allowlist подтверждён без вывода ID;
- pilot limit 0;
- handlers включаются только для owner на время теста;
- нет pending/processing state;
- сделан backup и SQLite `quick_check`;
- лимит 4 фиксируется до включения;
- после теста: три OpenAI flags `false`, `MAX_POLL_OBSERVE_ONLY=true`, handlers
  остановлены, health/quick_check/orphans проверены.

## Сравниваем

- identity, фон, одежду, композицию, артефакты и visual drift;
- parent/input GalleryVersion lineage;
- provider mode, response chain, HTTP status, request ID и usage;
- duration и оценку стоимости;
- fallback и demo quota.

Успех нельзя объявить только по наличию response ID. Решение о пилоте на 5
пользователей принимается после визуального и финансового сравнения.
