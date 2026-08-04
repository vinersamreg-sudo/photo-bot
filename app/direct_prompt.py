"""Minimal Unicode prompt policy for direct image-provider requests."""

from dataclasses import replace
from typing import Optional

from app.edit_intent import EditMode, EditPlan


DIRECT_PROMPT_VERSION = "direct-unicode-v3"


def build_direct_prompt(user_text: str) -> str:
    """Validate the request without changing the text sent to the provider."""

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
