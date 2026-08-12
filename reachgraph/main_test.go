package main

import "testing"

// Regression test for a real bug found scanning lodash/lodash: minimist is
// a dependency of coveralls (a devDependency of lodash), not of lodash
// itself, but deps.dev's per-subgraph "DIRECT" relation made it
// indistinguishable from a true one-hop repo dependency. Hop count is the
// only thing that actually answers "did the repository declare this."
func TestIsDirectDependencyPathUsesHopCountNotRelationLabel(t *testing.T) {
	repoDirect := attackPath{
		Hops: []pathHop{
			{Name: "lodash/lodash", Relation: "ROOT"},
			{Name: "dojo", Version: "1.15.0", Relation: "DEV"}, // real devDependency, one hop
		},
		Target: pathHop{Name: "dojo", Version: "1.15.0", Relation: "DEV"},
	}
	if !isDirectDependencyPath(repoDirect) {
		t.Error("a one-hop devDependency of the repo must count as a direct dependency path")
	}

	nestedButLabeledDirect := attackPath{
		Hops: []pathHop{
			{Name: "lodash/lodash", Relation: "ROOT"},
			{Name: "coveralls", Version: "2.11.15", Relation: "DEV"},
			{Name: "minimist", Version: "1.2.0", Relation: "DIRECT"}, // DIRECT relative to coveralls, not the repo
		},
		Target: pathHop{Name: "minimist", Version: "1.2.0", Relation: "DIRECT"},
	}
	if isDirectDependencyPath(nestedButLabeledDirect) {
		t.Error("a two-hop package must not count as a direct dependency path even when its Relation says DIRECT")
	}
}

func TestApplyReachabilitySkipsWhenNoDirectPathsFlagged(t *testing.T) {
	s := &apiServer{} // no github/osv clients — must not be reached
	paths := []attackPath{{
		Hops: []pathHop{
			{Name: "root", Relation: "ROOT"},
			{Name: "a", Relation: "DIRECT"},
			{Name: "b", Relation: "INDIRECT"}, // two hops — not a direct dependency path
		},
		Target: pathHop{Name: "b", Relation: "INDIRECT"},
		Score:  riskScore{Value: 50, Label: "Medium"},
	}}
	got := s.applyReachability(nil, "o", "r", "main", paths)
	if got[0].Score.Value != 50 {
		t.Fatalf("expected untouched score when nothing is a direct dependency path, got %d", got[0].Score.Value)
	}
}
