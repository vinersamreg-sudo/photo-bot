"""Minimal deterministic policy for direct image-provider requests."""

import re
from dataclasses import replace
from typing import Optional

from app.edit_intent import EditMode, EditPlan


DIRECT_PROMPT_VERSION = "direct-unicode-v5"

_ONLY_REQUESTED_CHANGE = "Измени только то, что прямо указано пользователем."
_PRESERVATION_PARTS = (
    ("identity", "личности и узнаваемые лица всех людей"),
    ("expression", "их мимику"),
    ("pose", "позы, положение тел"),
    ("proportions", "пропорции"),
    ("viewpoint", "ракурс"),
    ("composition", "композицию"),
)

_EXPLICIT_CHANGE_PATTERNS = {
    "identity": (
        re.compile(
            r"\b(?:измен\w*|помен\w*|замен\w*|смен\w*|улучш\w*|преврат\w*|"
            r"убер\w*|удал\w*|сдела\w*)\b.{0,60}\b(?:лиц\w*|внешност\w*|"
            r"личност\w*)\b"
        ),
        re.compile(
            r"\b(?:лиц\w*|внешност\w*|личност\w*)\b.{0,40}\b(?:измен\w*|"
            r"помен\w*|замен\w*|смен\w*|улучш\w*|преврат\w*|убер\w*|удал\w*)\b"
        ),
        re.compile(
            r"\b(?:замен\w*|преврат\w*|убер\w*|удал\w*)\b.{0,60}\b(?:человек\w*|"
            r"люд\w*|девуш\w*|женщин\w*|мужчин\w*|парн\w*|персонаж\w*)\b"
        ),
        re.compile(
            r"\bсдела\w*\b.{0,30}\b(?:меня|его|ее|её|человек\w*)\b.{0,30}\b"
            r"(?:как|похож\w*|персонаж\w*)\b"
        ),
        re.compile(r"\b(?:фейс\s*свап|face\s*swap)\b"),
        re.compile(
            r"\b(?:change|replace|swap|remove|transform|alter)\b.{0,60}\b(?:face|"
            r"appearance|identity)\b"
        ),
        re.compile(
            r"\b(?:replace|swap|remove|transform)\b.{0,60}\b(?:person|people|"
            r"woman|man|girl|boy|character)\b"
        ),
    ),
    "expression": (
        re.compile(
            r"\b(?:измен\w*|помен\w*|замен\w*|смен\w*|добав\w*|сдела\w*)\b"
            r".{0,40}\b(?:мимик\w*|выражен\w*\s+лиц\w*|улыб\w*|гримас\w*)\b"
        ),
        re.compile(r"\b(?:улыбни\w*|подмигни\w*|нахмур\w*)\b"),
        re.compile(
            r"\b(?:change|alter|add|make)\b.{0,40}\b(?:expression|smile|frown)\b"
        ),
    ),
    "pose": (
        re.compile(
            r"\b(?:измен\w*|помен\w*|замен\w*|смен\w*|сдела\w*)\b.{0,45}\b"
            r"(?:поз\w*|положен\w*\s+(?:тел\w*|человек\w*|люд\w*|рук\w*|"
            r"ног\w*|голов\w*))\b"
        ),
        re.compile(
            r"\b(?:подним\w*|опуст\w*|согн\w*|выпрям\w*|поверн\w*|разверн\w*|"
            r"наклон\w*)\b.{0,25}\b(?:рук\w*|ног\w*|голов\w*|тел\w*|корпус\w*|"
            r"плеч\w*)\b"
        ),
        re.compile(
            r"\b(?:change|alter|replace)\b.{0,40}\b(?:pose|stance|body\s+position)\b"
        ),
    ),
    "proportions": (
        re.compile(
            r"\b(?:измен\w*|помен\w*|сдела\w*)\b.{0,40}\b(?:пропорц\w*|рост\w*|"
            r"телосложен\w*|стройн\w*|худ\w*|полн\w*)\b"
        ),
        re.compile(
            r"\b(?:change|alter|make)\b.{0,40}\b(?:proportion|height|body\s+shape|"
            r"slimmer|thinner|heavier)\b"
        ),
    ),
    "viewpoint": (
        re.compile(
            r"\b(?:измен\w*|помен\w*|сдела\w*)\b.{0,35}\b(?:ракурс\w*|"
            r"угол\w*\s+съемк\w*)\b"
        ),
        re.compile(r"\bвид\s+(?:сверху|снизу|сбоку|сзади)\b"),
        re.compile(
            r"\b(?:change|alter)\b.{0,35}\b(?:viewpoint|camera\s+angle|perspective)\b"
        ),
    ),
    "composition": (
        re.compile(
            r"\b(?:измен\w*|помен\w*|сдела\w*|расшир\w*|обреж\w*)\b.{0,40}\b"
            r"(?:композиц\w*|кадр\w*|кадрирован\w*|соотношен\w*\s+сторон)\b"
        ),
        re.compile(r"\b(?:крупн\w*\s+план|в\s+полный\s+рост)\b"),
        re.compile(
            r"\b(?:change|alter|crop|reframe|expand)\b.{0,40}\b(?:composition|"
            r"framing|frame|crop|aspect\s+ratio)\b"
        ),
    ),
}


def _normalize_for_guard(user_text: str) -> str:
    return " ".join(user_text.casefold().replace("ё", "е").split())


def _join_preservation_parts(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " и " + parts[-1]


def build_preservation_guard(user_text: str) -> str:
    """Return only protections that do not conflict with an explicit request."""

    normalized = _normalize_for_guard(user_text)
    protected = [
        text
        for name, text in _PRESERVATION_PARTS
        if not any(pattern.search(normalized) for pattern in _EXPLICIT_CHANGE_PATTERNS[name])
    ]
    if not protected:
        return _ONLY_REQUESTED_CHANGE
    return f"Сохрани {_join_preservation_parts(protected)}. {_ONLY_REQUESTED_CHANGE}"


def build_direct_prompt(
    user_text: str, *, preservation_guard_enabled: bool = True
) -> str:
    """Return exact user text; the legacy guard argument is compatibility-only."""

    if not user_text.strip():
        raise ValueError("Direct provider prompt must not be empty")
    return user_text


def build_direct_edit_plan(
    user_text: str,
    *,
    mode: EditMode,
    parent: Optional[EditPlan] = None,
    correction_target_version_id: Optional[str] = None,
) -> EditPlan:
    """Create lineage metadata without translating or interpreting the request."""

    source = user_text.strip()
    if mode == "repeat" and parent is not None:
        return replace(
            parent,
            mode="repeat",
            unresolved_ambiguities=(),
            parser_version=DIRECT_PROMPT_VERSION,
            confidence=1.0,
        )
    inherited = ()
    if mode == "correction" and parent is not None and parent.effective_user_text:
        inherited = (parent.effective_user_text,)
    return EditPlan(
        mode=mode,
        primary_action="custom",
        target_regions=(),
        requested_changes=(source,),
        preservation_rules=(),
        forbidden_changes=(),
        continuity_requirements=(),
        unresolved_ambiguities=(),
        source_user_text=source,
        inherited_user_text=inherited,
        inherited_constraints=(),
        correction_target_version_id=correction_target_version_id,
        confidence=1.0,
        parser_version=DIRECT_PROMPT_VERSION,
    )
