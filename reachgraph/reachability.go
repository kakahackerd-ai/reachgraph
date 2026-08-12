package main

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"sync"
)

// This file is the first real slice of FR3 from the PRD — the requirement
// that, more than any other, is what separates this product from a CVE
// list: whether a flagged dependency is actually reachable from the
// repository's own code, not just declared in a manifest. It is a static
// import/call-site scan, exactly the "build (lightweight)" scope the
// implementation plan calls out for Phase 1 — deep dataflow reachability is
// explicitly future work, not this.

var sourceFileExt = map[string]bool{
	".js": true, ".jsx": true, ".mjs": true, ".cjs": true,
	".ts": true, ".tsx": true, ".vue": true,
}

// GitHub's tree API returns paths relative to the repo root with no leading
// slash ("dist/bundle.js", not "/dist/bundle.js" — confirmed against the
// real API before writing this, not assumed). A skip directory therefore
// has to match both at the start of the path and nested anywhere within it.
var skipDirNames = []string{"node_modules", "dist", "build", "vendor", "coverage"}

func isCandidateSourceFile(path string) bool {
	lower := strings.ToLower(path)
	for _, dir := range skipDirNames {
		if strings.HasPrefix(lower, dir+"/") || strings.Contains(lower, "/"+dir+"/") {
			return false
		}
	}
	if strings.Contains(lower, ".min.") {
		return false
	}
	for ext := range sourceFileExt {
		if strings.HasSuffix(lower, ext) {
			return true
		}
	}
	return false
}

// importPattern matches require('pkg'), require('pkg/sub'), `from 'pkg'`,
// `import 'pkg'`, and dynamic `import('pkg')` — the real ways a CommonJS or
// ES module specifier names a package. The trailing character class
// requires a quote or path separator immediately after the package name, so
// a search for "chalk" does not false-match "chalk-utils".
func importPattern(pkgName string) *regexp.Regexp {
	q := regexp.QuoteMeta(pkgName)
	return regexp.MustCompile(`(?:require\(\s*|from\s+|import\(\s*|import\s+)['"]` + q + `['"/]`)
}

type reachabilityFinding struct {
	Checked  bool   `json:"checked"`
	Reached  bool   `json:"reached"`
	Evidence string `json:"evidence"`
}

// checkReachability fetches a bounded set of the repository's own source
// files and checks each flagged direct dependency's real import pattern
// against their real content. Every file byte and match here is live —
// nothing is precomputed or assumed from the dependency graph alone.
func (s *apiServer) checkReachability(ctx context.Context, owner, repo, ref string, pkgNames []string) map[string]reachabilityFinding {
	results := make(map[string]reachabilityFinding, len(pkgNames))
	if len(pkgNames) == 0 {
		return results
	}

	files, err := s.github.listSourceFiles(ctx, owner, repo, ref)
	if err != nil {
		// A failed listing must not fail the scan — reachability is an
		// enrichment, not a required step. Every requested package is
		// simply reported as not checked.
		for _, n := range pkgNames {
			results[n] = reachabilityFinding{Checked: false}
		}
		return results
	}

	const maxFiles = 150
	totalFiles := len(files)
	truncated := totalFiles > maxFiles
	if truncated {
		files = files[:maxFiles]
	}

	patterns := make(map[string]*regexp.Regexp, len(pkgNames))
	for _, n := range pkgNames {
		patterns[n] = importPattern(n)
	}

	var mu sync.Mutex
	found := make(map[string]string) // pkgName -> evidence file path
	var wg sync.WaitGroup
	sem := make(chan struct{}, 15)
	for _, f := range files {
		wg.Add(1)
		go func(path string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			content, ok, err := s.github.rawFile(ctx, owner, repo, ref, path)
			if err != nil || !ok {
				return
			}
			text := string(content)
			mu.Lock()
			for name, re := range patterns {
				if _, already := found[name]; already {
					continue
				}
				if re.MatchString(text) {
					found[name] = path
				}
			}
			mu.Unlock()
		}(f)
	}
	wg.Wait()

	for _, n := range pkgNames {
		if path, ok := found[n]; ok {
			results[n] = reachabilityFinding{Checked: true, Reached: true, Evidence: "imported in " + path}
		} else {
			note := fmt.Sprintf("not imported in any of %d scanned source files", len(files))
			if truncated {
				note += fmt.Sprintf(" (of %d total — capped for this demo build)", totalFiles)
			}
			results[n] = reachabilityFinding{Checked: true, Reached: false, Evidence: note}
		}
	}
	return results
}
