// Reachgraph.
//
// This is real, running code: every dependency resolution comes from a live
// call to api.deps.dev, every vulnerability/malware finding comes from a live
// call to api.osv.dev, every repository ingestion reads package.json (and
// package-lock.json, when present) straight from raw.githubusercontent.com,
// and the attack-path ranking is computed in graph.go and risk.go against
// whatever those calls actually return. There is no mocked or hardcoded scan
// data anywhere in this binary.
package main

import (
	"context"
	"embed"
	"encoding/json"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

//go:embed web
var webFS embed.FS

type pathHop struct {
	Name     string `json:"name"`
	Version  string `json:"version"`
	Relation string `json:"relation"`
}

type attackPath struct {
	Target   pathHop      `json:"target"`
	Hops     []pathHop    `json:"hops"`
	Findings []osvFinding `json:"findings"`
	Score    riskScore    `json:"score"`
}

// dependencyNote reports, for a repo scan, exactly how each direct
// dependency's version was determined — the honesty trail for anything that
// wasn't read straight out of a lockfile.
type dependencyNote struct {
	Name     string `json:"name"`
	Version  string `json:"version"`
	Dev      bool   `json:"dev"`
	FromLock bool   `json:"fromLock"`
	Note     string `json:"note,omitempty"`
	Error    string `json:"error,omitempty"`
}

type scanResponse struct {
	Subject struct {
		Kind      string `json:"kind"` // "package" or "repository"
		Ecosystem string `json:"ecosystem,omitempty"`
		Name      string `json:"name"`
		Version   string `json:"version,omitempty"`
		Owner     string `json:"owner,omitempty"`
		Repo      string `json:"repo,omitempty"`
		Ref       string `json:"ref,omitempty"`
	} `json:"subject"`
	ScannedAt         string              `json:"scannedAt"`
	DurationMs        int64               `json:"durationMs"`
	TotalPackages     int                 `json:"totalPackages"`
	DirectPackages    int                 `json:"directPackages"`
	FlaggedCount      int                 `json:"flaggedCount"`
	Paths             []attackPath        `json:"paths"`
	Dependencies      []dependencyNote    `json:"dependencies,omitempty"`
	Dependabot        *dependabotSummary  `json:"dependabot,omitempty"`
	Typosquats        []typosquatFinding  `json:"typosquats,omitempty"`
	SharedMaintainers []maintainerFinding `json:"sharedMaintainers,omitempty"`
	Source            map[string]string   `json:"source"`
}

// dependabotSummary reports GitHub's own official Dependabot alerts for a
// repository alongside reachgraph's independently-computed attack paths —
// two independently-sourced signals, shown as two signals, never merged
// into one number. Status is always present so the frontend can render
// "not configured" honestly instead of an empty list that looks like
// "nothing found."
type dependabotSummary struct {
	Status string                `json:"status"` // ok | not_configured | error
	Detail string                `json:"detail,omitempty"`
	Alerts []dependabotAlertView `json:"alerts,omitempty"`
}

type dependabotAlertView struct {
	Package      string `json:"package"`
	Ecosystem    string `json:"ecosystem"`
	Severity     string `json:"severity"`
	Summary      string `json:"summary"`
	GHSAID       string `json:"ghsaId"`
	URL          string `json:"url"`
	ManifestPath string `json:"manifestPath"`
}

func (s *apiServer) fetchDependabotSummary(ctx context.Context, owner, repo string) *dependabotSummary {
	alerts, err := s.github.dependabotAlerts(ctx, owner, repo)
	if err != nil {
		if err == errNoGitHubToken {
			return &dependabotSummary{Status: "not_configured", Detail: err.Error()}
		}
		return &dependabotSummary{Status: "error", Detail: err.Error()}
	}
	views := make([]dependabotAlertView, len(alerts))
	for i, a := range alerts {
		views[i] = dependabotAlertView{
			Package:      a.Dependency.Package.Name,
			Ecosystem:    a.Dependency.Package.Ecosystem,
			Severity:     a.SecurityAdvisory.Severity,
			Summary:      a.SecurityAdvisory.Summary,
			GHSAID:       a.SecurityAdvisory.GHSAID,
			URL:          a.HTMLURL,
			ManifestPath: a.Dependency.ManifestPath,
		}
	}
	return &dependabotSummary{Status: "ok", Alerts: views}
}

type apiServer struct {
	deps   *depsDevClient
	osv    *osvClient
	github *githubClient
	guac   *guacClient    // nil when GUAC persistence isn't configured (see guac.go)
	hydra  *hydraDBClient // nil when HYDRADB_API_KEY isn't set (see hydradb.go)
}

func main() {
	addr := os.Getenv("REACHGRAPH_ADDR")
	if addr == "" {
		addr = ":8080"
	}

	srv := &apiServer{
		deps:   newDepsDevClient(),
		osv:    newOSVClient(),
		github: newGitHubClient(),
	}
	if endpoint := strings.TrimSpace(os.Getenv("GUAC_GRAPHQL_URL")); endpoint != "" {
		srv.guac = newGUACClient(endpoint)
		log.Printf("GUAC persistence enabled: %s", endpoint)
	} else {
		log.Printf("GUAC_GRAPHQL_URL not set — running Phase 0 style, without persistent graph storage")
	}
	if key := strings.TrimSpace(os.Getenv("HYDRADB_API_KEY")); key != "" {
		srv.hydra = newHydraDBClient(key, os.Getenv("HYDRADB_DATABASE"))
		log.Printf("HydraDB narrative timeline enabled")
	} else {
		log.Printf("HYDRADB_API_KEY not set — narrative timeline and code-context queries disabled")
	}

	webContent, err := fs.Sub(webFS, "web")
	if err != nil {
		log.Fatalf("embedded web/ directory is missing: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/scan", srv.handleScan)
	mux.HandleFunc("POST /api/scan-repo", srv.handleScanRepo)
	mux.HandleFunc("POST /api/expand", srv.handleExpand)
	mux.HandleFunc("POST /api/ask", srv.handleAsk)
	mux.HandleFunc("GET /api/repos", srv.handleListRepos)
	mux.HandleFunc("GET /api/status", srv.handleStatus)
	mux.Handle("/", http.FileServer(http.FS(webContent)))

	log.Printf("reachgraph listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, logRequests(mux)))
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(start))
	})
}

func (s *apiServer) handleStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"guacEnabled":        s.guac != nil,
		"githubTokenPresent": strings.TrimSpace(os.Getenv("GITHUB_TOKEN")) != "",
		"hydradbEnabled":     s.hydra != nil,
	})
}

type askRequest struct {
	Question string `json:"question"`
}

// handleAsk is the natural-language surface over whatever's been narrated
// into HydraDB: scan-timeline facts (see persistTimelineFacts) and, for
// repos that have had their source ingested, code-structure facts (see
// track_b.go). Answers are HydraDB's own ranked retrieval — presented as
// "here's what looked most relevant," never as a guaranteed-complete
// answer, because that's honestly what the underlying API gives back.
func (s *apiServer) handleAsk(w http.ResponseWriter, r *http.Request) {
	if s.hydra == nil {
		writeError(w, http.StatusServiceUnavailable, "HYDRADB_API_KEY is not configured on this server")
		return
	}
	var req askRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	req.Question = strings.TrimSpace(req.Question)
	if req.Question == "" {
		writeError(w, http.StatusBadRequest, "\"question\" is required")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	result, err := s.hydra.query(ctx, req.Question)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

var supportedEcosystems = map[string]bool{"npm": true, "pypi": true}

func ecosystemList() []string {
	return []string{"npm", "pypi"}
}

func normalizeEcosystem(ecosystem string) string {
	if ecosystem == "" {
		return "npm"
	}
	return strings.ToLower(ecosystem)
}

// osvEcosystem maps reachgraph's canonical lowercase ecosystem name (also
// what deps.dev's URL path expects) to the exact casing OSV.dev's schema
// requires for that field — verified against the live API before writing
// this: "npm" for npm, but "PyPI" for Python, not "pypi".
func osvEcosystem(ecosystem string) string {
	if strings.EqualFold(ecosystem, "pypi") {
		return "PyPI"
	}
	return "npm"
}

// findAttackPaths is the shared core behind both scan endpoints: given an
// already-built graph (whichever way it was built), check every node
// against OSV.dev and rank the flagged ones into attack paths. Neither
// caller needs to know the other exists.
func findAttackPaths(ctx context.Context, osv *osvClient, g *graph) ([]attackPath, int, error) {
	osvEco := osvEcosystem(g.ecosystem)
	queries := make([]osvQuery, len(g.Nodes))
	for i, n := range g.Nodes {
		queries[i].Package.Name = n.Name
		queries[i].Package.Ecosystem = osvEco
		queries[i].Version = n.Version
	}
	batchResults, err := osv.batchQuery(ctx, queries)
	if err != nil {
		return nil, 0, err
	}

	var idsToFetch []string
	nodeVulnIDs := make(map[int][]string)
	seen := make(map[string]bool)
	for i, res := range batchResults {
		if i >= len(g.Nodes) || len(res.Vulns) == 0 {
			continue
		}
		for _, v := range res.Vulns {
			nodeVulnIDs[i] = append(nodeVulnIDs[i], v.ID)
			if !seen[v.ID] {
				seen[v.ID] = true
				idsToFetch = append(idsToFetch, v.ID)
			}
		}
	}
	details := osv.vulnDetailsConcurrent(ctx, idsToFetch)

	var paths []attackPath
	directCount := 0
	for _, n := range g.Nodes {
		if n.Relation == "DIRECT" {
			directCount++
		}
		ids := nodeVulnIDs[n.Index]
		if len(ids) == 0 {
			continue
		}

		hopIndices := g.shortestPath(n.Index)
		if hopIndices == nil {
			continue // defensive: a node with no discoverable path from root
		}
		depth := len(hopIndices) - 1
		if g.Nodes[g.rootIndex].Relation == "ROOT" {
			depth-- // don't count the synthetic repo root as a hop
		}

		var findings []osvFinding
		worstScore := -1
		var worstSeverity string
		for _, id := range ids {
			f, ok := details[id]
			if !ok {
				f = osvFinding{ID: id, URL: "https://osv.dev/vulnerability/" + id}
			}
			findings = append(findings, f)
			sv, _ := severityWeight(f.Severity)
			if sv > worstScore {
				worstScore = sv
				worstSeverity = f.Severity
			}
		}

		hops := make([]pathHop, len(hopIndices))
		for i, idx := range hopIndices {
			hops[i] = pathHop{Name: g.Nodes[idx].Name, Version: g.Nodes[idx].Version, Relation: g.Nodes[idx].Relation}
		}

		paths = append(paths, attackPath{
			Target:   pathHop{Name: n.Name, Version: n.Version, Relation: n.Relation},
			Hops:     hops,
			Findings: findings,
			Score:    computeRiskScore(worstSeverity, depth),
		})
	}

	sort.Slice(paths, func(i, j int) bool { return paths[i].Score.Value > paths[j].Score.Value })
	return paths, directCount, nil
}

// isDirectDependencyPath reports whether a path's target is a genuine
// direct dependency of the *scanned repository* — exactly one hop from
// root. This deliberately does not use Target.Relation: that field is
// "DIRECT" whenever a node is one hop from whatever subgraph it happened to
// be resolved in, so a package two hops from the repo (a dependency of one
// of the repo's own dependencies) can carry "DIRECT" too, inherited from
// being direct *within its parent's own subgraph*. Real example caught
// while testing against lodash/lodash: minimist is a dependency of
// coveralls, not of lodash itself, but showed up labeled DIRECT; checking
// hop count instead (len(Hops) == 2: root, then target) is what actually
// answers "did the repository itself declare this."
func isDirectDependencyPath(p attackPath) bool { return len(p.Hops) == 2 }

// applyReachability is FR3 from the PRD wired into a real scan: for every
// flagged path whose target is a genuine direct dependency of the
// repository, check real source files for a real import of it, then
// re-rank, since a confirmed-unreachable finding can drop below others.
// Deliberately checked regardless of prod/dev classification — an unused
// *devDependency* with a CVE (a lint plugin nobody imports, say) is exactly
// the kind of noise this feature exists to cut through.
func (s *apiServer) applyReachability(ctx context.Context, ecosystem, owner, repo, ref string, paths []attackPath) []attackPath {
	var directNames []string
	seen := map[string]bool{}
	for _, p := range paths {
		if isDirectDependencyPath(p) && !seen[p.Target.Name] {
			seen[p.Target.Name] = true
			directNames = append(directNames, p.Target.Name)
		}
	}
	if len(directNames) == 0 {
		return paths
	}

	findings := s.checkReachability(ctx, ecosystem, owner, repo, ref, directNames)
	for i, p := range paths {
		if !isDirectDependencyPath(p) {
			continue
		}
		if rf, ok := findings[p.Target.Name]; ok {
			paths[i].Score = adjustScoreForCodeReachability(p.Score, rf)
		}
	}

	sort.Slice(paths, func(i, j int) bool { return paths[i].Score.Value > paths[j].Score.Value })
	return paths
}

type scanRequest struct {
	Ecosystem string `json:"ecosystem"`
	Package   string `json:"package"`
	Version   string `json:"version"` // optional; resolved against the registry if empty
}

func (s *apiServer) handleScan(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(r.Context(), 45*time.Second)
	defer cancel()

	var req scanRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	req.Package = strings.TrimSpace(req.Package)
	if req.Package == "" {
		writeError(w, http.StatusBadRequest, "\"package\" is required")
		return
	}
	req.Ecosystem = normalizeEcosystem(req.Ecosystem)
	if !supportedEcosystems[req.Ecosystem] {
		writeError(w, http.StatusBadRequest, "unsupported ecosystem \""+req.Ecosystem+"\" — this build supports: "+strings.Join(ecosystemList(), ", "))
		return
	}

	version := req.Version
	if version == "" {
		v, err := s.deps.resolveLatestVersion(ctx, req.Ecosystem, req.Package)
		if err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		version = v
	}

	dd, err := s.deps.dependencyGraph(ctx, req.Ecosystem, req.Package, version)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	truncated := truncateDepsDevGraph(dd, 400)

	g := buildGraph(dd, req.Ecosystem)
	paths, directCount, err := findAttackPaths(ctx, s.osv, g)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	resp := scanResponse{
		ScannedAt:      start.UTC().Format(time.RFC3339),
		DurationMs:     time.Since(start).Milliseconds(),
		TotalPackages:  len(g.Nodes),
		DirectPackages: directCount,
		FlaggedCount:   len(paths),
		Paths:          paths,
		Source: map[string]string{
			"dependencies": sourceLabel("api.deps.dev", truncated),
			"advisories":   "api.osv.dev",
		},
	}
	resp.Subject.Kind = "package"
	resp.Subject.Ecosystem = req.Ecosystem
	resp.Subject.Name = req.Package
	resp.Subject.Version = version

	s.persistToGUAC(context.WithoutCancel(ctx), resp.Subject.Name, g, paths)
	s.persistTimelineFacts(ctx, "package", resp.Subject.Name, req.Ecosystem, start, paths)
	writeJSON(w, http.StatusOK, resp)
}

type scanRepoRequest struct {
	Owner     string `json:"owner"`
	Repo      string `json:"repo"`
	Ref       string `json:"ref"`       // optional; default branch is used when empty
	Ecosystem string `json:"ecosystem"` // optional; "npm" (default) or "pypi"
}

// handleScanRepo is the feature this build adds over the Phase 0 slice:
// given a GitHub repository, it reads the manifest for the requested
// ecosystem (package.json + package-lock.json for npm, requirements.txt for
// pypi) directly from raw.githubusercontent.com, resolves every direct
// dependency's full transitive graph from deps.dev in parallel, merges them
// under one synthetic root, and runs the same attack-path ranking a
// single-package scan uses.
func (s *apiServer) handleScanRepo(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()

	var req scanRepoRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	req.Owner = strings.TrimSpace(req.Owner)
	req.Repo = strings.TrimSpace(req.Repo)
	if req.Owner == "" || req.Repo == "" {
		writeError(w, http.StatusBadRequest, "\"owner\" and \"repo\" are required")
		return
	}
	req.Ecosystem = normalizeEcosystem(req.Ecosystem)
	if !supportedEcosystems[req.Ecosystem] {
		writeError(w, http.StatusBadRequest, "unsupported ecosystem \""+req.Ecosystem+"\" — this build supports: "+strings.Join(ecosystemList(), ", "))
		return
	}

	ref := req.Ref
	if ref == "" {
		branch, err := s.github.defaultBranch(ctx, req.Owner, req.Repo)
		if err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		ref = branch
	}

	var deps []resolvedDependency
	var err error
	if req.Ecosystem == "pypi" {
		deps, err = s.github.resolvePyPIDependencies(ctx, req.Owner, req.Repo, ref)
	} else {
		deps, err = s.github.resolveDirectDependencies(ctx, req.Owner, req.Repo, ref)
	}
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	const maxDirect = 60
	limited := false
	if len(deps) > maxDirect {
		deps = deps[:maxDirect]
		limited = true
	}

	// Resolve any dependency whose manifest range couldn't be turned into a
	// concrete version, then fetch every direct dependency's full deps.dev
	// subgraph — both steps run with bounded concurrency, since a repo can
	// easily have 40+ direct dependencies and these are independent network
	// calls.
	type depResult struct {
		dep   resolvedDependency
		graph *depsDevGraph
		err   error
	}
	results := make([]depResult, len(deps))
	const workers = 10
	sem := make(chan struct{}, workers)
	var wg sync.WaitGroup
	for i, d := range deps {
		wg.Add(1)
		go func(i int, d resolvedDependency) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			if d.NeedsLatest {
				v, err := s.deps.resolveLatestVersion(ctx, req.Ecosystem, d.Name)
				if err != nil {
					results[i] = depResult{dep: d, err: err}
					return
				}
				d.Version = v
			}
			dg, err := s.deps.dependencyGraph(ctx, req.Ecosystem, d.Name, d.Version)
			results[i] = depResult{dep: d, graph: dg, err: err}
		}(i, d)
	}
	wg.Wait()

	builder := newGraphBuilder(req.Owner+"/"+req.Repo, req.Ecosystem)
	notes := make([]dependencyNote, 0, len(results))
	nodeBudget := 400
	for _, res := range results {
		note := dependencyNote{Name: res.dep.Name, Version: res.dep.Version, Dev: res.dep.Dev, FromLock: res.dep.FromLock, Note: res.dep.RangeNote}
		if res.err != nil {
			note.Error = res.err.Error()
			notes = append(notes, note)
			continue
		}
		relation := "DIRECT"
		if res.dep.Dev {
			relation = "DEV"
		}
		if len(res.graph.Nodes) > nodeBudget {
			res.graph.Nodes = res.graph.Nodes[:nodeBudget]
		}
		builder.mergeDirectDependency(res.graph, relation)
		nodeBudget -= len(res.graph.Nodes)
		notes = append(notes, note)
		if nodeBudget <= 0 {
			nodeBudget = 0
		}
	}

	g := builder.build()
	paths, directCount, err := findAttackPaths(ctx, s.osv, g)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	paths = s.applyReachability(ctx, req.Ecosystem, req.Owner, req.Repo, ref, paths)

	directNames := make([]string, len(deps))
	for i, d := range deps {
		directNames[i] = d.Name
	}
	var flaggedDirectNames []string
	for _, p := range paths {
		if isDirectDependencyPath(p) {
			flaggedDirectNames = append(flaggedDirectNames, p.Target.Name)
		}
	}
	typosquats := checkTyposquat(req.Ecosystem, directNames)
	sharedMaintainers := s.checkSharedMaintainers(ctx, req.Ecosystem, directNames, flaggedDirectNames)

	sourceNote := "raw.githubusercontent.com + api.deps.dev"
	if limited {
		sourceNote += fmt.Sprintf(" (first %d direct dependencies only, for this demo build)", maxDirect)
	}

	resp := scanResponse{
		ScannedAt:         start.UTC().Format(time.RFC3339),
		DurationMs:        time.Since(start).Milliseconds(),
		TotalPackages:     len(g.Nodes) - 1, // exclude the synthetic repo root
		DirectPackages:    directCount,
		FlaggedCount:      len(paths),
		Paths:             paths,
		Dependencies:      notes,
		Typosquats:        typosquats,
		SharedMaintainers: sharedMaintainers,
		Source: map[string]string{
			"dependencies": sourceNote,
			"advisories":   "api.osv.dev",
		},
	}
	resp.Subject.Kind = "repository"
	resp.Subject.Owner = req.Owner
	resp.Subject.Repo = req.Repo
	resp.Subject.Ref = ref
	resp.Subject.Ecosystem = req.Ecosystem
	resp.Subject.Name = req.Owner + "/" + req.Repo
	resp.Dependabot = s.fetchDependabotSummary(ctx, req.Owner, req.Repo)

	s.persistToGUAC(context.WithoutCancel(ctx), resp.Subject.Name, g, paths)
	s.persistTimelineFacts(ctx, "repository", resp.Subject.Name, req.Ecosystem, start, paths)
	writeJSON(w, http.StatusOK, resp)
}

func (s *apiServer) handleListRepos(w http.ResponseWriter, r *http.Request) {
	if s.guac == nil {
		writeJSON(w, http.StatusOK, map[string]any{"repos": []any{}, "guacEnabled": false})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	repos, err := s.guac.listScannedSubjects(ctx)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"repos": repos, "guacEnabled": true})
}

type expandRequest struct {
	Ecosystem string `json:"ecosystem"`
	Package   string `json:"package"`
	Version   string `json:"version"`
}

type graphNodeView struct {
	Name     string `json:"name"`
	Version  string `json:"version"`
	Relation string `json:"relation"`
}

type graphEdgeView struct {
	From string `json:"from"` // "name@version"
	To   string `json:"to"`
}

// handleExpand answers "what does this package itself directly depend on" —
// a real, live deps.dev lookup for exactly one node, used by the graph view
// to reveal more of the dependency tree on click instead of shipping the
// entire (potentially 400-node) graph to the browser up front. This is what
// makes "show the necessary subgraph first, expand on demand" real rather
// than a UI trick over data that was already fully loaded.
func (s *apiServer) handleExpand(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()

	var req expandRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	req.Ecosystem = normalizeEcosystem(req.Ecosystem)
	if !supportedEcosystems[req.Ecosystem] {
		writeError(w, http.StatusBadRequest, "unsupported ecosystem \""+req.Ecosystem+"\" — this build supports: "+strings.Join(ecosystemList(), ", "))
		return
	}
	if req.Package == "" || req.Version == "" {
		writeError(w, http.StatusBadRequest, "\"package\" and \"version\" are required")
		return
	}

	dd, err := s.deps.dependencyGraph(ctx, req.Ecosystem, req.Package, req.Version)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}

	var rootIdx = -1
	for i, n := range dd.Nodes {
		if n.Relation == "SELF" {
			rootIdx = i
			break
		}
	}
	nodes := []graphNodeView{}
	edges := []graphEdgeView{}
	if rootIdx >= 0 {
		for i, n := range dd.Nodes {
			if i != rootIdx && n.Relation != "DIRECT" {
				continue // one level only — this is a click-to-expand step, not a full re-resolve
			}
			nodes = append(nodes, graphNodeView{Name: n.VersionKey.Name, Version: n.VersionKey.Version, Relation: n.Relation})
		}
		for _, e := range dd.Edges {
			if e.FromNode == rootIdx && dd.Nodes[e.ToNode].Relation == "DIRECT" {
				edges = append(edges, graphEdgeView{
					From: dd.Nodes[e.FromNode].VersionKey.Name + "@" + dd.Nodes[e.FromNode].VersionKey.Version,
					To:   dd.Nodes[e.ToNode].VersionKey.Name + "@" + dd.Nodes[e.ToNode].VersionKey.Version,
				})
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"nodes": nodes, "edges": edges})
}

func truncateDepsDevGraph(dd *depsDevGraph, maxNodes int) bool {
	if len(dd.Nodes) <= maxNodes {
		return false
	}
	dd.Nodes = dd.Nodes[:maxNodes]
	filteredEdges := dd.Edges[:0]
	for _, e := range dd.Edges {
		if e.FromNode < maxNodes && e.ToNode < maxNodes {
			filteredEdges = append(filteredEdges, e)
		}
	}
	dd.Edges = filteredEdges
	return true
}

func sourceLabel(base string, truncated bool) string {
	if truncated {
		return base + " (truncated at 400 nodes for this demo build)"
	}
	return base
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
