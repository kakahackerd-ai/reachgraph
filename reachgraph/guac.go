package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"
)

// guacClient talks to a real, running GUAC guacgql GraphQL server — the
// Phase 1 persistent graph store. Mutation and query shapes below are taken
// directly from GUAC's published schema
// (pkg/assembler/graphql/schema/{package,vulnerability,certifyVuln,isDependency}.graphql
// in guacsec/guac), not guessed.
type guacClient struct {
	http     *http.Client
	endpoint string
}

func newGUACClient(endpoint string) *guacClient {
	return &guacClient{http: &http.Client{Timeout: 30 * time.Second}, endpoint: endpoint}
}

type graphqlRequest struct {
	Query     string         `json:"query"`
	Variables map[string]any `json:"variables"`
}

type graphqlError struct {
	Message string `json:"message"`
}

func (c *guacClient) do(ctx context.Context, query string, variables map[string]any, out any) error {
	body, err := json.Marshal(graphqlRequest{Query: query, Variables: variables})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("guac: %w", err)
	}
	defer resp.Body.Close()

	var parsed struct {
		Data   json.RawMessage `json:"data"`
		Errors []graphqlError  `json:"errors"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return fmt.Errorf("guac: decoding response: %w", err)
	}
	if len(parsed.Errors) > 0 {
		msgs := make([]string, len(parsed.Errors))
		for i, e := range parsed.Errors {
			msgs[i] = e.Message
		}
		return fmt.Errorf("guac: %s", strings.Join(msgs, "; "))
	}
	if out != nil && parsed.Data != nil {
		if err := json.Unmarshal(parsed.Data, out); err != nil {
			return fmt.Errorf("guac: unmarshaling data: %w", err)
		}
	}
	return nil
}

func pkgInput(pkgType, name, version string) map[string]any {
	in := map[string]any{"type": pkgType, "name": name}
	if version != "" {
		in["version"] = version
	}
	return map[string]any{"packageInput": in}
}

// pkgInputQualified attaches pURL qualifiers (GUAC's PackageQualifierInputSpec)
// to a package input. Used to tag the synthetic repository root with which
// ecosystem it was scanned as — GUAC's own DIRECT/INDIRECT dependency labels
// don't carry that, and without it, re-scanning a tracked repository from
// the dashboard has no way to know whether to call deps.dev with "npm" or
// "pypi" (a real gap this closes, not a hypothetical one: it broke a repo
// re-scan for real once npm and PyPI repos coexisted in one tracked list).
func pkgInputQualified(pkgType, name, version string, qualifiers map[string]string) map[string]any {
	in := pkgInput(pkgType, name, version)["packageInput"].(map[string]any)
	if len(qualifiers) > 0 {
		var q []map[string]string
		for k, v := range qualifiers {
			q = append(q, map[string]string{"key": k, "value": v})
		}
		in["qualifiers"] = q
	}
	return map[string]any{"packageInput": in}
}

const bulkIngestPackagesQuery = `
mutation IngestPackages($pkgs: [IDorPkgInput!]!) {
  ingestPackages(pkgs: $pkgs) { packageVersionID }
}`

func (c *guacClient) ingestPackages(ctx context.Context, pkgs []map[string]any) error {
	if len(pkgs) == 0 {
		return nil
	}
	return c.do(ctx, bulkIngestPackagesQuery, map[string]any{"pkgs": pkgs}, nil)
}

const bulkIngestDependenciesQuery = `
mutation IngestDependencies($pkgs: [IDorPkgInput!]!, $depPkgs: [IDorPkgInput!]!, $dependencies: [IsDependencyInputSpec!]!) {
  ingestDependencies(pkgs: $pkgs, depPkgs: $depPkgs, dependencies: $dependencies)
}`

type dependencyEdgeInput struct {
	Pkg    map[string]any
	DepPkg map[string]any
	Type   string // DIRECT | INDIRECT | UNKNOWN
	Note   string
}

func (c *guacClient) ingestDependencies(ctx context.Context, edges []dependencyEdgeInput) error {
	if len(edges) == 0 {
		return nil
	}
	pkgs := make([]map[string]any, len(edges))
	depPkgs := make([]map[string]any, len(edges))
	deps := make([]map[string]any, len(edges))
	for i, e := range edges {
		pkgs[i] = e.Pkg
		depPkgs[i] = e.DepPkg
		deps[i] = map[string]any{
			"dependencyType": e.Type,
			"justification":  e.Note,
			"origin":         "reachgraph",
			"collector":      "reachgraph-scan",
			"documentRef":    "",
		}
	}
	return c.do(ctx, bulkIngestDependenciesQuery, map[string]any{"pkgs": pkgs, "depPkgs": depPkgs, "dependencies": deps}, nil)
}

const bulkIngestVulnerabilitiesQuery = `
mutation IngestVulnerabilities($vulns: [IDorVulnerabilityInput!]!) {
  ingestVulnerabilities(vulns: $vulns) { vulnerabilityNodeID }
}`

func vulnInput(id string) map[string]any {
	// GUAC requires vulnerability type + ID to be lowercase; OSV/GHSA IDs are
	// stored under the uniform "osv" namespace rather than parsed per-vendor,
	// matching how GUAC's own OSV certifier records them.
	return map[string]any{"vulnerabilityInput": map[string]any{
		"type":            "osv",
		"vulnerabilityID": strings.ToLower(id),
	}}
}

func (c *guacClient) ingestVulnerabilities(ctx context.Context, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	vulns := make([]map[string]any, len(ids))
	for i, id := range ids {
		vulns[i] = vulnInput(id)
	}
	return c.do(ctx, bulkIngestVulnerabilitiesQuery, map[string]any{"vulns": vulns}, nil)
}

const bulkIngestCertifyVulnQuery = `
mutation IngestCertifyVulns($pkgs: [IDorPkgInput!]!, $vulnerabilities: [IDorVulnerabilityInput!]!, $certifyVulns: [ScanMetadataInput!]!) {
  ingestCertifyVulns(pkgs: $pkgs, vulnerabilities: $vulnerabilities, certifyVulns: $certifyVulns)
}`

type certifyVulnInput struct {
	Pkg      map[string]any
	VulnID   string
	ScanTime time.Time
}

func (c *guacClient) ingestCertifyVulns(ctx context.Context, certs []certifyVulnInput) error {
	if len(certs) == 0 {
		return nil
	}
	pkgs := make([]map[string]any, len(certs))
	vulns := make([]map[string]any, len(certs))
	meta := make([]map[string]any, len(certs))
	for i, c2 := range certs {
		pkgs[i] = c2.Pkg
		vulns[i] = vulnInput(c2.VulnID)
		meta[i] = map[string]any{
			"timeScanned":    c2.ScanTime.UTC().Format(time.RFC3339),
			"dbUri":          "https://osv.dev",
			"dbVersion":      "v1",
			"scannerUri":     "reachgraph",
			"scannerVersion": "0.1.0",
			"origin":         "reachgraph",
			"collector":      "reachgraph-scan",
			"documentRef":    "",
		}
	}
	return c.do(ctx, bulkIngestCertifyVulnQuery, map[string]any{"pkgs": pkgs, "vulnerabilities": vulns, "certifyVulns": meta}, nil)
}

const listGuacSubjectsQuery = `
query ListSubjects($pkgSpec: PkgSpec!) {
  packages(pkgSpec: $pkgSpec) {
    namespaces {
      names {
        name
        versions { qualifiers { key value } }
      }
    }
  }
}`

// trackedRepo is one previously-scanned repository read back from GUAC,
// including which ecosystem it was scanned as — read from the "ecosystem"
// qualifier pkgInputForNode wrote onto the root package at ingest time, so
// re-scanning it from the dashboard calls deps.dev with the right system
// instead of assuming npm.
type trackedRepo struct {
	Name      string `json:"name"`
	Ecosystem string `json:"ecosystem"`
}

// listScannedSubjects returns every previously-ingested repository — pURL
// type "guac" is the synthetic root graphBuilder gives a repo scan (see
// nodePkgType), which unambiguously distinguishes "this was a scan subject"
// from "this showed up somewhere as a transitive dependency." Filtering by
// that type, rather than querying every package in the graph, is what makes
// this a tracked-repositories list instead of a dump of the whole store.
func (c *guacClient) listScannedSubjects(ctx context.Context) ([]trackedRepo, error) {
	var out struct {
		Packages []struct {
			Namespaces []struct {
				Names []struct {
					Name     string `json:"name"`
					Versions []struct {
						Qualifiers []struct {
							Key   string `json:"key"`
							Value string `json:"value"`
						} `json:"qualifiers"`
					} `json:"versions"`
				} `json:"names"`
			} `json:"namespaces"`
		} `json:"packages"`
	}
	spec := map[string]any{"type": "guac"}
	if err := c.do(ctx, listGuacSubjectsQuery, map[string]any{"pkgSpec": spec}, &out); err != nil {
		return nil, err
	}
	var subjects []trackedRepo
	seen := map[string]bool{}
	for _, ns := range out.Packages {
		for _, n := range ns.Namespaces {
			for _, name := range n.Names {
				if seen[name.Name] {
					continue
				}
				seen[name.Name] = true
				eco := "npm" // default for repos ingested before this qualifier existed
				for _, v := range name.Versions {
					for _, q := range v.Qualifiers {
						if q.Key == "ecosystem" && q.Value != "" {
							eco = q.Value
						}
					}
				}
				subjects = append(subjects, trackedRepo{Name: name.Name, Ecosystem: eco})
			}
		}
	}
	return subjects, nil
}

func dependencyType(relation string) string {
	switch relation {
	case "DIRECT", "DEV":
		return "DIRECT"
	case "INDIRECT":
		return "INDIRECT"
	default:
		return "UNKNOWN"
	}
}

func chunk[T any](items []T, size int) [][]T {
	var out [][]T
	for i := 0; i < len(items); i += size {
		end := i + size
		if end > len(items) {
			end = len(items)
		}
		out = append(out, items[i:end])
	}
	return out
}

// persistToGUAC writes one completed scan into the persistent graph store:
// every resolved package, every dependency edge, every flagged finding, and
// the certification linking them. It is a real write path, but a
// best-effort, fire-and-forget one — a GUAC outage must never fail a scan
// the user is actively waiting on, so errors are logged, not returned.
func (s *apiServer) persistToGUAC(ctx context.Context, subjectName string, g *graph, paths []attackPath) {
	if s.guac == nil {
		return
	}
	go func() {
		if err := s.doPersistToGUAC(ctx, g, paths); err != nil {
			log.Printf("guac persistence failed for %s: %v", subjectName, err)
		} else {
			log.Printf("guac persistence complete for %s: %d packages, %d flagged", subjectName, len(g.Nodes), len(paths))
		}
	}()
}

func nodePkgType(n graphNode, ecosystem string) string {
	if n.Relation == "ROOT" {
		return "guac"
	}
	if ecosystem == "" {
		return "npm"
	}
	return ecosystem
}

// pkgInputForNode builds a package input for one graph node, tagging the
// synthetic repository root with an "ecosystem" qualifier so it can be read
// back later — see pkgInputQualified for why that round trip matters.
func pkgInputForNode(n graphNode, ecosystem string) map[string]any {
	if n.Relation == "ROOT" {
		return pkgInputQualified("guac", n.Name, n.Version, map[string]string{"ecosystem": ecosystem})
	}
	return pkgInput(nodePkgType(n, ecosystem), n.Name, n.Version)
}

func (s *apiServer) doPersistToGUAC(ctx context.Context, g *graph, paths []attackPath) error {
	// 1. Every node becomes a package in the trie.
	pkgs := make([]map[string]any, len(g.Nodes))
	for i, n := range g.Nodes {
		pkgs[i] = pkgInputForNode(n, g.ecosystem)
	}
	for _, group := range chunk(pkgs, 200) {
		if err := s.guac.ingestPackages(ctx, group); err != nil {
			return fmt.Errorf("ingesting packages: %w", err)
		}
	}

	// 2. Every edge becomes an IsDependency attestation.
	var edges []dependencyEdgeInput
	for from, tos := range g.adjacency {
		for _, to := range tos {
			note := "resolved via deps.dev"
			if g.Nodes[to].Relation == "DEV" {
				note = "development dependency, resolved via deps.dev"
			}
			edges = append(edges, dependencyEdgeInput{
				Pkg:    pkgInputForNode(g.Nodes[from], g.ecosystem),
				DepPkg: pkgInputForNode(g.Nodes[to], g.ecosystem),
				Type:   dependencyType(g.Nodes[to].Relation),
				Note:   note,
			})
		}
	}
	for _, group := range chunk(edges, 200) {
		if err := s.guac.ingestDependencies(ctx, group); err != nil {
			return fmt.Errorf("ingesting dependency edges: %w", err)
		}
	}

	// 3. Every distinct finding ID becomes a vulnerability, and every
	// (package, finding) pair a certification, exactly as GUAC's own OSV
	// certifier would record it — this build gathers the finding via our own
	// intel-svc-equivalent code path instead of GUAC's collector, but writes
	// it into the same shape.
	seenVulnIDs := map[string]bool{}
	var vulnIDs []string
	var certs []certifyVulnInput
	now := time.Now()
	for _, p := range paths {
		pkg := pkgInput(nodePkgType(graphNode{Relation: p.Target.Relation}, g.ecosystem), p.Target.Name, p.Target.Version)
		for _, f := range p.Findings {
			if !seenVulnIDs[f.ID] {
				seenVulnIDs[f.ID] = true
				vulnIDs = append(vulnIDs, f.ID)
			}
			certs = append(certs, certifyVulnInput{Pkg: pkg, VulnID: f.ID, ScanTime: now})
		}
	}
	for _, group := range chunk(vulnIDs, 200) {
		if err := s.guac.ingestVulnerabilities(ctx, group); err != nil {
			return fmt.Errorf("ingesting vulnerabilities: %w", err)
		}
	}
	for _, group := range chunk(certs, 200) {
		if err := s.guac.ingestCertifyVulns(ctx, group); err != nil {
			return fmt.Errorf("ingesting vulnerability certifications: %w", err)
		}
	}
	return nil
}
