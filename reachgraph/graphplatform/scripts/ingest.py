#!/usr/bin/env python3
"""Ingestion CLI.

Registry connectors always publish onto the real event queue (Redis
Streams) rather than writing HydraDB directly -- run `consume` in a second
process/terminal to actually drain the queue into the graph. This mirrors
the real intended shape (an independent fetcher process and an independent
writer process), not a shortcut for the demo.

Manifest discovery has no queue in front of it by design (see
graphplatform/README.md) -- it writes directly through GraphWriteService.

Examples:
    # terminal 1: drain the registry stream into HydraDB as events arrive
    python scripts/ingest.py consume --stop-after-idle 3

    # terminal 2: real bounded npm/PyPI backfill for a handful of packages
    python scripts/ingest.py registry npm backfill lodash express is-number
    python scripts/ingest.py registry pypi backfill requests flask

    # manifest discovery against a real checked-out repo
    python scripts/ingest.py manifest /path/to/repo --org some-org --repo some-repo
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graphplatform import GraphWriteService  # noqa: E402
from graphplatform.ingestion.events import STREAM_REGISTRY  # noqa: E402
from graphplatform.ingestion.manifest.service import discover_and_ingest  # noqa: E402
from graphplatform.ingestion.queue import RedisStreamQueue  # noqa: E402
from graphplatform.ingestion.registry.npm import NpmConnector  # noqa: E402
from graphplatform.ingestion.registry.pypi import PyPIConnector  # noqa: E402
from graphplatform.ingestion.writer import GraphIngestionWriter  # noqa: E402

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DEFAULT_TOKEN_FILE = os.path.join(REPO_ROOT, "setup", "hydra-db-data", "auth-token")


def _load_hydra_token() -> str:
    token = os.environ.get("HYDRADB_TOKEN")
    if token:
        return token
    token_file = os.environ.get("HYDRADB_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    return open(token_file).read().strip()


def _service() -> GraphWriteService:
    uri = os.environ.get("HYDRADB_URI", "neo4j://127.0.0.1:7687")
    svc = GraphWriteService(uri, _load_hydra_token())
    svc.verify_connectivity()
    return svc


def _queue() -> RedisStreamQueue:
    return RedisStreamQueue(os.environ.get("GRAPHPLATFORM_REDIS_URL", "redis://127.0.0.1:6379/0"))


def cmd_registry(args: argparse.Namespace) -> None:
    conn = NpmConnector() if args.registry == "npm" else PyPIConnector()
    q = _queue()
    try:
        if args.mode == "backfill":
            events = conn.backfill(args.packages)
        elif args.registry == "npm":
            events = conn.fetch_or_subscribe(since=args.since or conn.current_seq(), max_iterations=args.max_iterations)
        else:
            events = conn.fetch_or_subscribe(
                since_serial=args.since or conn.current_serial(), max_iterations=args.max_iterations
            )
        n = q.publish_all(STREAM_REGISTRY, (e.to_dict() for e in events))
        print(f"published {n} {args.registry} events to {STREAM_REGISTRY}")
    finally:
        conn.close()
        q.close()


def cmd_consume(args: argparse.Namespace) -> None:
    svc = _service()
    writer = GraphIngestionWriter(svc)
    q = _queue()
    total = 0

    def handler(event: dict) -> None:
        nonlocal total
        writer.handle(event)
        total += 1

    q.subscribe(
        STREAM_REGISTRY,
        "graphwriter",
        "consumer-1",
        handler,
        stop_after_idle_reads=args.stop_after_idle,
        block_ms=2000,
        max_messages=args.max_events,
    )
    print(f"wrote {total} events into HydraDB")
    svc.close()
    q.close()


def cmd_manifest(args: argparse.Namespace) -> None:
    svc = _service()
    try:
        result = discover_and_ingest(args.repo_path, args.org, args.repo, svc)
    finally:
        svc.close()
    print(f"discovered {len(result.sub_packages)} sub-package(s):")
    for sp in result.sub_packages:
        label = sp.subpath or "(root)"
        print(f"  {label} [{sp.ecosystem}] -- {len(sp.resolved)} resolved deps, manifests: {sp.manifest_files}")
    if result.stub_manifests_found:
        print(f"unparsed-ecosystem manifests found (TODO): {result.stub_manifests_found}")


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_registry = sub.add_parser("registry", help="npm/PyPI registry ingestion -> queue")
    p_registry.add_argument("registry", choices=["npm", "pypi"])
    p_registry.add_argument("mode", choices=["backfill", "live"])
    p_registry.add_argument("packages", nargs="*", help="package names (backfill mode)")
    p_registry.add_argument("--since", type=int, default=None, help="live mode: seq/serial to resume from")
    p_registry.add_argument("--max-iterations", type=int, default=1, help="live mode: bounded poll loops")
    p_registry.set_defaults(func=cmd_registry)

    p_consume = sub.add_parser("consume", help="drain the queue into HydraDB via GraphIngestionWriter")
    p_consume.add_argument("--stop-after-idle", type=int, default=2, help="stop after N consecutive empty polls")
    p_consume.add_argument(
        "--max-events", type=int, default=None, help="stop after N total events written (see README's known limitations)"
    )
    p_consume.set_defaults(func=cmd_consume)

    p_manifest = sub.add_parser("manifest", help="manifest discovery -> HydraDB (no queue)")
    p_manifest.add_argument("repo_path")
    p_manifest.add_argument("--org", required=True)
    p_manifest.add_argument("--repo", required=True)
    p_manifest.set_defaults(func=cmd_manifest)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
