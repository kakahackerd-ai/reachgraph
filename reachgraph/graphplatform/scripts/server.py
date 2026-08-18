#!/usr/bin/env python3
"""Run ReachGraph Product API Server."""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graphplatform import GraphWriteService
from graphplatform.product.api import run_api_server

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DEFAULT_TOKEN_FILE = os.path.join(REPO_ROOT, "setup", "hydra-db-data", "auth-token")


def _load_hydra_token() -> str:
    token = os.environ.get("HYDRADB_TOKEN")
    if token:
        return token
    return open(os.environ.get("HYDRADB_TOKEN_FILE", DEFAULT_TOKEN_FILE)).read().strip()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Host address")
    parser.add_argument("--port", type=int, default=8081, help="Port to listen on")
    args = parser.parse_args()

    uri = os.environ.get("HYDRADB_URI", "neo4j://127.0.0.1:7687")
    token = _load_hydra_token()
    svc = GraphWriteService(uri, token)
    svc.verify_connectivity()

    server = run_api_server(svc, host=args.host, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.server_close()
        svc.close()


if __name__ == "__main__":
    main()
