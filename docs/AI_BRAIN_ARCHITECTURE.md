# AI Brain architecture

> Pixora v1 production uses OpenAI `gpt-image-2` only. Provider-neutral
> structures support deterministic interpretation and lineage; they do not
> authorize a second provider or semantic parser before pilot evidence.

## Scope

AI Brain is a deterministic interpretation layer between MAX/user input and the existing image provider. It is not an autonomous agent, does not call a second model, does not inspect a photo through an extra vision service and does not change quota, payment, storage, watermark, retention, owner-only or delivery rules.

```text
Russian user text
  -> edit_intent.parse_edit_intent()
  -> typed EditPlan
  -> merge_edit_plans(parent, correction) / repeat_edit_plan(parent)
  -> prompt_builder.build_provider_prompt()
  -> configured ImageProvider.edit(source, technical_prompt)
  -> private original + watermarked preview + delivery
  -> generation_attempt + GalleryVersion in one final transaction
```

`GalleryVersion` is still created only after successful preview delivery.

## EditPlan v2

`app/edit_intent.py` owns the serializable `EditPlan` dataclass. It contains:

- mode and primary action;
- target regions and current requested changes;
- explicit/contextual preservation and forbidden changes;
- continuity requirements;
- unresolved contradictions;
- exact current user text and inherited user text;
- inherited structured constraints;
- correction target version;
- parser confidence/version and JSON schema version.

Schema v2 also carries a provider-neutral `scene` object. Identity, face and skin
policy, background operation/setting/sharpness, lighting, camera framing, outfit,
pose, added/removed objects and negative constraints are typed fields rather than
a concatenated prompt. Older v1 JSON remains readable and receives conservative
defaults; no SQLite schema change is required.

The exact user phrase is persisted in `prompt`/`correction_prompt` and `edit_plan_json`. It is not written to aggregate telemetry. `provider_prompt` is a separate column and never replaces the original phrase.

## Deterministic parsing

The v2 parser uses normalized Russian stems, phrase rules, synonyms and explicit negation handling. Negative instructions are evaluated before positive keyword effects. Supported high-value categories include background replacement/preservation/sharpness, lighting, camera framing, clothing, pose, hair, identity/skin preservation, restoration, quality, add/remove object and correction complaints.

No paid LLM parser is enabled. A future parser must be feature-flagged, must not receive a photo by default and must be evaluated against the deterministic regression corpus before activation.

## Merge and lineage

- Initial/scenario edit uses the immutable Gallery item source.
- Correction requires a selected successful parent and uses that parent's private original as image input.
- A second correction uses the immediately selected parent original, so visible successful changes are present in the next request.
- Repeat keeps the parent's effective EditPlan and uses the same image input that produced the parent, creating an alternative branch without compounding image drift.
- `parent_version_id` describes history; `source_version_id` separately records which version supplied the actual image bytes.
- A missing/expired parent original fails before provider work and before an attempt is inserted.

For corrections, only non-empty semantic fields replace parent fields. For example,
`lighting=sunset` changes lighting while retaining the structured mountain background
and hiking outfit. Prior text fields remain only for compatibility/audit; they are
not used as the provider instruction source.

## Prompt builder

`app/prompt_builder.py` renders English technical sections from `scene`: main changes, preserve, do-not-change, continuity, background/subject rules, realism and quality. Contextual defaults protect identity, face, skin, hair, pose, clothing, anatomy and background unless the corresponding region is explicitly targeted.

Raw Russian user text is never appended to a provider prompt, including `custom`
requests. The provider boundary rejects every non-ASCII prompt before an API call.
This makes OpenAI/Flux/Imagen adapters consumers of one normalized English contract.

The technical prompt redacts credential-like values, local paths and long internal identifiers. Normal users never receive it. `python -m app.main ai-inspect --attempt-id ...` provides an administrative, identity-free inspection view.

## Persistence migration

SQLite migration v4 adds compatible nullable fields to attempts and versions:

- `edit_plan_json`;
- `provider_prompt`;
- `source_version_id`;
- `prompt_builder_version`.

Legacy rows are backfilled with `legacy-backfill-v1` plans and keep their historical prompt behavior. No files are moved. `version_feedback` stores version/user foreign keys, sentiment and an optional technical reason category; it has no free-text user field.

## Provider quality

Provider parameters are configurable through `IMAGE_EDIT_QUALITY`, `IMAGE_EDIT_SIZE`, `IMAGE_EDIT_INPUT_FIDELITY`, `IMAGE_EDIT_OUTPUT_FORMAT` and `OPENAI_MAX_RETRIES`. Default demo quality is `medium`; fake tests make no external request. For `gpt-image-2`, input fidelity is always high and the parameter is deliberately omitted because the API rejects overrides.

The provider uses the SDK raw-response wrapper only to record `retries_taken` and then parses the normal typed response; response bodies and credentials are not logged.

The paid original is currently the already generated private original, not a second high-quality generation. A separate paid quality tier would require a new priced regeneration product and is not silently simulated.

## Known limitations

Rule-based intent parsing does not understand every Russian formulation. Unknown
custom phrasing therefore falls back to a conservative structured edit instead of
leaking Russian to the provider. Image models can still change identity, composition
or materials despite instructions, and sharpness cannot be guaranteed if the parent
pixels lack detail. Direct contradictions are resolved conservatively in favor of
an explicit negation; the main flow has no clarification screen.

## Hybrid execution layer (v3)

`EditPlan` no longer implies that every request is a generative edit. `ModeRouter`
converts it to a typed `ProcessingPlan` with one of five modes, asset/mask lineage
and preservation flags. `PromptBuilder v3` renders mode-specific English
instructions. Corrections inherit the parent asset and effective scene; Repeat
inherits the complete parent execution plan.

For a real photographic location the preferred mode is a local licensed-asset
composite. If the catalog or segmenter is unavailable, execution stops instead of
quietly generating a different background. Enhancement is local and conservative.
See `PROCESSING_MODES_ARCHITECTURE.md`.
