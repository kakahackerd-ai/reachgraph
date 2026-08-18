"""GUAC (Graph for Understanding Artifact Composition) enrichment adapter.

Treated as optional external enrichment, per the phase-3 brief: every
public method here catches its own failures, logs clearly, and degrades to
a no-op ([] / False / None) rather than raising -- a GUAC outage or missing
config must never take down the rest of the pipeline.

**A real finding, not an assumption**: GUAC's own docs (docs.guac.sh,
verified by hand) document exactly one query surface -- a GraphQL API at
`<endpoint>/query` (default `http://localhost:8080/query`, playground at
`/`) -- and exactly one *documented* ingestion path: the `guacone`/
`guaccollect` CLI tools reading SBOM files, not an HTTP or GraphQL
endpoint. There is no documented `IngestSBOM`-style mutation a third-party
service is meant to call directly. So `submit_sbom` here does the real
thing GUAC actually supports: writes the generated SBOM to a temp file and
shells out to `guacone collect files <path>` (binary path configurable,
see GUAC_GUACONE_BIN) -- and, exactly like an unreachable GUAC_ENDPOINT,
treats a missing/failing `guacone` binary as a normal degrade-and-log
case, not an error. `query_vulnerabilities`/`query_dependencies` use the
real, documented GraphQL query shapes (CertifyVuln / IsDependency).

None of this has been exercised against a live GUAC instance in this
environment (none is running here) -- the query/mutation shapes below are
built from GUAC's public docs, not hand-verified round trips the way
phase 1/2's HydraDB and registry/advisory work was. Flagged, not smoothed
over.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any

import httpx

from ..write_service import GraphWriteService

log = logging.getLogger("graphplatform.enrichment.guac")

CERTIFY_VULN_QUERY = """
query CertifyVuln($pkgType: String!, $namespace: String, $name: String!, $version: String) {
  CertifyVuln(certifyVulnSpec: {
    package: { type: $pkgType, namespace: $namespace, name: $name, version: $version }
  }) {
    vulnerability { ... on OSV { osvId } ... on CVE { cveId } ... on GHSA { ghsaId } }
    metadata { timeScanned dbUri collector }
  }
}
"""

IS_DEPENDENCY_QUERY = """
query IsDependency($pkgType: String!, $namespace: String, $name: String!, $version: String) {
  IsDependency(isDependencySpec: {
    package: { type: $pkgType, namespace: $namespace, name: $name, version: $version }
  }) {
    dependencyPackage { type namespaces { namespace names { name versions { version } } } }
    versionRange
  }
}
"""


def _purl(ecosystem: str, name: str, version: str) -> str:
    """Best-effort purl construction -- not independently verified against
    the purl spec's full escaping rules for every edge case (e.g. npm
    scoped packages' `@` segment), just the common, widely-accepted form.
    """
    return f"pkg:{ecosystem}/{name}@{version}"


def _purl_to_pkgspec(ecosystem: str, name: str, version: str) -> dict[str, str | None]:
    """GUAC's PkgSpec is type/namespace/name/version/subpath/qualifiers.
    npm scoped packages (name starting with '@scope/') map the scope onto
    namespace, per how GUAC's own examples decompose purls -- unverified
    against a live instance, see module docstring.
    """
    pkg_type = "pypi" if ecosystem == "pypi" else ecosystem
    namespace = None
    if "/" in name:
        namespace, name = name.split("/", 1)
    return {"pkgType": pkg_type, "namespace": namespace, "name": name, "version": version}


class GUACAdapter:
    def __init__(
        self,
        write_service: GraphWriteService,
        *,
        endpoint: str | None = None,
        guacone_bin: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._svc = write_service
        self._endpoint = endpoint if endpoint is not None else os.environ.get("GUAC_ENDPOINT")
        self._guacone_bin = guacone_bin if guacone_bin is not None else os.environ.get("GUAC_GUACONE_BIN", "guacone")
        self._http = http or httpx.Client(timeout=15.0)

    @property
    def configured(self) -> bool:
        return bool(self._endpoint)

    def close(self) -> None:
        self._http.close()

    # -- SBOM generation ---------------------------------------------------

    def generate_sbom(self, app_key: str, org: str, repo: str, resolved: dict[str, str], ecosystem: str) -> dict[str, Any]:
        """A minimal, valid CycloneDX 1.5 document for one Application's
        resolved dependency set. `resolved` is {name: version}, same shape
        Manifest Discovery (phase 2) produces.
        """
        return {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "version": 1,
            "metadata": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "component": {"type": "application", "name": f"{org}/{repo}", "bom-ref": app_key},
            },
            "components": [
                {
                    "type": "library",
                    "name": name,
                    "version": version,
                    "purl": _purl(ecosystem, name, version),
                    "bom-ref": _purl(ecosystem, name, version),
                }
                for name, version in resolved.items()
            ],
        }

    # -- submission (guacone CLI -- see module docstring) -------------------

    def submit_sbom(self, sbom: dict[str, Any]) -> bool:
        if not self.configured:
            log.info("guac: not configured (GUAC_ENDPOINT unset) -- skipping SBOM submission")
            return False
        if shutil.which(self._guacone_bin) is None:
            log.warning(
                "guac: guacone binary not found -- skipping SBOM submission",
                extra={"guacone_bin": self._guacone_bin},
            )
            return False
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".cdx.json", delete=False) as f:
                json.dump(sbom, f)
                path = f.name
            result = subprocess.run(
                [self._guacone_bin, "collect", "files", path],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "GUAC_GQL_ADDR": self._endpoint} if self._endpoint else os.environ,
            )
            if result.returncode != 0:
                log.warning(
                    "guac: guacone collect files failed",
                    extra={"returncode": result.returncode, "stderr": result.stderr[-500:]},
                )
                return False
            log.info("guac: submitted SBOM via guacone", extra={"components": len(sbom.get("components", []))})
            return True
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("guac: SBOM submission failed", extra={"error": str(e)})
            return False
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- querying ------------------------------------------------------------

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
        if not self.configured:
            return None
        try:
            resp = self._http.post(f"{self._endpoint}/query", json={"query": query, "variables": variables})
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("guac: GraphQL request failed -- degrading to no enrichment", extra={"error": str(e)})
            return None
        if body.get("errors"):
            log.warning("guac: GraphQL returned errors -- degrading to no enrichment", extra={"errors": body["errors"]})
            return None
        return body.get("data")

    def query_vulnerabilities(self, ecosystem: str, name: str, version: str) -> list[dict[str, Any]]:
        data = self._graphql(CERTIFY_VULN_QUERY, _purl_to_pkgspec(ecosystem, name, version))
        if not data:
            return []
        return data.get("CertifyVuln") or []

    def query_dependencies(self, ecosystem: str, name: str, version: str) -> list[dict[str, Any]]:
        data = self._graphql(IS_DEPENDENCY_QUERY, _purl_to_pkgspec(ecosystem, name, version))
        if not data:
            return []
        return data.get("IsDependency") or []

    # -- sync enrichment onto existing graph nodes ----------------------------

    def sync_enrichment(self, ecosystem: str, name: str, version: str) -> bool:
        """Pull GUAC's correlated vuln data for one Version and annotate the
        existing node (never creates one -- see annotate_version). Returns
        True if GUAC was reachable and had data, False on any degrade path
        (not configured, unreachable, no data).
        """
        version_key = f"{ecosystem}:{name}@{version}"
        vulns = self.query_vulnerabilities(ecosystem, name, version)
        if not vulns:
            return False
        self._svc.annotate_version(
            version_key,
            guac_vuln_count=len(vulns),
            guac_last_synced_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info("guac: synced enrichment", extra={"version_key": version_key, "vuln_count": len(vulns)})
        return True
