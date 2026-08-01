# Segmentation evaluation

## Кандидаты

- **rembg + U-2-Net human segmentation** — локальный CPU ONNX pipeline, прозрачная
  интеграция, MIT для rembg и Apache-2.0 для U-2-Net code. rembg 2.0.76 требует
  Python >=3.11,<3.14 и поддерживает CPU provider. Репозитории:
  [rembg](https://github.com/danielgatis/rembg),
  [U-2-Net](https://github.com/xuebinqin/U-2-Net).
- **BiRefNet** — сильный современный кандидат, но лицензирование конкретных weights
  и datasets нужно проверять отдельно до коммерческого использования.
- **BRIA RMBG 2.0** — публичные weights имеют ограничения, несовместимые с
  автоматическим коммерческим включением; отклонён для MVP.

## Выбор MVP

Выбран опциональный `rembg==2.0.76` CPU с заранее provisioned и проверенным по
SHA-256 `u2net_human_seg.onnx`. Production-код запрещает автоматическую загрузку
модели, symlink, отсутствие checksum и невалидную mask. Mask должна совпадать с
source geometry, содержать и foreground, и background.

Основной `requirements.txt` не включает rembg: тяжёлая зависимость вынесена в
`requirements-segmentation.txt`. Это предотвращает случайную активацию и загрузку
weights при обычном deploy.

## Что проверено

Unit/integration tests проверяют mask validation, composite geometry, отсутствие
OpenAI-вызова, cleanup и транзакционную семантику при segmentation failure.
Offline benchmark использовал fixed synthetic mask: это проверка composite, **не
качества сегментации**.

## Gate до включения

1. Зафиксировать источник, лицензию и SHA-256 конкретного ONNX weight.
2. Установить optional requirements на staging/owner-only VPS.
3. Измерить wall time, RSS и CPU на реальных разрешённых adult synthetic photos.
4. Визуально проверить волосы, пальцы, полупрозрачные края, одежду и сложный фон.
5. Проверить timeout/kill и отсутствие orphan model/temp files.
6. Только затем выставить `SEGMENTATION_BACKEND=rembg` и отдельным решением
   включить composite для owner-only.
