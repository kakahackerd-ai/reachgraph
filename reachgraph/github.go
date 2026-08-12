package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
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

func (c *githubClient) token() string { return strings.TrimSpace(os.Getenv("GITHUB_TOKEN")) }

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
		return "", fmt.Errorf("github API rate limit hit — set GITHUB_TOKEN to raise the unauthenticated 60/hour limit")
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
	case http.StatusUnauthorized, http.StatusForbidden:
		return nil, fmt.Errorf("github rejected the configured GITHUB_TOKEN for Dependabot alerts (%d)", resp.StatusCode)
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
