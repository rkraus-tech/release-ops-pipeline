#!/usr/bin/env bash
#
# deploy.sh - deploy, roll back and inspect releases of release-app
#
# Usage:
#   deploy.sh deploy   <path/to/release-app-<id>.tar.gz>
#   deploy.sh rollback [release_id]      # default: previous release
#   deploy.sh status
#
# Layout:
#   $APP_ROOT/releases/<release_id>/   one directory per release
#   $APP_ROOT/current -> releases/<id> symlink served by nginx
#
# Exit codes:
#   0  success
#   1  error before anything changed (bad input, checksum, lock, ...)
#   2  new release failed the health check -> automatically rolled back
#   3  CRITICAL: health check failed and rollback failed / impossible
#
set -Eeuo pipefail

# --- Configuration (override via environment) -------------------------------
APP_ROOT="${APP_ROOT:-/opt/release-app}"
HEALTH_URL="${HEALTH_URL:-http://localhost/health}"
HEALTH_RETRIES="${HEALTH_RETRIES:-5}"
HEALTH_DELAY="${HEALTH_DELAY:-2}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"
LOG_FILE="${LOG_FILE:-/var/log/release-app/deploy.log}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASES_DIR="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
LOCK_FILE="$APP_ROOT/.deploy.lock"

# --- Logging ----------------------------------------------------------------
log() {
  local level="$1"; shift
  local line
  line="$(date --iso-8601=seconds) [deploy] $level $*"
  echo "$line" >&2
  if [[ -w "$LOG_FILE" || ( ! -e "$LOG_FILE" && -w "$(dirname "$LOG_FILE")" ) ]]; then
    echo "$line" >> "$LOG_FILE"
  fi
}
die() { log ERROR "$*"; exit 1; }

# --- Helpers ----------------------------------------------------------------
require_tools() {
  local t
  for t in curl jq tar sha256sum flock; do
    command -v "$t" >/dev/null 2>&1 || die "required tool missing: $t"
  done
}

acquire_lock() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another deployment is running (lock: $LOCK_FILE)"
}

current_release() {
  if [[ -L "$CURRENT_LINK" ]]; then
    basename "$(readlink "$CURRENT_LINK")"
  fi
}

# All valid releases, oldest -> newest (version-aware sort, skips failed ones)
list_releases() {
  find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d \
       ! -name '*.failed' ! -name '*.tmp' -printf '%f\n' | sort -V
}

# Atomically point "current" at a release
switch_to() {
  local id="$1"
  ln -sfn "releases/$id" "$APP_ROOT/current.tmp"
  mv -Tf "$APP_ROOT/current.tmp" "$CURRENT_LINK"
  log INFO "current -> releases/$id"
}

# Is the given release really live? Compares /health with the release's own health.json
health_check() {
  local id="$1" expected_version expected_build attempt response
  expected_version="$(jq -r '.version' "$RELEASES_DIR/$id/health.json")"
  expected_build="$(jq -r '.build' "$RELEASES_DIR/$id/health.json")"

  for (( attempt = 1; attempt <= HEALTH_RETRIES; attempt++ )); do
    if response="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null)" \
       && [[ "$(jq -r '.status'  <<<"$response" 2>/dev/null)" == "ok" ]] \
       && [[ "$(jq -r '.version' <<<"$response" 2>/dev/null)" == "$expected_version" ]] \
       && [[ "$(jq -r '.build'   <<<"$response" 2>/dev/null)" == "$expected_build" ]]; then
      log INFO "health check passed for $id (attempt $attempt/$HEALTH_RETRIES)"
      return 0
    fi
    log WARN "health check attempt $attempt/$HEALTH_RETRIES failed for $id"
    sleep "$HEALTH_DELAY"
  done
  log ERROR "health check FAILED for $id after $HEALTH_RETRIES attempts"
  return 1
}

# Keep the newest $KEEP_RELEASES releases, never delete the current one
cleanup_old_releases() {
  local current old
  current="$(current_release)"
  mapfile -t old < <(list_releases | head -n -"$KEEP_RELEASES")
  for id in "${old[@]}"; do
    [[ "$id" == "$current" ]] && continue
    rm -rf "${RELEASES_DIR:?}/$id"
    log INFO "removed old release $id"
  done
}

# Optional hook: record the result (e.g. in a database). Never fails the deploy.
record() {
  local hook="$SCRIPT_DIR/record-deploy.sh"
  if [[ -x "$hook" ]]; then
    "$hook" "$@" || log WARN "could not record result via $(basename "$hook")"
  fi
}

# --- Commands ---------------------------------------------------------------
cmd_deploy() {
  local artifact="${1:-}" id previous started duration tmp
  [[ -n "$artifact" ]]  || die "usage: deploy.sh deploy <artifact.tar.gz>"
  [[ -f "$artifact" ]]  || die "artifact not found: $artifact"

  id="$(basename "$artifact" .tar.gz)"
  id="${id#release-app-}"
  [[ "$id" =~ ^[0-9]+\.[0-9]+\.[0-9]+-b[0-9]+$ ]] || die "cannot parse release id from '$artifact'"

  started=$SECONDS
  previous="$(current_release)"
  log INFO "deploy $id requested (current: ${previous:-none})"

  # 1. Integrity check
  [[ -f "$artifact.sha256" ]] || die "checksum file missing: $artifact.sha256"
  ( cd "$(dirname "$artifact")" && sha256sum --quiet -c "$(basename "$artifact").sha256" ) \
    || die "checksum mismatch for $artifact"
  log INFO "checksum OK"

  # 2. Unpack into its own release directory (via .tmp, then rename = atomic)
  [[ ! -e "$RELEASES_DIR/$id" ]] || die "release $id already exists"
  tmp="$RELEASES_DIR/$id.tmp"
  rm -rf "$tmp"
  mkdir -p "$tmp"
  tar -xzf "$artifact" -C "$tmp"
  chmod -R u=rwX,go=rX "$tmp"          # never trust permissions inside an artifact
  mv "$tmp" "$RELEASES_DIR/$id"
  log INFO "unpacked to releases/$id"

  # 3. Switch + verify
  switch_to "$id"
  if health_check "$id"; then
    duration=$(( SECONDS - started ))
    log INFO "DEPLOY SUCCESS $id in ${duration}s"
    record deploy "$id" success "$duration" "$previous"
    cleanup_old_releases
    return 0
  fi

  # 4. Automatic rollback
  duration=$(( SECONDS - started ))
  if [[ -z "$previous" ]]; then
    log ERROR "DEPLOY FAILED $id and there is no previous release to roll back to"
    record deploy "$id" failed "$duration" ""
    exit 3
  fi

  log WARN "rolling back to $previous"
  switch_to "$previous"
  mv "$RELEASES_DIR/$id" "$RELEASES_DIR/$id.failed"
  if health_check "$previous"; then
    duration=$(( SECONDS - started ))
    log ERROR "DEPLOY FAILED $id - rolled back to $previous in ${duration}s"
    record deploy "$id" rolled_back "$duration" "$previous"
    exit 2
  fi

  duration=$(( SECONDS - started ))
  log ERROR "CRITICAL: rollback to $previous failed as well - manual action required"
  record deploy "$id" failed "$duration" "$previous"
  exit 3
}

cmd_rollback() {
  local target="${1:-}" current started duration
  started=$SECONDS
  current="$(current_release)"
  [[ -n "$current" ]] || die "nothing deployed yet - nothing to roll back"

  if [[ -z "$target" ]]; then
    # previous = highest release id below the current one
    target="$(list_releases | grep -vxF "$current" \
              | { cat; echo "$current"; } | sort -V | grep -B1 -xF "$current" | head -n 1)"
    [[ -n "$target" && "$target" != "$current" ]] || die "no older release available"
  fi
  [[ -d "$RELEASES_DIR/$target" ]] || die "release not found: $target"
  [[ "$target" != "$current" ]]    || die "$target is already current"

  log INFO "rollback $current -> $target requested"
  switch_to "$target"
  if health_check "$target"; then
    duration=$(( SECONDS - started ))
    log INFO "ROLLBACK SUCCESS $current -> $target in ${duration}s"
    record rollback "$target" success "$duration" "$current"
    return 0
  fi

  log ERROR "rollback target $target is unhealthy - restoring $current"
  switch_to "$current"
  duration=$(( SECONDS - started ))
  record rollback "$target" failed "$duration" "$current"
  exit 3
}

cmd_status() {
  local current id marker
  current="$(current_release)"
  echo "current: ${current:-<none>}"
  echo "releases (oldest -> newest):"
  while read -r id; do
    [[ -n "$id" ]] || continue
    marker="  "; [[ "$id" == "$current" ]] && marker="* "
    echo "  $marker$id"
  done < <(list_releases)
  if compgen -G "$RELEASES_DIR/*.failed" >/dev/null; then
    echo "failed releases:"
    find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -name '*.failed' -printf '    %f\n' | sort -V
  fi
}

# --- Main -------------------------------------------------------------------
main() {
  local cmd="${1:-}"; shift || true
  require_tools
  [[ -d "$RELEASES_DIR" ]] || die "releases directory missing: $RELEASES_DIR"

  case "$cmd" in
    deploy)   acquire_lock; cmd_deploy "$@" ;;
    rollback) acquire_lock; cmd_rollback "$@" ;;
    status)   cmd_status ;;
    *) echo "usage: $(basename "$0") {deploy <artifact>|rollback [release_id]|status}" >&2; exit 1 ;;
  esac
}

main "$@"
