# Direct Provider Mode and Subject Preservation Guard

## Why this experiment exists

Ravuna previously converted the user's text into a structured edit intent and an
expanded English technical prompt. The experiment tests the hypothesis that this
layer can conflict with the original intent or make the requested change less
visible.

Direct Provider Mode keeps the exact Unicode string received from the product flow
as the first provider-prompt segment. It performs no translation, artistic
interpretation, prompt expansion, system-instruction injection, `edit_intent`,
`prompt_builder` or LLM analysis. A short deterministic subject-preservation guard
is appended after the unchanged text so Gemini does not regenerate people when the
request only targets clothing, background or another local attribute.

The guard protects identities and recognizable faces, expressions, poses, body
positions, proportions, viewpoint and composition. Each protection is removed
independently when the user explicitly asks to change that attribute. Requests for
glasses, hairstyle or makeup therefore keep identity, pose and composition
protection. The final sentence always limits the edit to what the user directly
requested.

The same passthrough rule applies to initial requests, corrections, scenarios and
repeats: the current user text is the provider text. Scenario templates and parent
prompts are not substituted in direct mode.

## Enable or disable

The feature flag is:

```text
IMAGE_DIRECT_PROMPT_ENABLED=true
IMAGE_SUBJECT_PRESERVE_GUARD_ENABLED=true
IMAGE_FACE_PRESERVE_GUARD_ENABLED=false
```

`true` is the repository default. Set `IMAGE_DIRECT_PROMPT_ENABLED=false` to
restore the preserved `edit_intent` plus English technical prompt layer. The old
`OPENAI_DIRECT_PROMPT_ENABLED` environment name remains a compatibility fallback
when the provider-neutral flag is absent. A production change or deploy still
requires separate approval and an exact pre-test snapshot/restoration plan.

Set `IMAGE_SUBJECT_PRESERVE_GUARD_ENABLED=false` for exact prompt-only rollback.
When the new flag is absent, the deprecated `IMAGE_FACE_PRESERVE_GUARD_ENABLED`
value is accepted as a compatibility fallback.

Attempts are labeled with `prompt_builder_version=direct-unicode-v4` in direct mode
and the current technical builder version in the baseline mode.

Provider selection is independent:

```text
IMAGE_PROVIDER=gemini
OPENAI_IMAGE_MODEL=gpt-image-2
GEMINI_IMAGE_MODEL=gemini-3.1-flash-image
NANOBANANA_IMAGE_MODEL=gemini-3-pro-image
```

The repository default is Gemini Nano Banana 2 (`gemini-3.1-flash-image`). Set
`IMAGE_PROVIDER=openai` for the preserved OpenAI path, or
`IMAGE_PROVIDER=nanobanana` for the preserved Gemini 3 Pro Image alias. These are
configuration choices, not an automatic second request after a provider failure.

## A/B comparison

Run A with the flag `false` and B with the flag `true`. Keep provider, model, size
and source image fixed. Use the same synthetic
adult source images and the same requests in both modes:

- "сделай меня красивее";
- "поменяй одежду";
- "замени фон";
- "убери человека";
- "улучши старое фото";
- "сделай меня как персонажа".

For each paired result record:

- whether the model completed the requested edit;
- how visible the requested change is;
- whether faces remain recognizable;
- number of retries;
- number of refusals;
- subjective satisfaction using the same rating scale.

Do not conclude that either mode is better without side-by-side visual comparison
of matched A/B results. Real provider image requests are a separate T4 action and
must not be inferred from this document or from enabling a local feature flag.
