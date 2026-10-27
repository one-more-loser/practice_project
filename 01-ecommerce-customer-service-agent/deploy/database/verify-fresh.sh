#!/usr/bin/env bash
set -Eeuo pipefail
root=/home/zzh/ecommerce-customer-service-db
name=cs01-db-initcheck
volume=cs01_db_pg17_initcheck
image=pgvector/pgvector:0.8.7-pg17-bookworm@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d
if docker container inspect "$name" >/dev/null 2>&1 || docker volume inspect "$volume" >/dev/null 2>&1; then
  echo 'Scratch target already exists; refuse to overwrite'; exit 1
fi
cleanup() {
  if test "$(docker inspect --format '{{index .Config.Labels "com.codex.owner"}}' "$name" 2>/dev/null || true)" = cs01-db-initcheck; then docker rm -f "$name" >/dev/null; fi
  if test "$(docker volume inspect --format '{{index .Labels "com.codex.owner"}}' "$volume" 2>/dev/null || true)" = cs01-db-initcheck; then docker volume rm "$volume" >/dev/null; fi
}
trap cleanup EXIT
docker volume create --label com.codex.owner=cs01-db-initcheck "$volume" >/dev/null
docker run -d --name "$name" --label com.codex.owner=cs01-db-initcheck --network none --memory 512m --cpus 1 \
  --mount "type=volume,source=$volume,target=/var/lib/postgresql/data" \
  --mount "type=bind,source=$root/initdb,target=/docker-entrypoint-initdb.d,readonly" \
  --mount "type=bind,source=$root/secrets/bootstrap_password,target=/run/secrets/bootstrap_password,readonly" \
  --mount "type=bind,source=$root/secrets/migrator_password,target=/run/secrets/migrator_password,readonly" \
  --mount "type=bind,source=$root/secrets/app_password,target=/run/secrets/app_password,readonly" \
  -e POSTGRES_USER=cs01_bootstrap -e POSTGRES_DB=postgres \
  -e POSTGRES_PASSWORD_FILE=/run/secrets/bootstrap_password \
  -e 'POSTGRES_INITDB_ARGS=--encoding=UTF8 --locale=C --auth-host=scram-sha-256' \
  "$image" postgres -c shared_buffers=64MB -c max_connections=20 >/dev/null
ready=false
for i in $(seq 1 45); do
  if docker exec "$name" pg_isready -h 127.0.0.1 -U cs01_bootstrap -d postgres >/dev/null 2>&1; then ready=true; break; fi
  sleep 1
done
test "$ready" = true
for db in cs_agent_b0 cs_agent_b0_test cs_agent_b0_upgrade; do
 docker exec "$name" sh -c 'export PGPASSWORD="$(cat /run/secrets/app_password)"; psql -h 127.0.0.1 -U cs_agent_app_b0 -d "$1" -v ON_ERROR_STOP=1 -tAc "SELECT length(instance_id) FROM cs_project_guard"' sh "$db"
done
docker exec "$name" sh -c 'export PGPASSWORD="$(cat /run/secrets/migrator_password)"; psql -h 127.0.0.1 -U cs_agent_owner_b0 -d cs_agent_b0 -v ON_ERROR_STOP=1 -tAc "SELECT current_user, current_setting(chr(100)||chr(97)||chr(116)||chr(97)||chr(95)||chr(100)||chr(105)||chr(114)||chr(101)||chr(99)||chr(116)||chr(111)||chr(114)||chr(121))"'
echo FRESH_INITIALIZATION_AUTH_AND_GUARD_PASSED
