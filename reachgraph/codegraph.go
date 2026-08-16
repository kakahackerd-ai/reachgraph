package main

import (
	"context"
	"fmt"
	"log"
	"regexp"
	"strings"
	"time"
)

// This file is Track B: "build a code graph and serve better context with
// it" for IDE assistants, as an alternative to plain embedding-similarity
// chunking. It reuses the exact source-file listing this build already has
// for reachability (listSourceFiles/rawFile — see reachability.go) but
// extracts structure instead of just checking one import at a time:
// function and class/type definitions, plus every module a file imports —
// real static analysis via regex, not an LLM guessing at code structure.
//
// That structure is narrated into HydraDB (see hydradb.go) the same way
// timeline.go narrates scan history: real facts in, HydraDB's own
// extraction turns them into a queryable graph, and /api/ask lets an IDE
// assistant (or a person) ask "where is X defined" / "what does Y import"
// and get graph-relevant answers instead of the top-k similar-looking
// chunks a plain vector index would return. This is HydraDB's own stated
// designed purpose — a context graph for AI agents — used for exactly that,
// unlike the timeline feature, which uses it slightly against the grain.

var jsDefPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(`),
	regexp.MustCompile(`(?m)^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)`),
	regexp.MustCompile(`(?m)^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>`),
}
var jsImportPattern = regexp.MustCompile(`(?:require\(\s*['"]([^'"]+)['"]\)|from\s+['"]([^'"]+)['"])`)

var pyDefPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?m)^\s*def\s+([A-Za-z_]\w*)\s*\(`),
	regexp.MustCompile(`(?m)^\s*class\s+([A-Za-z_]\w*)`),
}
var pyImportPattern = regexp.MustCompile(`(?m)^\s*(?:import\s+([\w.]+)|from\s+([\w.]+)\s+import)`)

type codeSymbol struct {
	File string
	Kind string // "function" | "class"
	Name string
}

type codeImport struct {
	File   string
	Module string
}

// extractCodeStructure runs the right regex set for the ecosystem against
// one file's real content.
func extractCodeStructure(ecosystem, path, content string) ([]codeSymbol, []codeImport) {
	defPatterns, importPattern, kinds := jsDefPatterns, jsImportPattern, []string{"function", "class", "function"}
	if ecosystem == "pypi" {
		defPatterns, importPattern, kinds = pyDefPatterns, pyImportPattern, []string{"function", "class"}
	}

	var symbols []codeSymbol
	for i, re := range defPatterns {
		for _, m := range re.FindAllStringSubmatch(content, -1) {
			symbols = append(symbols, codeSymbol{File: path, Kind: kinds[i], Name: m[1]})
		}
	}

	var imports []codeImport
	seen := map[string]bool{}
	for _, m := range importPattern.FindAllStringSubmatch(content, -1) {
		mod := firstNonEmpty(m[1:])
		if mod == "" || seen[mod] {
			continue
		}
		seen[mod] = true
		imports = append(imports, codeImport{File: path, Module: mod})
	}
	return symbols, imports
}

func firstNonEmpty(ss []string) string {
	for _, s := range ss {
		if s != "" {
			return s
		}
	}
	return ""
}

const maxCodeGraphFiles = 60 // bounded: this narrates one fact per symbol/import to HydraDB, real network calls each

// indexCodeGraph fetches a bounded set of the repository's real source
// files, extracts their real structure, and narrates it into HydraDB.
// Fire-and-forget, like the timeline and GUAC persistence — a slow or
// unconfigured HydraDB must never hold up a scan response.
func (s *apiServer) indexCodeGraph(ctx context.Context, ecosystem, owner, repo, ref string) {
	if s.hydra == nil {
		return
	}
	go func() {
		bgCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 120*time.Second)
		defer cancel()

		files, err := s.github.listSourceFiles(bgCtx, ecosystem, owner, repo, ref)
		if err != nil {
			log.Printf("code graph: listing source files failed for %s/%s: %v", owner, repo, err)
			return
		}
		if len(files) > maxCodeGraphFiles {
			files = files[:maxCodeGraphFiles]
		}

		var facts []hydraKnowledgeItem
		repoLabel := owner + "/" + repo
		for _, path := range files {
			content, ok, err := s.github.rawFile(bgCtx, owner, repo, ref, path)
			if err != nil || !ok {
				continue
			}
			symbols, imports := extractCodeStructure(ecosystem, path, string(content))
			if len(symbols) == 0 && len(imports) == 0 {
				continue
			}

			var sb strings.Builder
			fmt.Fprintf(&sb, "In repository %s, file %s ", repoLabel, path)
			var parts []string
			for _, sym := range symbols {
				parts = append(parts, fmt.Sprintf("defines %s %s", sym.Kind, sym.Name))
			}
			for _, imp := range imports {
				parts = append(parts, fmt.Sprintf("imports %s", imp.Module))
			}
			sb.WriteString(strings.Join(parts, "; "))
			sb.WriteString(".")

			item := hydraKnowledgeItem{Title: repoLabel + ":" + path}
			item.Content.Text = sb.String()
			facts = append(facts, item)
		}

		if len(facts) == 0 {
			return
		}
		ids, err := s.hydra.ingestFacts(bgCtx, facts)
		if err != nil {
			log.Printf("code graph: hydradb ingest failed for %s: %v", repoLabel, err)
			return
		}
		log.Printf("code graph: indexed %d file(s) into hydradb for %s", len(ids), repoLabel)
	}()
}
