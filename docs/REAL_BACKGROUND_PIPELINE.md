# Real background pipeline

## Pipeline

1. Router определяет реальную категорию и подбирает лицензированный asset по
   orientation/aspect ratio/lighting/horizon.
2. Catalog повторно проверяет metadata, path и SHA-256.
3. Локальный segmenter создаёт grayscale subject mask той же геометрии.
4. Background кропится через `ImageOps.fit` без растяжения.
5. Яркость фона ограниченно согласуется с source; mask слегка feather-ится.
6. Source subject alpha-composite-ится поверх background.
7. Результат сохраняет размер и orientation source и отдаётся в существующий
   watermark/storage/delivery flow.

Временная mask живёт в `TemporaryDirectory` и удаляется при успехе и ошибке.
Дополнительный cleanup удаляет только старые объекты с известными префиксами,
не следует symlink и не затрагивает чужие файлы.

## Границы качества

Локальный composite сохраняет пиксели лица/одежды/позы лучше generative redraw, но
может выглядеть искусственно при плохой mask, несовпадающем свете, перспективе,
DOF или цветовой температуре. AI finishing пока выключён: он снова создаёт риск
изменения личности и лицензированного фона.

## Benchmark 16.07.2026

Offline Windows, procedural 1024×1024, 3 итерации, fixed mask:

- composite mean ≈93 ms;
- enhancement mean ≈72 ms;
- external provider calls: 0;
- Python-traced peak ≈0.36 MB (не включает native Pillow buffers).

Этот результат доказывает низкую стоимость Pillow-части, но не измеряет ONNX RSS
или latency. VPS deploy повторяет тот же offline benchmark; rembg остаётся disabled
до отдельного resource/visual gate.

Production VPS dry benchmark того же procedural fixture: composite ≈132 ms,
enhancement ≈102 ms, external calls 0. Deploy также требует нулевое количество
stale `pixora-mask-*`/`pixora-composite-*` объектов старше часа. Native ONNX/RSS
по-прежнему не измерены, поэтому segmentation gate остаётся закрыт.

## Enable checklist

- [ ] хотя бы один legally approved active asset;
- [ ] pinned model license/source/SHA;
- [ ] VPS CPU/RSS/timeout benchmark rembg;
- [ ] owner-only visual validation на разрешённых synthetic adult photos;
- [ ] волосы/края/поза/identity приемлемы;
- [ ] orphan cleanup и SQLite quick_check после ошибок;
- [ ] feature flag включён только owner-only;
- [ ] явный UX fallback, без silent AI substitution.
