#!/usr/bin/env python3
"""Phase 3 enrichment CLI.

Examples:
    # GUAC / Socket -- per package/version, graceful no-op if unconfigured
    python scripts/enrich.py guac sync npm lodash 4.17.21
    python scripts/enrich.py socket sync npm lodash 4.17.21

    # version-introduction and maintainer-resolution are reactive consumers
    # on their own consumer groups -- replay the existing registry/advisory
    # streams (see scripts/ingest.py) rather than the graph directly.
    python scripts/enrich.py version-introduction consume --max-events 200
    python scripts/enrich.py maintainer-resolution consume --max-events 200
    python scripts/enrich.py maintainer-resolution fuzzy-candidates

    # typosquat is a periodic batch job over the real ingested package set
    python scripts/enrich.py typosquat run npm
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graphplatform import GraphWriteService  # noqa: E402
from graphplatform.enrichment.guac import GUACAdapter  # noqa: E402
from graphplatform.enrichment.maintainer_resolution import MaintainerResolutionService  # noqa: E402
from graphplatform.enrichment.socket import SocketAdapter  # noqa: E402
from graphplatform.enrichment.typosquat import TyposquatService  # noqa: E402
from graphplatform.enrichment.version_introduction import VersionIntroductionService  # noqa: E402
from graphplatform.ingestion.events import STREAM_ADVISORY, STREAM_REGISTRY  # noqa: E402
from graphplatform.ingestion.queue import RedisStreamQueue  # noqa: E402

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DEFAULT_TOKEN_FILE = os.path.join(REPO_ROOT, "setup", "hydra-db-data", "auth-token")


def _load_hydra_token() -> str:
    token = os.environ.get("HYDRADB_TOKEN")
    if token:
        return token
    return open(os.environ.get("HYDRADB_TOKEN_FILE", DEFAULT_TOKEN_FILE)).read().strip()


def _service() -> GraphWriteService:
    uri = os.environ.get("HYDRADB_URI", "neo4j://127.0.0.1:7687")
    svc = GraphWriteService(uri, _load_hydra_token())
    svc.verify_connectivity()
    return svc


def _queue() -> RedisStreamQueue:
    return RedisStreamQueue(os.environ.get("GRAPHPLATFORM_REDIS_URL", "redis://127.0.0.1:6379/0"))


def cmd_guac(args: argparse.Namespace) -> None:
    svc = _service()
    adapter = GUACAdapter(svc)
    print(f"guac configured: {adapter.configured}")
    if args.action == "sync":
        ok = adapter.sync_enrichment(args.ecosystem, args.name, args.version)
        print(f"synced: {ok}")
    adapter.close()
    svc.close()


def cmd_socket(args: argparse.Namespace) -> None:
    svc = _service()
    adapter = SocketAdapter(svc)
    print(f"socket configured: {adapter.configured}")
    if args.action == "sync":
        ok = adapter.sync_enrichment(args.ecosystem, args.name, args.version)
        print(f"synced: {ok}")
    adapter.close()
    svc.close()


def cmd_version_introduction(args: argparse.Namespace) -> None:
    svc = _service()
    vi = VersionIntroductionService(svc)
    q = _queue()
    total = 0

    def handle_registry(event: dict) -> None:
        nonlocal total
        vi.record_publish(event)
        total += 1

    def handle_advisory(event: dict) -> None:
        nonlocal total
        vi.detect_introduction(event)
        total += 1

    remaining = args.max_events
    q.subscribe(
        STREAM_REGISTRY, "version-introduction", "consumer-1", handle_registry,
        stop_after_idle_reads=args.stop_after_idle, block_ms=2000, max_messages=remaining,
    )
    if remaining is not None:
        remaining = max(0, remaining - total)
    if remaining != 0:
        q.subscribe(
            STREAM_ADVISORY, "version-introduction", "consumer-1", handle_advisory,
            stop_after_idle_reads=args.stop_after_idle, block_ms=2000, max_messages=remaining,
        )
    print(f"processed {total} events")
    svc.close()
    q.close()


def cmd_maintainer_resolution(args: argparse.Namespace) -> None:
    svc = _service()
    mr = MaintainerResolutionService(svc)
    if args.action == "fuzzy-candidates":
        for c in mr.find_fuzzy_candidates(threshold=args.threshold):
            print(c)
        svc.close()
        return

    q = _queue()
    total = 0

    def handler(event: dict) -> None:
        nonlocal total
        mr.process_publish(event)
        total += 1

    q.subscribe(
        STREAM_REGISTRY, "maintainer-resolution", "consumer-1", handler,
        stop_after_idle_reads=args.stop_after_idle, block_ms=2000, max_messages=args.max_events,
    )
    print(f"processed {total} events")
    svc.close()
    q.close()


def cmd_typosquat(args: argparse.Namespace) -> None:
    svc = _service()
    ts = TyposquatService(svc)
    flagged = ts.run_once(args.ecosystem)
    print(f"flagged {len(flagged)} candidate(s):")
    for f in flagged:
        print(f"  {f['candidate']} ~ {f['popular']}  score={f['score']}  method={f['method']}")
    svc.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_guac = sub.add_parser("guac")
    p_guac.add_argument("action", choices=["sync"])
    p_guac.add_argument("ecosystem")
    p_guac.add_argument("name")
    p_guac.add_argument("version")
    p_guac.set_defaults(func=cmd_guac)

    p_socket = sub.add_parser("socket")
    p_socket.add_argument("action", choices=["sync"])
    p_socket.add_argument("ecosystem")
    p_socket.add_argument("name")
    p_socket.add_argument("version")
    p_socket.set_defaults(func=cmd_socket)

    p_vi = sub.add_parser("version-introduction")
    p_vi.add_argument("action", choices=["consume"])
    p_vi.add_argument("--stop-after-idle", type=int, default=2)
    p_vi.add_argument("--max-events", type=int, default=None)
    p_vi.set_defaults(func=cmd_version_introduction)

    p_mr = sub.add_parser("maintainer-resolution")
    p_mr.add_argument("action", choices=["consume", "fuzzy-candidates"])
    p_mr.add_argument("--stop-after-idle", type=int, default=2)
    p_mr.add_argument("--max-events", type=int, default=None)
    p_mr.add_argument("--threshold", type=float, default=0.85)
    p_mr.set_defaults(func=cmd_maintainer_resolution)

    p_ts = sub.add_parser("typosquat")
    p_ts.add_argument("action", choices=["run"])
    p_ts.add_argument("ecosystem")
    p_ts.set_defaults(func=cmd_typosquat)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
