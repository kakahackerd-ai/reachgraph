package main

import "testing"

func TestComputeRiskScoreCriticalDirect(t *testing.T) {
	s := computeRiskScore("CRITICAL", 1)
	if s.Label != "Critical" {
		t.Fatalf("expected Critical label for critical severity at depth 1, got %s (%d)", s.Label, s.Value)
	}
}

func TestComputeRiskScoreLowFarAway(t *testing.T) {
	s := computeRiskScore("LOW", 6)
	if s.Label != "Low" && s.Label != "Medium" {
		t.Fatalf("expected a low-ish label for low severity 6 hops out, got %s (%d)", s.Label, s.Value)
	}
	if s.Value >= computeRiskScore("CRITICAL", 1).Value {
		t.Fatalf("a low-severity distant finding must never outscore a critical direct one")
	}
}

func TestComputeRiskScoreDeeperIsAlwaysLowerAtSameSeverity(t *testing.T) {
	shallow := computeRiskScore("HIGH", 1)
	deep := computeRiskScore("HIGH", 5)
	if deep.Value >= shallow.Value {
		t.Fatalf("expected deeper finding to score lower: shallow=%d deep=%d", shallow.Value, deep.Value)
	}
}

func TestComputeRiskScoreMissingSeverityIsNotTreatedAsSafe(t *testing.T) {
	s := computeRiskScore("", 1)
	if s.Value < 50 {
		t.Fatalf("a flagged package with unknown severity should not score as low risk, got %d", s.Value)
	}
}

func TestComputeRiskScoreValueAlwaysInRange(t *testing.T) {
	for _, sev := range []string{"CRITICAL", "HIGH", "MODERATE", "LOW", "", "MEDIUM"} {
		for depth := 0; depth <= 10; depth++ {
			s := computeRiskScore(sev, depth)
			if s.Value < 0 || s.Value > 100 {
				t.Fatalf("score out of range for severity=%s depth=%d: %d", sev, depth, s.Value)
			}
		}
	}
}
