package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// depsDevNode and depsDevEdge mirror the real response shape of
// GET https://api.deps.dev/v3/systems/{system}/packages/{name}/versions/{version}:dependencies
// as returned by the live API (verified by hand before writing these structs).
type depsDevGraph struct {
	Nodes []depsDevNode `json:"nodes"`
	Edges []depsDevEdge `json:"edges"`
	Error string        `json:"error"`
}

type depsDevNode struct {
	VersionKey struct {
		System  string `json:"system"`
		Name    string `json:"name"`
		Version string `json:"version"`
	} `json:"versionKey"`
	Relation string `json:"relation"` // SELF, DIRECT, INDIRECT
}

type depsDevEdge struct {
	FromNode    int    `json:"fromNode"`
	ToNode      int    `json:"toNode"`
	Requirement string `json:"requirement"`
}

type depsDevClient struct {
	http *http.Client
	base string
}

func newDepsDevClient() *depsDevClient {
	return &depsDevClient{
		http: &http.Client{Timeout: 20 * time.Second},
		base: "https://api.deps.dev/v3",
	}
}

// resolveLatestVersion asks the npm registry directly for the version behind the
// "latest" dist-tag. deps.dev's dependency graph endpoint requires an exact
// version, so a bare package name (no version supplied by the caller) is
// resolved against the real registry rather than guessed.
func (c *depsDevClient) resolveLatestNpmVersion(ctx context.Context, name string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		"https://registry.npmjs.org/"+url.PathEscape(name)+"/latest", nil)
	if err != nil {
		return "", err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("npm registry: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return "", fmt.Errorf("package %q not found on npm", name)
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("npm registry returned %d for %q", resp.StatusCode, name)
	}
	var body struct {
		Version string `json:"version"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return "", fmt.Errorf("decoding npm registry response: %w", err)
	}
	if body.Version == "" {
		return "", fmt.Errorf("npm registry did not return a version for %q", name)
	}
	return body.Version, nil
}

// resolveLatestPyPIVersion asks PyPI's own JSON API for a project's current
// release — verified against the live API (https://pypi.org/pypi/{name}/json,
// info.version) before writing this, the same pattern as the npm resolver.
func (c *depsDevClient) resolveLatestPyPIVersion(ctx context.Context, name string) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		"https://pypi.org/pypi/"+url.PathEscape(name)+"/json", nil)
	if err != nil {
		return "", err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("pypi registry: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return "", fmt.Errorf("package %q not found on PyPI", name)
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("pypi registry returned %d for %q", resp.StatusCode, name)
	}
	var body struct {
		Info struct {
			Version string `json:"version"`
		} `json:"info"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return "", fmt.Errorf("decoding pypi registry response: %w", err)
	}
	if body.Info.Version == "" {
		return "", fmt.Errorf("pypi registry did not return a version for %q", name)
	}
	return body.Info.Version, nil
}

// resolveLatestVersion dispatches to the right registry for the ecosystem.
func (c *depsDevClient) resolveLatestVersion(ctx context.Context, ecosystem, name string) (string, error) {
	if strings.EqualFold(ecosystem, "pypi") {
		return c.resolveLatestPyPIVersion(ctx, name)
	}
	return c.resolveLatestNpmVersion(ctx, name)
}

// dependencyGraph calls the live deps.dev API and returns the fully resolved
// transitive dependency graph for one package version. No caching, no mock
// fixtures — every call hits api.deps.dev.
func (c *depsDevClient) dependencyGraph(ctx context.Context, system, name, version string) (*depsDevGraph, error) {
	path := fmt.Sprintf("%s/systems/%s/packages/%s/versions/%s:dependencies",
		c.base,
		url.PathEscape(strings.ToLower(system)),
		url.PathEscape(name),
		url.PathEscape(version),
	)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, path, nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("deps.dev: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return nil, fmt.Errorf("deps.dev has no record of %s %s@%s", system, name, version)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("deps.dev returned %d for %s %s@%s", resp.StatusCode, system, name, version)
	}

	var g depsDevGraph
	if err := json.NewDecoder(resp.Body).Decode(&g); err != nil {
		return nil, fmt.Errorf("decoding deps.dev response: %w", err)
	}
	if g.Error != "" {
		return nil, fmt.Errorf("deps.dev: %s", g.Error)
	}
	return &g, nil
}
