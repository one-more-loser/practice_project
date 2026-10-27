#!/usr/bin/env bash
set -Eeuo pipefail

# 本脚本只在本项目新卷首次初始化时执行；不删除库或改写已有业务数据。
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
  'cs_agent_owner_b0', rtrim(pg_read_file('/run/secrets/migrator_password'),chr(10)||chr(13))) \gexec
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
  'cs_agent_app_b0', rtrim(pg_read_file('/run/secrets/app_password'),chr(10)||chr(13))) \gexec
GRANT pg_read_all_settings TO cs_agent_owner_b0;
CREATE DATABASE cs_agent_b0 OWNER cs_agent_owner_b0;
CREATE DATABASE cs_agent_b0_test OWNER cs_agent_owner_b0;
CREATE DATABASE cs_agent_b0_upgrade OWNER cs_agent_owner_b0;
SQL

for db in cs_agent_b0 cs_agent_b0_test cs_agent_b0_upgrade; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<SQL
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE $db FROM PUBLIC;
GRANT CONNECT ON DATABASE $db TO cs_agent_owner_b0,cs_agent_app_b0;
GRANT USAGE ON SCHEMA public TO cs_agent_owner_b0,cs_agent_app_b0;
CREATE EXTENSION vector;
CREATE TABLE cs_project_guard(instance_id text PRIMARY KEY);
INSERT INTO cs_project_guard VALUES ('$(tr -d '\r\n' < /docker-entrypoint-initdb.d/instance-id)');
GRANT SELECT ON cs_project_guard TO cs_agent_owner_b0,cs_agent_app_b0;
SQL
done
