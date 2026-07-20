# Launch Readiness

## Current verdict

The public website is ready for Robokassa moderation: DNS, trusted HTTPS, canonical redirects, HSTS, seller INN/contact data, legal routes and the exact 49 ₽ price are live and externally verified. The bot is not public-sales-ready: every payment flag remains off, observe-only is true and pilot limit is 0. Robokassa sandbox/ResultURL, specialist legal/fiscal review and a supported five-user pilot remain separate gates before accepting money.

## Automated evidence

`python -m app.main launch-status --strict` requires: correct model, configured credentials/owner, active service, fresh polling, live MAX/OpenAI connectivity, SQLite quick_check + migration v8, disk reserve, no active processing, recent backup, matching restore test and off-site confirmation. `health-report` adds payment, refund, pilot, storage, cleanup and cost state without identifiers.

The report exposes six separate gates: `site_moderation_ready`, `robokassa_sandbox_ready`, `robokassa_production_ready`, `owner_e2e_ready`, `pilot_5_ready`, and the deliberately false `public_launch_ready`. They must not be collapsed into one “ready” statement. Site readiness checks public HTTPS routes, 49 ₽/no 149 ₽, redirects and HSTS; owner E2E is derived without exposing owner ID.

## Blockers beyond five users

- no Robokassa sandbox or real-payment evidence through the production HTTPS ResultURL;
- automatic refund cannot run without a reconciled provider operation key;
- site legal pages contain confirmed name/status/city, owner-supplied INN and working e-mail; specialist legal review remains;
- no evidence yet for ≥80% first-result completion and <5% delivery failure;
- estimated OpenAI cost is not reconciled to invoice;
- support and abuse response are manual;
- gpt-image-2 identity/background drift remains.

## Public blockers

Final legal/consent/transborder/fiscal position, verified sandbox payment/refunds, support SLA, abuse controls, pilot evidence and scale decision. DNS, trusted TLS, external HTTPS smoke, seller data and the MAX deep link are verified and are no longer blockers.

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

Current in-bot and site documents are engineering-prepared launch candidates. They are not a substitute for a qualified legal review.
