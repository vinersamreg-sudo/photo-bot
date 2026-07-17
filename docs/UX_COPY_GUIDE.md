# UX Copy Guide

## Tone

Calm, short, adult and concrete. One screen has one primary action. Explain the next step, not the implementation. Avoid excitement, infantilism and repeated exclamation marks.

## Terms

Use: фотография, изменение, вариант, демо, водяной знак, работа, версия, оригинал.

Do not show: GPT, provider, API, HTTP, SceneIntent, EditPlan, parser, mode, storage, quota internals, request ID or stack trace.

Allowed core symbols: ✨ 📷 ✏️ 🎲 ⭐ 📂 ⬇ 🗑 ℹ️ and the required confirmation mark ✅. Do not decorate every line.

## Core copy

- Start: `✨ Pixora` + `Отправьте фотографию и напишите, что хотите изменить.` + short consent notice.
- Upload success: `Фото загружено ✅` + `Что хотите изменить?`
- Correction: `Что нужно поправить?`
- Processing: `✨ Создаю новый вариант.` + `Обычно это занимает 1–3 минуты. Можно закрыть MAX — результат придёт сюда.`
- Result status: `✨ Готово`
- Result caption: `Демо с водяным знаком.`

Primary result CTA order: original, correction, repeat, favorite, works, delete. Show remaining quota only at one remaining and zero.

## Errors

Errors state what happened, whether the attempt was charged, and the next action. Never blame the user or expose internals. Exact mappings are regression-tested for invalid file, 15 MB size, timeout, provider/network/quota, policy, MAX delivery, exhausted demo and daily budget. During processing, edit the single status message instead of adding conflicting messages.
