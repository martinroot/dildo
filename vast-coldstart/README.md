# Vast.ai cold-start scaffold — первый образ: Z-Image

Каркас для развёртывания кастомных моделей на Vast.ai, собранный на базе уже
имеющегося в `martinroot/dildo` механизма `gpu_vast.py` (аренда → SSH → работа →
сбор результата → освобождение).

## Что здесь есть

- `catalog/hotapi-models.json` — снимок публичного `GET https://api.hotapi.ai/v1/models`: **67 endpoint / 34 семейства**.
- `catalog/models.md` — тот же список, сгруппированный по семействам, с ценами.
- `catalog/selfhost-candidates.md` — какие модели имеют открытые веса и могут ехать на Vast.
- `image/z-image/` — **первый образ**: Docker-образ + HTTP API для открытой модели `Tongyi-MAI/Z-Image-Turbo` (эквивалент HotAPI `z-image-spicy`).
- `scripts/vast_deploy.py` — контроллер аренды и холодного запуска: search → create → SSH → upload → bootstrap → health → smoke → release.
- `config.example.json` — пример конфигурации (без секретов).
- `tests/test_coldstart.py` — 9 контрактных тестов, включая `--dry-run` без Vast-ключа.

## Почему Z-Image первым

- Открытые веса (`Apache-2.0` согласно карточке HF), 6B DiT, Turbo-вариант на 8 шагов, помещается в 16 ГБ VRAM.
- Есть нативная интеграция в `diffusers`: `ZImagePipeline`.
- Только текст→изображение: нет видео/аудио/референсов — минимальная поверхность холодного старта.
- Бренд HotAPI и его проприетарные модели (`Seedance`, `Seedream`, `Berry`, `MiniMax H3`, `W3.x`) **не** копируются: это hosted gateway, у них нет открытых весов.

## Архитектура

```
LOCAL/CONTROLLER                       VAST INSTANCE (GPU)
vast_deploy.py  --PUT /asks-->  Vast API
      |  SSH/SCP (только свой инстанс)
      +----------------------->  /workspace/zimage-coldstart/app
                                   bootstrap.sh → server.py :8096
      <----------------------  GET /healthz, POST /v1/z-image-spicy (smoke)
      `--DELETE /instances/{id}/`  (если release_after_smoke=true)
```

Внутри `server.py` тяжёлые зависимости импортируются лениво: `/healthz` и
учёт задач работают без GPU/torch, а реальный движок грузится при первой
генерации (или при `ZIMAGE_PRELOAD=1` в `bootstrap.sh`).

## Проверка локально (без Vast и без GPU)

```bash
cd vast-coldstart
python3 -m unittest discover -s tests -v
python3 scripts/vast_deploy.py deploy --config config.example.json \
  --state-dir /tmp/zimg-state --dry-run
```

`--dry-run` подменяет и Vast API, и SSH детерминированными заглушками:
инстанс не арендуется, ключ не нужен, но проходится тот же путь:
search → create → ready → upload → bootstrap → health → smoke → release.

## Реальный деплой

1. Собрать и запушить образ (пример):

   ```bash
   docker build -t ghcr.io/martinroot/bigpu-coldstart/z-image:latest image/z-image
   docker push ghcr.io/martinroot/bigpu-coldstart/z-image:latest
   ```

2. Подготовить на контроллере:
   - `VAST_API_KEY` в окружении или файл из `vast_key_file` (по умолчанию `/etc/ft-node/vast_api_key`);
   - приватный SSH-ключ по пути `ssh_key` (публичную часть добавить в аккаунт Vast.ai);
   - `config.example.json` скопировать и подправить под нужный GPU/цену.

3. Запустить:

   ```bash
   python3 scripts/vast_deploy.py search --config config.yaml
   python3 scripts/vast_deploy.py deploy --config config.yaml --state-dir /var/lib/vast-coldstart/state
   python3 scripts/vast_deploy.py status --config config.yaml
   python3 scripts/vast_deploy.py release --config config.yaml
   ```

Секреты не пишутся в state-файлы и не печатаются: `redact()` вырезает ключ из
сообщений об ошибках. Удаление ограничено инстансом с сохранённым ID и ровно
совпадающим label; автоматического повтора `PUT /asks/` нет, чтобы не создать
вторую аренду после неоднозначного сетевого ответа.

## API контейнера

- `GET /healthz` — состояние, модель, движок.
- `GET /v1/models` — список моделей.
- `POST /v1/z-image-spicy` (или `/v1/tasks` с `{"model": ..., "input": {...}}`) — асинхронная задача, `202 Accepted`.
- `GET /v1/tasks/{id}` — статус `queued|processing|succeeded|failed|cancelled`.
- `GET /files/{id}/0.png` — готовый PNG.
- `Idempotency-Key` — повтор того же тела возвращает ту же задачу (`200`), другое тело — `409`.

Пример:

```bash
curl -X POST http://127.0.0.1:8096/v1/z-image-spicy \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a low-poly fox in a snowy forest","width":768,"height":512,"seed":7}'
```

## Что реально проверено, а что нет

✅ Проверено реальным запуском: 9/9 тестов, `bash -n` на `bootstrap.sh`,
`py_compile` всех Python-файлов, полный dry-run деплоя.

⚠️ Не проверено здесь (нет Vast API-ключа и GPU на этой машине): сборка Docker-образа,
загрузка весов с HuggingFace, реальная генерация на CUDA, реальная аренда и SSH
на Vast. Это первый следующий шаг после получения доступа.

## Следующий образ

Добавить новый каталог `image/<model>/`, переиспользовать `vast_deploy.py` с
другим `config.yaml` и endpoint-контрактом. Следующие кандидаты — Qwen-Image и
Wan 2.2 ( открытые веса, но тяжелее по VRAM и времени холодного старта).
