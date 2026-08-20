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
    } catch (err) {
      setError(
        err instanceof TypeError
          ? 'Could not reach the ReachGraph backend. Is scripts/server.py running on :8081?'
          : err instanceof Error
            ? err.message
            : 'scan failed',
      )
      setPhase('error')
    }
  }

  const sorted = result ? [...result.dependency_options].sort((a, b) => b.total_blast_reach - a.total_blast_reach) : []
  const extraLocal = selectedOption
    ? selectedOption.locally_affected_files.filter((p) => !selectedOption.importing_files.includes(p))
    : []

  return (
    <div className="stage">
      <div className="stage-graph">
        {graph ? (
          <GraphView graph={graph} highlightKeys={highlightKeys} onNodeClick={setSelectedDep} />
        ) : (
          <div className="stage-empty">
            {phase === 'scanning'
              ? 'Cloning and discovering manifests…'
              : 'Scan a repository to build its dependency graph.'}
          </div>
        )}
      </div>

      <div className="dock dock-top">
        <div className="dock-top-row">
          <div className="dock-heading">
            <h1>Repository blast radius</h1>
            <p>Monorepo-aware — every package.json / requirements.txt, cloned and cross-referenced.</p>
          </div>
          <form className="command-form" onSubmit={onSubmit}>
            <input
              className="text-input wide"
              placeholder="https://github.com/owner/repo"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            />
            <button className="primary-btn" type="submit" disabled={phase === 'scanning' || !target.trim()}>
              {phase === 'scanning' ? 'Scanning…' : 'Build graph'}
            </button>
          </form>
        </div>
        {phase === 'scanning' && (
          <div className="status-line">
            <span className="status-dot running" />
            {job?.progress ?? 'Cloning repository…'}
          </div>
        )}
        {error && <div className="error-box">{error}</div>}
      </div>

      {result && (
        <div className="dock-col-left">
          <div className="dock">
            <div className="dock-title">Repository</div>
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
          </div>

          <div className="dock grow">
            <div className="dock-title">
              {selectedOption ? 'Selected dependency' : 'Pick a dependency for its blast radius'}
            </div>
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
        </div>
      )}

      {selectedOption && (
        <div className="dock dock-bottom-right">
          <div className="dock-title">{selectedOption.name}</div>
          <div className="info-card" style={{ background: 'transparent', border: 'none', padding: 0, marginBottom: 12 }}>
            <div className="info-row">
              <span className="k">ecosystem</span>
              <span className="v">{selectedOption.ecosystem}</span>
            </div>
            <div className="info-row">
              <span className="k">affects</span>
              <span className="v">{selectedOption.in_repo_blast_radius.join(', ') || '—'}</span>
            </div>
          </div>

          <div className="dock-title" style={{ marginTop: 4 }}>
            Imported directly
            <span style={{ color: 'var(--text-faint)', fontWeight: 400, textTransform: 'none', letterSpacing: 0 }}>
              {' '}
              · {selectedOption.importing_files_count} file{selectedOption.importing_files_count === 1 ? '' : 's'}
            </span>
          </div>
          {selectedOption.importing_files.length > 0 ? (
            <div className="dep-list" style={{ marginBottom: extraLocal.length ? 14 : 0 }}>
              {selectedOption.importing_files.map((path) => (
                <div key={path} className="dep-item" style={{ cursor: 'default' }}>
                  <span className="name">{path}</span>
                </div>
              ))}
            </div>
          ) : (
            <p style={{ fontSize: 12.5, color: 'var(--text-dim)' }}>
              No direct imports found in scanned source — likely only a transitive dependency.
            </p>
          )}

          {extraLocal.length > 0 && (
            <>
              <div className="dock-title">Also reachable via local calls (gitnexus)</div>
              <div className="dep-list">
                {extraLocal.map((path) => (
                  <div key={path} className="dep-item" style={{ cursor: 'default' }}>
                    <span className="name">{path}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
