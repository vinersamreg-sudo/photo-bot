# UX

## Product flow

The primary MAX flow is intentionally compact:

1. `/start` shows the complete main menu with examples, a photo-processing
   action, «Мои работы» and legal links.
2. Only after the user chooses photo processing does Ravuna show the upload
   instruction and enter the photo-waiting state. The user then sends an image,
   preferably with the instruction in its caption.
3. Ravuna produces one watermarked preview with direct actions.
4. User corrects/repeats, opens works/history, or requests the original.
5. If no original entitlement exists, Ravuna shows one compact 49 ₽ package
   screen without creating a PaymentIntent.
6. PaymentIntent and the Robokassa link are created/reused only after the user
   presses “Оплатить 49 ₽”; the link screen does not repeat the package copy.
7. After ResultURL confirms payment, download is the primary action.

If a new photo and prompt arrive with no edits available, Ravuna stores them as
a pending request and shows that photo with an exhausted-balance notice plus the
same package block. The source-only request stays out of «Моих работ» until its
processing succeeds. Payment and post-payment processing remain bound to that
pending request; a previous or latest completed work is never used as fallback.

## Single-screen shell

- With `MAX_SINGLE_SCREEN_UI_ENABLED=true`, each MAX dialog has one bot-owned
  active UI message. Menu callbacks replace its text, media and keyboard with
  `notify=false`; they do not append a new menu message.
- The active message id, logical screen, compact JSON context and monotonically
  increasing UI revision are stored in `active_ui_sessions`.
- Callback payloads carry the revision. A callback from an older message or
  revision receives a short state-changed notification and has no product side
  effects.
- If MAX cannot edit the active message, Ravuna sends exactly one replacement,
  adopts its id and then attempts to delete only the previous bot-owned message.
  User photos, prompts and messages are never deleted.
- A new user photo, prompt, correction or source retires the previous bot screen.
  Ravuna sends one new progress/status message below that user input and stores it
  as the active message; the result or error then edits that new message in place.
- A completed asynchronous edit updates the UI only while its processing
  revision is current. Otherwise the result remains available in «Моих работах»
  and a short service notification may be sent.

## Start state

- Show the complete welcome text and main-menu keyboard; `/start` and
  `bot_started` do not enter the photo-waiting state.
- The explicit photo-processing action replaces the active screen with the
  upload instruction, enters the photo-waiting state and provides “← Назад”.
- “← Назад” clears the transient upload state and restores the complete main
  menu in place without calling an image provider.
- Caption and photo in one message are supported.
- Do not add a separate “Фото принято” screen when the caption is sufficient.
- Invalid or unsupported media receives a concrete recovery instruction.
- `/start` clears stale dialog binding but not gallery/payment history.

## Result state

- Send the preview once.
- Keep copy short: result plus actions.
- Primary action depends on entitlement/allowance state.
- “Исправить” must explain in one response what to type, with short examples.
- When no edits remain, do not invite correction; offer the existing package.
- Every callback must produce visible feedback or a clear unavailable message.
- The processing screen becomes the watermarked result in the same active
  message. The result and selected-work screens include
  “📤 Поделиться результатом”.

## Works and versions

- «Мои работы» contains only successfully completed works whose selected
  succeeded version has an existing watermarked preview. Source-only,
  processing, failed, rejected and missing-preview records are skipped before
  page counts and callbacks are calculated.
- A contact sheet has one to six real tiles and no placeholders. Its layout is
  dynamic for 1–6 items, each tile has a contrasting number badge, and the
  matching buttons are labelled “Открыть N”. A true clickable tile gallery would
  require a separate MAX mini-app and is not simulated by the bot image.
- Page identity includes user/chat, page, ordered item/version ids, preview
  revision and layout. Every page switch uploads and explicitly replaces the
  image attachment; 20 ready works produce four pages (6 + 6 + 6 + 2).
- Version history applies the same readiness filter, dynamic layout and
  attachment replacement. Opening a version preserves the selected preview and
  Back returns to the same history/work context.

## Payment state

For an unprocessed pending photo, prefix the package language with:

```text
У вас закончились обработки.

Чтобы обработать эту фотографию, приобретите пакет Ravuna.
```

Use exactly the product language:

```text
Пакет Ravuna — 49 ₽

В пакет входит:
• 2 обработки фотографий
• оригинал этой фотографии без водяного знака

Пакет начислится сразу после оплаты.
```

Do not call the package “generations”. Browser return pages explain that only the
server ResultURL confirms payment.

## Post-payment state

- First action: download the original for the selected version.
- Show current edit/original balances briefly.
- Do not force a return through “Мои работы” to obtain the paid original.
- Repeated download of an already unlocked version remains understandable and
  does not create a second charge.

## Referrals and attribution

- Each user receives one random opaque 10–20 character referral code. Share
  links use the configured `MAX_BOT_URL` and a `ref_<code>` start payload.
- The share screen keeps the watermarked preview and provides two MAX
  `clipboard` buttons: one copies the complete Unicode invitation and one copies
  only the referral URL. It does not use the unstable `:share` browser flow,
  expose original media or invite the owner to open their own referral link.
- The first valid referrer is retained only for a previously unused account.
  Self-referrals, existing users and duplicate/concurrent rewards are rejected.
- After the invitee's first successfully delivered preview, the inviter receives
  exactly two bonus edits. The additive bonus ledger does not alter paid grants
  and does not create an original entitlement.
- Allowlisted start sources are MAX channel, site, VK, OK, partner and campaign
  payloads. First-touch attribution is immutable; analytics stores event/source
  metadata but no prompts or images.
- The private operator command `python -m app.main growth-status --days 30
  --format human` reports starts, first edits, payments, conversion, shares and
  referral rewards without personal data.

## Buttons and stale state

- Every nested menu, including the upload instruction, ends with “← Назад”;
  only the main menu may omit it.
- Back restores the previous logical work/version/list screen, preserves the
  selected preview and never starts processing, debits an edit or creates a
  PaymentIntent.
- Back from correction/photo input clears the corresponding transient state.
- The keyboard that triggers a transition is deactivated where MAX permits it;
  callbacks from older keyboards return a visible “state changed” reply without
  removing the current keyboard.
- Double taps on payment actions reuse an applicable pending intent/order rather
  than creating confusing parallel purchase prompts.
- Observe-only mode always returns a temporary-unavailability message.
- No callback may fail silently.

## Language

- Active brand: Ravuna.
- Say “обработка”, not “генерация”, in user-facing copy.
- Sell the photo result, not the model/provider.
- One message has one purpose; do not repeat prior explanations.
- Never expose technical prompts, provider names, IDs, paths or internal status.

## Regression routing

Run `python scripts/test_fast.py max` for scoped MAX changes. Add named application
tests for exact callback/state behavior. Real MAX walkthrough is T4 and requires
explicit authorization.
