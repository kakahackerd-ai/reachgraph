import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graphplatform import GraphWriteService  # noqa: E402
from graphplatform import schema  # noqa: E402
from graphplatform.ingestion.queue import RedisStreamQueue  # noqa: E402
from graphplatform.ingestion.writer import GraphIngestionWriter  # noqa: E402

HYDRA_URI = os.environ.get("HYDRADB_URI", "neo4j://127.0.0.1:7687")
REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DEFAULT_TOKEN_FILE = os.path.join(REPO_ROOT, "setup", "hydra-db-data", "auth-token")
REDIS_URL = os.environ.get("GRAPHPLATFORM_REDIS_URL", "redis://127.0.0.1:6379/0")


def _load_token() -> str:
    token = os.environ.get("HYDRADB_TOKEN")
    if token:
        return token
    token_file = os.environ.get("HYDRADB_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    if os.path.exists(token_file):
        return open(token_file).read().strip()
    raise RuntimeError(
        "No HydraDB auth token found. Set HYDRADB_TOKEN or HYDRADB_TOKEN_FILE, "
        f"or make sure {DEFAULT_TOKEN_FILE} exists (local dev instance)."
    )


@pytest.fixture(scope="session")
def service():
    svc = GraphWriteService(HYDRA_URI, _load_token())
    svc.verify_connectivity()
    yield svc
    svc.close()


@pytest.fixture(scope="session")
def writer(service):
    return GraphIngestionWriter(service)


@pytest.fixture()
def queue():
    q = RedisStreamQueue(REDIS_URL)
    yield q
    q.close()


@pytest.fixture()
def run_id() -> str:
    """A short unique token per test, so parallel/repeat test runs never collide."""
    return uuid.uuid4().hex[:8]


@pytest.fixture()
def cleanup(service):
    """Tests register (label_or_rel_type, a_key, b_key_or_None) tuples here;
    everything registered is DETACH DELETEd after the test, regardless of
    outcome. Node cleanup also removes any relationship attached to it.
    """
    created: list[tuple[str, str]] = []

    def register_node(label: str, key: str) -> None:
        created.append((label, key))

    yield register_node

    for label, key in created:
        if label in schema.NODE_LABELS:
            service._run(f"MATCH (n:{label} {{key:$key}}) DETACH DELETE n", key=key, write=True)
