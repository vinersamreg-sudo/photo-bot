# Provider context lifecycle

## Создание и использование

- Контекст создаётся только при трёх включённых feature flags.
- На работу существует один локальный context record; ветви определяются response
  ID конкретных versions, а не одним `last_response_id`.
- Максимальная глубина: `OPENAI_CONTEXT_MAX_DEPTH` (по умолчанию 8).
- Максимальный idle: `OPENAI_CONTEXT_MAX_IDLE_DAYS` (по умолчанию 14).
- Retention: `OPENAI_CONTEXT_RETENTION_DAYS` (по умолчанию 30).

## Удаление

При purge GalleryItem и `OPENAI_CONTEXT_DELETE_ON_GALLERY_DELETE=true`:

1. Собираются уникальные response/conversation IDs этой работы.
2. Выполняются DELETE calls provider API.
3. Успех очищает provider IDs в local attempts/versions и оставляет минимальный
   tombstone `status=deleted`.
4. Ошибка не блокирует локальное удаление: остаётся `status=delete_pending`,
   error class и счётчик попыток.
5. Следующий cleanup повторяет операцию.

Метод `delete_for_user` реализован для будущего user-erasure workflow и проверяет
изоляцию по local user ID. Текущий продукт пока не имеет публичной команды
удаления аккаунта; её нельзя считать юридически закрытой только наличием hook.

## CLI

Dry-run по умолчанию:

```powershell
python -m app.main provider-context-cleanup
```

Применение:

```powershell
python -m app.main provider-context-cleanup --execute
```

Общий `maintenance-cleanup` также включает provider context retention. Cleanup не
печатает IDs, prompts, paths или payload.

## Provider retention

Responses chain требует `store=true`. Обычные stored Responses имеют provider
retention, описанный OpenAI; Conversation objects живут до удаления. Ravuna не
обещает мгновенное физическое удаление без успешного ответа API. При выключенных
feature flags новые provider contexts не создаются, но cleanup ранее созданных
контекстов продолжает работать.

См. [официальную политику данных OpenAI](https://developers.openai.com/api/docs/guides/your-data).
