"""Deterministic Russian edit-intent parsing and cumulative EditPlan merging."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal, Mapping, Optional, Sequence


EditMode = Literal["initial_edit", "correction", "repeat", "scenario"]
PrimaryAction = Literal[
    "replace_background",
    "preserve_background",
    "sharpen_background",
    "change_clothes",
    "change_pose",
    "restore_photo",
    "improve_quality",
    "remove_object",
    "add_object",
    "custom",
]

PARSER_VERSION = "rules-ru-v4"
EDIT_PLAN_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class BackgroundIntent:
    operation: str = "unchanged"
    setting: Optional[str] = None
    sharpness: str = "preserve"
    category: Optional[str] = None
    realism: str = "preserve"
    blur: str = "preserve"
    source: str = "auto"


@dataclass(frozen=True)
class LightingIntent:
    style: Optional[str] = None


@dataclass(frozen=True)
class CameraIntent:
    framing: Optional[str] = None


@dataclass(frozen=True)
class OutfitIntent:
    operation: str = "unchanged"
    style: Optional[str] = None
    color: Optional[str] = None


@dataclass(frozen=True)
class PoseIntent:
    operation: str = "unchanged"
    description: Optional[str] = None


@dataclass(frozen=True)
class ObjectIntent:
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class SceneIntent:
    """Provider-neutral semantic state carried by every version."""

    identity: str = "preserve"
    face: str = "preserve"
    skin: str = "preserve"
    background: BackgroundIntent = field(default_factory=BackgroundIntent)
    lighting: LightingIntent = field(default_factory=LightingIntent)
    camera: CameraIntent = field(default_factory=CameraIntent)
    outfit: OutfitIntent = field(default_factory=OutfitIntent)
    pose: PoseIntent = field(default_factory=PoseIntent)
    objects: ObjectIntent = field(default_factory=ObjectIntent)
    negative: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "SceneIntent":
        raw = value or {}

        def mapping(name: str) -> Mapping[str, Any]:
            nested = raw.get(name)
            return nested if isinstance(nested, Mapping) else {}

        background = mapping("background")
        lighting = mapping("lighting")
        camera = mapping("camera")
        outfit = mapping("outfit")
        pose = mapping("pose")
        objects = mapping("objects")
        return cls(
            identity=str(raw.get("identity") or "preserve"),
            face=str(raw.get("face") or "preserve"),
            skin=str(raw.get("skin") or "preserve"),
            background=BackgroundIntent(
                operation=str(background.get("operation") or "unchanged"),
                setting=str(background["setting"]) if background.get("setting") else None,
                sharpness=str(background.get("sharpness") or "preserve"),
                category=(
                    str(background["category"]) if background.get("category") else None
                ),
                realism=str(background.get("realism") or "preserve"),
                blur=str(background.get("blur") or "preserve"),
                source=str(background.get("source") or "auto"),
            ),
            lighting=LightingIntent(
                style=str(lighting["style"]) if lighting.get("style") else None,
            ),
            camera=CameraIntent(
                framing=str(camera["framing"]) if camera.get("framing") else None,
            ),
            outfit=OutfitIntent(
                operation=str(outfit.get("operation") or "unchanged"),
                style=str(outfit["style"]) if outfit.get("style") else None,
                color=str(outfit["color"]) if outfit.get("color") else None,
            ),
            pose=PoseIntent(
                operation=str(pose.get("operation") or "unchanged"),
                description=str(pose["description"]) if pose.get("description") else None,
            ),
            objects=ObjectIntent(
                add=tuple(str(item) for item in objects.get("add") or ()),
                remove=tuple(str(item) for item in objects.get("remove") or ()),
            ),
            negative=tuple(str(item) for item in raw.get("negative") or ()),
        )


@dataclass(frozen=True)
class EditPlan:
    """Serializable, provider-independent representation of an image edit."""

    mode: EditMode
    primary_action: PrimaryAction
    target_regions: tuple[str, ...]
    requested_changes: tuple[str, ...]
    preservation_rules: tuple[str, ...]
    forbidden_changes: tuple[str, ...]
    continuity_requirements: tuple[str, ...]
    unresolved_ambiguities: tuple[str, ...]
    source_user_text: str
    inherited_user_text: tuple[str, ...]
    inherited_constraints: tuple[str, ...]
    correction_target_version_id: Optional[str]
    confidence: float
    parser_version: str = PARSER_VERSION
    schema_version: int = EDIT_PLAN_SCHEMA_VERSION
    scene: SceneIntent = field(default_factory=SceneIntent)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EditPlan":
        def strings(name: str) -> tuple[str, ...]:
            raw = value.get(name) or ()
            return tuple(str(item) for item in raw if str(item).strip())

        mode = str(value.get("mode") or "initial_edit")
        if mode not in {"initial_edit", "correction", "repeat", "scenario"}:
            mode = "initial_edit"
        action = str(value.get("primary_action") or "custom")
        if action not in {
            "replace_background", "preserve_background", "sharpen_background",
            "change_clothes", "change_pose", "restore_photo", "improve_quality",
            "remove_object", "add_object", "custom",
        }:
            action = "custom"
        return cls(
            mode=mode,  # type: ignore[arg-type]
            primary_action=action,  # type: ignore[arg-type]
            target_regions=strings("target_regions"),
            requested_changes=strings("requested_changes"),
            preservation_rules=strings("preservation_rules"),
            forbidden_changes=strings("forbidden_changes"),
            continuity_requirements=strings("continuity_requirements"),
            unresolved_ambiguities=strings("unresolved_ambiguities"),
            source_user_text=str(value.get("source_user_text") or ""),
            inherited_user_text=strings("inherited_user_text"),
            inherited_constraints=strings("inherited_constraints"),
            correction_target_version_id=(
                str(value["correction_target_version_id"])
                if value.get("correction_target_version_id") else None
            ),
            confidence=max(0.0, min(1.0, float(value.get("confidence", 0.5)))),
            parser_version=str(value.get("parser_version") or "legacy"),
            schema_version=int(value.get("schema_version") or 1),
            scene=SceneIntent.from_dict(
                value.get("scene") if isinstance(value.get("scene"), Mapping) else None
            ),
        )

    @classmethod
    def from_json(cls, raw: str) -> "EditPlan":
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("EditPlan JSON must contain an object")
        return cls.from_dict(value)

    @classmethod
    def from_legacy(
        cls,
        user_text: str,
        *,
        correction: bool = False,
        correction_target_version_id: Optional[str] = None,
    ) -> "EditPlan":
        plan = parse_edit_intent(
            user_text,
            mode="correction" if correction else "initial_edit",
            correction_target_version_id=correction_target_version_id,
        )
        return replace(plan, parser_version="legacy-backfill-v1", confidence=min(plan.confidence, 0.5))

    @property
    def effective_user_text(self) -> str:
        return "\n".join((*self.inherited_user_text, self.source_user_text)).strip()


def _normalized(text: str) -> str:
    value = text.casefold().replace("ё", "е")
    value = re.sub(r"[^\w\s-]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


SCENARIO_RULES: dict[str, tuple[PrimaryAction, str, str]] = {
    "light-background": (
        "replace_background", "background",
        "Replace the background with a clean, neutral, light studio background.",
    ),
    "resume": (
        "custom", "whole_image",
        "Create a realistic, restrained professional portrait suitable for a resume.",
    ),
    "business-look": (
        "change_clothes", "clothing",
        "Replace the clothing with a realistic modern business outfit.",
    ),
    "restore": (
        "restore_photo", "whole_image",
        "Restore damage, scratches and lost detail while preserving historical authenticity.",
    ),
    "enhance": (
        "improve_quality", "whole_image",
        "Improve sharpness, lighting and natural detail without redesigning the photograph.",
    ),
    "cafe": (
        "replace_background", "background",
        "Place the subject in a realistic, cozy modern cafe environment.",
    ),
    "beach": (
        "replace_background", "background",
        "Place the subject in a realistic bright beach environment.",
    ),
    "cat": (
        "add_object", "object",
        "Add a realistic friendly cat beside the subject.",
    ),
    "neuro-session": (
        "custom", "whole_image",
        "Create a polished photorealistic portrait while preserving the same person.",
    ),
    "documents": (
        "replace_background", "background",
        "Create a neutral document-style portrait with even light and a plain background.",
    ),
    "memorial": (
        "restore_photo", "whole_image",
        "Create a respectful restored portrait suitable for memorial use.",
    ),
}


def parse_edit_intent(
    user_text: str,
    *,
    mode: EditMode = "initial_edit",
    scenario_id: Optional[str] = None,
    correction_target_version_id: Optional[str] = None,
) -> EditPlan:
    """Parse common Russian photo-edit phrasing without an extra model call."""

    source = user_text.strip()
    text = _normalized(source)
    targets: list[str] = []
    changes: list[str] = []
    preserve: list[str] = []
    forbid: list[str] = []
    continuity: list[str] = []
    ambiguities: list[str] = []
    actions: list[PrimaryAction] = []
    background = BackgroundIntent()
    lighting = LightingIntent()
    camera = CameraIntent()
    outfit = OutfitIntent()
    pose = PoseIntent()
    objects_to_add: list[str] = []
    objects_to_remove: list[str] = []
    negative: list[str] = []

    explicit_replace_background = _has(
        text,
        r"(?:замени|поменяй|смени|измени|заменить|поменять|сменить|изменить)\s+(?:мне\s+)?(?:фон|задн\w*\s+план)",
        r"фон\s+(?:на|в)\s+скал",
        r"(?:сделай|добавь)\s+скал\w*\s+(?:на\s+)?фон",
        r"(?:сделай|поставь)\s+(?:на\s+фон\s+)?(?:горы|скалы|альпы)",
        r"сделай\s+(?:мне\s+)?(?:светл|бел|темн|студийн)\w*\s+(?:фон|задн\w*\s+план)",
        r"сделай\s+(?:мне\s+)?(?:фон|задн\w*\s+план)\s+(?:светл|бел|темн|студийн|скалист|горн)",
        r"^(?:светл|бел|темн|студийн|скалист|горн)\w*\s+фон$",
    )
    explicit_ai_background = _has(
        text,
        r"(?:создай|сгенерируй|используй|сделай)\s+(?:мне\s+)?(?:ai|ии)[ -]?(?:фон|задн\w*\s+план)",
        r"(?:ai|ии)[ -]?фон",
    )
    explicit_replace_background = explicit_replace_background or explicit_ai_background
    explicit_preserve_background = _has(
        text,
        r"не\s+(?:меняй|заменяй|трогай)\s+(?:эт\w*\s+|текущ\w*\s+)?(?:фон|скал|задн\w*\s+план)",
        r"(?:фон|скал\w*|задн\w*\s+план)\s+не\s+(?:меняй|заменяй|трогай)",
        r"(?:оставь|сохрани)\s+(?:эт\w*\s+же\s+|текущ\w*\s+|прошл\w*\s+)?(?:фон|скал|гор\w*|задн\w*\s+план)",
        r"не\s+надо\s+(?:менять|заменять)\s+(?:скал|гор\w*|фон)",
    )
    if "итоговое решение сохранить текущий фон" in text:
        explicit_replace_background = False
        explicit_preserve_background = True
    elif "итоговое решение заменить текущий фон" in text:
        explicit_replace_background = True
        explicit_preserve_background = False
    contradictory_background = explicit_replace_background and explicit_preserve_background
    if contradictory_background:
        # The direct flow has no confirmation screen. Prefer the conservative,
        # explicitly negated instruction and keep the existing background.
        explicit_replace_background = False
        explicit_preserve_background = True
        negative.append("replace background")
    if explicit_replace_background:
        targets.append("background")
        actions.append("replace_background")
        if "альп" in text:
            changes.append("Replace the background with realistic alpine mountains.")
            background = BackgroundIntent(
                "replace", "realistic alpine mountains", "sharp",
                "alpine_mountains", "photorealistic", "forbidden",
            )
        elif "скал" in text or "камен" in text or "гор" in text:
            changes.append("Replace the background with realistic rocky mountains and visible rock formations.")
            background = BackgroundIntent(
                "replace", "realistic rocky mountains", "sharp",
                "rocky_mountains", "photorealistic", "forbidden",
            )
        elif _has(text, r"светл|бел|студи"):
            changes.append("Replace the background with a clean light modern photo studio.")
            background = BackgroundIntent(
                "replace", "clean light modern photo studio", "sharp",
                "light_photo_studio", "photorealistic", "forbidden",
            )
        else:
            changes.append(
                "Replace the entire background with a clearly different, realistic, "
                "clean and context-appropriate setting."
            )
            background = BackgroundIntent(
                "replace",
                "a clearly different realistic clean context-appropriate setting",
                "sharp",
                None,
                "photorealistic",
                "forbidden",
            )
        if explicit_ai_background:
            background = replace(background, source="ai")

    if explicit_preserve_background:
        targets.append("background")
        preserve.append("Preserve the current background setting and its recognizable visual content.")
        forbid.append("Do not replace the current background with a different setting.")
        continuity.append("Continue from the exact background visible in the parent version.")
        actions.append("preserve_background")
        background = BackgroundIntent("preserve", None, "preserve", None, "preserve", "preserve")
        negative.append("replace background")

    improve_existing_background = (
        not explicit_replace_background
        and not explicit_preserve_background
        and _has(
            text,
            r"(?:улучш|сделай\s+красив|приведи\s+в\s+порядок).{0,24}(?:фон|задн\w*\s+план)",
            r"(?:фон|задн\w*\s+план).{0,24}(?:улучш|красив|аккурат|качествен)",
        )
    )
    if improve_existing_background:
        targets.append("background")
        actions.append("improve_quality")
        changes.append(
            "Visibly upgrade the existing background while keeping the same broad "
            "location, camera perspective and subject placement. Improve the decor "
            "and surfaces, remove distracting clutter, and refine lighting, clarity "
            "and color balance. The environmental improvement must be unmistakable, "
            "not merely a brightness, contrast or color adjustment."
        )
        background = BackgroundIntent(
            "enhance", None, "improve", None, "photorealistic", "preserve"
        )

    blur_complaint = _has(
        text,
        r"(?:фон|задн\w*\s+план|скал\w*)[^.]{0,25}(?:размыт|мыльн|нечетк)",
        r"(?:размыт|мыльн|нечетк)[^.]{0,25}(?:фон|задн\w*\s+план|скал)",
        r"все\s+равно\s+(?:размыт|мыльн|нечетк)",
        r"(?:четч|резч|детальн).{0,20}(?:фон|скал|гор|задн)",
        r"(?:фон|скал|гор).{0,20}(?:четч|резч|детальн)",
        r"убери\s+размыт",
        r"не\s+размывай\s+(?:фон|скал|задн)",
    )
    request_more_blur = _has(text, r"(?:сильнее|больше)\s+размо", r"добавь\s+(?:боке|размыт)")
    if blur_complaint and request_more_blur:
        # A complaint that the background is blurred wins over a positive blur keyword.
        request_more_blur = False
        negative.append("background blur")
    if blur_complaint:
        targets.append("background")
        actions.append("sharpen_background")
        changes.append("Make the existing background sharp, detailed and clearly readable.")
        preserve.append("Preserve the current background scene, layout and recognizable rocks or landmarks.")
        forbid.extend((
            "Do not replace the current background.",
            "Do not apply background blur, bokeh, defocus or shallow depth of field.",
        ))
        continuity.append("Correct only the residual background blur in the parent version.")
        background = replace(
            background, operation="sharpen", sharpness="sharp", blur="forbidden"
        )
        negative.extend(("background blur", "replacement background"))
    elif request_more_blur:
        targets.append("background")
        changes.append("Increase natural background blur while keeping the subject sharp.")
        background = replace(
            background, operation="blur", sharpness="blurred", blur="requested"
        )

    if _has(text, r"верни\s+(?:прошл\w*|предыдущ\w*)\s+(?:фон|задн)", r"сделай\s+как\s+было"):
        targets.append("background")
        actions.append("preserve_background")
        changes.append("Restore the background appearance from the previous successful version.")
        continuity.append("Use the previous successful background while preserving later successful edits.")
        forbid.append("Do not invent a new replacement background.")
        background = BackgroundIntent(
            "restore_previous", None, "preserve", None, "preserve", "preserve"
        )
        negative.append("new replacement background")

    if _has(
        text,
        r"(?:переодень|переодеть|одень|одеть)(?:\s+(?:всех|людей|их))?",
        r"(?:смени|замени|заменить|поменяй|поменять)\s+(?:всех\s+|людей\s+|их\s+|только\s+)?одежд",
        r"(?:сделай|измени)\s+одежд\w*",
        r"одежд\w*\s+(?:для\s+хайкинг|торжествен|празднич|нарядн|вечерн)",
    ):
        targets.append("clothing")
        actions.append("change_clothes")
        explicit_single_person = _has(
            text,
            r"\b(?:мне|меня|ему|ей|его|её)\b",
            r"\b(?:мужчину|женщину|ребенка|ребёнка|девочку|мальчика|человека)\b",
            r"\b(?:слева|справа|в\s+центре)\b",
        )
        every_person = (
            _has(text, r"\b(?:все|всех|людей|кажд\w*)\b")
            or not explicit_single_person
        )
        subject = (
            "every visible person whose clothing is visible"
            if every_person
            else "the requested visible subject"
        )
        if _has(text, r"торжествен|празднич|нарядн|вечерн"):
            style = (
                "realistic formal, occasion-appropriate clothing suited individually "
                "to each person's age and role"
            )
            changes.append(
                f"Replace the clothing of {subject} with {style}. "
                "Make the wardrobe change clear and complete; do not leave a targeted "
                "person's original outfit unchanged."
            )
            outfit = OutfitIntent("replace", style)
        elif _has(text, r"хайкинг|поход|турист"):
            style = "realistic, practical hiking clothing"
            changes.append(f"Replace the clothing of {subject} with {style}.")
            outfit = OutfitIntent("replace", style)
        elif _has(text, r"делов|бизнес|костюм"):
            style = "a realistic modern business outfit"
            changes.append(f"Replace the clothing of {subject} with {style}.")
            outfit = OutfitIntent("replace", style)
        else:
            if every_person:
                style = (
                    "clearly different, realistic, context-appropriate outfits "
                    "coordinated naturally across the group"
                )
                changes.append(
                    f"Replace the complete visible outfit of {subject} with {style}. "
                    "Apply the wardrobe change independently to each person and do "
                    "not leave any targeted person's original outfit unchanged."
                )
                outfit = OutfitIntent("replace", style)
            else:
                changes.append(
                    f"Replace the clothing of {subject} according to the user's request. "
                    "Make the requested wardrobe change clearly visible and photorealistic."
                )
                outfit = OutfitIntent("replace", "the clearly requested realistic clothing")

    if _has(text, r"(?:добавь|надень|сделай).{0,15}(?:куртк|пиджак)"):
        targets.append("clothing")
        actions.append("change_clothes")
        style = "realistic hiking jacket" if _has(text, r"хайкинг|поход|турист") else "realistic jacket"
        changes.append(f"Change the outfit to include a {style}.")
        outfit = OutfitIntent("replace", style)

    if _has(text, r"(?:смени|измени|поменяй|замени)\s+(?:мне\s+)?(?:куртк|пиджак)"):
        targets.append("clothing")
        actions.append("change_clothes")
        changes.append("Replace the jacket with the requested realistic jacket.")
        outfit = OutfitIntent("replace", "requested realistic jacket")

    clothing_color = None
    for russian, english in {
        "темно зелен": "dark green",
        "темно-зелен": "dark green",
        "зелен": "green",
        "черн": "black",
        "бел": "white",
        "син": "blue",
        "красн": "red",
        "сер": "gray",
        "коричнев": "brown",
        "бежев": "beige",
    }.items():
        if russian in text:
            clothing_color = english
            break
    if clothing_color and _has(text, r"(?:цвет\s+)?(?:куртк|одежд|пиджак)", r"перекрась"):
        targets.append("clothing")
        actions.append("change_clothes")
        changes.append(f"Change only the clothing color to {clothing_color}.")
        outfit = OutfitIntent("recolor", outfit.style, clothing_color)

    if _has(text, r"(?:смени|измени|поменяй|скорректируй)\s+(?:немного\s+)?поз"):
        targets.append("pose")
        actions.append("change_pose")
        changes.append("Adjust the subject's pose naturally as requested.")
        pose = PoseIntent("change", "natural requested pose")

    if _has(text, r"расслабленн\w*\s+поз|поз\w*\s+расслаблен"):
        targets.append("pose")
        actions.append("change_pose")
        changes.append("Use a natural relaxed pose.")
        pose = PoseIntent("change", "natural relaxed pose")

    if _has(text, r"(?:смени|измени|поменяй|замени)\s+(?:прическ|волос)"):
        targets.append("hair")
        changes.append("Change the hairstyle only as requested while preserving identity and hairline realism.")

    if _has(text, r"(?:смени|измени|поменяй)\s+(?:лицо|черты\s+лица)") and not _has(
        text, r"не\s+(?:смени|изменяй|меняй).{0,10}(?:лицо|черты)"
    ):
        targets.append("face")
        changes.append("Change only the explicitly requested facial attribute conservatively.")

    if _has(text, r"(?:восстанов|реставрир|убери\s+царап)"):
        targets.append("whole_image")
        actions.append("restore_photo")
        changes.append("Restore damage and lost detail without inventing a different photograph.")

    if _has(text, r"(?:улучш|повысь).{0,15}(?:качеств|резк|детал)") and "background" not in targets:
        targets.append("whole_image")
        actions.append("improve_quality")
        changes.append("Improve natural sharpness, lighting and detail across the photograph.")

    if (
        _has(
            text,
            r"(?:сделай|сделать)\s+(?:фото\s+)?красив",
            r"(?:улучш|обработай)\s+(?:это\s+)?(?:фото|фотограф)\w*$",
        )
        and not actions
    ):
        targets.append("whole_image")
        actions.append("improve_quality")
        changes.append(
            "Improve the photograph visibly with natural lighting, balanced color, "
            "clean detail and restrained retouching, without redesigning its people, "
            "objects, setting or composition."
        )

    if _has(
        text,
        r"(?:убери|удали(?:ть)?)\s+(?:(?:лишн|ненужн)\w*\s+)?(?:объект|предмет|человека|девушк\w*|мужчин\w*|надпись)",
    ):
        targets.append("object")
        actions.append("remove_object")
        changes.append("Remove only the requested object and reconstruct the occluded area naturally.")
        if "девуш" in text:
            objects_to_remove.append("woman")
        elif "мужчин" in text:
            objects_to_remove.append("man")
        elif "надпис" in text:
            objects_to_remove.append("text")
        else:
            objects_to_remove.append("requested object")
    if _has(text, r"(?:добавь|добавить|дорисуй)\s+(?:объект|предмет|человека|животн|кот|солнц)"):
        targets.append("object")
        actions.append("add_object")
        changes.append("Add only the requested object with realistic scale, light and perspective.")
        if "солнц" in text:
            objects_to_add.append("sun")
        elif "кот" in text:
            objects_to_add.append("cat")
        else:
            objects_to_add.append("requested object")

    if _has(text, r"(?:сделай|добавь).{0,12}закат|закатн\w*\s+(?:свет|освещ)"):
        targets.append("whole_image")
        changes.append("Change the lighting to realistic warm sunset light.")
        lighting = LightingIntent("realistic warm sunset light")

    if _has(text, r"(?:делов\w*|профессиональн\w*)\s+фото|фото\s+для\s+(?:работ|резюме)"):
        targets.extend(("whole_image", "clothing"))
        changes.append("Create a restrained professional business portrait.")
        outfit = OutfitIntent("replace", "realistic modern business outfit")
        camera = CameraIntent("professional portrait")

    identity_rule = "Preserve the same recognizable person, facial geometry, age and ethnicity."
    skin_rule = "Preserve natural skin texture; do not beautify or over-retouch the face."
    if _has(
        text,
        r"(?:лицо|внешност|черты)\s+не\s+мен",
        r"не\s+меняй\s+(?:лицо|внешност|черты)",
        r"сохрани\s+(?:лицо|внешност|человека)",
        r"оставь\s+человека\s+(?:таким|такой)\s+же",
        r"не\s+меняй\s+возраст",
    ):
        preserve.append(identity_rule)
        forbid.append("Do not redesign, replace or beautify the face.")
        negative.append("change identity or facial geometry")
    if _has(text, r"не\s+ретушируй\s+кож", r"сохрани\s+(?:естественн\w*\s+)?текстур\w*\s+кож"):
        preserve.append(skin_rule)
        forbid.append("Do not smooth or plasticize skin texture.")
        negative.append("over-retouch skin")

    only_clothing = _has(text, r"(?:поменяй|измени|смени)\s+только\s+одежд", r"ничего\s+кроме\s+одежд\w*\s+не\s+мен")
    only_background = _has(text, r"(?:поменяй|измени|замени)\s+только\s+(?:фон|задн)")
    keep_everything_else = _has(text, r"(?:оставь|сохрани)\s+все\s+остальн", r"остальн\w*\s+не\s+мен", r"ничего\s+больше\s+не\s+мен")
    if only_clothing:
        preserve.extend((identity_rule, skin_rule, "Preserve the pose, body proportions and current background."))
        forbid.extend(("Do not change the background.", "Do not change the pose or hairstyle."))
    if only_background:
        preserve.extend((identity_rule, skin_rule, "Preserve pose, clothing, hair and body proportions."))
        forbid.append("Do not alter the subject except for natural integration with the new background.")
    if keep_everything_else:
        continuity.append("Preserve every successful element not explicitly targeted by this correction.")

    if mode == "correction" and _has(text, r"ты\s+опять\s+поменял\s+фон", r"не\s+заменяй\s+скал\w*\s+на\s+друг"):
        preserve.append("Preserve the exact rocky setting from the parent version.")
        forbid.append("Do not regenerate or substitute the rocks with different rocks.")
        continuity.append("Treat the message as a complaint about the parent result, not a new scene request.")

    scenario = SCENARIO_RULES.get(scenario_id or "")
    if scenario:
        scenario_action, scenario_target, scenario_change = scenario
        if scenario_action == "replace_background" and explicit_preserve_background:
            negative.append("scenario background replacement")
        else:
            if scenario_target not in targets:
                targets.append(scenario_target)
            if scenario_change not in changes:
                changes.append(scenario_change)
            if scenario_action not in actions:
                actions.append(scenario_action)
            if scenario_id == "light-background":
                background = BackgroundIntent(
                    "replace", "clean neutral light studio", "sharp",
                    "light_photo_studio", "photorealistic", "forbidden",
                )
            elif scenario_id == "resume":
                camera = CameraIntent("professional portrait")
                outfit = OutfitIntent("replace", "restrained professional business outfit")
            elif scenario_id == "business-look":
                outfit = OutfitIntent("replace", "realistic modern business outfit")
            elif scenario_id == "cafe":
                background = BackgroundIntent(
                    "replace", "cozy modern cafe", "sharp",
                    "cafe", "photorealistic", "forbidden",
                )
            elif scenario_id == "beach":
                background = BackgroundIntent(
                    "replace", "bright realistic beach", "sharp",
                    "beach", "photorealistic", "forbidden",
                )
            elif scenario_id == "cat":
                objects_to_add.append("cat")
            elif scenario_id == "neuro-session":
                camera = CameraIntent("polished portrait")
            elif scenario_id == "documents":
                background = BackgroundIntent(
                    "replace", "plain neutral document background", "sharp",
                    "neutral_resume_background", "photorealistic", "forbidden",
                )
                camera = CameraIntent("document portrait")
            elif scenario_id == "memorial":
                camera = CameraIntent("respectful memorial portrait")

    if not changes and source:
        changes.append(
            "Carry out the user's requested edit visibly and faithfully while preserving "
            "unrelated details."
        )

    target_set = set(targets)
    if "face" not in target_set:
        preserve.extend((identity_rule, skin_rule))
    if "hair" not in target_set:
        preserve.append("Preserve the hairstyle, hair color and hairline.")
    if "body" not in target_set:
        preserve.append("Preserve natural body proportions and anatomy.")
    if "pose" not in target_set:
        preserve.append("Preserve the current pose and camera viewpoint.")
    if "clothing" not in target_set:
        preserve.append("Preserve the current clothing and accessories.")
    if "background" not in target_set:
        preserve.append("Preserve the current background, perspective and composition.")

    distinct_actions = _unique(actions)
    if not distinct_actions:
        primary: PrimaryAction = "custom"
    elif len(distinct_actions) == 1:
        primary = distinct_actions[0]  # type: ignore[assignment]
    elif "sharpen_background" in distinct_actions and mode == "correction":
        primary = "sharpen_background"
    else:
        primary = "custom"

    recognized = len(_unique(targets)) + len(_unique(preserve)) + len(_unique(forbid))
    confidence = 0.45 if recognized == 0 else min(0.98, 0.68 + recognized * 0.04)
    if ambiguities:
        confidence = min(confidence, 0.35)

    return EditPlan(
        mode=mode,
        primary_action=primary,
        target_regions=_unique(targets) or ("whole_image",),
        requested_changes=_unique(changes),
        preservation_rules=_unique(preserve),
        forbidden_changes=_unique(forbid),
        continuity_requirements=_unique(continuity),
        unresolved_ambiguities=_unique(ambiguities),
        source_user_text=source,
        inherited_user_text=(),
        inherited_constraints=(),
        correction_target_version_id=correction_target_version_id,
        confidence=confidence,
        scene=SceneIntent(
            background=background,
            lighting=lighting,
            camera=camera,
            outfit=outfit,
            pose=pose,
            objects=ObjectIntent(_unique(objects_to_add), _unique(objects_to_remove)),
            negative=_unique(negative),
        ),
    )


def merge_edit_plans(parent: EditPlan, correction: EditPlan) -> EditPlan:
    """Merge a correction with parent intent while treating the parent image as truth."""

    if correction.mode != "correction":
        raise ValueError("Only correction EditPlans can be merged")
    inherited_text = _unique(
        (*parent.inherited_user_text, parent.source_user_text, *correction.inherited_user_text)
    )
    override_fields: set[str] = set()
    if correction.scene.outfit.operation != "unchanged":
        override_fields.add("clothing")
    if correction.scene.background.operation in {"replace", "restore_previous"}:
        override_fields.add("background")
    if correction.scene.pose.operation != "unchanged":
        override_fields.add("pose")
    if correction.scene.lighting.style is not None:
        override_fields.add("lighting")
    if correction.scene.camera.framing is not None:
        override_fields.add("camera")
    if "hair" in correction.target_regions:
        override_fields.add("hair")

    field_terms = {
        "clothing": ("clothing", "outfit", "wardrobe", "accessor", "jacket"),
        "background": (
            "background", "scene", "setting", "rock", "mountain", "landmark"
        ),
        "pose": ("pose",),
        "lighting": ("lighting", "light", "sunset"),
        "camera": ("camera", "framing", "viewpoint"),
        "hair": ("hair", "hairstyle", "hairline"),
    }

    def superseded(value: str) -> bool:
        normalized = value.lower()
        return any(
            term in normalized
            for field in override_fields
            for term in field_terms[field]
        )

    parent_preservation = tuple(
        value for value in parent.preservation_rules if not superseded(value)
    )
    parent_forbidden = tuple(
        value for value in parent.forbidden_changes if not superseded(value)
    )
    inherited_constraints = _unique(
        value
        for value in (*parent.inherited_constraints, *parent.requested_changes)
        if not superseded(value)
    )
    continuity = list(parent.continuity_requirements)
    continuity.extend(correction.continuity_requirements)
    continuity.append("Preserve every successful edit already visible in the parent version unless explicitly changed now.")

    if "background" in correction.target_regions and (
        correction.primary_action in {"sharpen_background", "preserve_background"}
        or any("background" in rule.lower() for rule in correction.forbidden_changes)
    ):
        continuity.append("Keep the parent version's exact background setting, composition and recognizable landmarks.")

    def choose(current: Any, update: Any, empty: Any) -> Any:
        return current if update == empty else update

    parent_scene = parent.scene
    correction_scene = correction.scene
    correction_background = correction_scene.background
    if correction_background != BackgroundIntent() and correction_background.setting is None:
        correction_background = replace(
            correction_background, setting=parent_scene.background.setting
        )
    merged_scene = SceneIntent(
        identity=correction_scene.identity or parent_scene.identity,
        face=correction_scene.face or parent_scene.face,
        skin=correction_scene.skin or parent_scene.skin,
        background=choose(
            parent_scene.background, correction_background, BackgroundIntent()
        ),
        lighting=choose(
            parent_scene.lighting, correction_scene.lighting, LightingIntent()
        ),
        camera=choose(parent_scene.camera, correction_scene.camera, CameraIntent()),
        outfit=choose(parent_scene.outfit, correction_scene.outfit, OutfitIntent()),
        pose=choose(parent_scene.pose, correction_scene.pose, PoseIntent()),
        objects=ObjectIntent(
            add=_unique((*parent_scene.objects.add, *correction_scene.objects.add)),
            remove=_unique((*parent_scene.objects.remove, *correction_scene.objects.remove)),
        ),
        negative=_unique(
            value
            for value in (*parent_scene.negative, *correction_scene.negative)
            if value in correction_scene.negative or not superseded(value)
        ),
    )

    return replace(
        correction,
        preservation_rules=_unique((*parent_preservation, *correction.preservation_rules)),
        forbidden_changes=_unique((*parent_forbidden, *correction.forbidden_changes)),
        continuity_requirements=_unique(continuity),
        inherited_user_text=inherited_text,
        inherited_constraints=inherited_constraints,
        confidence=min(parent.confidence, correction.confidence),
        scene=merged_scene,
    )


def repeat_edit_plan(parent: EditPlan) -> EditPlan:
    """Reuse the exact effective intent for an alternative generation."""

    return replace(
        parent,
        mode="repeat",
        continuity_requirements=_unique((
            *parent.continuity_requirements,
            "Create an alternative rendering of the same effective intent without changing its constraints.",
        )),
        unresolved_ambiguities=(),
        correction_target_version_id=None,
    )
