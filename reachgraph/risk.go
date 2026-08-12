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
)

func computeRiskScore(severity string, depth int) riskScore {
	sevVal, sevNote := severityWeight(severity)
	reachVal, reachNote := reachabilityWeight(depth)

	overall := (sevVal*severityWeightPct + reachVal*reachabilityWeightPct) / 100
	if overall > 100 {
		overall = 100
	}

	label := "Low"
	switch {
	case overall >= 85:
		label = "Critical"
	case overall >= 65:
		label = "High"
	case overall >= 40:
		label = "Medium"
	}

	return riskScore{
		Value: overall,
		Label: label,
		Factors: []scoreFactor{
			{Label: "Advisory severity", Weight: severityWeightPct, Value: sevVal, Note: sevNote},
			{Label: "Reachability", Weight: reachabilityWeightPct, Value: reachVal, Note: reachNote},
		},
	}
}
