# ReaderRSS

Персональный новостной агрегатор: сервер собирает RSS, группирует статьи в сюжеты,
суммирует через LLM на русском и отдаёт по REST API; Android-клиент показывает ленту.
Подробное описание, эксплуатация, техдолг и дорожная карта — в `ReaderRSS - readme.md`
(читать его при любом вопросе об устройстве проекта).

## Два репозитория

| Часть | Путь | Ветка |
|---|---|---|
| Бэкенд (Python, FastAPI, PostgreSQL) | `/root/reasderrss` | `main` |
| Android-клиент (Kotlin, Compose) | `/root/newsreader` | `main` |

Это **отдельные git-репозитории**. Задачи про админку, экраны, настройки клиента
почти всегда касаются `/root/newsreader` — искать там, а не только в бэкенде.
Удобный запуск: `claude --add-dir /root/newsreader`.

## Где я работаю: это прод-сервер

Claude Code запущен **на самом прод-сервере** `readerhost`. На нём две копии бэкенда:

| Папка | Роль |
|---|---|
| `~/reasderrss` (`/root/reasderrss`) | **dev-копия**: здесь правлю код, коммичу, `git push` |
| `/opt/reasderrss` | **прод-копия**: `.env` с секретами, отсюда работают контейнеры и `news-worker.timer`; обновляется только `git pull` |

Обе копии нужны — ни одну не удалять. `~/reasderrss` без `.env`, поэтому
`docker compose` из неё ругается на `POSTGRES_*` — команды Compose запускать из `/opt/reasderrss`.

Выкатка после пуша (делает пользователь или я — только с его согласия):

```bash
cd /opt/reasderrss && git pull && docker compose up -d --build && docker compose build worker
```

`docker compose build worker` обязателен: воркер в профиле `worker`, и `up --build` его не
пересобирает. Не использовать `--profile worker up` — он ещё и запускает прогон, который может
пойти параллельно с тиком таймера.

**Что можно самому, без вопросов** — только чтение: `journalctl -u news-worker.service`,
`systemctl status`, `docker compose logs`, `SELECT` через `docker compose exec postgres psql -U news -d news`,
`curl` к `/health` и `/api/v1/meta`. Проверки после прогона смотреть самому, а не просить логи.

**Тесты — тоже без вопросов:** `scripts/run_tests.sh` создаёт временные контейнер
PostgreSQL и сеть `readerrss-test-*` и сам их удаляет; прод-контейнеры, том `pgdata`,
`.env` и таймер не затрагиваются, к LLM запросов нет.

**Только с согласия пользователя** — всё, что меняет прод: `git pull` в `/opt`, сборка и
перезапуск контейнеров, `INSERT/UPDATE/DELETE` и `VACUUM` в БД, правка `.env`, `systemctl`-команды
кроме `status`. Содержимое `.env` не выводить и никуда не отправлять.

Время: `journalctl` — по Москве, поля `TIMESTAMPTZ` в `psql` — в UTC.

## Бэкенд

- `app/api.py` — FastAPI: публичные `/api/v1/stories`, `/api/v1/meta`, админские
  `/api/v1/admin/*` (Bearer `ADMIN_TOKEN`).
- `app/pipeline.py` — прогон: RSS → эмбеддинги (sentence-transformers) + DBSCAN →
  фильтр `min_sources` → дедупликация `story_exists` → LLM → `stories`.
  Запуск `python -m app.pipeline` (systemd `news-worker.timer` на сервере).
- `app/llm.py` — OpenRouter; `app/db.py` — схема и подключение.
- `app/categories.py` — справочник категорий: описания для промпта, какие принимаются,
  русские названия для клиента (`/meta` → `category_titles`). Новая категория — одна строка здесь.
- `rss_reader_old.py` — старый standalone-скрипт, предшественник пайплайна; не используется, оставлен для истории.
- Параметры пайплайна (`pipeline_interval_hours`, `lookback_hours`, `min_sources`, `retention_days`)
  живут в таблице `settings`; дефолты — `DEFAULT_SETTINGS` в `app/db.py`.
  `.env` на них не влияет.
- Прод: VPS `readerhost`, `/opt/reasderrss`, Docker Compose (postgres, api, worker, caddy),
  домен `readerrss.duckdns.org`. Подробнее — раздел «Где я работаю» выше.

## Android-клиент

- `applicationId` `io.github.basilio_kasimov.newsreader` (admin — `.admin`), пакеты кода (`namespace`) —
  `com.example.newsreader`; flavors `public` и `admin`, для разработки — `adminDebug`.
- Стек: Compose + Material 3, Hilt, Retrofit + kotlinx.serialization, Room, DataStore, WorkManager.
- Сетевые интерфейсы: `data/remote/NewsApi.kt`, `data/remote/AdminApi.kt`; DTO — `data/remote/dto/`
  (пакеты `com.example.newsreader.data.remote` и `...data.remote.dto`, совпадают с папками).
- Админка: `ui/admin/AdminScreen.kt`, `ui/admin/AdminViewModel.kt`.
- Версии: AGP 8.13, Gradle 9.5 (9.6 несовместим). Сборка идёт в Android Studio на компьютере
  пользователя (он подтягивает изменения с GitHub); на сервере Android SDK нет — собрать
  и проверить на устройстве нельзя, так и говорить. Поэтому Android-правки пользователь
  проверяет только после коммита и пуша.
- Ловушка Retrofit: generic-параметры `@Body` вида `Map<String, JsonElement>` требуют
  `@JvmSuppressWildcards`, иначе Retrofit отвергает метод до отправки запроса.

## Как работать

- Общение и сообщения коммитов — на русском.
- Коммитить только по просьбе пользователя; в каком репозитории коммит — указывать явно.
- После исправления — обновить раздел «Требуют проверки» в README, если пункт оттуда.

## Тесты — перед каждым коммитом кода сервера

- Перед коммитом любого изменения в `app/` — `scripts/run_tests.sh`; коммитить только
  при зелёном результате. Перед выкаткой — тоже.
- Новое поведение или исправленная ошибка — новый тест в том же коммите
  (`tests/test_llm.py`, `test_api.py`, `test_pipeline.py`; данные — `tests/factories.py`).
  Для исправленного бага тест должен падать на старом коде.
- Тесты не делают настоящих запросов к LLM и в сеть: `httpx.post`, сбор RSS и
  модель эмбеддингов подменяются (`monkeypatch`).
- Код для тестов — из `~/reasderrss`, зависимости — из образа воркера: новую
  библиотеку в `requirements.txt` сначала выкатить, иначе тесты её не увидят.

## Документация — вместе с изменением, а не потом

Любое изменение архитектуры, API, схемы БД, параметров `settings`, переменных `.env`,
поведения пайплайна или экранов приложения — **в том же коммите** обновить:

- `ReaderRSS - readme.md` — соответствующий раздел (модули, таблицы, API, пайплайн,
  конфигурация, экраны, техдолг) и строку «Актуально на»;
- `DECISIONS.md` — запись сверху (дата, проблема, решение, почему; что отвергли),
  если был выбор между вариантами или решение неочевидно из кода;
- `.env.example` — если появилась или изменилась переменная окружения;
- этот файл — если меняется то, что мне нужно знать в начале сессии.

Изменения только в `newsreader` документируются тоже здесь, в `reasderrss`
(README описывает обе части).
