# Prompt engineering rules

## Principles

1. Preserve the user's exact text in persistence; never overwrite it with a translation.
2. Build provider instructions from `EditPlan`, not from string concatenation.
3. Treat explicit negation as stronger than a colliding positive keyword.
4. A correction describes the delta from the selected parent image.
5. Preserve successful parent edits unless the current correction targets them.
6. Never expose the technical prompt in ordinary MAX UX.
7. Never append raw Russian text to a provider prompt; provider prompts must be English ASCII.
8. Provider prompts are rendered from typed scene fields, not from historical text columns.

## Context defaults for people

Unless targeted, preserve recognizable identity, facial geometry, eyes, nose, mouth, age, ethnicity, natural skin texture, hairline/style, pose, clothing, body proportions, anatomy, viewpoint, background, perspective and lighting direction. A target grants only the narrow permission implied by it:

- `pose` allows pose changes, not face changes;
- `clothing` allows clothing changes, not background or anatomy changes;
- `background` allows background work, not subject redesign;
- `hair` allows hairstyle work while identity stays protected.

## Background corrections

Phrases such as «фон всё равно размыт», «убери размытие фона», «задний план чётче» and «не размывай фон» produce all three controls:

- requested change: increase local detail/sharpness;
- preserve: current setting, layout and recognizable landmarks;
- forbid: replacement, bokeh, defocus and shallow depth of field.

«Не меняй фон, только сделай его чётче» must never become a background replacement request.

## Continuity

The prompt distinguishes new changes from inherited constraints. For Correction, inherited changes are described as already established in the parent result. For Repeat, the complete effective intent is rendered because Repeat starts from the same input branch as its parent.

## Contradictions without an extra screen

Do not ask for confirmation or repeat the prompt. Direct contradictions are resolved
conservatively by explicit-negation precedence:

- replace + preserve background becomes preserve background;
- a complaint about blur wins over a colliding request to add blur.

Short dissatisfaction such as «не так» is not by itself treated as a new scene.

## Safety and observability

Technical prompts must contain no API keys, local paths or long internal IDs. Aggregate telemetry may contain intent category, mode, correction flag, duration, version count, provider status and positive/negative feedback, but not arbitrary user phrases or image bytes.

## Quality

`quality=low` is reserved for explicit draft/smoke use. Demo production defaults to `medium` pending a controlled owner-only comparison. Do not claim an exact cost from the configured ruble reserve: confirm pricing from current official documentation and actual billing/usage first.
