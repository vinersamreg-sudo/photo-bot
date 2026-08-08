# Direct Provider Mode and Exact Gemini Passthrough

## Why this experiment exists

Ravuna previously converted the user's text into a structured edit intent and an
expanded English technical prompt. The experiment tests the hypothesis that this
layer can conflict with the original intent or make the requested change less
visible.

Direct Provider Mode sends the exact Unicode string received from the product flow
to Gemini. It performs no translation, artistic interpretation, prompt expansion,
system-instruction injection, `edit_intent`, `prompt_builder`, LLM analysis or
preservation-instruction injection. One or two source images may accompany the
same unchanged text.

The deterministic subject-preservation guard remains in the repository for
compatibility with non-Gemini direct-provider paths, but `gemini` and its
`nanobanana` routing alias do not append it. Lineage and edit-plan metadata do not
modify the provider prompt.

The same passthrough rule applies to initial requests, corrections, scenarios and
repeats: the current user text is the provider text. Scenario templates and parent
prompts are not substituted in direct mode.

## Enable or disable

The feature flag is:

```text
IMAGE_DIRECT_PROMPT_ENABLED=true
IMAGE_SUBJECT_PRESERVE_GUARD_ENABLED=true
IMAGE_FACE_PRESERVE_GUARD_ENABLED=true
```

Direct prompting is the repository default. Set
`IMAGE_DIRECT_PROMPT_ENABLED=false` to
restore the preserved `edit_intent` plus English technical prompt layer. The old
`OPENAI_DIRECT_PROMPT_ENABLED` environment name remains a compatibility fallback
when the provider-neutral flag is absent. A production change or deploy still
requires separate approval and an exact pre-test snapshot/restoration plan.

The preservation flags apply only to non-Gemini direct-provider paths. They are
ignored for `gemini` and `nanobanana`, where exact passthrough is unconditional
while direct mode is enabled. When the subject flag is absent, the deprecated
`IMAGE_FACE_PRESERVE_GUARD_ENABLED` value remains a compatibility fallback.

Attempts are labeled with `prompt_builder_version=direct-unicode-v4` in direct mode
and the current technical builder version in the baseline mode.

Provider selection is independent:

```text
IMAGE_PROVIDER=gemini
OPENAI_IMAGE_MODEL=gpt-image-2
GEMINI_IMAGE_MODEL=gemini-3-pro-image
NANOBANANA_IMAGE_MODEL=gemini-3-pro-image
```

The repository default is Gemini Nano Banana Pro (`gemini-3-pro-image`). Set
`IMAGE_PROVIDER=openai` for the preserved OpenAI path, or use
`IMAGE_PROVIDER=nanobanana` for the preserved Gemini Pro routing alias. These are
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
