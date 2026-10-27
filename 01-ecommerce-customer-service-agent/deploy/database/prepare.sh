#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "$0")"
umask 077
mkdir -p secrets backups initdb
chmod 700 secrets backups
for name in bootstrap_password migrator_password app_password; do
  if test -e "secrets/$name"; then
    echo 'Existing credentials found; refusing to overwrite. Use the current deployment files.' >&2
    exit 1
  fi
done
if test -e initdb/instance-id; then
  echo 'Existing instance marker found; refusing to replace.' >&2
  exit 1
fi
for name in bootstrap_password migrator_password app_password; do
  od -An -N36 -tx1 /dev/urandom | tr -d ' \n' > "secrets/$name"
  chmod 444 "secrets/$name"
done
od -An -N16 -tx1 /dev/urandom | tr -d ' \n' > initdb/instance-id
chmod 755 initdb
chmod 644 initdb/instance-id
echo 'Project-local secrets and instance marker prepared; no values printed.'
