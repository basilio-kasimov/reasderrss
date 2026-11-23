#!/usr/bin/env bash
# Запуск тестов на одноразовой инфраструктуре, отдельно от прода.
#
# Что делает:
#   1. создаёт отдельную docker-сеть и временный PostgreSQL (данные в tmpfs, без тома);
#   2. собирает тестовый образ (Dockerfile.test = образ воркера + pytest; по кэшу —
#      мгновенно) и запускает pytest в одноразовом контейнере, код — из ЭТОЙ папки;
#   3. удаляет контейнеры и сеть — даже если тесты упали.
#
# Рабочие контейнеры, том pgdata, .env и таймер не затрагиваются.
#
# Использование:  scripts/run_tests.sh [аргументы pytest]
#   scripts/run_tests.sh                 — все тесты
#   scripts/run_tests.sh tests/test_llm.py -k truncated
set -euo pipefail

cd "$(dirname "$0")/.."
SRC="$(pwd)"
ID="readerrss-test-$$"
NET="$ID-net"
DB="$ID-db"
BASE_IMAGE="${BASE_IMAGE:-reasderrss-worker}"
TEST_IMAGE="reasderrss-test"

cleanup() {
    docker rm -f "$DB" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Собираю тестовый образ (обычно из кэша)..."
docker build -q -f Dockerfile.test --build-arg BASE_IMAGE="$BASE_IMAGE" \
    -t "$TEST_IMAGE" . >/dev/null

docker network create "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" \
    -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
    --tmpfs /var/lib/postgresql/data \
    postgres:16 >/dev/null

echo "Жду тестовый PostgreSQL..."
for _ in $(seq 1 30); do
    if docker exec "$DB" pg_isready -U test -d test >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

docker run --rm --network "$NET" \
    -v "$SRC":/src:ro -w /src \
    -e DATABASE_URL="postgresql://test:test@$DB:5432/test" \
    -e OPENROUTER_API_KEY=test-key-not-real \
    -e ADMIN_TOKEN=test-admin-token \
    -e PYTHONDONTWRITEBYTECODE=1 \
    --entrypoint python "$TEST_IMAGE" -m pytest -p no:cacheprovider "$@"
