"""Build provider-ready technical prompts from structured EditPlans."""

from __future__ import annotations

import re
from typing import Iterable, Optional

from app.edit_intent import EditPlan
from app.processing_modes import ProcessingMode, ProcessingPlan


PROMPT_BUILDER_VERSION = "technical-en-v5-target-completion"


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
    """Protect identity and non-target details without suppressing the requested edit."""

    targets = set(plan.target_regions)
    rules: list[str] = list(plan.preservation_rules)
    if "face" not in targets:
        rules.extend((
            "Preserve the same recognizable identity and facial geometry.",
            "Preserve eyes, nose, mouth, age, perceived gender, ethnicity, hairline and natural skin texture.",
        ))
    else:
        rules.append(
            "Preserve the same recognizable identity while changing only the explicitly requested facial attribute."
        )
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


def _structured_change_lines(
    plan: EditPlan, processing_plan: Optional[ProcessingPlan] = None
) -> tuple[str, ...]:
    """Translate provider-neutral scene fields into English model instructions."""

    scene = plan.scene
    lines: list[str] = list(plan.requested_changes)
    targets = set(plan.target_regions)
    render_all = plan.mode in {"initial_edit", "scenario", "repeat"}
    background = scene.background
    render_background = render_all or "background" in targets
    if render_background and background.operation == "replace":
        lines.append(f"Replace the background with {background.setting or 'the requested setting'}.")
    elif render_background and background.operation == "preserve":
        lines.append("Preserve the exact current background.")
    elif render_background and background.operation == "restore_previous":
        lines.append("Restore the background visible in the previous successful version.")
    elif render_background and background.operation == "sharpen":
        lines.append("Keep the current background and make it sharp, detailed and clearly readable.")
    elif render_background and background.operation == "blur":
        lines.append("Apply natural background blur while keeping the subject sharp.")
    elif render_background and background.operation == "enhance":
        lines.append(
            "Visibly upgrade the existing background design and finish while keeping "
            "the same broad location, camera perspective and subject placement. "
            "Improve decor, surfaces, visual clutter and lighting; the result must "
            "not be limited to brightness, contrast or color correction."
        )
    if (
        render_background
        and background.sharpness == "sharp"
        and background.operation != "sharpen"
    ):
        lines.append("Render the background sharp and detailed without shallow depth of field.")

    if scene.lighting.style and (render_all or "whole_image" in targets):
        lines.append(f"Use {scene.lighting.style}.")
    if scene.camera.framing and (render_all or "whole_image" in targets):
        lines.append(f"Use {scene.camera.framing} framing.")
    if scene.outfit.operation == "replace" and (render_all or "clothing" in targets):
        if any("every visible person" in value.lower() for value in lines):
            lines.append(
                "Ensure every visible person's complete visible outfit is replaced."
            )
        else:
            lines.append(
                f"Replace the outfit with {scene.outfit.style or 'the requested clothing'}."
            )
    elif scene.outfit.operation == "recolor" and (render_all or "clothing" in targets):
        lines.append(
            f"Change only the clothing color to {scene.outfit.color or 'the requested color'}."
        )
    if scene.pose.operation == "change" and (render_all or "pose" in targets):
        lines.append(f"Change the pose to {scene.pose.description or 'a natural requested pose'}.")
    if render_all or "object" in targets:
        lines.extend(f"Add a realistic {value}." for value in scene.objects.add)
        lines.extend(
            f"Remove the {value} and reconstruct the occluded area naturally."
            for value in scene.objects.remove
        )

    if plan.primary_action == "restore_photo":
        lines.append("Restore damage, scratches, fading and lost detail without inventing a different photograph.")
    elif plan.primary_action == "improve_quality":
        lines.append("Improve natural sharpness, lighting and detail without redesigning the photograph.")
    elif plan.primary_action == "custom" and not lines:
        lines.append(
            "Carry out the expanded requested changes visibly and faithfully."
        )
    if processing_plan is not None:
        if processing_plan.selected_mode == ProcessingMode.AI_GENERATION:
            lines.extend((
                "Create the requested scene photorealistically.",
                "Avoid CGI materials, repeated synthetic textures and impossible terrain patterns.",
            ))
        elif processing_plan.selected_mode == ProcessingMode.REAL_BACKGROUND_COMPOSITE:
            lines.extend((
                "Do not generate or replace the supplied licensed background.",
                "Limit any finishing to edge, shadow, light and color integration.",
            ))
        elif processing_plan.selected_mode == ProcessingMode.LOCAL_AI_EDIT:
            lines.append(
                "Edit every explicitly targeted region sufficiently for the requested "
                "change to be clearly visible; leave unrelated regions unchanged."
            )
        elif processing_plan.selected_mode == ProcessingMode.ENHANCEMENT:
            lines.extend((
                "Enhance only existing pixels and detail.",
                "Do not add objects, redesign the composition, change the face or redraw the background.",
            ))
        elif processing_plan.selected_mode == ProcessingMode.RESTORATION:
            lines.extend((
                "Preserve historical authenticity, age and original facial features.",
                "Do not modernize makeup, clothing or photographic style.",
            ))
    return _unique(lines)


def _negative_rule(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"replace background", "replacement background"}:
        return "Do not replace or regenerate the background."
    if normalized == "background blur":
        return "Do not blur, defocus or add bokeh to the background."
    if normalized == "change identity or facial geometry":
        return "Do not change identity or facial geometry."
    if normalized == "over-retouch skin":
        return "Do not over-retouch or plasticize skin."
    return f"Do not {value.rstrip('.')}."


def build_provider_prompt(
    plan: EditPlan, processing_plan: Optional[ProcessingPlan] = None
) -> str:
    """Render an EditPlan as explicit English instructions for an image-edit model."""

    changes = _structured_change_lines(plan, processing_plan)
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
        "PRIORITY ORDER",
        "- First, fully perform every explicit requested change. The requested edit "
        "must be clearly visible, not reduced to a minor color, brightness or contrast adjustment.",
        "- Second, preserve each person's recognizable identity and facial geometry "
        "unless the user explicitly requests a facial attribute change.",
        "- Third, preserve photographic realism, natural anatomy and coherent lighting.",
        "- Preservation rules apply only outside the requested target regions and must "
        "never cancel or weaken an explicit requested change.",
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
    sections.extend(f"- {_negative_rule(value)}" for value in plan.scene.negative)

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

    clothing_changes = " ".join(changes).lower()
    if (
        "clothing" in plan.target_regions
        and "every visible person" in clothing_changes
    ):
        sections.extend((
            "",
            "GROUP WARDROBE COVERAGE",
            "- Identify every visible person whose clothing can be seen, including "
            "smaller and partially occluded group members.",
            "- Apply a complete, visibly different outfit to each identified person independently.",
            "- Do not stop after changing only one person, and do not leave any "
            "identified person's original outfit unchanged.",
            "- Keep each person's face, head, hairstyle, expression, age and identity unchanged; "
            "confine wardrobe edits below the face and hair boundary.",
        ))

    if (
        "background" in plan.target_regions
        and plan.scene.background.operation == "enhance"
    ):
        sections.extend((
            "",
            "BACKGROUND COMPLETION CHECK",
            "- The background must show a clearly noticeable environmental improvement "
            "in decor, surfaces or distracting details, not merely a brightness, "
            "contrast or color shift.",
            "- Preserve the broad location and camera perspective while allowing "
            "coherent, realistic improvements within the background.",
        ))

    sections.extend((
        "",
        "SUBJECT AND REALISM REQUIREMENTS",
        "- Treat each visible face as an independent protected identity reference; "
        "preserve every person, not only the largest or most prominent face.",
        "- Keep the result photorealistic with coherent lighting, shadows, perspective and color response.",
        "- Blend edited regions naturally; avoid halos, cutout edges and plastic textures.",
        "",
        "QUALITY REQUIREMENTS",
        "- Produce clean high-detail photographic texture without artificial sharpening artifacts.",
    ))

    prompt = "\n".join(sections).strip()
    if not prompt.isascii():
        raise ValueError("Provider prompt must contain English ASCII instructions only")
    return prompt


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
