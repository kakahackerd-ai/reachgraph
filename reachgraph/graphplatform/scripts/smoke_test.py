#!/usr/bin/env python3
"""Phase 1 smoke test: create a Package, a Version, and a Maintainer, link
them with PUBLISHED_BY, read the whole subgraph back, and print it.

Run:
    cd graphplatform && source .venv/bin/activate
    python scripts/smoke_test.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graphplatform import GraphWriteService  # noqa: E402

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DEFAULT_TOKEN_FILE = os.path.join(REPO_ROOT, "setup", "hydra-db-data", "auth-token")


def load_token() -> str:
    token = os.environ.get("HYDRADB_TOKEN")
    if token:
        return token
    token_file = os.environ.get("HYDRADB_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    return open(token_file).read().strip()


def main() -> None:
    uri = os.environ.get("HYDRADB_URI", "neo4j://127.0.0.1:7687")
    service = GraphWriteService(uri, load_token())
    service.verify_connectivity()
    print(f"connected to {uri}")

    now = dt.datetime.now(dt.timezone.utc)
    published_at = dt.datetime(2024, 4, 1, tzinfo=dt.timezone.utc)

    pkg_key = "npm:smoke-test-pkg"
    ver_key = "npm:smoke-test-pkg@1.0.0"
    maintainer_key = "npm:maintainer:smoke-test"

    print("\n-- writing --")
    created = service.upsert_package(pkg_key, "npm", "smoke-test-pkg", first_observed_at=now, event_time=now)
    print(f"upsert_package({pkg_key!r}) created={created}")

    created = service.upsert_version(
        ver_key, pkg_key, "1.0.0", first_observed_at=now, event_time=published_at
    )
    print(f"upsert_version({ver_key!r}) created={created}")

    created = service.upsert_maintainer(
        maintainer_key, "npm", "smoke-test@example.com", first_observed_at=now, event_time=now
    )
    print(f"upsert_maintainer({maintainer_key!r}) created={created}")

    created = service.write_published_by(ver_key, maintainer_key, first_observed_at=now, event_time=published_at)
    print(f"write_published_by({ver_key!r} -> {maintainer_key!r}) created={created}")

    print("\n-- reading the subgraph back (consistency=strong) --")
    subgraph = {
        "package": service.get_package(pkg_key, consistency="strong"),
        "version": service.get_version(ver_key, consistency="strong"),
        "maintainer": service.get_maintainer(maintainer_key, consistency="strong"),
        "published_by": service.get_maintainers_of(ver_key, consistency="strong"),
    }
    print(json.dumps(subgraph, indent=2))

    service.close()
    print("\nsmoke test OK")


if __name__ == "__main__":
    main()
