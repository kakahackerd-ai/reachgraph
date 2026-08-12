package main

// This file holds the one piece of this product that has no open-source
// substitute: turning a resolved dependency graph into ranked attack paths.
// Everything upstream (deps.dev, OSV.dev) supplies data; this is the logic
// the PRD calls the product's actual differentiation.

type graphNode struct {
	Index    int
	System   string
	Name     string
	Version  string
	Relation string // SELF, DIRECT, INDIRECT
}

type graph struct {
	Nodes []graphNode
	// adjacency: node index -> indices it directly depends on
	adjacency map[int][]int
	rootIndex int
}

func buildGraph(dd *depsDevGraph) *graph {
	g := &graph{
		Nodes:     make([]graphNode, len(dd.Nodes)),
		adjacency: make(map[int][]int, len(dd.Nodes)),
		rootIndex: -1,
	}
	for i, n := range dd.Nodes {
		g.Nodes[i] = graphNode{
			Index:    i,
			System:   n.VersionKey.System,
			Name:     n.VersionKey.Name,
			Version:  n.VersionKey.Version,
			Relation: n.Relation,
		}
		if n.Relation == "SELF" {
			g.rootIndex = i
		}
	}
	for _, e := range dd.Edges {
		g.adjacency[e.FromNode] = append(g.adjacency[e.FromNode], e.ToNode)
	}
	return g
}

// graphBuilder assembles a composite graph out of several independent
// deps.dev subgraphs — one per direct dependency of a repository — merged
// under a single synthetic root. A package that shows up under more than one
// direct dependency (a diamond dependency, extremely common in real npm
// trees) is deduplicated to one node with multiple incoming edges, so
// shortestPath still finds the true shortest route from the repository.
type graphBuilder struct {
	g       *graph
	indexOf map[string]int // "name@version" -> node index
}

func newGraphBuilder(rootName string) *graphBuilder {
	g := &graph{
		adjacency: make(map[int][]int),
		rootIndex: 0,
	}
	g.Nodes = append(g.Nodes, graphNode{Index: 0, Name: rootName, Relation: "ROOT"})
	return &graphBuilder{g: g, indexOf: map[string]int{"\x00root": 0}}
}

func nodeKey(name, version string) string { return name + "@" + version }

// ensureNode returns the index of name@version, creating it with the given
// relation if it doesn't exist yet. An existing node's relation is never
// downgraded by a later, less-direct sighting of the same package.
func (b *graphBuilder) ensureNode(name, version, relation string) int {
	key := nodeKey(name, version)
	if idx, ok := b.indexOf[key]; ok {
		return idx
	}
	idx := len(b.g.Nodes)
	b.g.Nodes = append(b.g.Nodes, graphNode{Index: idx, Name: name, Version: version, Relation: relation})
	b.indexOf[key] = idx
	return idx
}

func (b *graphBuilder) addEdge(from, to int) {
	for _, existing := range b.g.adjacency[from] {
		if existing == to {
			return // no duplicate edges between the same two nodes
		}
	}
	b.g.adjacency[from] = append(b.g.adjacency[from], to)
}

// mergeDirectDependency folds one direct dependency's full deps.dev subgraph
// into the composite graph, wiring the repository root to that dependency's
// own SELF node.
func (b *graphBuilder) mergeDirectDependency(dd *depsDevGraph, relation string) {
	localToComposite := make(map[int]int, len(dd.Nodes))
	for i, n := range dd.Nodes {
		rel := n.Relation
		if rel == "SELF" {
			rel = relation // this package's role relative to the *repo*, not itself
		}
		localToComposite[i] = b.ensureNode(n.VersionKey.Name, n.VersionKey.Version, rel)
	}
	for i, n := range dd.Nodes {
		if n.Relation == "SELF" {
			b.addEdge(b.g.rootIndex, localToComposite[i])
		}
	}
	for _, e := range dd.Edges {
		b.addEdge(localToComposite[e.FromNode], localToComposite[e.ToNode])
	}
}

func (b *graphBuilder) build() *graph { return b.g }

// shortestPath returns the node indices from the root to target (inclusive
// of both ends), walking dependency edges root -> ... -> target. Returns nil
// if target is unreachable from root (should not happen for nodes deps.dev
// itself returned, but handled defensively).
func (g *graph) shortestPath(target int) []int {
	if g.rootIndex < 0 {
		return nil
	}
	if target == g.rootIndex {
		return []int{g.rootIndex}
	}

	prev := make(map[int]int, len(g.Nodes))
	visited := make(map[int]bool, len(g.Nodes))
	visited[g.rootIndex] = true
	queue := []int{g.rootIndex}

	for len(queue) > 0 {
		cur := queue[0]
		queue = queue[1:]
		for _, next := range g.adjacency[cur] {
			if visited[next] {
				continue
			}
			visited[next] = true
			prev[next] = cur
			if next == target {
				return reconstructPath(prev, g.rootIndex, target)
			}
			queue = append(queue, next)
		}
	}
	return nil // unreachable
}

func reconstructPath(prev map[int]int, root, target int) []int {
	path := []int{target}
	cur := target
	for cur != root {
		p, ok := prev[cur]
		if !ok {
			return nil
		}
		path = append([]int{p}, path...)
		cur = p
	}
	return path
}
