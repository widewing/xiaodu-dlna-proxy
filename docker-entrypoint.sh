#!/bin/sh
set -eu

HTTP_PORT="${HTTP_PORT:-18080}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

set -- \
  python3 /app/proxy_upnp.py \
  --upstream-friendly-name "${UPSTREAM_FRIENDLY_NAME:?UPSTREAM_FRIENDLY_NAME is required}" \
  --http-port "$HTTP_PORT" \
  --log-level "$LOG_LEVEL"

if [ -n "${FIXED_UUID:-}" ]; then
  set -- "$@" --fixed-uuid "$FIXED_UUID"
fi

if [ -n "${ADVERTISE_HOST:-}" ]; then
  set -- "$@" --advertise-host "$ADVERTISE_HOST"
fi

if [ -n "${BIND_HOST:-}" ]; then
  set -- "$@" --bind-host "$BIND_HOST"
fi

if [ -n "${CACHE_TTL:-}" ]; then
  set -- "$@" --cache-ttl "$CACHE_TTL"
fi

if [ -n "${REQUEST_TIMEOUT:-}" ]; then
  set -- "$@" --request-timeout "$REQUEST_TIMEOUT"
fi

if [ -n "${DISCOVERY_TIMEOUT:-}" ]; then
  set -- "$@" --discovery-timeout "$DISCOVERY_TIMEOUT"
fi

if [ -n "${SSDP_MAX_AGE:-}" ]; then
  set -- "$@" --ssdp-max-age "$SSDP_MAX_AGE"
fi

if [ -n "${NOTIFY_INTERVAL:-}" ]; then
  set -- "$@" --notify-interval "$NOTIFY_INTERVAL"
fi

exec "$@"
