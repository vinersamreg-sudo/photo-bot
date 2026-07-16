"""Build provider-ready technical prompts from structured EditPlans."""

from __future__ import annotations

import re
from typing import Iterable

from app.edit_intent import EditPlan


PROMPT_BUILDER_VERSION = "technical-en-v1"


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _safe_user_text(text: str) -> str:
    """Remove likely credentials and machine-local identifiers from provider text."""

    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[redacted credential]", text)
    value = re.sub(r"\b(?:[A-Za-z]:\\|/opt/|/home/|/Users/)[^\s]+", "[redacted path]", value)
    value = re.sub(r"\b[0-9a-f]{24,}\b", "[redacted identifier]", value, flags=re.IGNORECASE)
    value = re.sub(r"\b[A-Za-z0-9_-]{48,}\b", "[redacted token]", value)
    return value.strip()


def contextual_preservation_rules(plan: EditPlan) -> tuple[str, ...]:
    """Apply conservative defaults, allowing only explicitly targeted regions to change."""

    targets = set(plan.target_regions)
    rules: list[str] = list(plan.preservation_rules)
    if "face" not in targets:
        rules.extend((
            "Preserve the same recognizable identity and facial geometry.",
            "Preserve eyes, nose, mouth, age, ethnicity, hairline and natural skin texture.",
        ))
    if "hair" not in targets:
        rules.append("Preserve the hairstyle, hair color and hair identity.")
    if "pose" not in targets:
        rules.append("Preserve the pose and camera viewpoint.")
    if "clothing" not in targets:
        rules.append("Preserve the current clothing and accessories.")
    if "body" not in targets:
        rules.append("Preserve natural body proportions, anatomy, limb count and finger count.")
    if "background" not in targets:
        rules.append("Preserve the current background, composition and perspective.")
    return _unique(rules)


def contextual_forbidden_rules(plan: EditPlan) -> tuple[str, ...]:
    rules: list[str] = list(plan.forbidden_changes)
    rules.extend((
        "Do not add text, logos, watermarks or unrelated objects.",
        "Do not create anatomy defects, duplicated limbs or malformed fingers.",
        "Do not over-smooth skin or make the image look synthetic.",
    ))
    return _unique(rules)


def build_provider_prompt(plan: EditPlan) -> str:
    """Render an EditPlan as explicit English instructions for an image-edit model."""

    changes = list(plan.requested_changes)
    if plan.mode in {"initial_edit", "scenario", "repeat"}:
        changes = [*plan.inherited_constraints, *changes]
    preserve = contextual_preservation_rules(plan)
    forbidden = contextual_forbidden_rules(plan)
    continuity = list(plan.continuity_requirements)
    if plan.mode == "correction":
        continuity.extend(
            f"Already established in the parent result; preserve rather than recreate: {value}"
            for value in plan.inherited_constraints
        )

    sections: list[str] = [
        "Edit the provided image as a realistic photograph.",
        "",
        "MAIN EDIT INSTRUCTION",
        f"- Mode: {plan.mode}.",
        f"- Primary action: {plan.primary_action}.",
    ]
    sections.extend(f"- {value}" for value in _unique(changes))

    sections.extend(("", "PRESERVE"))
    sections.extend(f"- {value}" for value in preserve)

    sections.extend(("", "DO NOT CHANGE"))
    sections.extend(f"- {value}" for value in forbidden)

    if continuity:
        sections.extend(("", "CONTINUITY FROM THE PARENT VERSION"))
        sections.extend(f"- {value}" for value in _unique(continuity))

    if "background" in plan.target_regions:
        sections.extend((
            "",
            "BACKGROUND REQUIREMENTS",
            "- Keep edges around the subject natural and integrate light, scale and perspective consistently.",
            "- Render background materials with physically plausible texture and local detail.",
        ))
        if plan.primary_action == "sharpen_background" or any(
            "blur" in value.lower() for value in forbidden
        ):
            sections.append("- Use a deep depth of field: the background must be sharp, not blurred or replaced.")

    sections.extend((
        "",
        "SUBJECT AND REALISM REQUIREMENTS",
        "- Keep the result photorealistic with coherent lighting, shadows, perspective and color response.",
        "- Blend edited regions naturally; avoid halos, cutout edges and plastic textures.",
        "",
        "QUALITY REQUIREMENTS",
        "- Produce clean high-detail photographic texture without artificial sharpening artifacts.",
    ))

    safe_source = _safe_user_text(plan.source_user_text)
    if plan.primary_action == "custom" and safe_source:
        sections.extend(("", "SANITIZED USER REQUEST", f"- {safe_source}"))

    return "\n".join(sections).strip()


def safe_prompt_inspection(plan: EditPlan, provider_prompt: str) -> dict[str, object]:
    """Return an admin-safe prompt view without platform identity, paths or credentials."""

    return {
        "mode": plan.mode,
        "primary_action": plan.primary_action,
        "target_regions": list(plan.target_regions),
        "parser_version": plan.parser_version,
        "prompt_builder_version": PROMPT_BUILDER_VERSION,
        "confidence": plan.confidence,
        "technical_prompt": _safe_user_text(provider_prompt),
    }
