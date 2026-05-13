#!/bin/sh
set -eu

: "${LABRASTRO_AUTH_TOKEN_SECRET:?LABRASTRO_AUTH_TOKEN_SECRET is required}"
: "${LABRASTRO_SUPERADMIN_USERNAME:?LABRASTRO_SUPERADMIN_USERNAME is required}"
: "${LABRASTRO_SUPERADMIN_PASSWORD:?LABRASTRO_SUPERADMIN_PASSWORD is required}"
: "${LABRASTRO_SANDBOX_HOST_BASE_URL:?LABRASTRO_SANDBOX_HOST_BASE_URL is required}"

CONFIG_PATH="${RCODER_CONFIG_PATH:-/app/.rcoder/config.host.yaml}"
CONFIG_DIR="$(dirname "$CONFIG_PATH")"

mkdir -p "$CONFIG_DIR"
if [ -z "${RCODER_CONFIG_PATH:-}" ] || [ ! -f "$CONFIG_PATH" ]; then
  envsubst < /app/docker/config.host.yaml.template > "$CONFIG_PATH"
fi

if [ -n "${LABRASTRO_DATABASE_URL:-}" ] && [ "${LABRASTRO_AUTO_MIGRATE:-true}" = "true" ]; then
  rcoder --config "$CONFIG_PATH" db migrate
fi

exec rcoder --config "$CONFIG_PATH" --server
