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

## Бэкенд

- `app/api.py` — FastAPI: публичные `/api/v1/stories`, `/api/v1/meta`, админские
  `/api/v1/admin/*` (Bearer `ADMIN_TOKEN`).
- `app/pipeline.py` — прогон: RSS → эмбеддинги (sentence-transformers) + DBSCAN →
  фильтр `min_sources` → дедупликация `story_exists` → LLM → `stories`.
  Запуск `python -m app.pipeline` (systemd `news-worker.timer` на сервере).
- `app/llm.py` — OpenRouter; `app/db.py` — схема и подключение.
- `rss_reader.py` — старый standalone-скрипт, предшественник пайплайна; не основной код.
- Параметры пайплайна (`pipeline_interval_hours`, `lookback_hours`, `min_sources`, `retention_days`)
  живут в таблице `settings`; дефолты — `DEFAULT_SETTINGS` в `app/db.py`.
  `.env` на них не влияет.
- Прод: VPS, `/opt/reasderrss`, Docker Compose (postgres, api, worker, caddy),
  домен `readerrss.duckdns.org`. Локально в этом окружении сервер не запущен.

## Android-клиент

- Пакет `com.example.newsreader`; flavors `public` и `admin`, для разработки — `adminDebug`.
- Стек: Compose + Material 3, Hilt, Retrofit + kotlinx.serialization, Room, DataStore, WorkManager.
- Сетевые интерфейсы: `data/remote/NewsApi.kt`, `data/remote/dto/AdminApi.kt`
  (пакеты `data.remote` и `remote` исторически разъехались).
- Админка: `ui/admin/AdminScreen.kt`, `ui/admin/AdminViewModel.kt`.
- Версии: AGP 8.13, Gradle 9.5 (9.6 несовместим). Сборка обычно идёт в Android Studio
  у пользователя; здесь собрать и проверить на устройстве нельзя — так и говорить.
- Ловушка Retrofit: generic-параметры `@Body` вида `Map<String, JsonElement>` требуют
  `@JvmSuppressWildcards`, иначе Retrofit отвергает метод до отправки запроса.

## Как работать

- Общение и сообщения коммитов — на русском.
- Коммитить только по просьбе пользователя; в каком репозитории коммит — указывать явно.
- После исправления — обновить раздел «Требуют проверки» в README, если пункт оттуда.
