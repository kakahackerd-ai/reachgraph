import type { GraphData, ScanResult } from './types'

/** dependency_options' in_repo_blast_radius entries are subpath labels
 * ("(root)" for the top-level package, or a real subpath) -- not full
 * application_keys -- so map each label back to its discovered
 * application's key. */
export function subpathLabelToAppKey(result: ScanResult, label: string): string | undefined {
  const match = result.discovered_applications.find((a) => (a.subpath || '(root)') === label)
  return match?.application_key
}

/** Build a client-side {nodes, edges} graph straight from a completed scan
 * job's result: Application nodes (discovered sub-packages) and Package
 * nodes (discovered dependencies), edged wherever a dependency's
 * in_repo_blast_radius says that application resolves it. */
export function buildRepoGraph(result: ScanResult): GraphData {
  const nodes: GraphData['nodes'] = []
  const edges: GraphData['edges'] = []
  const seen = new Set<string>()

  for (const app of result.discovered_applications) {
    if (seen.has(app.application_key)) continue
    seen.add(app.application_key)
    nodes.push({ key: app.application_key, label: 'Application', depth: 0 })
  }

  for (const dep of result.dependency_options) {
    if (!seen.has(dep.package_key)) {
      seen.add(dep.package_key)
      nodes.push({ key: dep.package_key, label: 'Package', depth: 1 })
    }
    for (const subpathLabel of dep.in_repo_blast_radius) {
      const appKey = subpathLabelToAppKey(result, subpathLabel)
      if (appKey) edges.push({ source: appKey, target: dep.package_key })
    }
  }

  return { nodes, edges }
}

/** Node keys to highlight for a selected dependency: the package itself
 * plus every application in its in_repo_blast_radius. */
export function dependencyHighlightKeys(result: ScanResult, packageKey: string): Set<string> {
  const dep = result.dependency_options.find((d) => d.package_key === packageKey)
  const keys = new Set<string>([packageKey])
  if (dep) {
    for (const label of dep.in_repo_blast_radius) {
      const appKey = subpathLabelToAppKey(result, label)
      if (appKey) keys.add(appKey)
    }
  }
  return keys
}
