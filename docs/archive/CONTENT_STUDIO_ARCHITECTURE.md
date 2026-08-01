# Ravuna Content Studio v1 — architecture

## Purpose and boundary

Content Studio turns Ravuna-owned demonstration before/after cases into reviewed,
scheduled content. It is an operator subsystem, not part of the MAX user dialog.
It imports finished before/after images, renders branded cards with Pillow, builds
Russian copy from deterministic templates and prepares a platform payload.

It does not call OpenAI, does not transform user photos, does not read or write
Gallery/payment tables and does not publish automatically. Its SQLite database is
`data/content_studio/content_studio.sqlite3`; files live under
`data/content_studio/storage`. Main product migrations remain unchanged.

```text
Ravuna-owned source
  → DemoAsset + verified rights
  → DemoTransformation (English technical instruction)
  → DemoResult + quality flags
  → Pillow cards: square / vertical / stories
  → deterministic Russian DemoPost
  → needs_review → approved → scheduled
  → platform adapter preview/dry-run
  → explicit future publish/manual confirmation
  → analytics snapshots
```

## Modules

- `models.py` — enums and immutable domain records;
- `repository.py` — dedicated SQLite schema, transactions and lifecycle guards;
- `storage.py` — validated JPEG/PNG/WEBP import, checksum and root confinement;
- `content_generator.py` — English internal instruction and factual Russian templates;
- `before_after_renderer.py` — three branded Pillow layouts;
- `quality.py` — objective image checks plus pluggable semantic issue flags;
- `planner.py` — deterministic weekly plan;
- `publisher.py` — platform-neutral protocol and fail-closed MAX adapter;
- `service.py` — atomic orchestration;
- `cli.py` — `ravuna content ...` operator surface.

## Database schema v2

| Table | Purpose |
|---|---|
| `demo_assets` | Ravuna-owned source, licence state, checksum, tags/category |
| `demo_transformations` | transformation kind, English prompt, SceneIntent/EditPlan |
| `demo_results` | before/after/card/thumbnail, provider metadata, quality issues |
| `demo_posts` | Russian copy, CTA/disclosure, UTM and lifecycle |
| `content_review_actions` | reviewer/action/reason audit |
| `content_plan_entries` | reusable platform-neutral editorial calendar |
| `content_publication_attempts` | preview/dry-run/manual/publish/retry audit |
| `content_analytics` | views, clicks, reactions, comments, CTR and bot conversion |

Transformation + result + post are inserted in one SQLite transaction. Files are
copied atomically and removed if persistence fails. The source asset is imported
separately so it can support multiple transformations. Approval atomically updates
both `DemoResult` and `DemoPost` and writes the reviewer audit row. Schema v2 adds
the English generator-contract metadata with a non-destructive migration from the
pre-release schema.

## Publication adapter

`PublisherAdapter` defines `preview`, `dry_run`, `manual_publish`, `publish` and
`retry`. The core knows only a platform name and payload. `MaxPublisher` is the
only v1 implementation. Network methods require both
`CONTENT_STUDIO_PUBLISHING_ENABLED=true` and an injected transport; normal deploy
forces the flag to `false` and provides no transport. Telegram/VK/Instagram-style
adapters can be added without changing assets, posts, review or analytics.

## Quality contract

Every generated post starts in `needs_review`. Invalid/small/uniform/unchanged
images are detected locally. Semantic defects such as fingers, eyes, teeth, hands,
skin, background and segmentation enter through typed detector/reviewer flags and
block approval. V1 does not pretend Pillow can recognize anatomy: a future vision
inspector can implement that input without changing the lifecycle.

## Operational safety

- source must use the immutable persisted value `created_for_pixora`, licence `verified`, commercial use allowed;
- source and result never escape the private storage root;
- no user/MAX identifiers, prompts or Gallery data are copied;
- approval/scheduling changes are dry-run unless `--apply` is explicit;
- every post contains both mandatory demo disclosure sentences;
- real publication and OpenAI requests are zero in v1 rollout;
- failed future network publication attempts are recorded without storing raw
  exception text or credentials;
- backend deploy initializes/quick-checks this separate DB and asserts zero
  published posts while publication remains disabled.

The existing encrypted backup workflow currently covers the main product DB, not
this independent content DB or its image library. Because this rollout contains no
real assets or posts, that is not a data-loss exposure today. A restore-tested,
off-site backup for both Content Studio SQLite and storage is a mandatory gate
before the first real demonstration asset is imported.
