# Pixora 2.0 — owner-only visual validation

Дата проверки: 16 июля 2026 года  
Проверенный production commit: `82040c04e1172c6606eb2ecfe4fac5f115733798`  
Модель: `gpt-image-2`, endpoint `/v1/images/edits`, `1024x1024`, quality `medium`  
Prompt Builder: `technical-en-v2`

## Итоговый вердикт

Pixora 2.0 стала технически лучше: реальная цепочка corrections использует оригинал предыдущей GalleryVersion, структурированный SceneIntent корректно накопил распознанные изменения, а английский Prompt Builder сформировал пригодные provider prompts. Это подтверждено не unit-тестами, а пятью последовательными production-вызовами OpenAI.

Однако заявлять, что AI Brain уже надёжно выполняет свободные пользовательские формулировки, нельзя.

- Запросы 1–3 были распознаны и в основном выполнены.
- Запрос 4 не распознал изменение только цвета куртки и дал неправильный результат.
- Запрос 5 не распознал разговорную формулировку; визуально похожий на travel-редактуру результат получился случайно, а не благодаря корректному SceneIntent.
- Личность оставалась узнаваемой во всех пяти версиях, но наблюдался накопительный drift лица, кожи и волос.
- Фон перегенерировался в corrections, хотя его требовалось сохранять. Одних текстовых constraints для точного визуального сохранения недостаточно.
- Унаследованные `outfit replace` и `pose change` повторно попали в активную часть prompts. Это увеличивает drift и противоречит смыслу точечной correction.

Решение: semantic parser прямо сейчас не добавлять. Сначала закрыть детерминированные пробелы и исправить Prompt Builder: различать активное изменение и уже достигнутое состояние, блокировать provider call при пустом `changed_fields`, добавить цвет одежды и редакционное настроение как структурированные поля.

## Методика и ограничения

- Использована одна безопасная синтетическая фотография взрослого человека.
- Выполнено ровно 5 реальных image requests, без повторов и скрытых ретраев (`OPENAI_MAX_RETRIES=0` на время опыта).
- Первый запрос использовал исходную синтетическую фотографию; запросы 2–5 использовали приватный original предыдущей GalleryVersion.
- Пользовательские идентификаторы, токены и серверные пути не включены в отчёт.
- Визуальная оценка выполнена вручную по исходнику и пяти output-файлам.
- Точный HTTP-код SDK не сохраняет в текущую БД. Все пять generation attempts имеют статус `succeeded`, provider response и output bytes получены, поэтому успешный HTTP 2xx следует из результата, но утверждать конкретный `200` по сохранённым данным нельзя.
- Provider request ID сохранён для каждого запроса.

## Сводка операций

| № | Parent → Version | Фактический input | Provider | Время | Output | GalleryVersion | Demo quota |
|---:|---|---|---|---:|---:|---|---|
| 1 | source → 6 | исходный synthetic WEBP, SHA-256 `5c197e654f80…` | 2xx inferred; `req_dc807779fb59435f810f365b3b94a4a3` | 102.298 с | 1,507,555 B | создана | 0 → 1 |
| 2 | 6 → 7 | original v6, SHA-256 `32fd9df322ae…` | 2xx inferred; `req_e584b09d4f6f43e7a0e928eda6b3f564` | 112.862 с | 1,925,224 B | создана | 1 → 2 |
| 3 | 7 → 8 | original v7, SHA-256 `c1b2bf342a4c…` | 2xx inferred; `req_16e65588deb240ac82f5c260ca1e447d` | 96.755 с | 2,182,392 B | создана | 2 → 3 |
| 4 | 8 → 9 | original v8, SHA-256 `7e13b7358be2…` | 2xx inferred; `req_b1ae591b3cd9427284302321a54e2eb3` | 92.577 с | 2,308,356 B | создана | 3 → 4 |
| 5 | 9 → 10 | original v9, SHA-256 `0e9089bd16b9…` | 2xx inferred; `req_0155ab1ac6b548d0bac152d465f68dd8` | 62.473 с | 2,250,991 B | создана | 4 → 5 |

Средняя фактическая длительность: **93.393 секунды**.  
Суммарная длительность provider calls: **466.965 секунды**.  
Все пять output-файлов имеют размер `1024x1024`.

Хэши подтверждают lineage: output №1 равен input №2, output №2 равен input №3, output №3 равен input №4, output №4 равен input №5. Это не повторное использование исходной фотографии.

> Примечание по quota: временный exporter ошибочно проверял строку `success`, тогда как production хранит `succeeded`, и поэтому записал нулевые per-request counters. Фактический финальный session state — 5 успешных генераций из 5; каждая успешно доставленная GalleryVersion списала ровно одну попытку. В таблице приведена восстановленная последовательность 0→5.

## Визуальная оценка

Шкала 1–5: 5 — требование выполнено полностью; 1 — провал. Для «соблюдения ограничений» высокая оценка означает отсутствие запрещённых изменений.

| № | Identity | Запрошенное изменение | Сохранение предыдущего | Ограничения | Детализация | Артефакты |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4 | 4 | 5 | 4 | 4 | 5 |
| 2 | 4 | 5 | 2 | 2 | 4 | 5 |
| 3 | 4 | 4 | 3 | 2 | 5 | 4 |
| 4 | 4 | 1 | 3 | 1 | 4 | 4 |
| 5 | 3 | 2 | 2 | 2 | 4 | 3 |

Identity означает узнаваемость, а не пиксельную идентичность. К пятой версии лицо, кожа и волосы заметно стилизованы сильнее исходника.

## Запрос 1 — скалистые горы

**Исходный текст:** «Замени фон на реалистичные скалистые горы».

**SceneIntent:** `background.operation=replace`, `background.setting=realistic rocky mountains`, `background.sharpness=sharp`; identity, face и skin — preserve; outfit и pose — unchanged.

**Изменённые поля:** background operation, setting и sharpness.  
**Inherited constraints:** отсутствуют.  
**Parent:** отсутствует; input — исходная синтетическая фотография.  
**Результат:** горы созданы; человек, белая блузка и поза в основном сохранены. Фон остался заметно размытым, хотя SceneIntent требовал `sharp`. Лицо слегка отретушировано, но узнаваемость высокая. Явных анатомических артефактов нет.

<details>
<summary>Полный English technical prompt</summary>

```text
Edit the provided image as a realistic photograph.

MAIN EDIT INSTRUCTION
- Mode: initial_edit.
- Primary action: replace_background.
- Replace the background with realistic rocky mountains.
- Render the background sharp and detailed without shallow depth of field.

PRESERVE
- Preserve the same recognizable person, facial geometry, age and ethnicity.
- Preserve natural skin texture; do not beautify or over-retouch the face.
- Preserve the hairstyle, hair color and hairline.
- Preserve natural body proportions and anatomy.
- Preserve the current pose and camera viewpoint.
- Preserve the current clothing and accessories.
- Preserve the same recognizable identity and facial geometry.
- Preserve eyes, nose, mouth, age, ethnicity, hairline and natural skin texture.
- Preserve the hairstyle, hair color and hair identity.
- Preserve the pose and camera viewpoint.
- Preserve natural body proportions, anatomy, limb count and finger count.

DO NOT CHANGE
- Do not add text, logos, watermarks or unrelated objects.
- Do not create anatomy defects, duplicated limbs or malformed fingers.
- Do not over-smooth skin or make the image look synthetic.

BACKGROUND REQUIREMENTS
- Keep edges around the subject natural and integrate light, scale and perspective consistently.
- Render background materials with physically plausible texture and local detail.

SUBJECT AND REALISM REQUIREMENTS
- Keep the result photorealistic with coherent lighting, shadows, perspective and color response.
- Blend edited regions naturally; avoid halos, cutout edges and plastic textures.

QUALITY REQUIREMENTS
- Produce clean high-detail photographic texture without artificial sharpening artifacts.
```

</details>

## Запрос 2 — hiking outfit и поза

**Исходный текст:** «Переодень в одежду для хайкинга и немного измени позу. Лицо не меняй».

**SceneIntent:** унаследованный rocky background; `outfit.operation=replace`, `outfit.style=realistic practical hiking clothing`; `pose.operation=change`, `pose.description=natural requested pose`; negative — `change identity or facial geometry`.

**Изменённые поля:** outfit, pose, negative.  
**Inherited constraints:** заменить фон на реалистичные скалистые горы с видимыми скальными образованиями.  
**Parent/input:** GalleryVersion 6, original предыдущего результата.  
**Результат:** одежда для хайкинга, рюкзак и поза изменены успешно; лицо узнаваемо. Но геометрия гор и композиция фона были созданы заново вместо сохранения exact parent background. Есть небольшой identity drift, крупных артефактов нет.

<details>
<summary>Полный English technical prompt</summary>

```text
Edit the provided image as a realistic photograph.

MAIN EDIT INSTRUCTION
- Mode: correction.
- Primary action: custom.
- Replace the background with realistic rocky mountains.
- Render the background sharp and detailed without shallow depth of field.
- Replace the outfit with realistic practical hiking clothing.
- Change the pose to natural requested pose.

PRESERVE
- Preserve the same recognizable person, facial geometry, age and ethnicity.
- Preserve natural skin texture; do not beautify or over-retouch the face.
- Preserve the hairstyle, hair color and hairline.
- Preserve natural body proportions and anatomy.
- Preserve the current pose and camera viewpoint.
- Preserve the current clothing and accessories.
- Preserve the current background, perspective and composition.
- Preserve the same recognizable identity and facial geometry.
- Preserve eyes, nose, mouth, age, ethnicity, hairline and natural skin texture.
- Preserve the hairstyle, hair color and hair identity.
- Preserve natural body proportions, anatomy, limb count and finger count.
- Preserve the current background, composition and perspective.

DO NOT CHANGE
- Do not redesign, replace or beautify the face.
- Do not add text, logos, watermarks or unrelated objects.
- Do not create anatomy defects, duplicated limbs or malformed fingers.
- Do not over-smooth skin or make the image look synthetic.
- Do not change identity or facial geometry.

CONTINUITY FROM THE PARENT VERSION
- Preserve every successful edit already visible in the parent version unless explicitly changed now.
- Already established in the parent result; preserve rather than recreate: Replace the background with realistic rocky mountains and visible rock formations.

SUBJECT AND REALISM REQUIREMENTS
- Keep the result photorealistic with coherent lighting, shadows, perspective and color response.
- Blend edited regions naturally; avoid halos, cutout edges and plastic textures.

QUALITY REQUIREMENTS
- Produce clean high-detail photographic texture without artificial sharpening artifacts.
```

</details>

## Запрос 3 — резкий фон без замены скал

**Исходный текст:** «Сохрани эти же скалы. Убери размытие фона, сделай скалы резкими и детальными. Остальное не меняй».

**SceneIntent:** `background.operation=sharpen`, setting и sharpness унаследованы; outfit и pose унаследованы; negative дополнен `replace background`, `background blur`, `replacement background`.

**Изменённые поля:** background operation и negative.  
**Inherited constraints:** rocky mountains, hiking clothing, изменённая поза.  
**Parent/input:** GalleryVersion 7, original предыдущего результата.  
**Результат:** фон стал заметно резче и детальнее. При этом скалы и crop снова перегенерированы, а цвет одежды сменился с тёмного на бирюзовый, хотя пользователь просил «остальное не менять». Узнаваемость сохранена, но synthetic/retouched look усилился.

<details>
<summary>Полный English technical prompt</summary>

```text
Edit the provided image as a realistic photograph.

MAIN EDIT INSTRUCTION
- Mode: correction.
- Primary action: sharpen_background.
- Keep the current background and make it sharp, detailed and clearly readable.
- Replace the outfit with realistic practical hiking clothing.
- Change the pose to natural requested pose.

PRESERVE
- Preserve the same recognizable person, facial geometry, age and ethnicity.
- Preserve natural skin texture; do not beautify or over-retouch the face.
- Preserve the hairstyle, hair color and hairline.
- Preserve natural body proportions and anatomy.
- Preserve the current pose and camera viewpoint.
- Preserve the current clothing and accessories.
- Preserve the current background, perspective and composition.
- Preserve the current background setting and its recognizable visual content.
- Preserve the current background scene, layout and recognizable rocks or landmarks.
- Preserve the same recognizable identity and facial geometry.
- Preserve eyes, nose, mouth, age, ethnicity, hairline and natural skin texture.
- Preserve the hairstyle, hair color and hair identity.
- Preserve the pose and camera viewpoint.
- Preserve natural body proportions, anatomy, limb count and finger count.

DO NOT CHANGE
- Do not redesign, replace or beautify the face.
- Do not replace the current background with a different setting.
- Do not replace the current background.
- Do not apply background blur, bokeh, defocus or shallow depth of field.
- Do not add text, logos, watermarks or unrelated objects.
- Do not create anatomy defects, duplicated limbs or malformed fingers.
- Do not over-smooth skin or make the image look synthetic.
- Do not change identity or facial geometry.
- Do not replace background.
- Do not background blur.
- Do not replacement background.

CONTINUITY FROM THE PARENT VERSION
- Preserve every successful edit already visible in the parent version unless explicitly changed now.
- Continue from the exact background visible in the parent version.
- Correct only the residual background blur in the parent version.
- Preserve every successful element not explicitly targeted by this correction.
- Keep the parent version's exact background setting, composition and recognizable landmarks.
- Already established in the parent result; preserve rather than recreate: Replace the background with realistic rocky mountains and visible rock formations.
- Already established in the parent result; preserve rather than recreate: Replace the clothing with realistic, practical hiking clothing.
- Already established in the parent result; preserve rather than recreate: Adjust the subject's pose naturally as requested.

BACKGROUND REQUIREMENTS
- Keep edges around the subject natural and integrate light, scale and perspective consistently.
- Render background materials with physically plausible texture and local detail.
- Use a deep depth of field: the background must be sharp, not blurred or replaced.

SUBJECT AND REALISM REQUIREMENTS
- Keep the result photorealistic with coherent lighting, shadows, perspective and color response.
- Blend edited regions naturally; avoid halos, cutout edges and plastic textures.

QUALITY REQUIREMENTS
- Produce clean high-detail photographic texture without artificial sharpening artifacts.
```

</details>

## Запрос 4 — только цвет куртки

**Исходный текст:** «Поменяй только цвет куртки на тёмно-зелёный».

**SceneIntent:** не изменился относительно запроса 3. Значение `dark green` отсутствует.  
**Изменённые поля:** `{}`.  
**Inherited constraints:** rocky mountains, hiking clothing, изменённая поза, sharp background.  
**Parent/input:** GalleryVersion 8, original предыдущего результата.  
**Результат:** parser не понял цвет одежды. Provider prompt не содержал `dark green`, повторно попросил заменить весь outfit и позу. В результате появилась серая толстовка, а не тёмно-зелёная куртка; изменились стиль одежды, фон и детали лица. Это полный функциональный провал correction.

<details>
<summary>Полный English technical prompt</summary>

```text
Edit the provided image as a realistic photograph.

MAIN EDIT INSTRUCTION
- Mode: correction.
- Primary action: custom.
- Keep the current background and make it sharp, detailed and clearly readable.
- Replace the outfit with realistic practical hiking clothing.
- Change the pose to natural requested pose.

PRESERVE
- Preserve the same recognizable person, facial geometry, age and ethnicity.
- Preserve natural skin texture; do not beautify or over-retouch the face.
- Preserve the hairstyle, hair color and hairline.
- Preserve natural body proportions and anatomy.
- Preserve the current pose and camera viewpoint.
- Preserve the current clothing and accessories.
- Preserve the current background, perspective and composition.
- Preserve the current background setting and its recognizable visual content.
- Preserve the current background scene, layout and recognizable rocks or landmarks.
- Preserve the same recognizable identity and facial geometry.
- Preserve eyes, nose, mouth, age, ethnicity, hairline and natural skin texture.
- Preserve the hairstyle, hair color and hair identity.
- Preserve the pose and camera viewpoint.
- Preserve natural body proportions, anatomy, limb count and finger count.
- Preserve the current background, composition and perspective.

DO NOT CHANGE
- Do not redesign, replace or beautify the face.
- Do not replace the current background with a different setting.
- Do not replace the current background.
- Do not apply background blur, bokeh, defocus or shallow depth of field.
- Do not add text, logos, watermarks or unrelated objects.
- Do not create anatomy defects, duplicated limbs or malformed fingers.
- Do not over-smooth skin or make the image look synthetic.
- Do not change identity or facial geometry.
- Do not replace background.
- Do not background blur.
- Do not replacement background.

CONTINUITY FROM THE PARENT VERSION
- Preserve every successful edit already visible in the parent version unless explicitly changed now.
- Continue from the exact background visible in the parent version.
- Correct only the residual background blur in the parent version.
- Preserve every successful element not explicitly targeted by this correction.
- Keep the parent version's exact background setting, composition and recognizable landmarks.
- Already established in the parent result; preserve rather than recreate: Replace the background with realistic rocky mountains and visible rock formations.
- Already established in the parent result; preserve rather than recreate: Replace the clothing with realistic, practical hiking clothing.
- Already established in the parent result; preserve rather than recreate: Adjust the subject's pose naturally as requested.
- Already established in the parent result; preserve rather than recreate: Make the existing background sharp, detailed and clearly readable.

SUBJECT AND REALISM REQUIREMENTS
- Keep the result photorealistic with coherent lighting, shadows, perspective and color response.
- Blend edited regions naturally; avoid halos, cutout edges and plastic textures.

QUALITY REQUIREMENTS
- Produce clean high-detail photographic texture without artificial sharpening artifacts.
```

</details>

## Запрос 5 — разговорный fallback

**Исходный текст:** «Сделай кадр поуютнее, как для хорошего тревел-журнала».

**SceneIntent:** не изменился относительно запроса 4. В нём нет mood, editorial style или travel-magazine semantics.  
**Изменённые поля:** `{}`.  
**Inherited constraints:** прежняя сцена плюс generic `Apply the user's requested edit faithfully and conservatively`.  
**Parent/input:** GalleryVersion 9, original предыдущего результата.  
**Результат:** parser не понял формулировку, а technical prompt не передал её смысл. Модель случайно получила более «журнальную» композицию, оливковую одежду и треккинговую палку, но одновременно снова изменила позу, одежду, фон и лицо. Такой fallback недетерминирован, тратит деньги при пустом `changed_fields` и не может считаться успешным.

<details>
<summary>Полный English technical prompt</summary>

```text
Edit the provided image as a realistic photograph.

MAIN EDIT INSTRUCTION
- Mode: correction.
- Primary action: custom.
- Keep the current background and make it sharp, detailed and clearly readable.
- Replace the outfit with realistic practical hiking clothing.
- Change the pose to natural requested pose.

PRESERVE
- Preserve the same recognizable person, facial geometry, age and ethnicity.
- Preserve natural skin texture; do not beautify or over-retouch the face.
- Preserve the hairstyle, hair color and hairline.
- Preserve natural body proportions and anatomy.
- Preserve the current pose and camera viewpoint.
- Preserve the current clothing and accessories.
- Preserve the current background, perspective and composition.
- Preserve the current background setting and its recognizable visual content.
- Preserve the current background scene, layout and recognizable rocks or landmarks.
- Preserve the same recognizable identity and facial geometry.
- Preserve eyes, nose, mouth, age, ethnicity, hairline and natural skin texture.
- Preserve the hairstyle, hair color and hair identity.
- Preserve the pose and camera viewpoint.
- Preserve natural body proportions, anatomy, limb count and finger count.
- Preserve the current background, composition and perspective.

DO NOT CHANGE
- Do not redesign, replace or beautify the face.
- Do not replace the current background with a different setting.
- Do not replace the current background.
- Do not apply background blur, bokeh, defocus or shallow depth of field.
- Do not add text, logos, watermarks or unrelated objects.
- Do not create anatomy defects, duplicated limbs or malformed fingers.
- Do not over-smooth skin or make the image look synthetic.
- Do not change identity or facial geometry.
- Do not replace background.
- Do not background blur.
- Do not replacement background.

CONTINUITY FROM THE PARENT VERSION
- Preserve every successful edit already visible in the parent version unless explicitly changed now.
- Continue from the exact background visible in the parent version.
- Correct only the residual background blur in the parent version.
- Preserve every successful element not explicitly targeted by this correction.
- Keep the parent version's exact background setting, composition and recognizable landmarks.
- Already established in the parent result; preserve rather than recreate: Replace the background with realistic rocky mountains and visible rock formations.
- Already established in the parent result; preserve rather than recreate: Replace the clothing with realistic, practical hiking clothing.
- Already established in the parent result; preserve rather than recreate: Adjust the subject's pose naturally as requested.
- Already established in the parent result; preserve rather than recreate: Make the existing background sharp, detailed and clearly readable.
- Already established in the parent result; preserve rather than recreate: Apply the user's requested edit faithfully and conservatively.

SUBJECT AND REALISM REQUIREMENTS
- Keep the result photorealistic with coherent lighting, shadows, perspective and color response.
- Blend edited regions naturally; avoid halos, cutout edges and plastic textures.

QUALITY REQUIREMENTS
- Produce clean high-detail photographic texture without artificial sharpening artifacts.
```

</details>

## Что сработало

- Production действительно вызвал OpenAI `images.edit`, получил пять успешных provider responses и создал пять GalleryVersion.
- Parent/source lineage работал правильно на каждом correction.
- Распознанные поля SceneIntent на запросах 1–3 накопились и дошли до английского prompt.
- Correction «убери размытие» визуально повысила резкость и детализацию гор.
- Demo quota списывалась только после успешной доставки результата.
- Во всех версиях человек оставался узнаваемым; тяжёлых анатомических дефектов нет.

## Что не сработало

- Первый результат не выполнил требование sharp background полностью.
- Corrections не сохранили exact background: модель перегенерировала геометрию скал.
- Correction 3 изменила цвет одежды без запроса.
- Correction 4 не распознана: тёмно-зелёный цвет отсутствует в SceneIntent и provider prompt.
- Fallback 5 не передал смысл разговорной фразы provider-у.
- `changed_fields={}` не остановил дорогой provider call.
- Prompt Builder повторяет унаследованные outfit/pose как активные edit instructions.
- В negative prompt есть неграмотные строки `Do not background blur` и `Do not replacement background`.
- Identity preservation работает как мягкое пожелание, а не как измеряемая гарантия; drift накапливается.

## Нужен ли semantic parser сейчас

**Нет, не первым шагом.** Эта проверка обнаружила конкретные детерминированные дефекты, которые semantic parser замаскирует, но не исправит:

1. Добавить `outfit.color` и правила для «цвет куртки/одежды», «перекрась», «тёмно-зелёный» и распространённых цветов.
2. Добавить структурированные `lighting.mood`/`camera.editorial_style` для «уютнее», «как для журнала», «travel/editorial».
3. Если correction дала пустой `changed_fields`, не вызывать OpenAI и не списывать quota. Возвращать короткое сообщение с примерами поддерживаемой переформулировки.
4. В Prompt Builder унаследованные поля использовать как `PRESERVE/CONTINUITY`, а не повторять их в `MAIN EDIT INSTRUCTION`.
5. Нормализовать negative phrases в валидный английский.
6. Добавить regression-набор именно на active-vs-inherited semantics и на пустой correction.
7. Для точного сохранения фона исследовать mask/region-based edit или отдельный композитинг: текстовый prompt не обеспечивает pixel-level continuity.

После этих правок следует повторить тот же пятишаговый eval. Semantic parser имеет смысл только если расширенный детерминированный словарь всё ещё оставляет существенную долю реальных пользовательских фраз нераспознанными.

## Время и стоимость

- Пять provider calls: 466.965 секунды, в среднем 93.393 секунды.
- Внутренний резерв Pixora: 10 RUB на операцию, 50 RUB суммарно. Это резерв, а не подтверждённый счёт OpenAI.
- Точную стоимость OpenAI восстановить нельзя: image/text token usage не сохранён в evidence manifest. Для точной суммы нужен billing/usage dashboard.
- Актуальная официальная тарификация GPT Image 2: image input $8/1M tokens, cached image input $2/1M, image output $30/1M; text input $5/1M. См. [OpenAI API Pricing](https://developers.openai.com/api/docs/pricing) и [GPT Image 2 model page](https://developers.openai.com/api/docs/models/gpt-image-2).

## Финальное состояние production

- Пользовательские handlers остановлены.
- `MAX_POLL_OBSERVE_ONLY=true`.
- Owner allowlist остаётся настроен; идентификатор не раскрывается.
- Transport: polling.
- `OPENAI_MAX_RETRIES=2` восстановлен.
- Незавершённых generation attempts: 0.
- Dialogs в processing: 0.
- GalleryVersions в processing: 0.
- SQLite `PRAGMA quick_check`: `ok`.
- Healthcheck: passed.
- Найдены 2 ранее существовавших orphan image files. Их количество после теста не увеличилось; автоматическое удаление не выполнялось, чтобы не затронуть неизвестные данные.

## Решение о готовности AI Brain

Архитектурная база стала лучше и реальный lineage подтверждён. Но свободные corrections пока нельзя считать production-ready для платного публичного использования: два из пяти тестовых запросов не были поняты, а точечные правки приводили к непредусмотренной перегенерации сцены. До следующего owner-only eval нужно исправить deterministic coverage, active-vs-inherited prompt semantics и fail-closed поведение при пустом `changed_fields`.
