#!/usr/bin/env bash
set -euo pipefail

backend_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
container_name="camino-pytest-db-$$"
host_port="${CAMINO_PYTEST_DB_PORT:-55432}"

cleanup() {
  docker stop "${container_name}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if docker ps -a --format '{{.Names}}' | grep -qx "${container_name}"; then
  echo "A container named ${container_name} already exists." >&2
  exit 1
fi

docker run --rm \
  --name "${container_name}" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_USER=test_runner \
  -e POSTGRES_DB=testdb \
  -p "127.0.0.1:${host_port}:5432" \
  -d pgvector/pgvector:pg16 >/dev/null

ready=false
for _attempt in {1..30}; do
  if docker exec "${container_name}" \
    pg_isready -U test_runner -d testdb >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done

if [[ "${ready}" != "true" ]]; then
  echo "Disposable PostgreSQL did not become ready within 30 seconds." >&2
  exit 1
fi

cd "${backend_dir}"
TEST_DATABASE_URL="postgresql://test_runner@127.0.0.1:${host_port}/testdb" \
  uv run pytest "$@"
