# UX

## Product flow

The primary MAX flow is intentionally compact:

1. `/start` shows the complete main menu with examples, a direct paperclip
   upload instruction, «Мои работы» and legal links. It does not show an upload
   callback button because MAX cannot open the attachment picker from it.
2. The user may attach the first source directly from the main screen and may
   add exactly one more before entering the instruction. When one MAX message
   contains two image attachments and a caption, their attachment order is
   preserved, both images are stored before generation starts, and the caption
   is used as the single unchanged prompt. More than two attachments are rejected
   explicitly; a failed second-image download never falls back to one-image
   generation.
3. Ravuna produces one watermarked preview with direct actions.
4. User corrects/repeats, opens works/history, or requests the original.
5. If no original entitlement exists, Ravuna shows one compact 49 ₽ package
   screen and creates/reuses its exact target-scoped PaymentIntent.
6. “Оплатить 49 ₽” opens Ravuna's opaque short URL and then Robokassa directly;
   there is no second link screen and no long URL in message text.
7. ResultURL remains the only payment confirmation. Browser return either shows
   “Проверяем оплату…” or resumes the exact confirmed purchase context.

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
- A new user photo, prompt, correction or source retires and deletes the previous
  bot UI screen. Ravuna sends one new progress/status message below that user
  input and stores it as the active message; the user message itself is never
  deleted.
- A completed asynchronous edit updates the UI only while its processing
  revision is current. Otherwise the result remains available in «Моих работах»
  and a short service notification may be sent.
- Callback-only navigation from a result, including correction, another photo,
  works, rating, feedback, referral and legal screens, continues in-place on the
  active UI message. If MAX cannot change the message type safely, Ravuna sends
  one replacement and deletes the previous bot UI message.
- A finished preview is posted as a native image message for fullscreen/download;
  only after that POST succeeds does Ravuna delete the processing screen and adopt
  the preview as the sole active UI screen.
- An original file is a delivered user artifact and may remain in chat history.
  The watermarked result/menu is still disposable UI: after delivery Ravuna
  replaces it with one confirmation/actions screen below the file, and subsequent
  callbacks reuse that single active screen.

## Start state

- Show the complete welcome text and main-menu keyboard; `/start` and
  `bot_started` do not enter the photo-waiting state.
- An explicit `/start`, start text or `bot_started`/deeplink retires the previous
  active bot screen and sends a new complete start screen below the user action.
  Callback navigation then continues to edit that new screen in place.
- The main screen tells the user to attach a photo through the paperclip and
  accepts that photo without a preliminary callback. It keeps «Мои работы»,
  public-offer and personal-data links, but has no misleading upload button.
- “📷 Другое фото” enters the photo-waiting state and shows the paperclip
  instruction, the existing public-offer and personal-data links plus
  “← Назад”. The screen reminds the user that one edit accepts up to two photos,
  requires the right to use them and sends them to the configured AI provider
  only for processing.
- “← Назад” clears the transient upload state and restores the complete main
  menu in place without calling an image provider.
- Caption and photo in one message are supported.
- Do not add a separate “Фото принято” screen when the caption is sufficient.
- Invalid or unsupported media receives a concrete recovery instruction.
- `/start` clears stale dialog binding but not gallery/payment history.
- The main screen shows `Доступно обработок: N`. At zero it shows the exhausted
  balance explanation and a direct package purchase button; stale upload state
  cannot call the image provider.

## Source input

- One edit accepts one or two source images and consumes one edit after normal
  successful processing and delivery.
- After the first source, Ravuna offers “Продолжить с одной фотографией” and
  “Добавить вторую фотографию”. A third source is rejected with
  “Можно использовать максимум 2 фотографии”.
- The provider receives the unchanged prompt followed by source image 1 and,
  when present, source image 2. Ravuna does not infer semantic roles for them.
- Both private sources and their lineage survive a zero-balance pending request
  and exact post-payment resume; unfinished sources do not appear in works.

## Result state

- Send the preview once.
- Keep copy short: result plus actions.
- Primary action depends on entitlement/allowance state.
- “Исправить” must explain in one response what to type, with short examples.
- When no edits remain, do not invite correction; offer the existing package.
- Every callback must produce visible feedback or a clear unavailable message.
- The processing text is temporary. A finished watermarked preview is sent as a
  new native image message, adopted as the active UI message, and only then is
  the processing message deleted. Subsequent callback navigation continues
  edit-in-place. The result card leads with the original action, groups
  “✏️ Исправить” and “📷 Другое фото” in one row, then shows the referral CTA.
  The inviter receives two bonus edits only after the invitee's first successful
  edit. “📷 Другое фото” reuses the existing one/two-source upload flow.
- The result screen groups “⭐ Оценить” and “💬 Отзыв о Ravuna” in one row. A
  1–5 rating and an optional text comment are stored
  with the internal user id, selected version and UTC timestamp. Feedback input
  never enters processing, calls an image provider or changes credits.

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

- A confirmed original purchase automatically delivers the original for the
  exact selected version; a processing purchase resumes the exact durable
  pending prompt and one or two source images.
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
