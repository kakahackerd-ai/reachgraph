package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"mime/multipart"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"time"
)

// hydraDBClient wraps the real HydraDB v2 API.
//
// The original three operations here (ingestFacts, waitForIndexing, query)
// were confirmed against the live API with a real key during earlier
// development — the documented request shapes turned out not to be what the
// API actually accepts (see the shapes below). Everything added after that
// — collections, additional_metadata, metadata_filters, batched status
// polling, listSourceIDs/deleteSources, relations, stats, submitFeedback —
// was implemented directly against HydraDB's own published OpenAPI document
// (https://docs.hydradb.com/api-reference/v2/openapi.json, fetched
// 2026-08-16), because no API key was available in this environment to
// re-confirm it live the way the original three were. That's flagged here,
// not smoothed over, in case a later run with a real key finds another
// documented-vs-actual gap the way ingest's JSON-vs-multipart one was found.
// One specific spot this matters: /context/list's response schema
// (list.V2SourceListResponse) renders in the OpenAPI doc as
// `{"inner": {...fields...}}`, unlike every sibling envelope in this API,
// which is very likely an artifact of how the spec generator represents an
// embedded Go struct rather than the real wire shape — but listSourceIDs
// below is written defensively and fails open (logs and continues) rather
// than blocking anything if that guess is wrong.
//
//   - POST /context/ingest is multipart/form-data, not JSON. A "database"
//     field plus an "app_knowledge" field: a JSON array of
//     {title, content: {text}, additional_metadata} objects. HydraDB
//     extracts entities and relations from that text itself, via its own
//     pipeline — you cannot hand it a pre-formed graph triplet directly; a
//     bare {subject,predicate,object} array is silently accepted and
//     produces an empty document with no extracted graph. A "collection"
//     field scopes the ingest to a sub-tenant namespace within the
//     database — used here to keep one repository's code-graph facts from
//     bleeding into another's.
//   - Ingestion is asynchronous: poll GET /context/status?database=...&ids=...
//     (batched — one call covers every pending id) until each source's
//     indexing_status is "completed" or "failed" (not "errored" — an
//     earlier version of this client checked for the wrong terminal string
//     and would poll every id until the context timeout instead of
//     stopping as soon as HydraDB actually finished).
//   - POST /query takes {database, query, type, graph_context, ...} as a
//     JSON body (database in the body, not the query string — the query
//     string form was tried first and rejected). Results are
//     relevancy-scored retrieval (graph_context.query_paths, each with a
//     relevancy_score), not a guaranteed-complete traversal — there is no
//     "give me every match" mode in this endpoint. metadata_filters and
//     collections narrow the candidate pool with exact matches before
//     ranking runs, which is the closest this API gets to an exact query
//     for anything tagged at ingest time.
//   - GET /context/relations returns the actual extracted graph — every
//     relation for a database or a single source, paginated, not ranked —
//     which is the "give me every match" primitive /query doesn't have.
//   - POST /context/list and DELETE /context manage what's already
//     ingested: used here to drop a repository's previous code-graph facts
//     before re-ingesting current ones, so a repeatedly-rescanned repo
//     doesn't accumulate stale per-file facts for code that no longer
//     exists.
//   - GET /databases/stats reports real row counts for a database's
//     knowledge/memory collections — surfaced on /api/status instead of
//     just a boolean "configured" flag.
//   - POST /feedback records a rating/comment against a previous query's
//     request_id (from that query's response meta) — the API rejects a
//     rating with no text, so submitFeedback always sends non-empty
//     feedback text, synthesizing one from the rating if the caller didn't
//     supply a comment.
type hydraDBClient struct {
	http     *http.Client
	apiKey   string
	database string
	base     string
}

func newHydraDBClient(apiKey, database string) *hydraDBClient {
	if database == "" {
		database = "default-tenant"
	}
	return &hydraDBClient{
		http:     &http.Client{Timeout: 30 * time.Second},
		apiKey:   apiKey,
		database: database,
		base:     "https://api.hydradb.com",
	}
}

func (c *hydraDBClient) authedRequest(ctx context.Context, method, url string, body *bytes.Buffer, contentType string) (*http.Request, error) {
	var reqBody *bytes.Buffer
	if body == nil {
		reqBody = &bytes.Buffer{}
	} else {
		reqBody = body
	}
	req, err := http.NewRequestWithContext(ctx, method, url, reqBody)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("API-Version", "2")
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	return req, nil
}

// hydraCollectionName turns an arbitrary label (typically "owner/repo")
// into a value safe to send as a HydraDB collection name: no slashes or
// other punctuation collection names aren't documented to accept.
var collectionUnsafe = regexp.MustCompile(`[^A-Za-z0-9_-]+`)

func hydraCollectionName(s string) string {
	s = collectionUnsafe.ReplaceAllString(s, "-")
	if len(s) > 128 {
		s = s[:128]
	}
	return s
}

type hydraKnowledgeItem struct {
	Title   string `json:"title"`
	Content struct {
		Text string `json:"text"`
	} `json:"content"`
	// AdditionalMetadata is free-form per-document metadata (capped at 1
	// KiB by the API) — used here to tag facts with the structured fields
	// (repo, ecosystem, target package, risk label, ...) a natural-language
	// query can't reliably filter on, so callers can pass them as exact
	// metadata_filters instead of hoping the ranker surfaces them.
	AdditionalMetadata map[string]any `json:"additional_metadata,omitempty"`
}

type hydraIngestResult struct {
	ID     string `json:"id"`
	Status string `json:"status"`
}

// ingestFacts submits a batch of natural-language facts for HydraDB's own
// pipeline to extract entities and relations from. collection scopes the
// ingest to a sub-tenant namespace within the database; pass "" to use the
// database's default collection. Returns the source IDs to poll for
// completion.
func (c *hydraDBClient) ingestFacts(ctx context.Context, collection string, facts []hydraKnowledgeItem) ([]string, error) {
	if len(facts) == 0 {
		return nil, nil
	}
	payload, err := json.Marshal(facts)
	if err != nil {
		return nil, err
	}

	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	if err := w.WriteField("database", c.database); err != nil {
		return nil, err
	}
	if collection != "" {
		if err := w.WriteField("collection", collection); err != nil {
			return nil, err
		}
	}
	if err := w.WriteField("upsert", "true"); err != nil {
		return nil, err
	}
	if err := w.WriteField("app_knowledge", string(payload)); err != nil {
		return nil, err
	}
	if err := w.Close(); err != nil {
		return nil, err
	}

	req, err := c.authedRequest(ctx, http.MethodPost, c.base+"/context/ingest", &buf, w.FormDataContentType())
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("hydradb ingest: %w", err)
	}
	defer resp.Body.Close()

	var body struct {
		Success bool `json:"success"`
		Data    struct {
			Results []hydraIngestResult `json:"results"`
		} `json:"data"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("decoding hydradb ingest response: %w", err)
	}
	if !body.Success {
		msg := "unknown error"
		if body.Error != nil {
			msg = body.Error.Message
		}
		return nil, fmt.Errorf("hydradb ingest rejected: %s", msg)
	}
	ids := make([]string, len(body.Data.Results))
	for i, r := range body.Data.Results {
		ids[i] = r.ID
	}
	return ids, nil
}

// waitForIndexing polls until every source finishes indexing or the
// context deadline is hit, in a single batched request per poll cycle
// (GET /context/status accepts a repeated "ids" query param covering every
// pending source, rather than one request per id — the earlier version of
// this client polled each id in its own sequential loop, which meant a
// slow first id could starve every id after it of most of the deadline).
// collection must match what the ids were ingested into (or be "" for the
// default collection) — live testing found that /context/status, like
// /context/delete, scopes id lookups by collection: omitting it for a
// collection-scoped id (every code-graph fact) doesn't error, it just
// answers "ID not found" (indexing_status "errored") for every poll until
// the context deadline, which looks indistinguishable from a real timeout
// unless you go looking. That's also why "errored" is treated as terminal
// below alongside "completed"/"failed": it's not in the OpenAPI status
// enum (which lists only queued/processing/completed/failed), but it's a
// real value the live API returns, and — whether it means "wrong scope"
// or a genuine indexing error — it will never become "completed" either
// way, so there's nothing to gain by continuing to poll it.
// Best-effort: a slow or failed index doesn't panic the caller, it just
// stops waiting.
func (c *hydraDBClient) waitForIndexing(ctx context.Context, collection string, ids []string) {
	if len(ids) == 0 {
		return
	}
	pending := make(map[string]bool, len(ids))
	for _, id := range ids {
		pending[id] = true
	}
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		q := url.Values{"database": {c.database}}
		if collection != "" {
			q.Set("collection", collection)
		}
		for id := range pending {
			q.Add("ids", id)
		}
		req, err := c.authedRequest(ctx, http.MethodGet, c.base+"/context/status?"+q.Encode(), nil, "")
		if err != nil {
			return
		}
		resp, err := c.http.Do(req)
		if err != nil {
			return
		}
		var body struct {
			Data struct {
				Statuses []struct {
					ID             string `json:"id"`
					IndexingStatus string `json:"indexing_status"`
				} `json:"statuses"`
			} `json:"data"`
		}
		_ = json.NewDecoder(resp.Body).Decode(&body)
		resp.Body.Close()
		for _, s := range body.Data.Statuses {
			if s.IndexingStatus == "completed" || s.IndexingStatus == "failed" || s.IndexingStatus == "errored" {
				delete(pending, s.ID)
			}
		}
		if len(pending) == 0 {
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}

type hydraTriplet struct {
	Source         string  `json:"source"`
	Relation       string  `json:"relation"`
	Target         string  `json:"target"`
	Context        string  `json:"context"`
	RelevancyScore float64 `json:"relevancyScore"`
}

type hydraChunk struct {
	SourceTitle    string  `json:"sourceTitle"`
	Content        string  `json:"content"`
	RelevancyScore float64 `json:"relevancyScore"`
}

type hydraSourceRef struct {
	ID    string `json:"id"`
	Title string `json:"title"`
}

type hydraQueryResult struct {
	RequestID string           `json:"requestId,omitempty"` // meta.request_id — pass to submitFeedback
	Answer    string           `json:"answer"`              // best combined_context, if any
	Triplets  []hydraTriplet   `json:"triplets"`
	Chunks    []hydraChunk     `json:"chunks,omitempty"`
	Sources   []hydraSourceRef `json:"sources,omitempty"`
}

// hydraQueryOptions narrows a query with HydraDB's exact-match primitives
// instead of relying only on ranked relevance: Collections restricts the
// search to one or more sub-tenant scopes (e.g. one repository's own
// code-graph collection), MetadataFilters does an exact match against
// additional_metadata set at ingest time, and SourceIDs restricts to
// specific already-known source ids.
type hydraQueryOptions struct {
	Collections     []string
	MetadataFilters map[string]any
	SourceIDs       []string
	MaxResults      int
}

// hydraRawRelationGroup is the wire shape shared by graph_context's
// query_paths and chunk_relations arrays.
type hydraRawRelationGroup struct {
	Triplets []struct {
		Source struct {
			Name string `json:"name"`
		} `json:"source"`
		Relation struct {
			CanonicalPredicate string `json:"canonical_predicate"`
			Context            string `json:"context"`
		} `json:"relation"`
		Target struct {
			Name string `json:"name"`
		} `json:"target"`
	} `json:"triplets"`
	RelevancyScore  float64 `json:"relevancy_score"`
	CombinedContext string  `json:"combined_context"`
}

// query asks a natural-language question and returns the graph paths
// HydraDB judged relevant — ranked retrieval, explicitly not a guaranteed-
// complete result set. Callers that need completeness (Track A's blast
// radius) do not use this; this is for the narrative-timeline and
// code-context features where "the most relevant answers" is the right
// model, not a limitation. opts can narrow the candidate pool with exact
// collection/metadata matches before that ranking runs.
func (c *hydraDBClient) query(ctx context.Context, question string, opts hydraQueryOptions) (*hydraQueryResult, error) {
	payload := map[string]any{
		"database":      c.database,
		"query":         question,
		"type":          "knowledge",
		"graph_context": true,
	}
	if len(opts.Collections) > 0 {
		payload["collections"] = opts.Collections
	}
	if len(opts.MetadataFilters) > 0 {
		payload["metadata_filters"] = opts.MetadataFilters
	}
	if len(opts.SourceIDs) > 0 {
		payload["ids"] = opts.SourceIDs
	}
	if opts.MaxResults > 0 {
		payload["max_results"] = opts.MaxResults
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	req, err := c.authedRequest(ctx, http.MethodPost, c.base+"/query", bytes.NewBuffer(body), "application/json")
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("hydradb query: %w", err)
	}
	defer resp.Body.Close()

	var parsed struct {
		Success bool `json:"success"`
		Data    struct {
			Chunks []struct {
				SourceTitle    string  `json:"source_title"`
				ChunkContent   string  `json:"chunk_content"`
				RelevancyScore float64 `json:"relevancy_score"`
			} `json:"chunks"`
			Sources []struct {
				ID    string `json:"id"`
				Title string `json:"title"`
			} `json:"sources"`
			GraphContext struct {
				// QueryPaths and ChunkRelations share the same
				// {triplets, relevancy_score, combined_context} shape.
				// Live testing against a real key found QueryPaths coming
				// back empty ([]) for a straightforward question while
				// ChunkRelations carried real, correctly-extracted triplets
				// for the exact same query — so both are parsed and
				// merged below rather than trusting QueryPaths alone the
				// way the OpenAPI doc's naming ("query_paths" sounds like
				// *the* answer) would suggest.
				QueryPaths     []hydraRawRelationGroup `json:"query_paths"`
				ChunkRelations []hydraRawRelationGroup `json:"chunk_relations"`
			} `json:"graph_context"`
		} `json:"data"`
		Meta struct {
			RequestID string `json:"request_id"`
		} `json:"meta"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, fmt.Errorf("decoding hydradb query response: %w", err)
	}
	if !parsed.Success {
		msg := "unknown error"
		if parsed.Error != nil {
			msg = parsed.Error.Message
		}
		return nil, fmt.Errorf("hydradb query rejected: %s", msg)
	}

	result := &hydraQueryResult{RequestID: parsed.Meta.RequestID}
	bestScore := -1.0
	for _, groups := range [][]hydraRawRelationGroup{parsed.Data.GraphContext.QueryPaths, parsed.Data.GraphContext.ChunkRelations} {
		for _, p := range groups {
			if p.CombinedContext != "" && p.RelevancyScore > bestScore {
				bestScore = p.RelevancyScore
				result.Answer = p.CombinedContext
			}
			for _, t := range p.Triplets {
				result.Triplets = append(result.Triplets, hydraTriplet{
					Source: t.Source.Name, Relation: t.Relation.CanonicalPredicate, Target: t.Target.Name,
					Context: t.Relation.Context, RelevancyScore: p.RelevancyScore,
				})
			}
		}
	}
	for _, ch := range parsed.Data.Chunks {
		result.Chunks = append(result.Chunks, hydraChunk{
			SourceTitle: ch.SourceTitle, Content: extractChunkText(ch.ChunkContent), RelevancyScore: ch.RelevancyScore,
		})
	}
	for _, s := range parsed.Data.Sources {
		result.Sources = append(result.Sources, hydraSourceRef{ID: s.ID, Title: s.Title})
	}
	return result, nil
}

// extractChunkText unwraps the plain sentence out of a chunk's raw content.
// Live testing found that for a "knowledge" source ingested via
// app_knowledge (this client's own ingestFacts path), chunk_content isn't
// the plain text — it's the entire underlying source document serialized
// as a JSON string, e.g. {"id":"...","content":{"text":"At 2026-...
// resolved dependency..."},"document_metadata":{...},...}. This pulls just
// content.text back out for display; if the string isn't that shape (a
// different ingestion path, e.g. a real uploaded file, may return plain
// text directly per the OpenAPI examples), it's returned unchanged.
func extractChunkText(raw string) string {
	var doc struct {
		Content struct {
			Text string `json:"text"`
		} `json:"content"`
	}
	if err := json.Unmarshal([]byte(raw), &doc); err == nil && doc.Content.Text != "" {
		return doc.Content.Text
	}
	return raw
}

// listSourceIDs returns every knowledge-source id in the given collection
// (all of the database's default collection if collection is ""). Used to
// find a repository's previous code-graph facts before deleting them ahead
// of a re-scan. Best-effort by design: see the package doc comment above
// on why this endpoint's exact response shape is the least-confirmed part
// of this client — a decode miss here returns an empty slice with no
// entries found, which the caller treats as "nothing to clean up," not an
// error.
func (c *hydraDBClient) listSourceIDs(ctx context.Context, collection string) ([]string, error) {
	sources, err := c.listSources(ctx, collection)
	if err != nil {
		return nil, err
	}
	ids := make([]string, len(sources))
	for i, s := range sources {
		ids[i] = s.ID
	}
	return ids, nil
}

type hydraListedSource struct {
	ID                 string
	Title              string
	AdditionalMetadata map[string]any
}

// listSources returns every knowledge source in the given collection (the
// database's default collection if collection is ""), with title and
// additional_metadata — used by listSourceIDs above for code-graph
// cleanup, and directly by the tracked-repository listing in reposdb.go,
// which needs the metadata (ecosystem) back, not just ids. Confirmed live
// that "sources" comes back flattened directly under data, not wrapped
// (see the package doc comment on why that was worth confirming).
func (c *hydraDBClient) listSources(ctx context.Context, collection string) ([]hydraListedSource, error) {
	payload := map[string]any{
		"database": c.database,
		"type":     "knowledge",
		"page":     1,
		// 100 is the real API's actual maximum — confirmed live, since the
		// OpenAPI doc's page_size field carries no documented upper bound.
		// maxCodeGraphFiles (60) keeps a single repo's code-graph facts
		// well under this, so one page is enough for listSourceIDs'
		// stale-cleanup use case; this isn't a general-purpose paginated
		// listing.
		"page_size": 100,
		// No include_fields: confirmed live that the API rejects
		// `["id"]` ("invalid include_fields: id") even though the
		// OpenAPI example shows exactly that — id is already always
		// returned per the field's own description ("plus id, database,
		// collection are returned"), so there's nothing to project.
	}
	if collection != "" {
		payload["collection"] = collection
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	req, err := c.authedRequest(ctx, http.MethodPost, c.base+"/context/list", bytes.NewBuffer(body), "application/json")
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("hydradb list: %w", err)
	}
	defer resp.Body.Close()

	var parsed struct {
		Success bool `json:"success"`
		Data    struct {
			Sources []map[string]any `json:"sources"`
		} `json:"data"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, fmt.Errorf("decoding hydradb list response: %w", err)
	}
	if !parsed.Success {
		msg := "unknown error"
		if parsed.Error != nil {
			msg = parsed.Error.Message
		}
		return nil, fmt.Errorf("hydradb list rejected: %s", msg)
	}
	sources := make([]hydraListedSource, 0, len(parsed.Data.Sources))
	for _, src := range parsed.Data.Sources {
		id, _ := src["id"].(string)
		if id == "" {
			continue
		}
		title, _ := src["title"].(string)
		meta, _ := src["additional_metadata"].(map[string]any)
		sources = append(sources, hydraListedSource{ID: id, Title: title, AdditionalMetadata: meta})
	}
	return sources, nil
}

// deleteSources deletes knowledge sources by id, scoped to collection (must
// match the collection the ids were ingested into — live testing found
// that omitting it makes the API silently match nothing and still answer
// 200/success, per its own documented "legacy" delete-status default,
// which is exactly the kind of no-op a caller could mistake for a real
// delete). Requests strict status codes and returns the real deleted
// count instead of trusting a 200 alone, so a caller can tell "deleted N
// of M" from "deleted nothing."
func (c *hydraDBClient) deleteSources(ctx context.Context, collection string, ids []string) (int, error) {
	if len(ids) == 0 {
		return 0, nil
	}
	payload := map[string]any{
		"database": c.database,
		"ids":      ids,
		"type":     "knowledge",
	}
	if collection != "" {
		payload["collection"] = collection
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return 0, err
	}
	req, err := c.authedRequest(ctx, http.MethodDelete, c.base+"/context", bytes.NewBuffer(body), "application/json")
	if err != nil {
		return 0, err
	}
	req.Header.Set("X-HydraDB-Delete-Status", "strict")
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, fmt.Errorf("hydradb delete: %w", err)
	}
	defer resp.Body.Close()

	var parsed struct {
		Success bool `json:"success"`
		Data    struct {
			DeletedCount int `json:"deleted_count"`
		} `json:"data"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return 0, fmt.Errorf("decoding hydradb delete response (status %d): %w", resp.StatusCode, err)
	}
	if resp.StatusCode >= 400 && resp.StatusCode != 404 {
		// 404 in strict mode just means "nothing matched" — not a failure
		// worth erroring the caller over, since the sources may simply
		// have expired or already been deleted.
		msg := "unknown error"
		if parsed.Error != nil {
			msg = parsed.Error.Message
		}
		return parsed.Data.DeletedCount, fmt.Errorf("hydradb delete: status %d: %s", resp.StatusCode, msg)
	}
	return parsed.Data.DeletedCount, nil
}

type hydraRelation struct {
	Source     string  `json:"source"`
	Predicate  string  `json:"predicate"`
	Target     string  `json:"target"`
	Context    string  `json:"context,omitempty"`
	Confidence float64 `json:"confidence,omitempty"`
}

type hydraRelationsResult struct {
	Relations []hydraRelation `json:"relations"`
	Truncated bool            `json:"truncated"`
}

// relations returns the actual extracted knowledge graph for a collection
// (or, if sourceID is set, for just that one source) — every relation
// HydraDB has, up to limit, not a ranked top-k. This is the "give me every
// match" primitive /query deliberately doesn't offer.
func (c *hydraDBClient) relations(ctx context.Context, collection, sourceID string, limit int) (*hydraRelationsResult, error) {
	q := url.Values{"database": {c.database}}
	if collection != "" {
		q.Set("collection", collection)
	}
	if sourceID != "" {
		q.Set("id", sourceID)
	}
	if limit > 0 {
		q.Set("limit", strconv.Itoa(limit))
	}
	req, err := c.authedRequest(ctx, http.MethodGet, c.base+"/context/relations?"+q.Encode(), nil, "")
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("hydradb relations: %w", err)
	}
	defer resp.Body.Close()

	var parsed struct {
		Success bool `json:"success"`
		Data    struct {
			IsTruncated bool `json:"is_truncated"`
			Relations   []struct {
				Source struct {
					Identifier string `json:"identifier"`
					Name       string `json:"name"`
				} `json:"source"`
				Target struct {
					Identifier string `json:"identifier"`
					Name       string `json:"name"`
				} `json:"target"`
				Relations []struct {
					CanonicalPredicate string  `json:"canonical_predicate"`
					Context            string  `json:"context"`
					Confidence         float64 `json:"confidence"`
				} `json:"relations"`
			} `json:"relations"`
		} `json:"data"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, fmt.Errorf("decoding hydradb relations response: %w", err)
	}
	if !parsed.Success {
		msg := "unknown error"
		if parsed.Error != nil {
			msg = parsed.Error.Message
		}
		return nil, fmt.Errorf("hydradb relations rejected: %s", msg)
	}

	result := &hydraRelationsResult{Truncated: parsed.Data.IsTruncated}
	for _, group := range parsed.Data.Relations {
		src := firstNonEmpty([]string{group.Source.Identifier, group.Source.Name})
		tgt := firstNonEmpty([]string{group.Target.Identifier, group.Target.Name})
		for _, rel := range group.Relations {
			result.Relations = append(result.Relations, hydraRelation{
				Source: src, Predicate: rel.CanonicalPredicate, Target: tgt,
				Context: rel.Context, Confidence: rel.Confidence,
			})
		}
	}
	return result, nil
}

type hydraStats struct {
	KnowledgeRows int `json:"knowledgeRows"`
	MemoryRows    int `json:"memoryRows"`
}

// stats reports real row counts for the database's knowledge and memory
// collections — surfaced on /api/status so "hydradbEnabled" isn't just a
// boolean, it's backed by an actual live count.
func (c *hydraDBClient) stats(ctx context.Context) (*hydraStats, error) {
	q := url.Values{"database": {c.database}}
	req, err := c.authedRequest(ctx, http.MethodGet, c.base+"/databases/stats?"+q.Encode(), nil, "")
	if err != nil {
		return nil, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("hydradb stats: %w", err)
	}
	defer resp.Body.Close()

	var parsed struct {
		Success bool `json:"success"`
		Data    struct {
			KnowledgeCollection struct {
				RowCount int `json:"row_count"`
			} `json:"knowledge_collection"`
			MemoryCollection struct {
				RowCount int `json:"row_count"`
			} `json:"memory_collection"`
		} `json:"data"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, fmt.Errorf("decoding hydradb stats response: %w", err)
	}
	if !parsed.Success {
		msg := "unknown error"
		if parsed.Error != nil {
			msg = parsed.Error.Message
		}
		return nil, fmt.Errorf("hydradb stats rejected: %s", msg)
	}
	return &hydraStats{
		KnowledgeRows: parsed.Data.KnowledgeCollection.RowCount,
		MemoryRows:    parsed.Data.MemoryCollection.RowCount,
	}, nil
}

// submitFeedback records a rating and/or comment against a previous
// query's request_id (hydraQueryResult.RequestID). The API rejects
// feedback that carries a rating but no text, so a rating-only call
// synthesizes a short comment rather than failing.
func (c *hydraDBClient) submitFeedback(ctx context.Context, requestID, rating, comment string) error {
	if requestID == "" {
		return fmt.Errorf("hydradb feedback: requestID is required")
	}
	if comment == "" && rating != "" {
		comment = "User rated this answer: " + rating
	}
	if comment == "" {
		return fmt.Errorf("hydradb feedback: rating or comment is required")
	}
	payload := map[string]any{
		"request_id": requestID,
		"source":     "user",
		"feedback":   comment,
	}
	if rating != "" {
		payload["rating"] = rating
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := c.authedRequest(ctx, http.MethodPost, c.base+"/feedback", bytes.NewBuffer(body), "application/json")
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("hydradb feedback: %w", err)
	}
	defer resp.Body.Close()

	var parsed struct {
		Success bool `json:"success"`
		Error   *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return fmt.Errorf("decoding hydradb feedback response: %w", err)
	}
	if !parsed.Success {
		msg := "unknown error"
		if parsed.Error != nil {
			msg = parsed.Error.Message
		}
		return fmt.Errorf("hydradb feedback rejected: %s", msg)
	}
	return nil
}
