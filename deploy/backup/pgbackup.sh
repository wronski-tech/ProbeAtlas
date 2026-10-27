#!/usr/bin/env bash
# ProbeAtlas PostgreSQL backups: logical dumps, restores, and restore drills.
#
#   deploy/backup/pgbackup.sh backup [DIR]            # write DIR/probeatlas-<UTC timestamp>.dump (default DIR=backups)
#   deploy/backup/pgbackup.sh restore FILE [DBNAME]   # restore into a NEW database (default probeatlas_restore)
#   deploy/backup/pgbackup.sh drill [DIR]             # backup, restore into a scratch DB, compare row counts, drop it
#
# Target selection:
#   * PGURL (libpq URL, e.g. postgresql://user:pass@host:5432/probeatlas?sslmode=require) uses the local
#     pg_dump/pg_restore/psql binaries (client major version >= server major version).
#   * Otherwise the tools run inside the Docker Compose `db` service (local development).
#
# Dumps use the custom format (compressed, restorable table by table) without owners or ACLs, so they
# restore into any role. restore never overwrites an existing database.
set -euo pipefail
# Dumps contain password hashes and webhook signing secrets: keep them private.
umask 077

COMPOSE=${COMPOSE:-docker compose}
DB_USER=${DB_USER:-probeatlas}
DB_NAME=${DB_NAME:-probeatlas}
# Tables whose row counts the drill compares (the ones that hold customer data).
TABLES=(users workspaces memberships api_keys monitors alert_rules incidents incident_events
        notification_channels agents jobs results audit_events)

die() { echo "error: $*" >&2; exit 1; }

# pg TOOL [DBNAME] ARGS...: run a PostgreSQL client tool against DBNAME (stdin is passed through).
pg() {
  local tool=$1 db=$2; shift 2
  if [[ -n "${PGURL:-}" ]]; then
    "$tool" --dbname="$(url_with_db "$db")" "$@"
  else
    $COMPOSE exec -T db "$tool" --username="$DB_USER" --dbname="$db" "$@"
  fi
}

# url_with_db DBNAME: PGURL with its database path replaced by DBNAME (query string kept).
url_with_db() {
  local base=${PGURL%%\?*} query=""
  [[ "$PGURL" == *\?* ]] && query="?${PGURL#*\?}"
  echo "${base%/*}/$1$query"
}

source_db() { if [[ -n "${PGURL:-}" ]]; then local b=${PGURL%%\?*}; echo "${b##*/}"; else echo "$DB_NAME"; fi; }

backup() {
  local dir=${1:-backups} file
  mkdir -p "$dir"
  file="$dir/probeatlas-$(date -u +%Y%m%dT%H%M%SZ).dump"
  pg pg_dump "$(source_db)" --format=custom --no-owner --no-acl > "$file.partial"
  # A dump that pg_restore cannot list is corrupt or truncated: never keep it under the final name.
  pg pg_restore postgres --list < "$file.partial" > /dev/null || die "dump verification failed: $file.partial"
  mv "$file.partial" "$file"
  echo "$file"
}

restore() {
  local file=${1:-} target=${2:-probeatlas_restore}
  [[ -f "$file" ]] || die "usage: $0 restore FILE [DBNAME]"
  [[ "$target" =~ ^[a-z_][a-z0-9_]*$ ]] || die "invalid database name: $target"
  local exists
  exists=$(pg psql postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$target'")
  [[ -z "$exists" ]] || die "database $target already exists; restore never overwrites (drop or rename it first)"
  pg psql postgres -qc "CREATE DATABASE \"$target\""
  pg pg_restore "$target" --no-owner --no-acl --exit-on-error < "$file"
  echo "restored $file into database $target"
}

counts() { # counts DBNAME: one "table rows" line per table in TABLES
  local db=$1 sql="" t
  for t in "${TABLES[@]}"; do sql+="SELECT '$t', count(*) FROM $t UNION ALL "; done
  pg psql "$db" -tAF ' ' -c "${sql% UNION ALL }"
}

drill() {
  local dir=${1:-backups} file scratch="probeatlas_restore_drill" before after
  file=$(backup "$dir")
  before=$(counts "$(source_db)")
  pg psql postgres -q -c "SET client_min_messages = warning" -c "DROP DATABASE IF EXISTS $scratch"
  restore "$file" "$scratch" > /dev/null
  after=$(counts "$scratch")
  pg psql "$scratch" -tAc "SELECT version_num FROM alembic_version" | sed 's/^/schema revision: /'
  pg psql postgres -qc "DROP DATABASE $scratch"
  echo "$after" | sed 's/^/  /'
  # Rows written between the dump and the count (live system) can make the source larger; never smaller.
  if [[ "$before" != "$after" ]]; then
    echo "row counts differ between source (taken after the dump) and restore:" >&2
    diff <(echo "$before") <(echo "$after") >&2 || true
    echo "drill: restore OK, but verify the differences above are only rows written after the dump" >&2
  else
    echo "drill passed: $file restores and every table matches the source row count"
  fi
}

case "${1:-}" in
  backup) shift; backup "$@" ;;
  restore) shift; restore "$@" ;;
  drill) shift; drill "$@" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
