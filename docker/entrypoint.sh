#!/bin/sh
set -eu

: "${RCODER_MODEL:?RCODER_MODEL is required}"
: "${RCODER_BASE_URL:?RCODER_BASE_URL is required}"
: "${RCODER_API_KEY:?RCODER_API_KEY is required}"
: "${LABRASTRO_AUTH_TOKEN_SECRET:?LABRASTRO_AUTH_TOKEN_SECRET is required}"
: "${LABRASTRO_SUPERADMIN_USERNAME:?LABRASTRO_SUPERADMIN_USERNAME is required}"
: "${LABRASTRO_SUPERADMIN_PASSWORD_HASH:?LABRASTRO_SUPERADMIN_PASSWORD_HASH is required}"

CONFIG_PATH="${RCODER_CONFIG_PATH:-/app/.rcoder/config.host.yaml}"
CONFIG_DIR="$(dirname "$CONFIG_PATH")"

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_PATH" ]; then
  envsubst < /app/docker/config.host.yaml.template > "$CONFIG_PATH"
fi

if [ -n "${LABRASTRO_DATABASE_URL:-}" ] && [ "${LABRASTRO_AUTO_MIGRATE:-true}" = "true" ]; then
  rcoder --config "$CONFIG_PATH" db migrate
fi

exec rcoder --config "$CONFIG_PATH" --server
