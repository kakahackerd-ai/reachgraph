"""Graceful-degradation tests for the GUAC and Socket adapters -- the
phase-3 brief explicitly requires proving these two never crash the
pipeline when unconfigured or unreachable. No live GUAC/Socket instance is
available in this environment, so these tests exercise exactly the path a
real deployment without those services configured would take.
"""

import httpx

from graphplatform.enrichment.guac import GUACAdapter
from graphplatform.enrichment.socket import SocketAdapter


def test_guac_unconfigured_degrades_cleanly(service):
    adapter = GUACAdapter(service, endpoint=None)
    assert adapter.configured is False
    assert adapter.query_vulnerabilities("npm", "lodash", "4.17.21") == []
    assert adapter.query_dependencies("npm", "lodash", "4.17.21") == []
    assert adapter.sync_enrichment("npm", "lodash", "4.17.21") is False
    sbom = adapter.generate_sbom("app-key", "org", "repo", {"lodash": "4.17.21"}, "npm")
    assert adapter.submit_sbom(sbom) is False  # not configured -- must not raise or attempt a subprocess
    adapter.close()


def test_guac_unreachable_endpoint_degrades_cleanly(service):
    adapter = GUACAdapter(service, endpoint="http://127.0.0.1:1", http=httpx.Client(timeout=1.0))
    assert adapter.configured is True
    assert adapter.query_vulnerabilities("npm", "lodash", "4.17.21") == []
    assert adapter.sync_enrichment("npm", "lodash", "4.17.21") is False
    adapter.close()


def test_guac_missing_guacone_binary_degrades_cleanly(service):
    adapter = GUACAdapter(service, endpoint="http://127.0.0.1:8080", guacone_bin="this-binary-does-not-exist-xyz")
    sbom = adapter.generate_sbom("app-key", "org", "repo", {"lodash": "4.17.21"}, "npm")
    assert adapter.submit_sbom(sbom) is False
    adapter.close()


def test_socket_unconfigured_degrades_cleanly(service):
    adapter = SocketAdapter(service, api_key=None, org_slug=None)
    assert adapter.configured is False
    assert adapter.get_risk("npm", "lodash", "4.17.21") is None
    assert adapter.sync_enrichment("npm", "lodash", "4.17.21") is False
    adapter.close()


def test_socket_missing_org_slug_only_is_still_unconfigured(service):
    adapter = SocketAdapter(service, api_key="fake-key", org_slug=None)
    assert adapter.configured is False
    adapter.close()


def test_socket_bad_credentials_degrades_cleanly(service):
    # configured, but with a fake key/org -- the real Socket API rejects
    # this (a genuine HTTP error path), which must still degrade cleanly
    # rather than raise.
    adapter = SocketAdapter(service, api_key="fake-key-not-real", org_slug="fake-org-not-real", min_interval_s=0)
    assert adapter.configured is True
    assert adapter.get_risk("npm", "lodash", "4.17.21") is None
    adapter.close()
