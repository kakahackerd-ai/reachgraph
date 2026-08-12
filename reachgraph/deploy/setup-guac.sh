#!/usr/bin/env bash
# Builds and runs a real GUAC GraphQL server (guacgql) against the Postgres
# instance from docker-compose.postgres.yml, using GUAC's own ent/PostgreSQL
# backend. This is the Phase 1 persistent graph store from the
# implementation plan, stood up for real rather than only described.
#
# Why a build-from-source script instead of a container image or a vendored
# copy: GUAC does not publish a stable "latest" container tag (verified —
# ghcr.io/guacsec/guac:latest does not exist), and vendoring an upstream
# project's full source into this repo would blur exactly the line the
# implementation plan's vendor-abstraction policy draws between "what we
# wrote" and "what we integrate." Building the one binary we need, pinned to
# a real release tag, from the untouched upstream source is the honest
# middle ground.
set -euo pipefail

GUAC_VERSION="${GUAC_VERSION:-v1.1.0}"   # verified buildable: see repo history
GUAC_SRC_DIR="${GUAC_SRC_DIR:-/tmp/guac-src}"
GUAC_BIN="${GUAC_BIN:-/tmp/guacgql}"
GQL_PORT="${GQL_PORT:-9090}"
DB_ADDRESS="${DB_ADDRESS:-postgres://guac:guac@localhost:5432/guac?sslmode=disable}"

echo "==> Starting Postgres"
if docker inspect guac-postgres >/dev/null 2>&1; then
  docker start guac-postgres >/dev/null
else
  docker run -d --name guac-postgres -p 5432:5432 \
    -e POSTGRES_USER=guac -e POSTGRES_PASSWORD=guac -e POSTGRES_DB=guac \
    -v guac-postgres-data:/var/lib/postgresql/data \
    docker.io/library/postgres:16-alpine >/dev/null
fi
echo "==> Waiting for Postgres to be healthy"
for i in $(seq 1 30); do
  if docker exec guac-postgres pg_isready -U guac >/dev/null 2>&1; then break; fi
  sleep 1
done

if [ ! -x "$GUAC_BIN" ]; then
  echo "==> Cloning guacsec/guac @ $GUAC_VERSION"
  rm -rf "$GUAC_SRC_DIR"
  git clone --depth 1 --branch "$GUAC_VERSION" https://github.com/guacsec/guac.git "$GUAC_SRC_DIR"
  echo "==> Building guacgql (this pulls a large dependency set the first time)"
  (cd "$GUAC_SRC_DIR" && go build -o "$GUAC_BIN" ./cmd/guacgql/)
else
  echo "==> Reusing existing build at $GUAC_BIN"
fi

echo "==> Starting guacgql on :$GQL_PORT (ent/PostgreSQL backend, auto-migrating)"
echo "    Once it logs \"starting server\", set:"
echo "      export GUAC_GRAPHQL_URL=http://localhost:$GQL_PORT/query"
echo "    before starting reachgraph to enable Phase 1 persistence."
exec "$GUAC_BIN" --gql-backend=ent --db-address="$DB_ADDRESS" --gql-listen-port="$GQL_PORT"
