# Decisions

## ADR-001 — Hetzner VPS вместо REG.RU

Принято 14.07.2026. Production перенесён на Ubuntu VPS в Nuremberg, потому что исходящий адрес REG.RU получал от OpenAI региональный отказ. Proxy/VPN не используются. После успешного первого deploy старые REG.RU secrets удалены из GitHub.

## ADR-002 — Один простой Python-сервис

Для MVP выбран Python 3.12, venv и один процесс. Docker, Redis, Celery и Kubernetes отложены до появления измеримой нагрузки или требований к изоляции.

## ADR-003 — Разделение доступа

`vineradmin` используется для администрирования, `photoapp` — для приложения и GitHub deploy. У `photoapp` нет `sudo`; deploy ограничен `/opt/photo-bot`.

## ADR-004 — GitHub main как источник истины

Production не редактируется вручную. GitHub Actions выполняет тесты перед deploy, сохраняет runtime state и записывает SHA.

## ADR-005 — Публичный бренд не утверждён

До отдельного продуктового решения во внешних заявлениях используется нейтральное описание, в технике — `photo-bot`. Ранее обсуждавшиеся названия не считаются принятыми.

## ADR-006 — Systemd отложен

Текущий `run` — idle-каркас без пользовательской функции. Постоянный сервис и автозапуск добавляются вместе с реальным transport handler и его operational checks.

## ADR-007 — Host firewall как первый слой

UFW разрешает только SSH, fail2ban защищает `sshd`. Hetzner Cloud Firewall пока не добавлен из-за отсутствия настроенного API/консольного шага; это допустимо до появления публичного HTTP-порта.
