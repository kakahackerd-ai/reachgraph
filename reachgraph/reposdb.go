package main

import (
	"context"
	"fmt"
	"log"
	"time"
)

// This file lets HydraDB stand in for GUAC on the one thing GUAC's
// persistence is actually read back for in this running app: the
// tracked-repository list behind GET /api/repos. GUAC's own bulk mutations
// (ingestPackages, ingestDependencies, ingestVulnerabilities,
// ingestCertifyVulns — see guac.go) build a real, exact SBOM-style graph,
// but the only piece of it any handler reads back out today is
// listScannedSubjects: a flat {name, ecosystem} list for the dashboard's
// tracked-repo cards. That's a real, exact-match need — "which repos have
// been scanned, under which ecosystem" — but it doesn't need GUAC's
// pURL/dependency-edge graph semantics to answer, so it's a genuine fit to
// hand to HydraDB instead, for anyone who'd rather not stand up a
// self-hosted GUAC + Postgres deployment just to get a persisted repo
// list back across restarts.
//
// This is deliberately *not* a wholesale GUAC replacement: the exact
// dependency graph, vulnerability certifications, and diamond-dependency
// deduplication GUAC persists are real supply-chain-schema data that
// HydraDB's own extraction pipeline isn't built to reproduce — see the
// "HydraDB" section of the README for why the graph-relations endpoint
// this build already uses for code-graph facts is fact-extraction-based,
// not a typed exact store, and would be the wrong tool for that job.
// If GUAC is configured, it still owns /api/repos (see handleListRepos in
// main.go); HydraDB only takes over when GUAC isn't configured, so a repo
// list survives restarts either way as long as one of the two is set.

const hydraTrackedReposCollection = "tracked-repos"

// persistTrackedRepo records that a repository was scanned, replacing any
// previous record for the same repo rather than accumulating one fact per
// scan (unlike the narrative timeline, which wants that history — this is
// a current-state list, not a log). Fire-and-forget, like every other
// HydraDB write in this build: a slow or unreachable HydraDB must never
// hold up the scan response.
func (s *apiServer) persistTrackedRepo(ctx context.Context, name, ecosystem string) {
	if s.hydra == nil {
		return
	}
	go func() {
		bgCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 60*time.Second)
		defer cancel()

		// Find and delete this repo's own previous entry by title match
		// before ingesting the new one. Ingest is sent with upsert=true,
		// but this client has no live confirmation of what key HydraDB
		// actually upserts on (see hydradb.go's doc comment on
		// unconfirmed corners), so this doesn't rely on that alone —
		// same defensive pattern proven live for code-graph cleanup in
		// codegraph.go.
		if existing, err := s.hydra.listSources(bgCtx, hydraTrackedReposCollection); err != nil {
			log.Printf("tracked-repo: listing existing entries failed for %s (continuing without cleanup): %v", name, err)
		} else {
			var staleIDs []string
			for _, src := range existing {
				if src.Title == name {
					staleIDs = append(staleIDs, src.ID)
				}
			}
			if len(staleIDs) > 0 {
				if _, err := s.hydra.deleteSources(bgCtx, hydraTrackedReposCollection, staleIDs); err != nil {
					log.Printf("tracked-repo: deleting stale entry for %s failed (continuing): %v", name, err)
				}
			}
		}

		item := hydraKnowledgeItem{Title: name}
		item.Content.Text = fmt.Sprintf("%s was scanned as a %s repository.", name, ecosystem)
		item.AdditionalMetadata = map[string]any{
			"kind":            "repository",
			"ecosystem":       ecosystem,
			"last_scanned_at": time.Now().UTC().Format(time.RFC3339),
		}
		ids, err := s.hydra.ingestFacts(bgCtx, hydraTrackedReposCollection, []hydraKnowledgeItem{item})
		if err != nil {
			log.Printf("tracked-repo: hydradb ingest failed for %s: %v", name, err)
			return
		}
		log.Printf("tracked-repo: recorded %s (%s) in hydradb", name, ecosystem)
		s.hydra.waitForIndexing(bgCtx, hydraTrackedReposCollection, ids)
	}()
}

// listTrackedRepos is the HydraDB-backed equivalent of
// guacClient.listScannedSubjects: every repository recorded via
// persistTrackedRepo, with the ecosystem it was scanned under.
func (s *apiServer) listTrackedRepos(ctx context.Context) ([]trackedRepo, error) {
	sources, err := s.hydra.listSources(ctx, hydraTrackedReposCollection)
	if err != nil {
		return nil, err
	}
	repos := make([]trackedRepo, 0, len(sources))
	for _, src := range sources {
		if src.Title == "" {
			continue
		}
		eco, _ := src.AdditionalMetadata["ecosystem"].(string)
		if eco == "" {
			eco = "npm" // default, matching guacClient.listScannedSubjects
		}
		repos = append(repos, trackedRepo{Name: src.Title, Ecosystem: eco})
	}
	return repos, nil
}
