#!/usr/bin/env bash
#
# record-deploy.sh - write one deployment result into PostgreSQL
#
# Called automatically by deploy.sh (hook), but can also be run by hand:
#   record-deploy.sh <action> <release_id> <status> <duration_s> [previous_release]
#
#   action      deploy | rollback
#   status      success | failed | rolled_back
#
# Authentication: peer auth over the local Unix socket - the Linux user
# (e.g. "jenkins") logs in as the PostgreSQL role of the same name.
# No password is stored anywhere.
#
set -Eeuo pipefail

DB_NAME="${DB_NAME:-releases}"
export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-5}"

if [[ $# -lt 4 ]]; then
  echo "usage: $(basename "$0") <action> <release_id> <status> <duration_s> [previous_release]" >&2
  exit 1
fi

action="$1"
release_id="$2"
status="$3"
duration="$4"
previous="${5:-}"
version="${release_id%-b*}"               # 1.1.0-b2 -> 1.1.0
build_url="${BUILD_URL:-}"                # set automatically inside Jenkins

# Values are passed as psql variables and quoted with :'var' -> no SQL injection.
psql -X -q -v ON_ERROR_STOP=1 -d "$DB_NAME" \
     -v action="$action" \
     -v release_id="$release_id" \
     -v version="$version" \
     -v status="$status" \
     -v duration="$duration" \
     -v previous="$previous" \
     -v build_url="$build_url" <<'SQL'
INSERT INTO deploy_history
       (action, release_id, version, status, duration_s, previous_release, build_url)
VALUES (:'action', :'release_id', :'version', :'status', :'duration'::integer,
        NULLIF(:'previous', ''), NULLIF(:'build_url', ''));
SQL

echo "$(date --iso-8601=seconds) [record] INFO recorded $action $release_id $status in database $DB_NAME" >&2
