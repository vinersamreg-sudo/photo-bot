"""Deterministic Russian post copy built from English internal instructions."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import TransformationType


DISCLOSURE = (
    "Демонстрационный пример Pixora.\n"
    "Изображения созданы специально для демонстрации возможностей сервиса."
)
CTA_LEAD = "🎁 Получите 2 бесплатные обработки\n\n👇 Попробовать Pixora"
FORBIDDEN_STORY_FRAGMENTS = (
    "к нам обрати",
    "наш клиент",
    "наша клиентка",
    "история клиента",
    "заказчик попросил",
    "отзыв клиента",
)


@dataclass(frozen=True)
class CopyTemplate:
    title: str
    task: str
    results: tuple[str, ...]
    hashtags: tuple[str, ...]


@dataclass(frozen=True)
class GeneratedPostCopy:
    title: str
    body: str
    hashtags: tuple[str, ...]
    cta: str
    disclosure: str
    utm_url: str
    internal_prompt_en: str


TEMPLATES: dict[TransformationType, CopyTemplate] = {
    TransformationType.REPLACE_BACKGROUND: CopyTemplate(
        "Замена фона",
        "Заменить фон, сохранив человека и естественное освещение.",
        ("Фон заменён.", "Освещение и перспектива сохранены.", "Основной объект не изменён."),
        ("#Pixora", "#ЗаменаФона", "#ОбработкаФото"),
    ),
    TransformationType.REMOVE_OBJECT: CopyTemplate(
        "Удаление объекта",
        "Удалить лишний объект из кадра.",
        ("Объект удалён.", "Фон восстановлен.", "Композиция кадра сохранена."),
        ("#Pixora", "#УдалениеОбъектов", "#ОбработкаФото"),
    ),
    TransformationType.REMOVE_PERSON: CopyTemplate(
        "Удаление человека из кадра",
        "Удалить человека на заднем плане.",
        ("Человек удалён.", "Перспектива сохранена.", "Лица основных героев не изменены."),
        ("#Pixora", "#УдалениеОбъектов", "#Фото"),
    ),
    TransformationType.PORTRAIT: CopyTemplate(
        "Портрет",
        "Подготовить аккуратный портрет, сохранив узнаваемость человека.",
        ("Портрет подготовлен.", "Черты лица сохранены.", "Свет и фон приведены к единому стилю."),
        ("#Pixora", "#Портрет", "#ИИФото"),
    ),
    TransformationType.BUSINESS_PHOTO: CopyTemplate(
        "Фото для работы",
        "Подготовить деловой портрет для резюме или профиля.",
        ("Деловой образ собран.", "Лицо сохранено.", "Фон сделан нейтральным."),
        ("#Pixora", "#ДеловоеФото", "#Резюме"),
    ),
    TransformationType.RESTORE_PHOTO: CopyTemplate(
        "Восстановление фотографии",
        "Восстановить повреждённую или выцветшую фотографию.",
        ("Повреждения уменьшены.", "Контраст и детали восстановлены.", "Исходная композиция сохранена."),
        ("#Pixora", "#ВосстановлениеФото", "#СтарыеФото"),
    ),
    TransformationType.UPSCALE: CopyTemplate(
        "Улучшение качества",
        "Увеличить детализацию и читаемость фотографии.",
        ("Детализация повышена.", "Шум уменьшен.", "Содержание кадра сохранено."),
        ("#Pixora", "#УлучшениеФото", "#КачествоФото"),
    ),
    TransformationType.COLORIZE: CopyTemplate(
        "Колоризация",
        "Добавить естественные цвета в чёрно-белую фотографию.",
        ("Цвет добавлен.", "Тон кожи и окружения сбалансирован.", "Детали исходника сохранены."),
        ("#Pixora", "#Колоризация", "#СтарыеФото"),
    ),
    TransformationType.REPLACE_CLOTHES: CopyTemplate(
        "Замена одежды",
        "Изменить одежду, не меняя лицо и общую позу.",
        ("Одежда заменена.", "Лицо сохранено.", "Свет и складки согласованы с кадром."),
        ("#Pixora", "#ЗаменаОдежды", "#ОбработкаФото"),
    ),
    TransformationType.DOCUMENT_PHOTO: CopyTemplate(
        "Фото на документы",
        "Подготовить нейтральное фото в документальном стиле.",
        ("Фон выровнен.", "Кадрирование подготовлено.", "Черты лица сохранены."),
        ("#Pixora", "#ФотоНаДокументы", "#Фото"),
    ),
    TransformationType.AVATAR: CopyTemplate(
        "Новый аватар",
        "Подготовить выразительный аватар на основе исходной фотографии.",
        ("Аватар подготовлен.", "Человек остаётся узнаваемым.", "Композиция адаптирована для профиля."),
        ("#Pixora", "#Аватар", "#ИИФото"),
    ),
    TransformationType.OTHER: CopyTemplate(
        "Обработка фотографии",
        "Выполнить указанное изменение фотографии.",
        ("Изменение выполнено.", "Основная композиция сохранена.", "Результат подготовлен для сравнения."),
        ("#Pixora", "#ОбработкаФото", "#ИИФото"),
    ),
}


class ContentGenerator:
    def __init__(self, bot_url: str) -> None:
        self.bot_url = bot_url

    def generate(
        self,
        transformation_type: TransformationType,
        *,
        post_id: str,
        platform: str = "max",
        title_override: str | None = None,
    ) -> GeneratedPostCopy:
        template = TEMPLATES[transformation_type]
        utm_url = build_utm_url(self.bot_url, platform=platform, content=post_id)
        title = (title_override or template.title).strip()
        result_lines = "\n".join(f"• {line}" for line in template.results)
        body = (
            f"Задача:\n{template.task}\n\n"
            f"Результат:\n{result_lines}\n\n"
            f"{DISCLOSURE}"
        )
        cta = f"{CTA_LEAD}:\n{utm_url}"
        internal_prompt = (
            "Create factual Russian demo copy for the transformation "
            f"'{transformation_type.value}'. Use a task/result structure, no customer story, "
            "no testimonial, include the mandatory Pixora demo disclosure and CTA."
        )
        _validate_copy(title, body, cta, internal_prompt)
        return GeneratedPostCopy(
            title=title,
            body=body,
            hashtags=template.hashtags,
            cta=cta,
            disclosure=DISCLOSURE,
            utm_url=utm_url,
            internal_prompt_en=internal_prompt,
        )


def build_utm_url(url: str, *, platform: str, content: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "utm_source": platform,
            "utm_medium": "channel",
            "utm_campaign": "demo_posts",
            "utm_content": content,
        }
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _validate_copy(title: str, body: str, cta: str, internal_prompt: str) -> None:
    if not title or len(title) > 120:
        raise ValueError("post title must contain 1..120 characters")
    combined = f"{title}\n{body}\n{cta}".lower()
    if any(fragment in combined for fragment in FORBIDDEN_STORY_FRAGMENTS):
        raise ValueError("customer stories and testimonials are forbidden")
    if DISCLOSURE not in body:
        raise ValueError("mandatory demonstration disclosure is missing")
    if "utm_source=" not in cta or "utm_medium=" not in cta or "utm_campaign=" not in cta:
        raise ValueError("CTA URL must carry UTM parameters")
    if not internal_prompt.isascii():
        raise ValueError("Content Studio internal prompts must be English/ASCII")
