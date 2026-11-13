# DILDO / FineTune Workspace

Рабочий FineTune-портал для **dildo.asia** на Bootstrap и стилях трекера из исходного архива. Python control plane + существующий LoRA trainer + локальный worker + Vast.ai SSH executor. Без сборки фронтенда, CDN и Node-зависимостей.

## Три контура

| Контур | Разделы | Что происходит |
| --- | --- | --- |
| Данные | Datasets, DataAPI | Импорт train/test JSONL, строгая схема, дубликаты, утечка, временное разделение, реестр push-провайдеров |
| Обучение | Training runs, Vast.ai GPU, Workers | Очередь, CPU/CUDA, автоматический GPU-цикл, loss по шагам, прогресс и диагностические журналы |
| Качество и выпуск | Before / after, Chat & test logs, MLL Base, Training criteria | Сравнение Base/LoRA, реальные ответы, серверные критерии допуска, активация и скачивание адаптеров |

Overview объединяет эти три контура. **Explore demo** показывает отдельные иллюстративные данные; операции записи и аренды в нём запрещены. Настоящая рабочая область не содержит выдуманных метрик.

## Быстрый запуск интерфейса

Control API требует только Python 3.10+; torch нужен именно тренеру.

```bash
cd ft-node
FT_DIR="$PWD/local-data" python3 ft_node.py
```

Открыть http://127.0.0.1:8094/ или `/ui`. Старые API `/status`, `/jobs`, `/report/{id}` сохранены. `/` теперь открывает портал; для машинного статуса использовать `/status`.

Для просмотра всех экранов без модели: **Explore demo**. Для локальной проверки данных: **Datasets → Synthetic dataset**. Это создаёт 512/48 синтетических примеров и ничего не обучает.

## Установка на сервер

Исходники размещаются в `/opt/ft-node`, данные — `/var/lib/ft-node`. Не заменять существующее хранилище данных при обновлении. Перед обновлением остановить активные задания и сохранить резервную копию данных и конфигурации.

1. Создать системного пользователя `ft-node`, каталог `/var/lib/ft-node` с владельцем `ft-node:ft-node` и правами `0700`.
2. Распаковать проект в `/opt/ft-node`. Создать `.venv`: `python3 -m venv /opt/ft-node/.venv`.
3. Для локального обучения установить подходящий PyTorch и `pip install -r requirements.txt` внутри `.venv`. Для интерфейса и удалённого Vast executor это не требуется.
4. Скопировать `deploy/ft-node.env.example` в `/etc/ft-node/ft-node.env`, указать настоящий `FT_API_TOKEN`. Файл `root:ft-node`, `0640`. Не использовать значение-заглушку.
5. Установить `ft-node.service` и при необходимости `ft-worker.service` в `/etc/systemd/system`, выполнить `systemctl daemon-reload` и запустить нужные службы.
6. Для локального воркера положить совместимый Qwen3.5 checkpoint в `/opt/ft-node/models/base`. Существующий unit использует `HF_HUB_OFFLINE=1` и лимит памяти 2600M. Для другой модели/нагрузки изменить ограничения осознанно.
7. Настроить HTTPS и Basic authentication в nginx. Шаблон — `deploy/nginx-dildo.conf.example`; создать `/etc/nginx/ft-node.htpasswd`, проверить `nginx -t`, затем reload. HTTPS-сертификат и пароль не создаются автоматически.

API остаётся на loopback. Не выставлять `FT_BIND=0.0.0.0` в production: заголовок `X-FT-Authenticated` доверяется только локальному аутентифицирующему proxy. Nginx перезаписывает этот заголовок. Для внешних клиентов маршрут `/api/*` использует Bearer token, браузерный интерфейс — Basic auth.

## Vast.ai: настройка и работа

В исходном архиве `gpu_vast.py` был привязан к GPU/Comfy endpoint, а «cycle» выполнял только up/health/status. Здесь вместо него реализован FineTune executor по SSH.

### Настройка один раз

- На контроллере нужны `ssh`, `scp`, выход в интернет к Vast API и SSH-серверам.
- API-ключ Vast хранить в `/etc/ft-node/vast_api_key` либо `VAST_API_KEY`. Он не передаётся на GPU.
- Приватный SSH-ключ: `/etc/ft-node/id_ed25519` (`FT_SSH_KEY`); публичную часть заранее добавить в свой аккаунт Vast.ai. Владельцем ключа должен быть `ft-node`, права `0600`.
- Указать GPU image или SSH-совместимый Vast template hash в **Vast.ai GPU**. По умолчанию задан образ PyTorch CUDA; шаблон из старого архива автоматически не используется, поскольку он ожидал другой worker API.
- `FT_REMOTE_MODEL` — checkpoint для удалённого обучения. Закрытые/gated HF-модели и доставка HF-токенов в этом варианте не поддерживаются.
- Папка `known_hosts` создаётся в FT_DIR. SSH использует TOFU (`accept-new`): новый ключ хоста принимается, изменившийся ключ отклоняется.

### Отладочный режим

**Search marketplace → Provision → Health check → Release instance.** Поиск не тратит деньги. Provision арендует выбранный дешёвый подходящий оффер; инстанс остаётся включённым до Release. Health проверяет NVIDIA и PyTorch/CUDA. Debug-инстанс нельзя одновременно использовать для автоматического цикла — сначала освободить его.

### Автоматический режим

**Выбрать dataset и preset → Launch automatic run.**

1. Повторно проверяется датасет, фиксируется отдельная копия для задания.
2. Ищется оффер в пределах GPU/VRAM/price/reliability.
3. Создаётся ровно один отслеживаемый инстанс; ID и уникальный label сохраняются до ожидания готовности.
4. По SSH загружаются trainer, worker, зависимости, JSONL и метаданные.
5. Worker выполняет обучение и одинаковую held-out оценку base/adapter.
6. Контроллер забирает status/progress/metrics/logs, затем полный `result/` с весами адаптера.
7. Свой отслеживаемый инстанс удаляется также при обычных ошибках, отмене и превышении времени.

Лимит времени включает provisioning/install/training и проверяется контроллером; сетевые timeout/cleanup добавляют задержку, поэтому это не жёсткий денежный лимит провайдера. Storage/transfer оплачиваются отдельно. При остановке контроллера, SIGKILL, аварии хоста или недоступности Vast API cleanup не гарантирован: проверить сохранённый instance ID и консоль Vast, затем **Sync / Release**. Автоматического retry аренды после неоднозначного сетевого результата нет: сначала проверить консоль, чтобы не создать вторую аренду. Для гарантированного внешнего ограничения нужен независимый provider-side watchdog.

Не делается массовое удаление инстансов по префиксу. Удаление ограничено конкретным сохранённым ID и точным уникальным label.

## Датасеты и DataAPI

Поддерживается **tracker-policy-v1**, соответствующий приложенному trainer. Это не универсальная система обучения всех архитектур и форматов.

```json
{"input":{"as_of":"2026-01-01T00:00:00+00:00","slices":[{"slice_id":"1290:BR","clicks":300,"conversions":4,"profit_cents":70,"postbacks_mature":true}]},"output":[{"slice_id":"1290:BR","action":"scale","reason":"profit"}]}
```

- 1–3 slices на input; поля и action/reason проверяются функциями самого trainer.
- В `test.jsonl` все timestamps должны быть позже всех train timestamps.
- Повторные input внутри split и пересечение train/test запрещены.
- Датасет с тем же именем нельзя перезаписать: импортировать новую версию.
- На запуск копируются файлы dataset и фиксируется SHA-256 импорта. Старые job/dataset каталоги сохраняются.
- Providers — реестр **push-источников**, не фоновый pull/scheduler. Продюсер отправляет данные сам.

Публичный запрос (тело — JSON с JSONL-строками):

```bash
curl --fail-with-body https://dildo.asia/api/datasets/import \
  -H "Authorization: Bearer $FT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @dataset-payload.json
```

DataAPI workbench отправляет настоящий запрос к локальному API портала: validation без записи либо import с созданием версии. Максимальное тело: 16 MiB. API debugger не делает произвольные внешние HTTP-запросы.

## Пресеты и критерии

| Preset | Steps | Gradient accumulation | Held-out examples |
| --- | ---: | ---: | ---: |
| smoke | 12 | 1 | до 4 |
| standard | 160 | 4 | до 16 |

LoRA rank 8, alpha 16, learning rate 2e-4, max length 640, seed 2026. Обучается Qwen3.5 text decoder. Параметры отражены в UI; произвольный выбор архитектуры не имитируется.

Активация проверяется **на сервере**, а не только в браузере: завершённый job, наличие весов, минимум тестов (8 по умолчанию), JSON validity (100%), slice accuracy (80%) и отсутствие регрессии к base. Smoke не проходит минимальное количество тестов по умолчанию. Локальные изменения критериев сохраняются, актуальные критерии применяются при активации. Предыдущий active ID сохраняется.

`MLL Base → Download` возвращает ZIP с adapter, job.json и report.json. Это **LoRA adapter**, не слитый полный checkpoint. Активация меняет pointer для `worker.py --predict`, но не разворачивает публичный inference endpoint.

`Chat & test logs` — записи настоящей evaluation (до 100 примеров), включая raw output при невалидном ответе. Интерактивного online-chat/inference в этом релизе нет.

## API

Для внешнего nginx-маршрута добавить `/api` к следующим путям.

| Метод | Путь | Действие |
| --- | --- | --- |
| GET | `/status` | Воркеры, модели, datasets, jobs, settings, GPU |
| GET | `/jobs` | История запусков |
| GET | `/progress/{job}` | Прогресс, loss series, tail лога |
| GET | `/report/{job}` | Base/LoRA отчёт + актуальные release gates |
| GET | `/export/{job}` | Скачать JSON отчёта |
| GET | `/artifacts/{job}` | Скачать адаптер + metadata ZIP |
| GET | `/gpu/state` | Сохранённый GPU state / offers / operations |
| GET | `/operations/{id}` | Статус и журнал GPU операции |
| POST | `/datasets/validate` | `{"name":"id"}` либо `{"train":"…","test":"…"}` |
| POST | `/datasets/import` | `{"name","provider","train","test"}` |
| POST | `/datasets/demo` | Создать синтетический demo dataset |
| POST | `/providers/save` | `{"name","description"}` |
| POST | `/jobs/queue` | `{"dataset","preset":"smoke|standard","device":"cpu|cuda"}` |
| POST | `/jobs/cancel` | `{"job":"id"}` |
| POST | `/jobs/activate` | `{"job":"id"}` |
| POST | `/settings` | `{"criteria":{…}}` / `{"gpu":{…}}` |
| POST | `/gpu/search,status,health,up` | `{}`; асинхронная операция |
| POST | `/gpu/down` | `{"instance_id":123}` — подтверждение сохранённого ID |
| POST | `/gpu/cycle` | `{"dataset","preset"}` — платный автоматический запуск |

## Проверка и ограничения этой поставки

```bash
python3 -m unittest discover -s tests -v
node --check assets/ftstudio.js
python3 -m py_compile ft_node.py gpu_vast.py worker.py trainer.py
```

Тесты используют временные каталоги и mock Vast; не арендуют GPU. Проверяются validation/leakage/duplicates, immutable import/snapshot, release gates, отсутствие артефакта, heartbeat, settings, отмена, структурные метрики, HTTP/assets/auth и GPU cleanup/ownership. UI проверяется отдельно в браузере.

В этой сборочной среде не запускалось фактическое обучение Qwen и не было оплаченной аренды Vast.ai. Для окончательной проверки на вашем аккаунте нужны ключ API, SSH-ключ с доступом в Vast и smoke-запуск. На dildo.asia проект автоматически не установлен: архив содержит код и конфигурационные примеры для вашего сервера.

## Файлы

- `ft_node.py` — control plane и раздача UI.
- `assets/index.html`, `ftstudio.js`, `portal.css` — портал.
- `assets/bootstrap-6.min.css`, `workspace.css` — исходные Bootstrap/стили трекера, сохранены.
- `trainer.py` — LoRA + evaluation + `metrics.jsonl`.
- `worker.py` — локальная очередь и удалённый `--executor vast --once`.
- `gpu_vast.py` — provisioning/SSH/collect/cleanup.
- `deploy/` — env и nginx примеры.
- `tests/` — проверки без платных ресурсов.

## Источники интеграции

Пути поиска/создания/удаления инстансов сверены с официальной [Vast.ai REST API](https://vast.ai/developers/api). Формат SSH-инстанса сопоставлен с [официальным CLI](https://github.com/vast-ai/vast-python). Проверка контракта API не заменяет smoke-запуск на конкретном аккаунте и образе.
