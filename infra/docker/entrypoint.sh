#!/bin/sh
# Container entry point: bring the database up to date, then serve.
#
# Migrations run here rather than in the image build because the database lives in the volume, not in
# the image: a new image against an existing volume must migrate it before the API starts. The step is
# idempotent (the runner records applied versions) and NATASHA_SKIP_MIGRATIONS=1 skips it when an
# operator runs migrations out of band.
set -eu

: "${NATASHA_HOME:=/data}"
: "${NATASHA_HOST:=0.0.0.0}"
: "${NATASHA_PORT:=8000}"
export NATASHA_HOME

echo "[entrypoint] NATASHA_HOME=${NATASHA_HOME} bind=${NATASHA_HOST}:${NATASHA_PORT}"

mkdir -p "${NATASHA_HOME}"
if [ ! -f "${NATASHA_HOME}/config/natasha.toml" ] && [ -w "${NATASHA_HOME}" ]; then
  # First run: write the machine's settings once so an operator can edit them in the volume.
  python3 -c "from natasha.core.config import load_settings; print('config at', load_settings().save())" || true
fi

if [ "${NATASHA_SKIP_MIGRATIONS:-0}" != "1" ]; then
  echo "[entrypoint] applying migrations"
  python3 -m natasha.cli migrate up
fi

exec python3 -m apps.server --host "${NATASHA_HOST}" --port "${NATASHA_PORT}"
