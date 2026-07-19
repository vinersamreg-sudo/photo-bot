# Privacy data flow

## Pixora-owned data

Pixora stores the uploaded source, private generated originals, watermarked
previews, GalleryItem/Version lineage, SceneIntent/EditPlan, legal consent, quota
and minimal operational telemetry under its documented retention rules.

## Optional OpenAI context

When all three disabled-by-default flags are explicitly enabled, the selected
parent private original and compact English technical prompt are sent to OpenAI
Responses with the image-generation tool. OpenAI may store the Response because
the chain requires `store=true`. Pixora stores only response/conversation/request
IDs, provider/model/mode, usage, duration, status, depth and error class.

Pixora does not store raw OpenAI Response payloads, data URLs, image bytes in
SQLite, API keys, signed URLs or free-form prompt text in provider-context
telemetry. IDs are not shown to users or public reports.

## Deletion

Gallery purge removes local image files and deletes or schedules deletion of
provider Responses. Provider outage leaves a retryable tombstone without blocking
local erasure. Successful cleanup clears provider IDs from attempts and versions.
A user-erasure service hook exists, but the product still needs a public
account/data deletion workflow and legally reviewed retention language.

Official details: [OpenAI — Your data](https://developers.openai.com/api/docs/guides/your-data).
