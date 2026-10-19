#!/usr/bin/env bash
#
# build.sh - package the sample app into a versioned release artifact
#
# Usage:   scripts/build.sh [BUILD_NUMBER]
# Output:  dist/release-app-<version>-b<build>.tar.gz (+ .sha256)
#          The artifact path is printed as the LAST line on stdout,
#          all log messages go to stderr.
#
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/app"
DIST_DIR="$ROOT_DIR/dist"

log() { echo "[build] $*" >&2; }
die() { echo "[build] ERROR: $*" >&2; exit 1; }

# --- Inputs ----------------------------------------------------------------
VERSION="$(tr -d '[:space:]' < "$APP_DIR/VERSION")"
BUILD="${1:-${BUILD_NUMBER:-0}}"          # argument > Jenkins env var > 0
BUILT_AT="$(date --iso-8601=seconds)"
RELEASE_ID="${VERSION}-b${BUILD}"

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "invalid version '$VERSION' in app/VERSION (expected X.Y.Z)"
[[ "$BUILD" =~ ^[0-9]+$ ]] \
  || die "invalid build number '$BUILD' (expected digits only)"

log "version=$VERSION build=$BUILD release_id=$RELEASE_ID"

# --- Stage files in a temp dir (removed automatically on exit) --------------
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

cp "$APP_DIR/index.html" "$APP_DIR/health.json" "$STAGE_DIR/"

# Stamp version information into the templates
sed -i \
  -e "s/__VERSION__/${VERSION}/g" \
  -e "s/__BUILD__/${BUILD}/g" \
  -e "s/__BUILT_AT__/${BUILT_AT}/g" \
  "$STAGE_DIR/index.html" "$STAGE_DIR/health.json"

# --- Quality gates ----------------------------------------------------------
if grep -q '__[A-Z_]*__' "$STAGE_DIR"/*; then
  die "unreplaced placeholder found in staged files"
fi
jq empty "$STAGE_DIR/health.json" 2>/dev/null \
  || die "health.json is not valid JSON"

# --- Package ----------------------------------------------------------------
mkdir -p "$DIST_DIR"
ARTIFACT="$DIST_DIR/release-app-${RELEASE_ID}.tar.gz"

tar -czf "$ARTIFACT" -C "$STAGE_DIR" .
( cd "$DIST_DIR" && sha256sum "$(basename "$ARTIFACT")" > "$(basename "$ARTIFACT").sha256" )

log "artifact created: $ARTIFACT"
echo "$ARTIFACT"
