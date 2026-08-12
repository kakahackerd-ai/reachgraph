package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"
)

// Shapes verified against the live API:
//
//	POST https://api.osv.dev/v1/querybatch  -> {"results":[{"vulns":[{"id":"..."}]}, {}, ...]}
//	GET  https://api.osv.dev/v1/vulns/{id}  -> full record, including database_specific.severity
type osvClient struct {
	http *http.Client
}

func newOSVClient() *osvClient {
	return &osvClient{http: &http.Client{Timeout: 20 * time.Second}}
}

type osvQuery struct {
	Package struct {
		Name      string `json:"name"`
		Ecosystem string `json:"ecosystem"`
	} `json:"package"`
	Version string `json:"version"`
}

type osvBatchResult struct {
	Vulns []struct {
		ID string `json:"id"`
	} `json:"vulns"`
}

// batchQuery reports, for each requested package version, the list of known
// vulnerability/malware advisory IDs affecting it. OSV's batch endpoint caps
// request size, so queries are chunked defensively.
func (c *osvClient) batchQuery(ctx context.Context, queries []osvQuery) ([]osvBatchResult, error) {
	const chunkSize = 100
	all := make([]osvBatchResult, 0, len(queries))

	for start := 0; start < len(queries); start += chunkSize {
		end := start + chunkSize
		if end > len(queries) {
			end = len(queries)
		}
		chunk := queries[start:end]

		body, err := json.Marshal(struct {
			Queries []osvQuery `json:"queries"`
		}{Queries: chunk})
		if err != nil {
			return nil, err
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://api.osv.dev/v1/querybatch", bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := c.http.Do(req)
		if err != nil {
			return nil, fmt.Errorf("osv.dev querybatch: %w", err)
		}
		var parsed struct {
			Results []osvBatchResult `json:"results"`
		}
		decErr := json.NewDecoder(resp.Body).Decode(&parsed)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("osv.dev querybatch returned %d", resp.StatusCode)
		}
		if decErr != nil {
			return nil, fmt.Errorf("decoding osv.dev querybatch response: %w", decErr)
		}
		all = append(all, parsed.Results...)
	}
	return all, nil
}

type osvFinding struct {
	ID       string `json:"id"`
	Summary  string `json:"summary"`
	Severity string `json:"severity"` // LOW / MODERATE / HIGH / CRITICAL / "" (unknown)
	URL      string `json:"url"`
}

// vulnDetail fetches one advisory's summary and severity from the live API.
func (c *osvClient) vulnDetail(ctx context.Context, id string) (osvFinding, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://api.osv.dev/v1/vulns/"+id, nil)
	if err != nil {
		return osvFinding{}, err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return osvFinding{}, fmt.Errorf("osv.dev vuln detail: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return osvFinding{}, fmt.Errorf("osv.dev vuln detail returned %d for %s", resp.StatusCode, id)
	}

	var body struct {
		ID               string `json:"id"`
		Summary          string `json:"summary"`
		DatabaseSpecific struct {
			Severity string `json:"severity"`
		} `json:"database_specific"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return osvFinding{}, fmt.Errorf("decoding osv.dev vuln detail: %w", err)
	}
	return osvFinding{
		ID:       body.ID,
		Summary:  body.Summary,
		Severity: body.DatabaseSpecific.Severity,
		URL:      "https://osv.dev/vulnerability/" + body.ID,
	}, nil
}

// vulnDetailsConcurrent fetches details for a set of advisory IDs with bounded
// parallelism, so a package with many findings doesn't serialize one HTTP
// round trip per advisory.
func (c *osvClient) vulnDetailsConcurrent(ctx context.Context, ids []string) map[string]osvFinding {
	const workers = 8
	results := make(map[string]osvFinding, len(ids))
	var mu sync.Mutex
	var wg sync.WaitGroup
	sem := make(chan struct{}, workers)

	for _, id := range ids {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			finding, err := c.vulnDetail(ctx, id)
			if err != nil {
				return // best-effort: a single failed lookup shouldn't fail the whole scan
			}
			mu.Lock()
			results[id] = finding
			mu.Unlock()
		}(id)
	}
	wg.Wait()
	return results
}
