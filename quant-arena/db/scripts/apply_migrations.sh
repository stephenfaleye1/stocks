#!/bin/sh
# Creates the quant_trading database + app role on the existing Postgres server
# (idempotent), then applies /db/migrations/*.sql in lexical order, each in its
# own transaction, recording filename + sha256 in schema_migrations.
set -eu

: "${PGHOST:?}" "${PGPORT:=5432}" "${PG_ADMIN_USER:?}" "${PG_ADMIN_PASSWORD:?}"
: "${QUANT_DB_NAME:=quant_trading}" "${QUANT_DB_USER:=quant_app}" "${QUANT_DB_PASSWORD:?}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-/db/migrations}"

case "$QUANT_DB_NAME$QUANT_DB_USER" in
  *[!A-Za-z0-9_]*) echo "DB/user names must be [A-Za-z0-9_]" >&2; exit 2;;
esac

export PGUSER="$PG_ADMIN_USER" PGPASSWORD="$PG_ADMIN_PASSWORD"
PSQL="psql -v ON_ERROR_STOP=1 -X -q"
export PGOPTIONS="-c client_min_messages=warning"

echo "==> Waiting for Postgres at $PGHOST:$PGPORT"
i=0
until pg_isready -h "$PGHOST" -p "$PGPORT" -q; do
  i=$((i+1)); [ "$i" -ge 30 ] && { echo "Postgres not reachable" >&2; exit 1; }
  sleep 2
done

echo "==> Ensuring role $QUANT_DB_USER"
$PSQL -d postgres -v role="$QUANT_DB_USER" -v pw="$QUANT_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'role', :'pw') \gexec
SQL

echo "==> Ensuring database $QUANT_DB_NAME"
$PSQL -d postgres -v db="$QUANT_DB_NAME" -v role="$QUANT_DB_USER" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
SQL

# Migrations run as the app role so it owns every object it creates.
export PGUSER="$QUANT_DB_USER" PGPASSWORD="$QUANT_DB_PASSWORD" PGDATABASE="$QUANT_DB_NAME"

$PSQL <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  sha256     TEXT NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
SQL

for f in $(ls "$MIGRATIONS_DIR"/*.sql | sort); do
  name=$(basename "$f")
  sum=$(sha256sum "$f" | cut -d' ' -f1)
  applied=$(echo "SELECT sha256 FROM schema_migrations WHERE filename = '$name'" | $PSQL -tA)
  if [ -n "$applied" ]; then
    if [ "$applied" != "$sum" ]; then
      echo "!! $name was modified after being applied (checksum mismatch). Add a new migration instead." >&2
      exit 3
    fi
    echo "    skip  $name"
    continue
  fi
  echo "    apply $name"
  { echo "BEGIN;"; cat "$f"; echo "INSERT INTO schema_migrations (filename, sha256) VALUES ('$name', '$sum');"; echo "COMMIT;"; } | $PSQL
done

echo "==> Migrations complete"
