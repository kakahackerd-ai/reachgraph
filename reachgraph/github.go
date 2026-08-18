package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"time"
)

// githubClient talks to the real GitHub REST API and raw content host.
// Unauthenticated requests work for public repos but are capped at 60/hour;
// setting GITHUB_TOKEN (read at request time, not cached) raises that to
// 5000/hour and unlocks the Dependabot alerts endpoint, which requires auth
// even on public repositories — verified by hand before writing this client,
// not assumed.
type githubClient struct {
	http *http.Client
}

func newGitHubClient() *githubClient {
	return &githubClient{http: &http.Client{Timeout: 20 * time.Second}}
}

func (c *githubClient) token() string { return getGitHubToken() }

func (c *githubClient) authedRequest(ctx context.Context, method, url string) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, method, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	if t := c.token(); t != "" {
		req.Header.Set("Authorization", "Bearer "+t)
	}
	return req, nil
}

// defaultBranch returns the repository's default branch, e.g. "main".
func (c *githubClient) defaultBranch(ctx context.Context, owner, repo string) (string, error) {
	req, err := c.authedRequest(ctx, http.MethodGet, fmt.Sprintf("https://api.github.com/repos/%s/%s", url.PathEscape(owner), url.PathEscape(repo)))
	if err != nil {
		return "", err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("github: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return "", fmt.Errorf("repository %s/%s not found (or private without a token)", owner, repo)
	}
	if resp.StatusCode == http.StatusUnauthorized {
		return "", fmt.Errorf("github rejected the configured GITHUB_TOKEN (401) — check that it's valid and unexpired")
	}
	if resp.StatusCode == http.StatusForbidden {
		// Fallback for unauthenticated rate limit: probe raw.githubusercontent.com
		if c.probeRaw(ctx, owner, repo, "main") {
			return "main", nil
		}
		if c.probeRaw(ctx, owner, repo, "master") {
			return "master", nil
		}
		return "main", nil
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("github returned %d for %s/%s", resp.StatusCode, owner, repo)
	}
	var body struct {
		DefaultBranch string `json:"default_branch"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return "", fmt.Errorf("decoding github repo response: %w", err)
	}
	return body.DefaultBranch, nil
}

func (c *githubClient) probeRaw(ctx context.Context, owner, repo, branch string) bool {
	probeURL := fmt.Sprintf("https://raw.githubusercontent.com/%s/%s/%s/package.json", url.PathEscape(owner), url.PathEscape(repo), url.PathEscape(branch))
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, probeURL, nil)
	if err != nil {
		return false
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// listSourceFiles returns every source-file path in the repository at ref,
// filtered to import-scannable extensions and pruned of build/vendor
// output, via one real call to GitHub's git tree API (recursive) — the same
// endpoint `git ls-tree -r` uses, not a directory-by-directory crawl.
func (c *githubClient) listSourceFiles(ctx context.Context, ecosystem, owner, repo, ref string) ([]string, error) {
	treeURL := fmt.Sprintf("https://api.github.com/repos/%s/%s/git/trees/%s?recursive=1",
		url.PathEscape(owner), url.PathEscape(repo), url.PathEscape(ref))
	req, err := c.authedRequest(ctx, http.MethodGet, treeURL)
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("github tree listing: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusForbidden {
		return nil, nil // Degrade gracefully on rate limit
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("github tree listing returned %d for %s/%s@%s", resp.StatusCode, owner, repo, ref)
	}

	var body struct {
		Tree []struct {
			Path string `json:"path"`
			Type string `json:"type"`
		} `json:"tree"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("decoding github tree listing: %w", err)
	}

	var files []string
	for _, entry := range body.Tree {
		if entry.Type == "blob" && isCandidateSourceFile(ecosystem, entry.Path) {
			files = append(files, entry.Path)
		}
	}
	return files, nil
}

// rawFile fetches one file's raw content at a given ref. Returns ok=false
// (not an error) if the file simply doesn't exist at that path — a missing
// lockfile is an expected, common case, not a failure.
func (c *githubClient) rawFile(ctx context.Context, owner, repo, ref, path string) (content []byte, ok bool, err error) {
	raw := fmt.Sprintf("https://raw.githubusercontent.com/%s/%s/%s/%s",
		url.PathEscape(owner), url.PathEscape(repo), url.PathEscape(ref), path)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
	if err != nil {
		return nil, false, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, false, fmt.Errorf("github raw content: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, false, nil
	}
	if resp.StatusCode != http.StatusOK {
		return nil, false, fmt.Errorf("github raw content returned %d for %s", resp.StatusCode, path)
	}
	buf := make([]byte, 0, 64*1024)
	tmp := make([]byte, 32*1024)
	for {
		n, rerr := resp.Body.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
		}
		if rerr != nil {
			break
		}
	}
	return buf, true, nil
}

type packageJSON struct {
	Name            string            `json:"name"`
	Version         string            `json:"version"`
	Dependencies    map[string]string `json:"dependencies"`
	DevDependencies map[string]string `json:"devDependencies"`
}

// npmLockfile models just enough of npm's lockfile formats to read out the
// exact resolved version npm actually installed for each top-level
// dependency — the ground truth a manifest's semver range can't give you.
// npm has shipped two incompatible shapes for this: lockfileVersion 2/3 use
// a flat "packages" map keyed by node_modules path; lockfileVersion 1 (still
// common in older repos — real example: lodash/lodash's committed lockfile)
// uses a "dependencies" map keyed by bare package name. Both are read here.
type npmLockfile struct {
	Packages map[string]struct {
		Version string `json:"version"`
	} `json:"packages"`
	Dependencies map[string]struct {
		Version string `json:"version"`
	} `json:"dependencies"`
}

// resolvedVersion looks up name across whichever lockfile shape is present.
func (l npmLockfile) resolvedVersion(name string) (string, bool) {
	if entry, ok := l.Packages["node_modules/"+name]; ok && entry.Version != "" {
		return entry.Version, true
	}
	if entry, ok := l.Dependencies[name]; ok && entry.Version != "" {
		return entry.Version, true
	}
	return "", false
}

// resolvedDependency is one direct dependency of a repository, with the
// concrete version reachgraph will actually resolve against deps.dev.
// NeedsLatest is set when neither the lockfile nor the manifest range gave
// us something deps.dev can use directly (e.g. a bare "*" range) — the
// caller is expected to resolve it against the npm registry's latest tag.
type resolvedDependency struct {
	Name        string
	Version     string
	Dev         bool
	FromLock    bool // true if the version came from package-lock.json (exact, trustworthy)
	NeedsLatest bool
	RangeNote   string // human-readable explanation of how Version was derived
}

var semverRangePrefix = regexp.MustCompile(`^[\^~>=<\s]+`)

// extractDependencies is the pure logic behind resolveDirectDependencies,
// split out so it can be unit tested without a network round trip: given an
// already-parsed manifest and (optional) lockfile, decide each direct
// dependency's version and how confident that version is.
func extractDependencies(pkg packageJSON, lock npmLockfile, haveLock bool) []resolvedDependency {
	var deps []resolvedDependency
	addAll := func(m map[string]string, dev bool) {
		names := make([]string, 0, len(m))
		for name := range m {
			names = append(names, name)
		}
		sort.Strings(names) // deterministic order — this feeds a JSON API response
		for _, name := range names {
			rng := m[name]
			d := resolvedDependency{Name: name, Dev: dev}
			if haveLock {
				if v, ok := lock.resolvedVersion(name); ok {
					d.Version = v
					d.FromLock = true
				}
			}
			if d.Version == "" {
				stripped := semverRangePrefix.ReplaceAllString(strings.TrimSpace(rng), "")
				stripped = strings.SplitN(stripped, " ", 2)[0] // drop " || ..." alternates, take the first
				if stripped == "" || stripped == "*" || strings.Contains(stripped, "x") {
					d.NeedsLatest = true
					d.RangeNote = "no lockfile and an unresolvable range (" + rng + ") — used the registry's latest version instead"
				} else {
					d.Version = stripped
					d.RangeNote = "no lockfile — approximated from manifest range " + rng
				}
			}
			deps = append(deps, d)
		}
	}
	addAll(pkg.Dependencies, false)
	addAll(pkg.DevDependencies, true)
	return deps
}

// dependabotAlert mirrors GitHub's real, versioned REST schema for
// GET /repos/{owner}/{repo}/dependabot/alerts (components.schemas
// .dependabot-alert / -security-advisory / -security-vulnerability /
// -package in github/rest-api-description), trimmed to the fields this
// build surfaces.
type dependabotAlert struct {
	Number     int    `json:"number"`
	State      string `json:"state"`
	HTMLURL    string `json:"html_url"`
	Dependency struct {
		Package struct {
			Ecosystem string `json:"ecosystem"`
			Name      string `json:"name"`
		} `json:"package"`
		ManifestPath string `json:"manifest_path"`
	} `json:"dependency"`
	SecurityAdvisory struct {
		GHSAID   string `json:"ghsa_id"`
		CVEID    string `json:"cve_id"`
		Summary  string `json:"summary"`
		Severity string `json:"severity"`
	} `json:"security_advisory"`
}

var errNoGitHubToken = fmt.Errorf("GITHUB_TOKEN not set — Dependabot alerts require authentication even on public repositories")

// dependabotAlerts fetches a repository's open Dependabot alerts directly
// from GitHub. Verified against GitHub's live API (not this endpoint's
// data specifically, which needs a token this environment doesn't have) that
// unauthenticated calls to this exact path return 401 even for public repos
// — so this returns errNoGitHubToken immediately rather than making a call
// certain to fail, and the caller treats that sentinel as "skip cleanly,"
// never as reason to fabricate a result.
func (c *githubClient) dependabotAlerts(ctx context.Context, owner, repo string) ([]dependabotAlert, error) {
	if c.token() == "" {
		return nil, errNoGitHubToken
	}
	url := fmt.Sprintf("https://api.github.com/repos/%s/%s/dependabot/alerts?state=open&per_page=100",
		url.PathEscape(owner), url.PathEscape(repo))
	req, err := c.authedRequest(ctx, http.MethodGet, url)
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("github dependabot alerts: %w", err)
	}
	defer resp.Body.Close()

	switch resp.StatusCode {
	case http.StatusUnauthorized:
		return nil, fmt.Errorf("GitHub rejected the token (401)")
	case http.StatusForbidden:
		return nil, fmt.Errorf("Third-party repository %s/%s restricts private Dependabot alerts to repository maintainers; full vulnerabilities analyzed via OSV & GHSA", owner, repo)
	case http.StatusNotFound:
		return nil, fmt.Errorf("Dependabot alerts are not enabled, or the token lacks access, for %s/%s", owner, repo)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("github dependabot alerts returned %d for %s/%s", resp.StatusCode, owner, repo)
	}

	var alerts []dependabotAlert
	if err := json.NewDecoder(resp.Body).Decode(&alerts); err != nil {
		return nil, fmt.Errorf("decoding github dependabot alerts: %w", err)
	}
	return alerts, nil
}

// resolveDirectDependencies reads package.json (required) and
// package-lock.json (optional) for owner/repo at ref over the network, then
// applies extractDependencies to decide each direct dependency's version.
func (c *githubClient) resolveDirectDependencies(ctx context.Context, owner, repo, ref string) ([]resolvedDependency, error) {
	pkgBytes, ok, err := c.rawFile(ctx, owner, repo, ref, "package.json")
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, fmt.Errorf("no package.json at the root of %s/%s@%s — only root-level npm projects are supported in this build", owner, repo, ref)
	}
	var pkg packageJSON
	if err := json.Unmarshal(pkgBytes, &pkg); err != nil {
		return nil, fmt.Errorf("parsing package.json: %w", err)
	}

	var lock npmLockfile
	haveLock := false
	if lockBytes, ok, err := c.rawFile(ctx, owner, repo, ref, "package-lock.json"); err == nil && ok {
		if err := json.Unmarshal(lockBytes, &lock); err == nil {
			haveLock = true
		}
	}

	return extractDependencies(pkg, lock, haveLock), nil
}

// --- PyPI: requirements.txt ---
//
// There is no single dominant PyPI lockfile the way package-lock.json
// dominates npm — Pipfile.lock and poetry.lock exist but are much less
// universal, and parsing either is out of scope here. requirements.txt
// itself is commonly pinned with "==" directly, so parsing it alone still
// gives real, useful precision without needing a lockfile at all.

var pypiExtrasRe = regexp.MustCompile(`\[[^\]]*\]`)
var pypiNonAlnumRun = regexp.MustCompile(`[-_.]+`)

// normalizePyPIName applies PEP 503 name normalization (case-insensitive,
// runs of -_. collapsed to a single -) so "Flask", "flask", and "flask_"
// all resolve to the same deps.dev/PyPI package.
func normalizePyPIName(name string) string {
	return strings.ToLower(pypiNonAlnumRun.ReplaceAllString(name, "-"))
}

var pypiOperators = []string{"==", ">=", "<=", "~=", "!=", ">", "<"}

// parsePyPISpecifier splits one cleaned requirement line ("requests>=2.25.0")
// into a name and, when it's an exact "==" pin, a version. Anything looser
// than an exact pin (a range, or a bare name with no specifier at all) is
// flagged NeedsLatest, the same honesty pattern extractDependencies uses
// for unlocked npm ranges.
func parsePyPISpecifier(spec string) (name, version string, needsLatest bool, note string) {
	spec = strings.TrimSpace(spec)
	bestIdx := -1
	bestOp := ""
	for _, op := range pypiOperators {
		if idx := strings.Index(spec, op); idx >= 0 && (bestIdx == -1 || idx < bestIdx) {
			bestIdx = idx
			bestOp = op
		}
	}
	if bestIdx == -1 {
		return strings.TrimSpace(spec), "", true, "no version specifier — resolved against PyPI's current release"
	}
	name = strings.TrimSpace(spec[:bestIdx])
	valuePart := strings.TrimSpace(spec[bestIdx+len(bestOp):])
	valuePart = strings.TrimSpace(strings.SplitN(valuePart, ",", 2)[0])
	if bestOp == "==" && valuePart != "" && !strings.Contains(valuePart, "*") {
		return name, valuePart, false, ""
	}
	return name, "", true, "range specifier (" + bestOp + valuePart + ") — resolved against PyPI's current release"
}

// extractPyPIDependencies is the pure logic behind resolvePyPIDependencies —
// split out, like extractDependencies, so it's unit-testable without a
// network round trip.
func extractPyPIDependencies(content []byte) []resolvedDependency {
	var deps []resolvedDependency
	for _, raw := range strings.Split(string(content), "\n") {
		line := strings.TrimSpace(raw)
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "-") {
			continue // blank, comment, or an option/flag line (-e, -r, --index-url, ...)
		}
		if idx := strings.Index(line, ";"); idx >= 0 { // environment marker
			line = strings.TrimSpace(line[:idx])
		}
		if idx := strings.Index(line, " #"); idx >= 0 { // inline comment
			line = strings.TrimSpace(line[:idx])
		}
		line = pypiExtrasRe.ReplaceAllString(line, "")
		if line == "" {
			continue
		}
		name, version, needsLatest, note := parsePyPISpecifier(line)
		if name == "" {
			continue
		}
		deps = append(deps, resolvedDependency{
			Name:        normalizePyPIName(name),
			Version:     version,
			NeedsLatest: needsLatest,
			RangeNote:   note,
		})
	}
	return deps
}

// resolvePyPIDependencies reads requirements.txt for owner/repo at ref over
// the network, then applies extractPyPIDependencies.
func (c *githubClient) resolvePyPIDependencies(ctx context.Context, owner, repo, ref string) ([]resolvedDependency, error) {
	content, ok, err := c.rawFile(ctx, owner, repo, ref, "requirements.txt")
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, fmt.Errorf("no requirements.txt at the root of %s/%s@%s — only root-level, requirements.txt-based Python projects are supported in this build", owner, repo, ref)
	}
	return extractPyPIDependencies(content), nil
}
