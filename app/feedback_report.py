"""Privacy-safe, deterministic feedback reporting; never initializes the app or DB."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Iterable


# Samara has used UTC+04 throughout the product's lifetime. A fixed timezone
# also works on operator Windows hosts without an installed IANA tzdata package.
SAMARA = timezone(timedelta(hours=4), "Europe/Samara")
UTC = timezone.utc
COHORTS = ("repeat_payer", "payer", "nonpayer", "unknown")
COHORT_LABELS = {
    "repeat_payer": "Повторная оплата (2+ подтверждённых покупок)",
    "payer": "Одна подтверждённая покупка",
    "nonpayer": "Нет подтверждённых покупок",
    "unknown": "Платёжный статус неизвестен",
}
SCREENS = {
    "main": "Главный экран", "purchase": "Покупка",
    "upload": "Загрузка фото", "result": "Результат",
    "works": "Мои работы", "work": "Работа", "history": "История",
    "more": "Дополнительные действия", "ideas": "Идеи", "rating": "Оценка",
    "unknown": "Неизвестно (включая историю)",
}
TOPICS = {
    "payment_loss": ("critical", "Списание без результата/доступа"),
    "privacy": ("critical", "Жалоба на чужое фото/утечку"),
    "quality": ("problem", "Искажения или неудовлетворительное качество"),
    "speed": ("problem", "Медленная обработка"),
    "navigation": ("problem", "Непонятная навигация"),
    "delivery": ("problem", "Не удалось получить или скачать результат"),
    "batch": ("feature", "Запрос пакетной обработки"),
    "styles": ("feature", "Запрос дополнительных стилей/шаблонов"),
    "formats": ("feature", "Запрос дополнительных форматов"),
    "positive_quality": ("positive", "Похвала качеству результата"),
    "positive_usability": ("positive", "Похвала удобству"),
    "positive_general": ("positive", "Общая положительная оценка"),
}


class ReportUnavailable(ValueError):
    """Contains only a fixed safe diagnostic, never a SQLite message or path."""


@dataclass(frozen=True)
class Feedback:
    user: str
    at: datetime
    kind: str
    source: str
    message: str = ""
    rating: int | None = None
    sentiment: str = ""


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Legacy DB timestamps without an offset have always represented UTC.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _at(value: str) -> datetime:
    result = _time(value)
    if result is None or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
        raise argparse.ArgumentTypeError("Нужен ISO-8601 момент с часовым поясом.")
    return result


def classify(message: str) -> tuple[str, ...]:
    """Conservative lexical signals, not sentiment inference or a claim of fact.

    Unknown wording stays unknown. Negated praise/requests and denial of problems
    do not become positive or negative evidence. Multiple explicit clauses may
    contribute different topics; each topic counts a message at most once.
    """
    text = message.lower().replace("ё", "е")
    clauses = re.split(r"[.!?;\n]+|\b(?:но|зато|однако)\b", text)
    found: set[str] = set()
    # Payment complaints often put the missing result after a contrasting 'но'.
    if re.search(r"(?:списал\w*|оплатил\w*|оплата\s+прошла)[^.!?\n]{0,160}(?:не\s+(?:получ\w*|приш\w*|выда\w*|зачисл\w*|откр\w*)|нет\s+(?:результата|доступа|кредитов|оригинала))", text) and not re.search(r"\bне\s+(?:списал\w*|оплатил\w*)", text):
        found.add("payment_loss")
    for clause in clauses:
        if re.search(r"\b(?:нет|никаких)\s+(?:никаких\s+)?(?:проблем|жалоб|ошибок)|без\s+(?:проблем|ошибок)", clause):
            continue
        if not re.search(r"\bне\s+(?:списал\w*|оплатил\w*)", clause) and re.search(r"(?:списал\w*|оплатил\w*|оплата\s+прошла)", clause) and re.search(
            r"не\s+(?:получ\w*|приш\w*|выда\w*|зачисл\w*|откр\w*)|нет\s+(?:результата|доступа|кредитов|оригинала)", clause
        ):
            found.add("payment_loss")
        if re.search(r"(?:показал\w*|получил\w*|прислал\w*)\s+(?:мне\s+)?чуж\w*\s+фото|(?:утечка|слив)\s+(?:моих\s+)?(?:фото|данных)", clause) and not re.search(r"\bне\s+(?:показал|получил|прислал)|\bнет\s+утечки", clause):
            found.add("privacy")
        if re.search(r"исказ\w*|искаж\w*|размыт\w*|плох\w*\s+качеств\w*|не\s+похож\w*", clause) and not re.search(r"\bне\s+(?:исказ\w*|искаж\w*|размыт\w*)|не\s+плох\w*", clause):
            found.add("quality")
        if re.search(r"медлен\w*|долго\s+(?:жд\w*|обрабат\w*|генер\w*|загруж\w*)|слишком\s+долго", clause) and not re.search(r"\bне\s+(?:медлен\w*|долго)|\bне\s+слишком\s+долго", clause):
            found.add("speed")
        if re.search(r"непонятн\w*\s+(?:где|как|куда|кнопк\w*|меню)|не\s+могу\s+найти\s+(?:кнопк\w*|меню)|запутан\w*\s+(?:меню|интерфейс)", clause):
            found.add("navigation")
        if re.search(r"не\s+(?:могу|получается|удалось)\s+(?:скачать|получить)|не\s+приш\w*\s+(?:фото|результат|оригинал)|нет\s+(?:результата|оригинала)", clause):
            found.add("delivery")
        request = re.search(r"\b(?:хочу|хотел\w*|добавьте|добавить|нужн\w*|хотелось|сделайте|нужен)\b", clause)
        negated_request = re.search(r"\bне\s+(?:хочу|хотел\w*|нуж\w*|добавля\w*)|\bбез\s+(?:новых\s+)?(?:стилей|шаблонов)", clause)
        if request and not negated_request:
            for topic, pattern in (
                ("batch", r"пакетн\w*\s+обработ\w*|несколько\s+фото\s+(?:сразу|одновременно)|массов\w*\s+обработ\w*"),
                ("styles", r"(?:нов\w*|больше|друг\w*|дополнительн\w*)\s+(?:стил\w*|шаблон\w*)"),
                ("formats", r"\b(?:png|webp|heic|pdf|raw)\b|(?:нов\w*|больше|друг\w*)\s+формат\w*"),
            ):
                if re.search(pattern, clause):
                    found.add(topic)
        if not re.search(r"\bне\s+(?:очень\s+)?(?:нрав\w*|удоб\w*|отлич\w*|красив\w*|хорош\w*|супер)|\bнеудоб\w*", clause):
            if re.search(r"(?:отличн\w*|хорош\w*|прекрасн\w*)\s+(?:качеств\w*|результат\w*)|красив\w*\s+(?:фото|результат\w*)", clause):
                found.add("positive_quality")
            if re.search(r"\bудобн\w*\b|понятн\w*\s+(?:меню|интерфейс)", clause) and not re.search(r"непонятн\w*", clause):
                found.add("positive_usability")
            if re.search(r"\b(?:нравится|понравилось|супер|спасибо)\b", clause):
                found.add("positive_general")
    return tuple(sorted(found))


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # Table names are hardcoded call-site constants, not user input.
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _available(connection: sqlite3.Connection, table: str, columns: str) -> bool:
    return set(columns.split()) <= _columns(connection, table)


def _owners(connection: sqlite3.Connection, platform_ids: Iterable[str]) -> set[str]:
    configured = {str(value).strip() for value in platform_ids if str(value).strip()}
    if not configured:
        raise ReportUnavailable("Отчёт закрыт: MAX_OWNER_USER_IDS не настроен.")
    if not _available(connection, "users", "id platform platform_user_id"):
        raise ReportUnavailable("Отчёт закрыт: нет проверяемого сопоставления владельца.")
    result: set[str] = set()
    for platform_id in sorted(configured):
        rows = connection.execute(
            "SELECT id FROM users WHERE lower(platform)='max' AND platform_user_id=?", (platform_id,)
        ).fetchall()
        if len(rows) != 1:
            raise ReportUnavailable("Отчёт закрыт: не все владельцы однозначно сопоставлены.")
        result.add(str(rows[0][0]))
    return result


def _read_feedback(connection: sqlite3.Connection, owners: set[str]):
    events: list[Feedback] = []
    coverage: dict[str, bool] = {}
    invalid = 0
    known_users = {str(row[0]) for row in connection.execute("SELECT id FROM users")}

    def append(row, kind, source="unknown", message="", rating=None, sentiment=""):
        nonlocal invalid
        user = str(row["user_id"])
        if user in owners:
            return
        at = _time(row["created_at"])
        if at is None or user not in known_users:
            invalid += 1
            return
        events.append(Feedback(user, at, kind, source, message, rating, sentiment))

    coverage["service_feedback"] = _available(connection, "service_feedback", "user_id message created_at source_screen")
    if coverage["service_feedback"]:
        for row in connection.execute("SELECT user_id,message,created_at,source_screen FROM service_feedback"):
            source = row["source_screen"] if row["source_screen"] in SCREENS else "unknown"
            append(row, "message", source, str(row["message"]))
    coverage["user_feedback"] = _available(connection, "user_feedback", "user_id version_id feedback_type rating message created_at")
    rated_pairs: set[tuple[str, str]] = set()
    if coverage["user_feedback"]:
        for row in connection.execute("SELECT user_id,version_id,feedback_type,rating,message,created_at FROM user_feedback"):
            if row["feedback_type"] == "comment":
                append(row, "message", message=str(row["message"] or ""))
            elif row["feedback_type"] == "rating" and row["rating"] in (1, 2, 3, 4, 5):
                rated_pairs.add((str(row["user_id"]), str(row["version_id"])))
                append(row, "rating", rating=int(row["rating"]))
    coverage["version_feedback"] = _available(connection, "version_feedback", "user_id sentiment updated_at")
    if coverage["version_feedback"]:
        for row in connection.execute("SELECT user_id,sentiment,updated_at AS created_at FROM version_feedback"):
            if row["sentiment"] in ("positive", "negative"):
                append(row, "reaction", sentiment=str(row["sentiment"]))
    # Legacy versions have no rated_at. Do not misrepresent generation time as
    # feedback time, or invent a historical payer cohort/period for those rows.
    undated: list[tuple[str, int]] = []
    coverage["gallery_ratings"] = _available(connection, "gallery_versions", "id gallery_item_id rating") and _available(connection, "gallery_items", "id user_id")
    if coverage["gallery_ratings"]:
        for row in connection.execute("SELECT v.id,i.user_id,v.rating FROM gallery_versions v JOIN gallery_items i ON i.id=v.gallery_item_id WHERE v.rating BETWEEN 1 AND 5"):
            user = str(row["user_id"])
            if user not in owners and user in known_users and (user, str(row["id"])) not in rated_pairs:
                undated.append((user, int(row["rating"])))
    return events, undated, coverage, invalid


def _payments(connection: sqlite3.Connection, owners: set[str]):
    required = {
        "payment_orders": "id user_id provider paid_at",
        "payment_events": "order_id provider event_type status processed_at",
        "continuation_pack_grants": "payment_order_id user_id created_at",
    }
    if not all(_available(connection, table, columns) for table, columns in required.items()):
        return None, {}
    confirmed: dict[str, list[datetime]] = defaultdict(list)
    uncertain: dict[str, list[tuple[datetime | None, datetime | None]]] = defaultdict(list)
    rows = connection.execute("""
        SELECT o.id,o.user_id,o.paid_at,e.processed_at,g.created_at AS granted_at
        FROM payment_orders o
        LEFT JOIN payment_events e ON e.order_id=o.id AND e.provider='robokassa'
          AND e.event_type='result_url' AND e.status='processed'
        LEFT JOIN continuation_pack_grants g ON g.payment_order_id=o.id AND g.user_id=o.user_id
        WHERE o.provider='robokassa' AND o.paid_at IS NOT NULL
    """)
    order_times: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    pending: dict[tuple[str, str], datetime | None] = {}
    paid_times: dict[tuple[str, str], datetime | None] = {}
    for row in rows:
        user = str(row["user_id"])
        if user in owners:
            continue
        key = (user, str(row["id"]))
        times = [_time(row[name]) for name in ("paid_at", "processed_at", "granted_at")]
        paid_times[key] = times[0]
        if all(value is not None for value in times):
            order_times[key].append(max(times))
        else:
            pending[key] = times[0]
    for key, values in order_times.items():
        ready = min(values)
        confirmed[key[0]].append(ready)
        uncertain[key[0]].append((paid_times[key], ready))
    for key, at in pending.items():
        if key not in order_times:
            uncertain[key[0]].append((at, None))
    return confirmed, uncertain


def _cohort(user: str, at: datetime, confirmed, uncertain) -> str:
    if confirmed is None:
        return "unknown"
    count = sum(value <= at for value in confirmed.get(user, ()))
    # At least two verified purchases prove repeat status even when another
    # order is incomplete. Otherwise incomplete payment evidence is unknown.
    if count >= 2:
        return "repeat_payer"
    if any((paid is None or paid <= at) and (ready is None or at < ready) for paid, ready in uncertain.get(user, ())):
        return "unknown"
    return "payer" if count else "nonpayer"


def _rating_stats(values: list[int]) -> dict:
    return {
        "count": len(values),
        "average": round(sum(values) / len(values), 3) if values else None,
        "distribution": {str(value): values.count(value) for value in range(1, 6)},
    }


def _period(events, start, end, confirmed, uncertain) -> dict:
    selected = [event for event in events if start <= event.at < end]
    messages = [event for event in selected if event.kind == "message"]
    ratings = [event for event in selected if event.kind == "rating"]
    reactions = [event for event in selected if event.kind == "reaction"]
    topics: dict[str, list[Feedback]] = defaultdict(list)
    unclassified: list[Feedback] = []
    for event in messages:
        tags = classify(event.message)
        if not tags:
            unclassified.append(event)
        for tag in tags:
            topics[tag].append(event)

    def counts(items):
        return {"messages": len(items), "authors": len({event.user for event in items})}

    def cohorts(items):
        return {cohort: counts([event for event in items if _cohort(event.user, event.at, confirmed, uncertain) == cohort]) for cohort in COHORTS}

    topic_rows = []
    for key, items in topics.items():
        category, title = TOPICS[key]
        topic_rows.append({"key": key, "category": category, "title": title, **counts(items), "cohorts": cohorts(items)})
    topic_rows.sort(key=lambda row: (-row["authors"], -row["messages"], row["key"]))
    by_author = Counter(event.user for event in messages)
    return {
        "start": start.astimezone(SAMARA).isoformat(),
        "end_exclusive": end.astimezone(SAMARA).isoformat(),
        **counts(messages),
        "respondents": len({event.user for event in messages + ratings}),
        "repeat_feedback_authors": sum(count > 1 for count in by_author.values()),
        "ratings": _rating_stats([event.rating for event in ratings]),
        "rating_authors": len({event.user for event in ratings}),
        "source_screens": dict(sorted(Counter(event.source for event in messages).items())),
        "legacy_reactions": {"positive": sum(event.sentiment == "positive" for event in reactions), "negative": sum(event.sentiment == "negative" for event in reactions), "authors": len({event.user for event in reactions})},
        "cohorts": cohorts(messages),
        "topics": topic_rows,
        "uncategorized": counts(unclassified),
    }


def _recommendations(current: dict, previous: dict) -> list[str]:
    recommendations = []
    critical = [topic for topic in current["topics"] if topic["category"] == "critical"]
    problems = [topic for topic in current["topics"] if topic["category"] == "problem" and topic["authors"] >= 2]
    features = [topic for topic in current["topics"] if topic["category"] == "feature" and topic["authors"] >= 2]
    for topic in critical[:1]:
        recommendations.append(f"Проверить сигнал «{topic['title']}» в поддержке: независимые авторы — {topic['authors']}, сообщения — {topic['messages']}. Это жалобы, не доказательство инцидента.")
    for topic in problems[:1]:
        recommendations.append(f"Воспроизвести проблему «{topic['title']}»: независимые авторы — {topic['authors']}. Приоритет исправления подтвердить диагностикой.")
    for topic in features[:1]:
        recommendations.append(f"Уточнить сценарий «{topic['title']}» у пользователей: независимые авторы — {topic['authors']}. Пока это основание для проверки спроса, не для разработки.")
    if current["uncategorized"]["messages"]:
        recommendations.append(f"Разобрать неклассифицированные отзывы в защищённом контуре: {current['uncategorized']['messages']} сообщений от {current['uncategorized']['authors']} авторов; отчёт не раскрывает их текст.")
    if len(recommendations) < 5 and current["cohorts"]["unknown"]["messages"]:
        recommendations.append(f"Восстановить проверяемость платёжного среза: статус неизвестен для {current['cohorts']['unknown']['messages']} сообщений; не считать их отзывами неплательщиков.")
    if len(recommendations) < 3:
        recommendations.append(f"Продолжить сбор независимых отзывов: сейчас {current['authors']} авторов и {current['messages']} сообщений; частые сообщения одного человека не равны массовому спросу.")
    if len(recommendations) < 3:
        recommendations.append(f"Повторить сравнение на следующем равном периоде: сейчас {current['messages']} сообщений, ранее {previous['messages']}; объём обращений сам по себе не измеряет удовлетворённость.")
    if len(recommendations) < 3:
        recommendations.append(f"Собирать числовые оценки вместе с контекстом: в периоде {current['ratings']['count']} оценок от {current['rating_authors']} авторов; не выводить 1–5 из реакций +/−.")
    return recommendations[:5]


def build_report(db_path: Path, *, days: int, owner_platform_ids: Iterable[str], at: datetime | None = None) -> dict:
    """Read one consistent snapshot, returning aggregates only (safe to JSON)."""
    if not 1 <= days <= 3660:
        raise ReportUnavailable("Период должен быть от 1 до 3660 дней.")
    cutoff = at or datetime.now(UTC)
    if cutoff.tzinfo is None:
        raise ReportUnavailable("Момент отчёта должен содержать часовой пояс.")
    cutoff = cutoff.astimezone(SAMARA)
    start = cutoff - timedelta(days=days)
    previous_start = start - timedelta(days=days)
    try:
        connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            owners = _owners(connection, owner_platform_ids)
            events, undated, coverage, invalid = _read_feedback(connection, owners)
            confirmed, uncertain = _payments(connection, owners)
            current = _period(events, start, cutoff, confirmed, uncertain)
            previous = _period(events, previous_start, start, confirmed, uncertain)
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        raise ReportUnavailable("Отчёт недоступен: существующая SQLite не читается или схема несовместима.") from None
    comparisons = {}
    for key in ("messages", "authors", "respondents", "rating_authors"):
        comparisons[key] = {"current": current[key], "previous": previous[key], "delta": current[key] - previous[key]}
    a, b = current["ratings"]["average"], previous["ratings"]["average"]
    comparisons["rating_average"] = {"current": a, "previous": b, "delta": round(a - b, 3) if a is not None and b is not None else None}
    old_topics = {topic["key"]: topic for topic in previous["topics"]}
    for topic in current["topics"]:
        old = old_topics.get(topic["key"], {})
        topic["previous_authors"] = old.get("authors", 0)
        topic["previous_messages"] = old.get("messages", 0)
    return {
        "report_version": 1,
        "timezone": "Europe/Samara",
        "days": days,
        "owner_exclusion": "verified_all_configured_max_owners",
        "coverage": {**coverage, "payment_evidence": confirmed is not None},
        "invalid_records_excluded": invalid,
        "current": current,
        "previous": previous,
        "comparison": comparisons,
        "undated_legacy_ratings": {**_rating_stats([rating for _, rating in undated]), "authors": len({user for user, _ in undated})},
        "recommendations": _recommendations(current, previous),
        "not_yet": [
            "Не менять цены, пакеты и продуктовую стратегию только по этой самоотобранной выборке.",
            "Не принимать повторные сообщения одного автора за массовый спрос и не запускать функцию по одному запросу.",
            "Не приравнивать реакции +/− к оценкам 1–5, неизвестный статус к отсутствию оплаты, а жалобу к доказанному инциденту.",
            "Не объявлять тренд качества по одному изменению числа сообщений или по оценкам без известной даты.",
        ],
    }


def render_markdown(report: dict) -> str:
    current, previous = report["current"], report["previous"]
    lines = ["# Отзывы Ravuna", "", f"Период Europe/Samara: [{current['start']}, {current['end_exclusive']}).",
             f"Предыдущий: [{previous['start']}, {previous['end_exclusive']}).",
             "Владельцы исключены по проверенному сопоставлению MAX_OWNER_USER_IDS; только агрегаты.", "",
             "| Показатель | Сейчас | Ранее |", "| --- | ---: | ---: |"]
    for key, label in (("messages", "Текстовые сообщения"), ("authors", "Независимые авторы текстов"), ("respondents", "Авторы текстов или оценок"), ("repeat_feedback_authors", "Авторы с 2+ сообщениями")):
        lines.append(f"| {label} | {current[key]} | {previous[key]} |")
    for key, label in (("count", "Оценки 1–5"), ("average", "Средняя оценка 1–5")):
        lines.append(f"| {label} | {current['ratings'][key] if current['ratings'][key] is not None else 'нет данных'} | {previous['ratings'][key] if previous['ratings'][key] is not None else 'нет данных'} |")
    lines += ["", "Распределение оценок (сейчас / ранее): " + "; ".join(f"{score}: {current['ratings']['distribution'][score]} / {previous['ratings']['distribution'][score]}" for score in "12345") + ".",
              "", "## Откуда пришли текстовые отзывы", ""]
    for screen in sorted(set(current["source_screens"]) | set(previous["source_screens"])):
        lines.append(f"- {SCREENS[screen]}: {current['source_screens'].get(screen, 0)} / {previous['source_screens'].get(screen, 0)}.")
    if not current["source_screens"] and not previous["source_screens"]:
        lines.append("Нет текстовых отзывов.")
    lines += ["", "## Платёжный срез текстовых отзывов на момент отправки", ""]
    for cohort in COHORTS:
        now, before = current["cohorts"][cohort], previous["cohorts"][cohort]
        lines.append(f"- {COHORT_LABELS[cohort]}: сообщения — {now['messages']}, авторы — {now['authors']}; ранее {before['messages']} / {before['authors']}.")
    lines += ["", "Один автор может попасть в разные срезы после оплаты; суммы авторов по срезам не складываются.", ""]
    for category, heading in (("critical", "Критичные жалобы (сигналы, не установленные факты)"), ("problem", "Повторяющиеся проблемы"), ("feature", "Запросы функций"), ("positive", "Положительные отзывы")):
        lines += [f"## {heading}", ""]
        topics = [topic for topic in current["topics"] if topic["category"] == category]
        for topic in topics:
            qualification = "повторяется у независимых авторов" if topic["authors"] >= 2 else "единичный автор; не массовый спрос"
            segments = "; ".join(f"{COHORT_LABELS[key]}: {topic['cohorts'][key]['authors']}" for key in COHORTS)
            lines.append(f"- {topic['title']}: сообщения — {topic['messages']}, авторы — {topic['authors']} ({qualification}); ранее {topic['previous_messages']} / {topic['previous_authors']}. Авторы по срезам: {segments}.")
        if not topics:
            lines.append("Явных лексических сигналов нет; это не доказывает отсутствия темы.")
        lines.append("")
    lines += [f"Неклассифицировано: {current['uncategorized']['messages']} сообщений / {current['uncategorized']['authors']} авторов; ранее {previous['uncategorized']['messages']} / {previous['uncategorized']['authors']}.", "", "## Исторические данные и ограничения", ""]
    for period, label in ((current, "Сейчас"), (previous, "Ранее")):
        reactions = period["legacy_reactions"]
        lines.append(f"- {label}: сохранённые реакции + {reactions['positive']}, − {reactions['negative']}, {reactions['authors']} авторов. Не включены в оценки или сообщения; могут отражать те же оценки.")
    legacy = report["undated_legacy_ratings"]
    lines += [f"- Числовые оценки старых версий без даты: {legacy['count']}, авторов {legacy['authors']}, средняя {legacy['average'] if legacy['average'] is not None else 'нет данных'}; распределение " + ", ".join(f"{key}: {value}" for key, value in legacy["distribution"].items()) + ". Не включены в периоды/платёжные срезы; исключены пары автор–версия с user_feedback.rating.",
              "- Оплата подтверждена только совокупностью paid_at, обработанного Robokassa ResultURL и выдачи пакета; все три момента не позже отзыва. SuccessURL, намерение оплаты и ручной грант сами по себе не подтверждают покупку. Повторная оплата — две разные подтверждённые покупки. Историческая покупка остаётся покупкой после возврата.",
              "- Темы определены локальными консервативными правилами, без AI/API и без цитат. Это сигналы из самоотобранных отзывов, не оценка всего рынка; неизвестные формулировки остаются неклассифицированными.",
              "- Для старых реакций используется последнее updated_at, а не история всех изменений; авторы могут пересекаться между темами."]
    unavailable = [key for key, value in report["coverage"].items() if not value]
    if unavailable:
        lines.append("- Недоступные источники (нули не означают отсутствие отзывов): " + ", ".join(unavailable) + ".")
    if report["invalid_records_excluded"]:
        lines.append(f"- Исключены записи без проверяемой даты/автора: {report['invalid_records_excluded']}.")
    lines += ["", "## Рекомендации", ""] + [f"{index}. {value}" for index, value in enumerate(report["recommendations"], 1)]
    lines += ["", "## Что пока не стоит делать на основании этих данных", ""] + ["- " + value for value in report["not_yet"]]
    return "\n".join(lines) + "\n"


class _SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats invalid arguments, which may contain IDs or
        # private paths; operator errors must be safe too.
        self.print_usage(sys.stderr)
        self.exit(2, "Ошибка аргументов. Используйте --help; значения не выводятся.\n")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeParser(description="Read-only отчёт отзывов Ravuna; владельцы исключаются по MAX_OWNER_USER_IDS.")
    parser.add_argument("--days", type=int, default=7, help="Длительность каждого из двух соседних периодов")
    parser.add_argument("--db", type=Path, help="Существующая SQLite; по умолчанию Settings.database_path")
    parser.add_argument("--at", type=_at, help="Правая исключённая граница ISO-8601 с часовым поясом")
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)
    try:
        # Loading Settings reads dotenv/process environment only: no application
        # construction, logging configuration, storage initialization or writes.
        from app.config import load_settings
        settings = load_settings()
        report = build_report(args.db or settings.database_path, days=args.days, at=args.at, owner_platform_ids=settings.max_owner_user_ids)
    except ReportUnavailable as error:
        print(str(error), file=sys.stderr)
        return 2
    except (ValueError, OSError):
        print("Отчёт недоступен: проверьте конфигурацию; значения не выводятся.", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else render_markdown(report), end="\n" if args.format == "json" else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
