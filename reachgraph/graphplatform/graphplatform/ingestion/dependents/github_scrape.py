"""GitHub dependents scrape.

`resolve_source_repo` finds a package's GitHub source repo from its real
registry metadata (npm's `repository.url` / PyPI's `info.project_urls`,
both verified by hand: `registry.npmjs.org/is-number` ->
`repository.url = "git+https://github.com/jonschlinkert/is-number.git"`,
`pypi.org/pypi/requests/json` -> `info.project_urls.Source =
"https://github.com/psf/requests"`).

`fetch_dependents` then scrapes `github.com/<owner>/<repo>/network/dependents`
-- there is no public API for this, it is GitHub's own web UI, verified by
hand against real pages (jonschlinkert/is-number, lodash/lodash):
  - each dependent is a `<a data-hovercard-type="repository" href="/OWNER/REPO">`
    inside a `.Box-row[data-test-id="dg-repo-pkg-dependent"]`.
  - pagination is a `Next` link (`.btn.BtnGroup-item` with an
    `?dependents_after=<opaque_cursor>` href) inside
    `div[data-test-selector="pagination"]`; GitHub renders it as a disabled
    `<button>` (not an `<a>`) on the last page, which is how we detect the end.
  - a repo that publishes multiple packages (monorepo) has a `?package_id=`
    filter with an opaque, page-specific id -- not resolved here, so a
    monorepo's dependents list mixes all of its published packages' dependents
    together. Documented limitation, not a bug: disambiguating it would need
    an extra page fetch to map package name -> package_id first.

This is HTML scraping of an undocumented page, not an API: no formal
contract, capped by GitHub itself at roughly the top ~9950 dependents by
star count, and brittle to markup changes. `fetch_dependents` additionally
self-imposes a `max_items` cap (see module docstring in write_service.py's
HydraDB dialect notes on why: the local HydraDB backend's GC has a real
write-volume ceiling, so ingesting a popular package's full reverse-dependency
set is not viable regardless of what GitHub would let us scrape).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx
from lxml import html

log = logging.getLogger("graphplatform.ingestion.dependents.github_scrape")

_USER_AGENT = "ReachGraph/1.0 (+https://github.com/kakahackerd-ai/reachgraph; supply-chain blast-radius research tool)"
_GITHUB_URL_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?(?:[#?].*)?$")

NPM_PACKAGE_URL = "https://registry.npmjs.org/{name}"
PYPI_PACKAGE_URL = "https://pypi.org/pypi/{name}/json"
DEPENDENTS_URL = "https://github.com/{owner}/{repo}/network/dependents"
DEPSDEV_DEPENDENTS_URL = "https://api.deps.dev/v3alpha/systems/{system}/packages/{name}/versions/{version}:dependents"

_DEPSDEV_SYSTEM = {"npm": "npm", "pypi": "pypi"}


@dataclass
class Dependent:
    owner: str
    repo: str

    @property
    def key(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class DependentsPage:
    dependents: list[Dependent] = field(default_factory=list)
    shown: int = 0
    known_total: int | None = None
    direct_known: int | None = None
    indirect_known: int | None = None


def _http_client(http: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if http is not None:
        return http, False
    return httpx.Client(timeout=15.0, headers={"User-Agent": _USER_AGENT}, follow_redirects=True), True


@dataclass
class PackageMetadata:
    ecosystem: str
    name: str
    latest_version: str | None
    source_repo: tuple[str, str] | None  # (owner, repo)


def fetch_package_metadata(ecosystem: str, name: str, *, http: httpx.Client | None = None) -> PackageMetadata | None:
    """One registry-doc fetch, giving both the latest version and the
    best-effort GitHub source repo (None if the registry doesn't list one,
    or it's not hosted on GitHub) -- avoids two round trips for the two
    things a lookup needs from the same document.
    """
    client, owns_client = _http_client(http)
    try:
        if ecosystem == "pypi":
            resp = client.get(PYPI_PACKAGE_URL.format(name=name))
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            info = resp.json().get("info") or {}
            latest_version = info.get("version")
            candidates = list((info.get("project_urls") or {}).values())
            if info.get("home_page"):
                candidates.append(info["home_page"])
        else:
            resp = client.get(NPM_PACKAGE_URL.format(name=name))
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            doc = resp.json()
            latest_version = (doc.get("dist-tags") or {}).get("latest")
            candidates = [(doc.get("repository") or {}).get("url") or ""]
            if doc.get("homepage"):
                candidates.append(doc["homepage"])

        source_repo = None
        for url in candidates:
            m = _GITHUB_URL_RE.search(url or "")
            if m:
                source_repo = (m.group(1), m.group(2))
                break
        return PackageMetadata(ecosystem=ecosystem, name=name, latest_version=latest_version, source_repo=source_repo)
    finally:
        if owns_client:
            client.close()


def resolve_source_repo(ecosystem: str, name: str, *, http: httpx.Client | None = None) -> tuple[str, str] | None:
    """Best-effort (owner, repo) for a package's GitHub source, or None."""
    meta = fetch_package_metadata(ecosystem, name, http=http)
    return meta.source_repo if meta else None


def fetch_dependent_counts(
    ecosystem: str, name: str, version: str, *, http: httpx.Client | None = None
) -> dict[str, int] | None:
    """Real dependent counts from deps.dev -- names are NOT included (see
    module docstring); use alongside fetch_dependents' `shown` count so the
    caller can report "showing N of ~total known".
    """
    system = _DEPSDEV_SYSTEM.get(ecosystem)
    if system is None:
        return None
    client, owns_client = _http_client(http)
    try:
        resp = client.get(DEPSDEV_DEPENDENTS_URL.format(system=system, name=name, version=version))
        if resp.status_code != 200:
            return None
        return resp.json()
    finally:
        if owns_client:
            client.close()


def fetch_dependents(
    owner: str,
    repo: str,
    *,
    max_items: int = 100,
    max_pages: int = 5,
    package_id: str | None = None,
    http: httpx.Client | None = None,
) -> DependentsPage:
    client, owns_client = _http_client(http)
    page = DependentsPage()
    seen: set[str] = set()
    url = DEPENDENTS_URL.format(owner=owner, repo=repo)
    # Only the first request needs an explicit params kwarg (for package_id);
    # every subsequent "Next" href already carries its own full query string,
    # and passing params={} on it would strip that query string right back
    # off -- httpx treats an explicit (even empty) params kwarg as replacing
    # the URL's existing query, not merging with it.
    params: dict[str, str] | None = {"package_id": package_id} if package_id else None

    try:
        for _ in range(max_pages):
            if page.shown >= max_items:
                break
            resp = client.get(url, params=params)
            if resp.status_code != 200:
                log.warning("dependents scrape non-200", extra={"owner": owner, "repo": repo, "status": resp.status_code})
                break
            tree = html.fromstring(resp.text)

            for anchor in tree.xpath('//a[@data-hovercard-type="repository"]'):
                href = (anchor.get("href") or "").strip("/")
                parts = href.split("/")
                if len(parts) != 2:
                    continue
                dep = Dependent(owner=parts[0], repo=parts[1])
                if dep.key in seen:
                    continue
                seen.add(dep.key)
                page.dependents.append(dep)
                page.shown += 1
                if page.shown >= max_items:
                    break

            next_href = None
            for anchor in tree.xpath('//div[@data-test-selector="pagination"]//a'):
                if anchor.text_content().strip() == "Next":
                    next_href = anchor.get("href")
                    break
            if not next_href:
                break
            url = next_href
            params = None
    finally:
        if owns_client:
            client.close()

    return page
