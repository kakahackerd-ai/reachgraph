package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"sync"
)

// This file answers another of Track 02's specific questions directly:
// "which other packages share maintainers or infrastructure with it?" — a
// real signal for coordinated or account-takeover-driven campaigns, where
// one compromised publisher account touches several packages at once.
//
// npm's registry is the only one of the two ecosystems this build supports
// that exposes a real, structured list of maintainer accounts per package
// (GET https://registry.npmjs.org/{name} -> maintainers: [{name, email}]).
// PyPI's JSON API only has free-text author/maintainer strings, not
// verifiable accounts, so cross-referencing "shared maintainer" there would
// be guessing dressed up as a finding — this is npm-only, on purpose, and
// says so rather than faking a weaker signal.

type maintainerFinding struct {
	Package       string   `json:"package"`
	Maintainer    string   `json:"maintainer"`
	AlsoMaintains []string `json:"alsoMaintains"`
}

func (c *depsDevClient) fetchNpmMaintainers(ctx context.Context, name string) ([]string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		"https://registry.npmjs.org/"+url.PathEscape(name), nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("npm registry: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("npm registry returned %d for %q", resp.StatusCode, name)
	}
	var body struct {
		Maintainers []struct {
			Name string `json:"name"`
		} `json:"maintainers"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("decoding npm registry response for %q: %w", name, err)
	}
	names := make([]string, len(body.Maintainers))
	for i, m := range body.Maintainers {
		names[i] = m.Name
	}
	return names, nil
}

// computeMaintainerOverlaps is the pure logic behind checkSharedMaintainers:
// given each candidate package's real maintainer list and which of those
// candidates are flagged, find every (flagged package, maintainer) pair
// where that maintainer also maintains at least one other candidate.
func computeMaintainerOverlaps(pkgMaintainers map[string][]string, flaggedNames []string) []maintainerFinding {
	ownerToPackages := map[string][]string{}
	for pkg, maintainers := range pkgMaintainers {
		for _, m := range maintainers {
			ownerToPackages[m] = append(ownerToPackages[m], pkg)
		}
	}

	var findings []maintainerFinding
	for _, flagged := range flaggedNames {
		for _, m := range pkgMaintainers[flagged] {
			var also []string
			for _, other := range ownerToPackages[m] {
				if other != flagged {
					also = append(also, other)
				}
			}
			if len(also) > 0 {
				sort.Strings(also)
				findings = append(findings, maintainerFinding{Package: flagged, Maintainer: m, AlsoMaintains: also})
			}
		}
	}
	sort.Slice(findings, func(i, j int) bool {
		if findings[i].Package != findings[j].Package {
			return findings[i].Package < findings[j].Package
		}
		return findings[i].Maintainer < findings[j].Maintainer
	})
	return findings
}

// checkSharedMaintainers fetches real maintainer lists (bounded concurrency)
// for every candidate package and returns overlaps involving a flagged one.
func (s *apiServer) checkSharedMaintainers(ctx context.Context, ecosystem string, candidates, flagged []string) []maintainerFinding {
	if ecosystem != "npm" || len(candidates) == 0 {
		return nil
	}

	pkgMaintainers := make(map[string][]string, len(candidates))
	var mu sync.Mutex
	var wg sync.WaitGroup
	sem := make(chan struct{}, 10)
	for _, name := range candidates {
		wg.Add(1)
		go func(name string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			maintainers, err := s.deps.fetchNpmMaintainers(ctx, name)
			if err != nil {
				return // best-effort: one failed lookup shouldn't drop the rest
			}
			mu.Lock()
			pkgMaintainers[name] = maintainers
			mu.Unlock()
		}(name)
	}
	wg.Wait()

	return computeMaintainerOverlaps(pkgMaintainers, flagged)
}
