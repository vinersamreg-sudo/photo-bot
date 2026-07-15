"""Data-driven user scenarios; model names stay outside the user-facing catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    emoji: str
    category: str
    short_description: str
    prompt_template: str
    preview_image: Optional[str]
    required_input_count: int
    active: bool
    sort_order: int
    tags: tuple[str, ...]


SCENARIO_CATEGORIES = (
    "работа",
    "фотосессия",
    "путешествия",
    "праздники",
    "семья",
    "животные",
    "романтика",
    "мужские образы",
    "женские образы",
    "восстановление",
    "улучшение",
    "другое",
)


SCENARIOS = (
    Scenario("resume", "Фото для резюме", "👔", "работа", "Аккуратный профессиональный портрет", "Сохрани узнаваемость человека. Создай профессиональное фото для резюме: {instruction}", None, 1, True, 10, ("резюме", "работа")),
    Scenario("cafe", "Фото в кафе", "☕", "фотосессия", "Естественный кадр в уютном кафе", "Сохрани узнаваемость человека и перенеси сцену в уютное современное кафе: {instruction}", None, 1, True, 20, ("кафе", "лайфстайл")),
    Scenario("beach", "Фото на пляже", "🏖", "путешествия", "Светлый отпускной кадр", "Сохрани узнаваемость человека и создай реалистичную сцену на пляже: {instruction}", None, 1, True, 30, ("пляж", "отпуск")),
    Scenario("cat", "Фото с котиком", "🐈", "животные", "Добавить дружелюбного кота", "Сохрани человека и композицию, добавь рядом реалистичного дружелюбного кота: {instruction}", None, 1, True, 40, ("кот", "животные")),
    Scenario("business-look", "Деловой образ", "👔", "мужские образы", "Сменить одежду на деловую", "Сохрани лицо и позу, замени одежду на современный деловой образ: {instruction}", None, 1, True, 50, ("одежда", "бизнес")),
    Scenario("light-background", "Нейтральный светлый фон", "🌤", "улучшение", "Чистый фон без лишних деталей", "Сохрани человека без изменений и замени фон на нейтральный светлый студийный: {instruction}", None, 1, True, 60, ("фон", "светлый")),
    Scenario("restore", "Восстановить старое фото", "🕰", "восстановление", "Убрать повреждения и вернуть детали", "Бережно восстанови старую фотографию, убери царапины и дефекты, сохрани лица и историческую достоверность: {instruction}", None, 1, True, 70, ("реставрация", "старое фото")),
    Scenario("enhance", "Улучшить качество", "✨", "улучшение", "Повысить чёткость без изменения лица", "Улучши резкость, свет и детализацию фотографии, не меняя личность, черты лица и композицию: {instruction}", None, 1, True, 80, ("качество", "резкость")),
)


def get_scenario(scenario_id: Optional[str]) -> Optional[Scenario]:
    if not scenario_id:
        return None
    return next((scenario for scenario in SCENARIOS if scenario.id == scenario_id and scenario.active), None)
