import { useMemo, useState } from 'react'
import GraphView from '../components/GraphView'
import { pollJob, scanRepo } from '../lib/api'
import { buildRepoGraph, dependencyHighlightKeys } from '../lib/repoGraph'
import type { ScanJob } from '../lib/types'
import './DataScreen.css'

type Phase = 'idle' | 'scanning' | 'done' | 'error'

export default function RepoScreen() {
  const [target, setTarget] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  const [job, setJob] = useState<ScanJob | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedDep, setSelectedDep] = useState<string | null>(null)

  const result = job?.result ?? null
  const graph = useMemo(() => (result ? buildRepoGraph(result) : null), [result])
  const highlightKeys = useMemo(
    () => (result && selectedDep ? dependencyHighlightKeys(result, selectedDep) : undefined),
    [result, selectedDep],
  )
  const selectedOption = result?.dependency_options.find((d) => d.package_key === selectedDep) ?? null

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!target.trim()) return
    setPhase('scanning')
    setError(null)
    setSelectedDep(null)
    setJob(null)
    try {
      const ack = await scanRepo(target.trim())
      const finished = await pollJob(ack.job_id, setJob)
      if (finished.status === 'failed') {
        setError(finished.error || 'scan failed')
        setPhase('error')
      } else {
        setJob(finished)
        setPhase('done')
      }
    } catch {
      setError('Could not reach the ReachGraph backend. Is scripts/server.py running on :8081?')
      setPhase('error')
    }
  }

  const sorted = result ? [...result.dependency_options].sort((a, b) => b.total_blast_reach - a.total_blast_reach) : []

  return (
    <div className="data-screen">
      <aside className="data-sidebar">
        <div>
          <h1 className="data-title">Repository blast radius</h1>
          <p className="data-subtitle">
            Enter a GitHub repo URL — monorepos with multiple package.json/requirements.txt files are
            supported. We clone it, discover every manifest, and build its dependency graph.
          </p>
        </div>

        <form className="field-group" onSubmit={onSubmit}>
          <div className="field-group">
            <span className="field-label">GitHub repository</span>
            <input
              className="text-input"
              placeholder="https://github.com/owner/repo"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            />
          </div>
          <button className="primary-btn" type="submit" disabled={phase === 'scanning' || !target.trim()}>
            {phase === 'scanning' ? 'Scanning…' : 'Build dependency graph'}
          </button>
        </form>

        {phase === 'scanning' && (
          <div className="status-line">
            <span className="status-dot running" />
            {job?.progress ?? 'Cloning repository…'}
          </div>
        )}
        {error && <div className="error-box">{error}</div>}

        {result && (
          <>
            <div className="stat-grid">
              <div className="stat-card">
                <div className="value">{result.unique_packages}</div>
                <div className="label">unique deps</div>
              </div>
              <div className="stat-card">
                <div className="value">{result.discovered_applications.length}</div>
                <div className="label">sub-packages</div>
              </div>
            </div>

            <div className="field-group">
              <span className="field-label">
                {selectedOption ? 'Selected dependency' : 'Pick a dependency for its blast radius'}
              </span>
              <div className="dep-list">
                {sorted.map((dep) => (
                  <button
                    key={dep.package_key}
                    type="button"
                    className={`dep-item${selectedDep === dep.package_key ? ' selected' : ''}`}
                    onClick={() => setSelectedDep(selectedDep === dep.package_key ? null : dep.package_key)}
                  >
                    <span className="name">{dep.name}</span>
                    <span className="reach">{dep.total_blast_reach} reached</span>
                  </button>
                ))}
                {sorted.length === 0 && <p style={{ fontSize: 13 }}>No resolved dependencies found (no lockfile?).</p>}
              </div>
            </div>

            {selectedOption && (
              <div className="info-card">
                <div className="info-row">
                  <span className="k">dependency</span>
                  <span className="v">{selectedOption.name}</span>
                </div>
                <div className="info-row">
                  <span className="k">ecosystem</span>
                  <span className="v">{selectedOption.ecosystem}</span>
                </div>
                <div className="info-row">
                  <span className="k">affects</span>
                  <span className="v">{selectedOption.in_repo_blast_radius.join(', ') || '—'}</span>
                </div>
              </div>
            )}
          </>
        )}
      </aside>

      <div className="graph-pane">
        {graph ? (
          <GraphView graph={graph} highlightKeys={highlightKeys} onNodeClick={setSelectedDep} />
        ) : (
          <div className="graph-empty">
            {phase === 'scanning'
              ? 'Cloning and discovering manifests…'
              : 'Scan a repository to build its dependency graph.'}
          </div>
        )}
      </div>
    </div>
  )
}
