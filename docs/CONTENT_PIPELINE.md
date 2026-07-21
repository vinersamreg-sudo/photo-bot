# Content Studio pipeline

## 1. Prepare an honest case

Use only an image created for Pixora with verified commercial rights. Prepare its
finished after-image outside Content Studio. No customer story, order or testimonial
may be invented.

```powershell
scripts\pixora.ps1 content generate `
  --source-image C:\safe\before.png `
  --after-image C:\safe\after.png `
  --title "Замена фона" `
  --category replace_background `
  --transformation replace_background `
  --prompt-en "Replace the background while preserving the subject." `
  --provider manual
```

Linux production operator command:

```bash
/opt/photo-bot/scripts/pixora content generate \
  --source-image /safe/before.png \
  --after-image /safe/after.png \
  --title 'Замена фона' \
  --category replace_background \
  --transformation replace_background \
  --prompt-en 'Replace the background while preserving the subject.'
```

The command validates and copies both images, creates square/vertical/stories
cards and a Russian template post. It performs zero AI calls and zero publications.

## 2. Review queue

```bash
pixora content status
pixora content queue --status needs_review
pixora content publish --post-id POST_ID --mode preview
pixora content publish --post-id POST_ID --mode dry_run
```

Preview and dry-run create only local audit entries. Review the original, result,
all cards, disclosure, CTA, spelling and every semantic quality item. If an anatomy,
face, background, segmentation or general artifact exists, generate a corrected
DemoResult instead of approving the bad one.

## 3. Approval and schedule

All state-changing commands are dry-run first:

```bash
pixora content approve --post-id POST_ID --reviewer owner --reason 'visual QA passed'
pixora content approve --post-id POST_ID --reviewer owner --reason 'visual QA passed' --apply

pixora content schedule --post-id POST_ID --at 2026-07-22T10:00:00+04:00
pixora content schedule --post-id POST_ID --at 2026-07-22T10:00:00+04:00 --apply
```

Generate the weekly plan without assigning posts:

```bash
pixora content schedule --plan-start 2026-07-20 --days 7
pixora content schedule --plan-start 2026-07-20 --days 7 --apply
```

## 4. Publication boundary

Current allowed modes are `preview` and `dry_run`. `manual_publish` records an
already completed operator action and requires `--apply` plus an external ID.
`publish` and `retry` are implemented at the adapter boundary but fail closed in
production because the flag is false and no transport is configured.

No command in this sprint performs a real MAX publication.

## 5. Analytics

Metrics are imported only after a post is confirmed published:

```bash
pixora content analytics --post-id POST_ID
pixora content analytics --post-id POST_ID --record --apply \
  --views 100 --clicks 8 --reactions 5 --comments 1 --conversion-to-bot 2
```

CTR is calculated as clicks/views. UTM dimensions are stored with every snapshot.

## Failure handling

- invalid file/licence/prompt: nothing is inserted;
- bundle DB failure: generated result/card files are removed;
- quality issue: post stays `needs_review` and approval fails;
- invalid lifecycle transition: no mutation;
- disabled publisher: no network request or published status; the failed attempt is
  recorded with only a safe error type;
- duplicate source checksum: second asset is rejected.

Before importing the first real content case, extend and restore-test the encrypted
off-site backup for `data/content_studio` (SQLite plus image library). Until then,
keep the production database empty and use only tests/dry-run.
