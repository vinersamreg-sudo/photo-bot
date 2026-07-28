# Ravuna v1 MAX UX

## Main path

`/start → send photo → describe change → one processing status → watermarked result`.

There is no start/continue button, scenario menu, «Своя идея» choice or confirmation screen. A valid returning source can be reused. Invalid/expired/missing sources cannot reach processing.

## Screens

- Start: short product promise, consent notice, optional «Идеи», «Мои работы», «Подробнее».
- After persistence: `Фото загружено ✅` and one question.
- Processing: one editable message with honest 1–3 minute expectation.
- Result: image + short demo caption + ordered original/correct/repeat/favorite/works/delete actions.
- Ideas: category first; it never slows the free-text path.
- Works: up to five preview cards with date/version/favorite, then selected-version navigation and actions.

## State and failure rules

`/start` clears stale dialog bindings unless a valid stored demo source is intentionally resumed. An image in any other state is processed explicitly: accepted as current source or rejected with a reason, never ignored. «Фото загружено» is sent only after validation and private persistence. Processing is closed in place on success/error/restart. Technical and delivery errors do not debit demo quota.

Exact copy and permitted vocabulary are in `docs/UX_COPY_GUIDE.md`. Historical UX mockups before this decision are not authoritative.
