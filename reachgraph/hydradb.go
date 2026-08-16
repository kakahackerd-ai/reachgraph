package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"mime/multipart"
	"net/http"
	"time"
)

// hydraDBClient wraps the real HydraDB v2 API. Every shape here was
// confirmed against the live API with a real key during development, not
// assumed from documentation — the documented request shapes (a JSON body
// to /context/ingest, an {subject,predicate,object} triplet array) turned
// out not to be what the API actually accepts. What's real:
//
//   - POST /context/ingest is multipart/form-data, not JSON. A "database"
//     field plus an "app_knowledge" field: a JSON array of
//     {title, content: {text}} objects. HydraDB extracts entities and
//     relations from that text itself, via its own pipeline — you cannot
//     hand it a pre-formed graph triplet directly; a bare
//     {subject,predicate,object} array is silently accepted and produces
//     an empty document with no extracted graph.
//   - Ingestion is asynchronous: poll GET /context/status?database=...&id=...
//     until indexing_status is "completed" (it passes through
//     "graph_creation" first) before querying.
//   - POST /query takes {database, query, type, graph_context} as a JSON
//     body (database in the body, not the query string — the query string
//     form was tried first and rejected). Results are relevancy-scored
//     retrieval (graph_context.query_paths, each with a relevancy_score),
//     not a guaranteed-complete traversal — there is no "give me every
//     match" mode in this API surface.
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

type hydraKnowledgeItem struct {
	Title   string `json:"title"`
	Content struct {
		Text string `json:"text"`
	} `json:"content"`
}

type hydraIngestResult struct {
	ID     string `json:"id"`
	Status string `json:"status"`
}

// ingestFacts submits a batch of natural-language facts for HydraDB's own
// pipeline to extract entities and relations from. Returns the source IDs
// to poll for completion.
func (c *hydraDBClient) ingestFacts(ctx context.Context, facts []hydraKnowledgeItem) ([]string, error) {
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
// context deadline is hit. Best-effort: a slow or failed index doesn't
// panic the caller, it just stops waiting.
func (c *hydraDBClient) waitForIndexing(ctx context.Context, ids []string) {
	for _, id := range ids {
		for {
			select {
			case <-ctx.Done():
				return
			default:
			}
			url := fmt.Sprintf("%s/context/status?database=%s&id=%s", c.base, c.database, id)
			req, err := c.authedRequest(ctx, http.MethodGet, url, nil, "")
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
						IndexingStatus string `json:"indexing_status"`
					} `json:"statuses"`
				} `json:"data"`
			}
			_ = json.NewDecoder(resp.Body).Decode(&body)
			resp.Body.Close()
			if len(body.Data.Statuses) > 0 {
				s := body.Data.Statuses[0].IndexingStatus
				if s == "completed" || s == "errored" {
					break
				}
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(2 * time.Second):
			}
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

type hydraQueryResult struct {
	Answer   string         `json:"answer"` // best combined_context, if any
	Triplets []hydraTriplet `json:"triplets"`
}

// query asks a natural-language question and returns the graph paths
// HydraDB judged relevant — ranked retrieval, explicitly not a guaranteed-
// complete result set. Callers that need completeness (Track A's blast
// radius) do not use this; this is for the narrative-timeline and
// code-context features where "the most relevant answers" is the right
// model, not a limitation.
func (c *hydraDBClient) query(ctx context.Context, question string) (*hydraQueryResult, error) {
	body, err := json.Marshal(map[string]any{
		"database":      c.database,
		"query":         question,
		"type":          "knowledge",
		"graph_context": true,
	})
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
			GraphContext struct {
				QueryPaths []struct {
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
				} `json:"query_paths"`
			} `json:"graph_context"`
		} `json:"data"`
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

	result := &hydraQueryResult{}
	bestScore := -1.0
	for _, p := range parsed.Data.GraphContext.QueryPaths {
		if p.RelevancyScore > bestScore {
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
	return result, nil
}
