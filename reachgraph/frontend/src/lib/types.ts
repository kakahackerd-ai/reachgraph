export type Ecosystem = 'npm' | 'pypi'

export interface GraphNode {
  key: string
  label: string
  depth: number
}

export interface GraphEdge {
  source: string
  target: string
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface BlastRadiusNode {
  key: string
  label: string
  depth: number
  path: string[]
}

export interface BlastRadiusResult {
  source_key: string
  total_reached: number
  max_depth: number
  packages: string[]
  applications: string[]
  nodes: BlastRadiusNode[]
}

export interface PackageInfo {
  ecosystem: Ecosystem
  name: string
  version: string | null
  repository: string | null
}

export interface DependentsSummary {
  shown: number
  known_total: number | null
  direct_known: number | null
  indirect_known: number | null
}

export interface LookupResponse {
  status: 'ok' | 'error' | 'processing'
  error?: string
  message?: string
  package?: PackageInfo
  dependents?: DependentsSummary
  graph?: GraphData
  blast_radius?: BlastRadiusResult
  cached_at?: string
}

export interface ScanRepoAck {
  status: string
  job_id: string
  poll_url: string
}

export interface DiscoveredApplication {
  application_key: string
  subpath: string
  ecosystem: string
  resolved_count: number
}

export interface DependencyOption {
  package_key: string
  name: string
  ecosystem: string
  subpath: string
  in_repo_blast_radius: string[]
  total_blast_reach: number
  importing_files: string[]
  importing_files_count: number
  locally_affected_files: string[]
  locally_affected_files_count: number
}

export interface ScanResult {
  org: string
  repo: string
  monorepo: boolean
  discovered_applications: DiscoveredApplication[]
  total_dependencies_scanned: number
  unique_packages: number
  dependency_options: DependencyOption[]
  scanned_at: string
}

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed'

export interface ScanJob {
  job_id: string
  target: string
  status: JobStatus
  started_at: string
  finished_at: string | null
  progress: string
  error: string | null
  result: ScanResult | null
}
