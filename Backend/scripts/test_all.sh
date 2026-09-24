#!/usr/bin/env bash
set -euo pipefail

backend_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
container_name="camino-pytest-db-$$"
container_id=""
host_port="${CAMINO_PYTEST_DB_PORT:-55432}"

cleanup() {
  if [[ -n "${container_id}" ]]; then
    docker stop "${container_id}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

container_id="$(docker run --rm \
  --name "${container_name}" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_USER=test_runner \
  -e POSTGRES_DB=testdb \
  -p "127.0.0.1:${host_port}:5432" \
  -d pgvector/pgvector:pg16)"

ready=false
for _attempt in {1..30}; do
  if docker exec "${container_id}" \
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
# The disposable container is isolated from the application database. Mask the
# caller's database identity so a coincidental name match cannot trip the
# integration fixture's deliberately conservative safety check.
DATABASE_URL="postgresql://localhost/camino_application_sentinel" \
TEST_DATABASE_URL="postgresql://test_runner@127.0.0.1:${host_port}/testdb" \
  uv run pytest "$@"
