"""Deterministic Russian post copy built from English internal instructions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import TransformationType


DISCLOSURE = (
    "Синтетический пример Ravuna."
)
CTA_LEAD = "🎁 Получите 2 бесплатные обработки\n\n👇 Попробовать Ravuna"
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
    source_code: str
    internal_prompt_en: str


TEMPLATES: dict[TransformationType, CopyTemplate] = {
    TransformationType.REPLACE_BACKGROUND: CopyTemplate(
        "Замена фона",
        "Заменить фон, сохранив человека и естественное освещение.",
        ("Фон заменён.", "Освещение и перспектива сохранены.", "Основной объект не изменён."),
        ("#Ravuna", "#ЗаменаФона", "#ОбработкаФото"),
    ),
    TransformationType.REMOVE_OBJECT: CopyTemplate(
        "Удаление объекта",
        "Удалить лишний объект из кадра.",
        ("Объект удалён.", "Фон восстановлен.", "Композиция кадра сохранена."),
        ("#Ravuna", "#УдалениеОбъектов", "#ОбработкаФото"),
    ),
    TransformationType.REMOVE_PERSON: CopyTemplate(
        "Удаление человека из кадра",
        "Удалить человека на заднем плане.",
        ("Человек удалён.", "Перспектива сохранена.", "Лица основных героев не изменены."),
        ("#Ravuna", "#УдалениеОбъектов", "#Фото"),
    ),
    TransformationType.PORTRAIT: CopyTemplate(
        "Портрет",
        "Подготовить аккуратный портрет, сохранив узнаваемость человека.",
        ("Портрет подготовлен.", "Черты лица сохранены.", "Свет и фон приведены к единому стилю."),
        ("#Ravuna", "#Портрет", "#ИИФото"),
    ),
    TransformationType.BUSINESS_PHOTO: CopyTemplate(
        "Фото для работы",
        "Подготовить деловой портрет для резюме или профиля.",
        ("Деловой образ собран.", "Лицо сохранено.", "Фон сделан нейтральным."),
        ("#Ravuna", "#ДеловоеФото", "#Резюме"),
    ),
    TransformationType.RESTORE_PHOTO: CopyTemplate(
        "Восстановление фотографии",
        "Восстановить повреждённую или выцветшую фотографию.",
        ("Повреждения уменьшены.", "Контраст и детали восстановлены.", "Исходная композиция сохранена."),
        ("#Ravuna", "#ВосстановлениеФото", "#СтарыеФото"),
    ),
    TransformationType.UPSCALE: CopyTemplate(
        "Улучшение качества",
        "Увеличить детализацию и читаемость фотографии.",
        ("Детализация повышена.", "Шум уменьшен.", "Содержание кадра сохранено."),
        ("#Ravuna", "#УлучшениеФото", "#КачествоФото"),
    ),
    TransformationType.COLORIZE: CopyTemplate(
        "Колоризация",
        "Добавить естественные цвета в чёрно-белую фотографию.",
        ("Цвет добавлен.", "Тон кожи и окружения сбалансирован.", "Детали исходника сохранены."),
        ("#Ravuna", "#Колоризация", "#СтарыеФото"),
    ),
    TransformationType.REPLACE_CLOTHES: CopyTemplate(
        "Замена одежды",
        "Изменить одежду, не меняя лицо и общую позу.",
        ("Одежда заменена.", "Лицо сохранено.", "Свет и складки согласованы с кадром."),
        ("#Ravuna", "#ЗаменаОдежды", "#ОбработкаФото"),
    ),
    TransformationType.DOCUMENT_PHOTO: CopyTemplate(
        "Фото на документы",
        "Подготовить нейтральное фото в документальном стиле.",
        ("Фон выровнен.", "Кадрирование подготовлено.", "Черты лица сохранены."),
        ("#Ravuna", "#ФотоНаДокументы", "#Фото"),
    ),
    TransformationType.AVATAR: CopyTemplate(
        "Новый аватар",
        "Подготовить выразительный аватар на основе исходной фотографии.",
        ("Аватар подготовлен.", "Человек остаётся узнаваемым.", "Композиция адаптирована для профиля."),
        ("#Ravuna", "#Аватар", "#ИИФото"),
    ),
    TransformationType.OTHER: CopyTemplate(
        "Обработка фотографии",
        "Выполнить указанное изменение фотографии.",
        ("Изменение выполнено.", "Основная композиция сохранена.", "Результат подготовлен для сравнения."),
        ("#Ravuna", "#ОбработкаФото", "#ИИФото"),
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
        source_code = build_source_code(platform, post_id)
        utm_url = build_utm_url(
            self.bot_url, platform=platform, content=post_id, source_code=source_code
        )
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
            "no testimonial, include the short Ravuna demo disclosure and CTA."
        )
        _validate_copy(title, body, cta, internal_prompt)
        return GeneratedPostCopy(
            title=title,
            body=body,
            hashtags=template.hashtags,
            cta=cta,
            disclosure=DISCLOSURE,
            utm_url=utm_url,
            source_code=source_code,
            internal_prompt_en=internal_prompt,
        )

    def generate_demo_case(
        self,
        transformation_type: TransformationType,
        *,
        post_id: str,
        platform: str,
        title: str,
        hook: str,
        prompt_example: str,
        hashtags: tuple[str, ...],
    ) -> GeneratedPostCopy:
        """Build varied factual copy without inventing a customer narrative."""

        variant = int(hashlib.sha256(post_id.encode("utf-8")).hexdigest()[:8], 16) % 3
        titles = (
            hook,
            f"До и после: {title}",
            f"{title} — один точный запрос",
        )
        task_leads = (
            "Пример запроса",
            "Демонстрационная задача",
            "Что меняем в этом примере",
        )
        result_lines = (
            "Показываем конкретный результат до и после — без сложных настроек.",
            "На карточке видно, что изменилось после одного точного запроса.",
            "Сравните исходное изображение и готовый демонстрационный результат.",
        )
        source_code = build_source_code(platform, post_id)
        utm_url = build_utm_url(
            self.bot_url, platform=platform, content=post_id, source_code=source_code
        )
        generated_title = titles[variant].strip()
        body = (
            f"{task_leads[variant]}: «{prompt_example.strip()}»\n\n"
            f"{result_lines[variant]}\n\n{DISCLOSURE}"
        )
        cta = f"{CTA_LEAD}:\n{utm_url}"
        internal_prompt = (
            "Create one factual Russian hook/task/result demo variant for "
            f"'{transformation_type.value}'. Never invent a customer story or testimonial. "
            "Include the short Ravuna demo disclosure and attributed CTA."
        )
        _validate_copy(generated_title, body, cta, internal_prompt)
        return GeneratedPostCopy(
            title=generated_title,
            body=body,
            hashtags=hashtags,
            cta=cta,
            disclosure=DISCLOSURE,
            utm_url=utm_url,
            source_code=source_code,
            internal_prompt_en=internal_prompt,
        )


def build_source_code(platform: str, content: str) -> str:
    platform_code = {"max": "max", "vk": "vk", "telegram": "tg"}.get(platform)
    if platform_code is None:
        raise ValueError("unsupported attribution platform")
    if platform == "vk" and content.startswith("vk-clip-"):
        platform_code = "vk-clip"
    elif platform == "vk" and content.startswith("vk-post-"):
        platform_code = "vk-post"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
    return f"src_{platform_code}-{digest}"


def build_utm_url(
    url: str, *, platform: str, content: str, source_code: str | None = None
) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "start": source_code or build_source_code(platform, content),
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
    if "start=src_" not in cta:
        raise ValueError("CTA URL must carry a MAX bot start source")
    if not internal_prompt.isascii():
        raise ValueError("Content Studio internal prompts must be English/ASCII")
