# Pixora demonstration content policy

## Allowed sources

- created specifically for Pixora;
- documented commercial permission;
- adult subjects only unless a later reviewed policy explicitly permits otherwise;
- no customer upload, Gallery result or private production path is imported;
- checksum and licence state are persisted before a case is generated.

`pending`, rejected or non-commercial assets cannot enter the pipeline.

## Required honesty

Every post and card is visibly marked as a demonstration. Do not claim a fictional
customer, order, testimonial, turnaround, popularity or business result. Do not
remove the disclosure during platform adaptation.

## Quality blockers

Do not approve content with six fingers, broken hands, bad eyes, double teeth,
smeared skin, unnatural background, segmentation errors or other visible artifacts.
Local checks also block invalid, undersized, near-uniform and unchanged results.

Pillow cannot reliably recognize anatomy. Therefore every post starts in
`needs_review`; semantic flags may come from a reviewer or future audited vision
inspector. Lack of an automatic flag is never approval.

## Lifecycle and publication

`draft → needs_review → approved → scheduled → published → archived` is enforced by
the repository. V1-generated posts enter at `needs_review`. Approval and scheduling
require explicit `--apply`. Automatic publication is disabled in production.

## Data and retention

The dedicated storage contains only demonstration assets/results/cards and is not
web-public. Operator CLI must not print production user IDs, secrets or unrelated
private paths. An archived case remains retained until a separately reviewed purge
policy is implemented; v1 does not silently delete source/licence evidence.

## Platforms

The disclosure, review state and source rights are core rules. A future Telegram,
VK, Instagram or other adapter may change payload formatting, never those rules.
