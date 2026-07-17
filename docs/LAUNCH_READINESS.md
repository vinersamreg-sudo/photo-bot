# Launch Readiness

## Current verdict

Not public-ready. Code is prepared for an owner validation and then a five-user closed pilot, but readiness is conditional on a green production deploy, real encrypted backup restore/off-site workflow, cleanup report and three owner E2E paths.

## Automated evidence

`python -m app.main launch-status --strict` requires: correct model, configured credentials/owner, active service, fresh polling, live MAX/OpenAI connectivity, SQLite quick_check + migration v6, disk reserve, no active processing, recent backup, matching restore test and off-site confirmation. It reports cleanup age/orphans and daily generation/duration/error/delivery/cost metrics.

## Blockers beyond five users

- no real payment/refund integration;
- legal drafts lack verified operator details and specialist review;
- no evidence yet for ≥80% first-result completion and <5% delivery failure;
- estimated OpenAI cost is not reconciled to invoice;
- support and abuse response are manual;
- gpt-image-2 identity/background drift remains.

## Public blockers

Final legal/consent/transborder processing position, payment and refunds, working MAX deep link/site decision, support contacts/SLA, abuse controls, pilot evidence and scale decision. Public readiness must remain false in CLI until these are explicitly closed.

## Legal checklist requiring specialist review

- final offer, privacy policy, personal-data consent and image-processing notice;
- legal name/status, tax and contact details of the operator;
- evidence and versioning of consent in MAX;
- explicit disclosure that photo and instruction are sent to OpenAI;
- documented DB/file locations, access, demo/paid/trash retention and deletion path;
- lawful basis and disclosure for cross-border transfer/processing;
- user warranty of rights to the photo and consent of depicted people;
- rule for minors and age/guardian confirmation;
- payment, failed delivery, cancellation and refund rules;
- support channel, response expectations and data-deletion requests.

Current in-bot and site documents are drafts. They are not a substitute for a qualified legal review.
