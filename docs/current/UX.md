# UX

## Product flow

The primary MAX flow is intentionally compact:

1. `/start` shows one photo+instruction request.
2. User sends an image, preferably with the instruction in its caption.
3. Ravuna produces one watermarked preview with direct actions.
4. User corrects/repeats, opens works/history, or requests the original.
5. If no original entitlement exists, Ravuna shows the compact 49 ₽ package.
6. After ResultURL confirms payment, download is the primary action.

## Start state

- Ask for a photo and the desired change.
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

## Payment state

Use exactly the product language:

```text
Пакет доступа Ravuna — 49 ₽
После оплаты начисляется:
• 2 обработки
• 1 оригинал
```

Do not call the package “generations”. Browser return pages explain that only the
server ResultURL confirms payment.

## Post-payment state

- First action: download the original for the selected version.
- Show current edit/original balances briefly.
- Do not force a return through “Мои работы” to obtain the paid original.
- Repeated download of an already unlocked version remains understandable and
  does not create a second charge.

## Buttons and stale state

- A state transition invalidates old inline keyboards where MAX permits it.
- Stale callbacks must return a visible “state changed / open current view” reply.
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
