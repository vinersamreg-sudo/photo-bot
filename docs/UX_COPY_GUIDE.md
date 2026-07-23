# UX Copy Guide

## Tone

Calm, short, adult and concrete. One screen has one primary action. Explain the next step, not the implementation. Avoid excitement, infantilism and repeated exclamation marks.

## Terms

Use: фотография, изменение, обработка, вариант, демо, водяной знак, работа, версия, оригинал, пакет доступа Pixora.

Do not show: генерация, GPT, provider, API, HTTP, SceneIntent, EditPlan, parser, mode, storage, quota internals, request ID or stack trace.

Allowed core symbols: ✨ 📷 ✏️ 🎲 ⭐ 📂 ⬇ 🗑 ℹ️ and the required confirmation mark ✅. Do not decorate every line.

## Core copy

- Start: `✨ Pixora` + `Отправьте фотографию и напишите, что хотите изменить.` + short consent notice.
- Upload success: `Фото загружено ✅` + `Что хотите изменить?`
- Correction: `Что нужно поправить?`
- Processing: `✨ Создаю новый вариант.` + `Обычно это занимает 1–3 минуты. Можно закрыть MAX — результат придёт сюда.`
- Result status: `✨ Готово`
- Result caption: `Демо с водяным знаком.`

Primary result CTA order: original, correction, repeat, works. Favorite, current best and trash live under «Ещё» in Gallery. Show remaining quota only at one remaining and zero.

## Payment copy

- Before redirect: `Пакет доступа Pixora — 49 ₽.` + `После оплаты вам начисляется пакет доступа Pixora. В пакет входят две обработки и один оригинал. Пакет начисляется сразу после подтверждения оплаты.` + link button `Оплатить 49 ₽ в Robokassa`.
- Paid delivery failure: say that payment is confirmed, the original remains available in «Мои работы», and retry does not charge again.
- Disabled payments: explain the same package composition, then say `Оплата пока недоступна — идёт закрытое тестирование.` Do not imply that money was accepted.
- After confirmation: `Пакет доступа Pixora начислен.` and show balances as `обработок` and `оригиналов`, never `генераций`.
- The public product name and Robokassa `Description` are «Пакет доступа Pixora». The separately controlled fiscal Receipt item is not user-interface copy.
- Never show merchant login, invoice, signature, operation key, provider payload or internal order/version IDs.
- Refund messages must distinguish `подготовлен`, `отправлен` and `подтверждён`; a local draft is not a completed refund.

## Errors

Errors state what happened, whether the attempt was charged, and the next action. Never blame the user or expose internals. Exact mappings are regression-tested for invalid file, 15 MB size, timeout, provider/network/quota, policy, MAX delivery, exhausted demo and daily budget. During processing, edit the single status message instead of adding conflicting messages.
