# Launch Readiness

## Current verdict

Not public-ready. Owner image E2E is complete, and commercial architecture is ready for deployment with all payment flags off. The next gates are a fresh migration-v8 backup/restore/off-site cycle, owner Robokassa sandbox through public HTTPS ResultURL, legal/fiscal review and a supported five-user pilot.

## Automated evidence

`python -m app.main launch-status --strict` requires: correct model, configured credentials/owner, active service, fresh polling, live MAX/OpenAI connectivity, SQLite quick_check + migration v8, disk reserve, no active processing, recent backup, matching restore test and off-site confirmation. `health-report` adds payment, refund, pilot, storage, cleanup and cost state without identifiers.

## Blockers beyond five users

- no Robokassa sandbox or real-payment evidence through the production HTTPS ResultURL;
- automatic refund cannot run without a reconciled provider operation key;
- legal drafts lack verified operator details and specialist review;
- no evidence yet for ≥80% first-result completion and <5% delivery failure;
- estimated OpenAI cost is not reconciled to invoice;
- support and abuse response are manual;
- gpt-image-2 identity/background drift remains.

## Public blockers

Final legal/consent/transborder/fiscal position, verified payment and refunds, working MAX deep link/site decision, support contacts/SLA, abuse controls, pilot evidence and scale decision. Public readiness must remain false until these are explicitly closed.

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
- fiscal receipt nomenclature/tax, Robokassa merchant agreement and accounting retention;
- support channel, response expectations and data-deletion requests.

Current in-bot and site documents are drafts. They are not a substitute for a qualified legal review.
