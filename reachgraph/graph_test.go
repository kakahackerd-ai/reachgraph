package main

import "testing"

// root(0) -> a(1) -> b(2) -> c(3)
//
//	-> d(4) (dead end, no findings)
func sampleGraph() *depsDevGraph {
	g := &depsDevGraph{
		Nodes: []depsDevNode{
			{Relation: "SELF"},
			{Relation: "DIRECT"},
			{Relation: "INDIRECT"},
			{Relation: "INDIRECT"},
			{Relation: "DIRECT"},
		},
		Edges: []depsDevEdge{
			{FromNode: 0, ToNode: 1},
			{FromNode: 1, ToNode: 2},
			{FromNode: 2, ToNode: 3},
			{FromNode: 0, ToNode: 4},
		},
	}
	for i := range g.Nodes {
		g.Nodes[i].VersionKey.Name = "pkg"
	}
	return g
}

func TestShortestPathToRootIsSingleNode(t *testing.T) {
	g := buildGraph(sampleGraph(), "npm")
	path := g.shortestPath(g.rootIndex)
	if len(path) != 1 || path[0] != g.rootIndex {
		t.Fatalf("expected root-only path, got %v", path)
	}
}

func TestShortestPathFollowsEdges(t *testing.T) {
	g := buildGraph(sampleGraph(), "npm")
	path := g.shortestPath(3)
	want := []int{0, 1, 2, 3}
	if len(path) != len(want) {
		t.Fatalf("expected path %v, got %v", want, path)
	}
	for i := range want {
		if path[i] != want[i] {
			t.Fatalf("expected path %v, got %v", want, path)
		}
	}
}

func TestShortestPathUnreachableReturnsNil(t *testing.T) {
	dd := sampleGraph()
	// node 5 exists but has no incoming edge from anywhere reachable
	dd.Nodes = append(dd.Nodes, depsDevNode{Relation: "INDIRECT"})
	g := buildGraph(dd, "npm")
	if path := g.shortestPath(5); path != nil {
		t.Fatalf("expected nil for unreachable node, got %v", path)
	}
}

func TestShortestPathPrefersFewerHops(t *testing.T) {
	// root -> a -> target
	// root -> target  (direct shortcut should win)
	dd := &depsDevGraph{
		Nodes: []depsDevNode{
			{Relation: "SELF"},
			{Relation: "DIRECT"},
			{Relation: "DIRECT"}, // target, index 2, reachable both via 1 and directly
		},
		Edges: []depsDevEdge{
			{FromNode: 0, ToNode: 1},
			{FromNode: 1, ToNode: 2},
			{FromNode: 0, ToNode: 2},
		},
	}
	g := buildGraph(dd, "npm")
	path := g.shortestPath(2)
	if len(path) != 2 {
		t.Fatalf("expected the 2-hop direct path (BFS shortest), got %v", path)
	}
}
