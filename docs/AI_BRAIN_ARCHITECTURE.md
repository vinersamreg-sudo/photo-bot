# AI Brain architecture

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

## EditPlan v1

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

The exact user phrase is persisted in `prompt`/`correction_prompt` and `edit_plan_json`. It is not written to aggregate telemetry. `provider_prompt` is a separate column and never replaces the original phrase.

## Deterministic parsing

The v1 parser uses normalized Russian stems, phrase rules, synonyms and explicit negation handling. Negative instructions are evaluated before positive keyword effects. Supported high-value categories include background replacement/preservation/sharpness, clothing, pose, hair, identity/skin preservation, restoration, quality, add/remove object and correction complaints.

No paid LLM parser is enabled. A future parser must be feature-flagged, must not receive a photo by default and must be evaluated against the deterministic regression corpus before activation.

## Merge and lineage

- Initial/scenario edit uses the immutable Gallery item source.
- Correction requires a selected successful parent and uses that parent's private original as image input.
- A second correction uses the immediately selected parent original, so visible successful changes are present in the next request.
- Repeat keeps the parent's effective EditPlan and uses the same image input that produced the parent, creating an alternative branch without compounding image drift.
- `parent_version_id` describes history; `source_version_id` separately records which version supplied the actual image bytes.
- A missing/expired parent original fails before provider work and before an attempt is inserted.

For corrections, prior changes move into inherited constraints and continuity. The prompt tells the provider to preserve what is already visible instead of recreating every previous edit.

## Prompt builder

`app/prompt_builder.py` renders English technical sections: main changes, preserve, do-not-change, continuity, background/subject rules, realism and quality. Contextual defaults protect identity, face, skin, hair, pose, clothing, anatomy and background unless the corresponding region is explicitly targeted.

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

Rule-based intent parsing does not understand every Russian formulation. Image models can still change identity, composition or materials despite instructions, and sharpness cannot be guaranteed if the parent pixels lack detail. Complex prompts can take up to about two minutes according to OpenAI. The system asks one clarification only for a detected high-confidence contradiction such as simultaneous replace/preserve background.
