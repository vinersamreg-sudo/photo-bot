"""Deterministic Russian edit-intent parsing and cumulative EditPlan merging."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
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

PARSER_VERSION = "rules-ru-v1"
EDIT_PLAN_SCHEMA_VERSION = 1


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

    explicit_replace_background = _has(
        text,
        r"(?:замени|поменяй|смени|измени)\s+(?:мне\s+)?(?:фон|задн\w*\s+план)",
        r"фон\s+(?:на|в)\s+скал",
        r"(?:сделай|добавь)\s+скал\w*\s+(?:на\s+)?фон",
        r"сделай\s+(?:мне\s+)?(?:фон|задн\w*\s+план)\s+(?:светл|бел|темн|студийн|скалист|горн)",
        r"^(?:светл|бел|темн|студийн|скалист|горн)\w*\s+фон$",
    )
    explicit_preserve_background = _has(
        text,
        r"не\s+(?:меняй|заменяй|трогай)\s+(?:эт\w*\s+|текущ\w*\s+)?(?:фон|скал|задн\w*\s+план)",
        r"(?:фон|скал\w*|задн\w*\s+план)\s+не\s+(?:меняй|заменяй|трогай)",
        r"(?:оставь|сохрани)\s+(?:эт\w*\s+же\s+|текущ\w*\s+|прошл\w*\s+)?(?:фон|скал|задн\w*\s+план)",
        r"не\s+надо\s+(?:менять|заменять)\s+(?:скал|фон)",
    )
    if "итоговое решение сохранить текущий фон" in text:
        explicit_replace_background = False
        explicit_preserve_background = True
    elif "итоговое решение заменить текущий фон" in text:
        explicit_replace_background = True
        explicit_preserve_background = False
    contradictory_background = explicit_replace_background and explicit_preserve_background
    if contradictory_background:
        ambiguities.append("replace_background_conflicts_with_preserve_background")
    elif explicit_replace_background:
        targets.append("background")
        actions.append("replace_background")
        if "скал" in text or "камен" in text or "гор" in text:
            changes.append("Replace the background with realistic rocky mountains and visible rock formations.")
        else:
            changes.append("Replace the background according to the user's requested setting.")

    if explicit_preserve_background:
        targets.append("background")
        preserve.append("Preserve the current background setting and its recognizable visual content.")
        forbid.append("Do not replace the current background with a different setting.")
        continuity.append("Continue from the exact background visible in the parent version.")
        actions.append("preserve_background")

    blur_complaint = _has(
        text,
        r"(?:фон|задн\w*\s+план|скал\w*)[^.]{0,25}(?:размыт|мыльн|нечетк)",
        r"(?:размыт|мыльн|нечетк)[^.]{0,25}(?:фон|задн\w*\s+план|скал)",
        r"все\s+равно\s+(?:размыт|мыльн|нечетк)",
        r"(?:четч|резч|детальн).{0,20}(?:фон|скал|задн)",
        r"(?:фон|скал|задн).{0,20}(?:четч|резч|детальн)",
        r"убери\s+размыт",
        r"не\s+размывай\s+(?:фон|скал|задн)",
    )
    request_more_blur = _has(text, r"(?:сильнее|больше)\s+размо", r"добавь\s+(?:боке|размыт)")
    if blur_complaint and request_more_blur:
        ambiguities.append("background_blur_direction_conflict")
    elif blur_complaint:
        targets.append("background")
        actions.append("sharpen_background")
        changes.append("Make the existing background sharp, detailed and clearly readable.")
        preserve.append("Preserve the current background scene, layout and recognizable rocks or landmarks.")
        forbid.extend((
            "Do not replace the current background.",
            "Do not apply background blur, bokeh, defocus or shallow depth of field.",
        ))
        continuity.append("Correct only the residual background blur in the parent version.")
    elif request_more_blur:
        targets.append("background")
        changes.append("Increase natural background blur while keeping the subject sharp.")

    if _has(text, r"верни\s+(?:прошл\w*|предыдущ\w*)\s+(?:фон|задн)", r"сделай\s+как\s+было"):
        targets.append("background")
        actions.append("preserve_background")
        changes.append("Restore the background appearance from the previous successful version.")
        continuity.append("Use the previous successful background while preserving later successful edits.")
        forbid.append("Do not invent a new replacement background.")

    if _has(text, r"(?:переодень|смени\s+(?:только\s+)?одежд|замени\s+(?:только\s+)?одежд|поменяй\s+(?:только\s+)?одежд|одежд\w*\s+для\s+хайкинг)"):
        targets.append("clothing")
        actions.append("change_clothes")
        if _has(text, r"хайкинг|поход|турист"):
            changes.append("Replace the clothing with realistic, practical hiking clothing.")
        else:
            changes.append("Change only the subject's clothing as requested.")

    if _has(text, r"(?:смени|измени|поменяй|скорректируй)\s+(?:немного\s+)?поз"):
        targets.append("pose")
        actions.append("change_pose")
        changes.append("Adjust the subject's pose naturally as requested.")

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

    if _has(text, r"(?:убери|удали)\s+(?:объект|предмет|человека|надпись)"):
        targets.append("object")
        actions.append("remove_object")
        changes.append("Remove only the requested object and reconstruct the occluded area naturally.")
    if _has(text, r"(?:добавь|дорисуй)\s+(?:объект|предмет|человека|животн|кот)"):
        targets.append("object")
        actions.append("add_object")
        changes.append("Add only the requested object with realistic scale, light and perspective.")

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
    if _has(text, r"не\s+ретушируй\s+кож", r"сохрани\s+(?:естественн\w*\s+)?текстур\w*\s+кож"):
        preserve.append(skin_rule)
        forbid.append("Do not smooth or plasticize skin texture.")

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
            ambiguities.append("scenario_background_change_conflicts_with_preserve_background")
        else:
            if scenario_target not in targets:
                targets.append(scenario_target)
            if scenario_change not in changes:
                changes.append(scenario_change)
            if scenario_action not in actions:
                actions.append(scenario_action)

    if not changes and source:
        changes.append("Apply the user's requested edit faithfully and conservatively.")

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
    )


def merge_edit_plans(parent: EditPlan, correction: EditPlan) -> EditPlan:
    """Merge a correction with parent intent while treating the parent image as truth."""

    if correction.mode != "correction":
        raise ValueError("Only correction EditPlans can be merged")
    inherited_text = _unique(
        (*parent.inherited_user_text, parent.source_user_text, *correction.inherited_user_text)
    )
    inherited_constraints = _unique(
        (*parent.inherited_constraints, *parent.requested_changes)
    )
    continuity = list(parent.continuity_requirements)
    continuity.extend(correction.continuity_requirements)
    continuity.append("Preserve every successful edit already visible in the parent version unless explicitly changed now.")

    if "background" in correction.target_regions and (
        correction.primary_action in {"sharpen_background", "preserve_background"}
        or any("background" in rule.lower() for rule in correction.forbidden_changes)
    ):
        continuity.append("Keep the parent version's exact background setting, composition and recognizable landmarks.")

    return replace(
        correction,
        preservation_rules=_unique((*parent.preservation_rules, *correction.preservation_rules)),
        forbidden_changes=_unique((*parent.forbidden_changes, *correction.forbidden_changes)),
        continuity_requirements=_unique(continuity),
        inherited_user_text=inherited_text,
        inherited_constraints=inherited_constraints,
        confidence=min(parent.confidence, correction.confidence),
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
