package main

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"
)

// This file is the "which lockfiles resolved to the compromised version
// while it was live" question from Track 02, built on the thing HydraDB's
// own docs lead with: "git-style temporal versioning recalls what was true
// at any point in time." Every scan narrates its flagged findings into
// HydraDB as timestamped natural-language facts, so a later question like
// "which repos were using http-retry 1.9.1" has a real timeline to answer
// from — not a guaranteed-complete audit log (see hydradb.go for why), but
// a real, growing, queryable history that gets more useful every scan.

// persistTimelineFacts is fire-and-forget, like persistToGUAC: HydraDB
// being slow or down must never hold up the scan response the user is
// waiting on.
func (s *apiServer) persistTimelineFacts(ctx context.Context, subjectKind, subjectName, ecosystem string, scannedAt time.Time, paths []attackPath) {
	if s.hydra == nil || len(paths) == 0 {
		return
	}
	go func() {
		facts := make([]hydraKnowledgeItem, 0, len(paths))
		ts := scannedAt.UTC().Format(time.RFC3339)
		for _, p := range paths {
			var findingIDs []string
			for _, f := range p.Findings {
				findingIDs = append(findingIDs, f.ID)
			}
			advisories := "no specific advisory ID on record"
			if len(findingIDs) > 0 {
				advisories = strings.Join(findingIDs, ", ")
			}
			text := fmt.Sprintf(
				"At %s, the %s %s (%s ecosystem) resolved dependency %s to version %s. "+
					"That version was flagged %s with a risk score of %d out of 100. "+
					"Related advisories: %s.",
				ts, subjectKind, subjectName, ecosystem,
				p.Target.Name, p.Target.Version,
				p.Score.Label, p.Score.Value, advisories,
			)
			item := hydraKnowledgeItem{Title: fmt.Sprintf("%s@%s:%s@%s", subjectName, ts, p.Target.Name, p.Target.Version)}
			item.Content.Text = text
			item.AdditionalMetadata = map[string]any{
				"subject_kind":   subjectKind,
				"subject_name":   subjectName,
				"ecosystem":      ecosystem,
				"target_name":    p.Target.Name,
				"target_version": p.Target.Version,
				"risk_label":     p.Score.Label,
				"risk_score":     p.Score.Value,
				"scanned_at":     ts,
			}
			if len(findingIDs) > 0 {
				item.AdditionalMetadata["finding_ids"] = findingIDs
			}
			facts = append(facts, item)
		}

		// Deliberately not collection-scoped: the whole point of this
		// timeline is answering "which repos/packages" across everything
		// ever scanned, so facts go into the database's default collection
		// where a cross-subject query can reach all of them. Exactness for
		// "just this package" or "just this ecosystem" comes from
		// AdditionalMetadata above plus metadata_filters at query time
		// (see handleAsk in main.go), not from collection scoping.
		bgCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 60*time.Second)
		defer cancel()
		ids, err := s.hydra.ingestFacts(bgCtx, "", facts)
		if err != nil {
			log.Printf("hydradb timeline ingest failed for %s: %v", subjectName, err)
			return
		}
		log.Printf("hydradb timeline ingest queued %d fact(s) for %s, waiting for indexing", len(ids), subjectName)
		s.hydra.waitForIndexing(bgCtx, ids)
		log.Printf("hydradb timeline ingest finished indexing for %s", subjectName)
	}()
}
