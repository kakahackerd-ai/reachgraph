package main

import "fmt"

// scoreFactor mirrors the PRD's explainability requirement (FR5.2): a risk
// score is never returned as a bare number, always as its weighted factors.
type scoreFactor struct {
	Label  string `json:"label"`
	Weight int    `json:"weightPct"`
	Value  int    `json:"value"` // 0-100
	Note   string `json:"note"`
}

type riskScore struct {
	Value   int           `json:"value"` // 0-100
	Label   string        `json:"label"` // Critical / High / Medium / Low
	Factors []scoreFactor `json:"factors"`
}

// severityWeight turns an OSV/GHSA severity string into a 0-100 danger value.
// A missing severity (common for malicious-package reports, which are not
// CVEs and carry no CVSS score) is treated as elevated risk rather than low —
// "flagged with no severity data" is not the same as "safe".
func severityWeight(severity string) (value int, note string) {
	switch severity {
	case "CRITICAL":
		return 97, "Advisory rated critical severity."
	case "HIGH":
		return 82, "Advisory rated high severity."
	case "MODERATE", "MEDIUM":
		return 55, "Advisory rated moderate severity."
	case "LOW":
		return 30, "Advisory rated low severity."
	default:
		return 70, "No severity rating published — treated as elevated pending review."
	}
}

// reachabilityWeight scores how close a flagged package sits to the root,
// measured in edges walked (depth), not raw hop count, so "direct dependency"
// and "root" are scored consistently regardless of path array length.
func reachabilityWeight(depth int) (value int, note string) {
	value = 100 - depth*12
	if value < 15 {
		value = 15
	}
	switch {
	case depth == 0:
		note = "This is the package you scanned."
	case depth == 1:
		note = "Direct dependency — no intermediate package to update instead."
	default:
		note = fmt.Sprintf("%d hops from the scanned package.", depth)
	}
	return value, note
}

const (
	severityWeightPct     = 60
	reachabilityWeightPct = 40
	reachabilityLabel     = "Reachability"
)

func scoreLabel(value int) string {
	switch {
	case value >= 85:
		return "Critical"
	case value >= 65:
		return "High"
	case value >= 40:
		return "Medium"
	default:
		return "Low"
	}
}

func computeRiskScore(severity string, depth int) riskScore {
	sevVal, sevNote := severityWeight(severity)
	reachVal, reachNote := reachabilityWeight(depth)

	overall := (sevVal*severityWeightPct + reachVal*reachabilityWeightPct) / 100
	if overall > 100 {
		overall = 100
	}

	return riskScore{
		Value: overall,
		Label: scoreLabel(overall),
		Factors: []scoreFactor{
			{Label: "Advisory severity", Weight: severityWeightPct, Value: sevVal, Note: sevNote},
			{Label: reachabilityLabel, Weight: reachabilityWeightPct, Value: reachVal, Note: reachNote},
		},
	}
}

// adjustScoreForCodeReachability replaces the hop-based reachability guess
// with real evidence once it exists (FR3 from the PRD: whether a flagged
// direct dependency is actually imported by the repository's own code, not
// just declared). Confirmed imports keep their hop-based value but gain
// real evidence in the note; confirmed non-imports get pulled down hard —
// a declared-but-unused dependency's install-time risk still exists, but
// its runtime-vulnerability risk is real news, not decoration.
func adjustScoreForCodeReachability(score riskScore, rf reachabilityFinding) riskScore {
	if !rf.Checked {
		return score
	}
	factors := make([]scoreFactor, len(score.Factors))
	copy(factors, score.Factors)

	total := 0
	for i, f := range factors {
		if f.Label == reachabilityLabel {
			if rf.Reached {
				factors[i].Note = "Confirmed: " + rf.Evidence
			} else {
				factors[i].Value = 12
				factors[i].Note = "Confirmed not reachable — " + rf.Evidence
			}
		}
		total += factors[i].Value * factors[i].Weight
	}
	overall := total / 100
	if overall > 100 {
		overall = 100
	}
	return riskScore{Value: overall, Label: scoreLabel(overall), Factors: factors}
}
