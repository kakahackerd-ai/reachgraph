package main

import "testing"

// Simulates a repo with two direct dependencies, A and B, that both
// transitively pull in the same shared package S — a diamond dependency,
// the single most common real-world case a naive per-package merge gets
// wrong by duplicating nodes.
//
//	repo -> A -> S
//	repo -> B -> S
func TestGraphBuilderDedupesSharedTransitiveDependency(t *testing.T) {
	b := newGraphBuilder("acme/widgets", "npm")

	depA := &depsDevGraph{
		Nodes: []depsDevNode{
			{Relation: "SELF"},
			{Relation: "DIRECT"},
		},
		Edges: []depsDevEdge{{FromNode: 0, ToNode: 1}},
	}
	depA.Nodes[0].VersionKey.Name, depA.Nodes[0].VersionKey.Version = "a", "1.0.0"
	depA.Nodes[1].VersionKey.Name, depA.Nodes[1].VersionKey.Version = "shared", "2.0.0"

	depB := &depsDevGraph{
		Nodes: []depsDevNode{
			{Relation: "SELF"},
			{Relation: "DIRECT"},
		},
		Edges: []depsDevEdge{{FromNode: 0, ToNode: 1}},
	}
	depB.Nodes[0].VersionKey.Name, depB.Nodes[0].VersionKey.Version = "b", "1.0.0"
	depB.Nodes[1].VersionKey.Name, depB.Nodes[1].VersionKey.Version = "shared", "2.0.0"

	b.mergeDirectDependency(depA, "DIRECT")
	b.mergeDirectDependency(depB, "DIRECT")
	g := b.build()

	if len(g.Nodes) != 4 { // root, a, b, shared (deduped, not 5)
		t.Fatalf("expected 4 nodes (root+a+b+shared deduped), got %d: %+v", len(g.Nodes), g.Nodes)
	}

	sharedIdx := -1
	for _, n := range g.Nodes {
		if n.Name == "shared" {
			sharedIdx = n.Index
		}
	}
	if sharedIdx == -1 {
		t.Fatal("shared node not found")
	}

	path := g.shortestPath(sharedIdx)
	if len(path) != 3 { // root -> a (or b) -> shared
		t.Fatalf("expected a 2-hop path to the shared node, got %d hops: %v", len(path)-1, path)
	}
}

func TestGraphBuilderRootWiredToEachDirectDependency(t *testing.T) {
	b := newGraphBuilder("acme/widgets", "npm")
	dep := &depsDevGraph{
		Nodes: []depsDevNode{{Relation: "SELF"}},
	}
	dep.Nodes[0].VersionKey.Name, dep.Nodes[0].VersionKey.Version = "leftpad", "1.3.0"
	b.mergeDirectDependency(dep, "DIRECT")
	g := b.build()

	path := g.shortestPath(1) // leftpad is node 1 (0 is root)
	if len(path) != 2 || path[0] != 0 || path[1] != 1 {
		t.Fatalf("expected direct root->leftpad edge, got path %v", path)
	}
	if g.Nodes[1].Relation != "DIRECT" {
		t.Fatalf("expected leftpad's relation to be DIRECT (relative to repo), got %s", g.Nodes[1].Relation)
	}
}
