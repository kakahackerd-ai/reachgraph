import { useState } from 'react'
import GraphView from '../components/GraphView'
import { lookupPackage } from '../lib/api'
import type { Ecosystem, LookupResponse } from '../lib/types'
import './DataScreen.css'

export default function PackageScreen() {
  const [ecosystem, setEcosystem] = useState<Ecosystem>('npm')
  const [name, setName] = useState('')
  const [version, setVersion] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<LookupResponse | null>(null)
  const [selected, setSelected] = useState<string | null>(null)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    setLoading(true)
    setError(null)
    setSelected(null)
    try {
      const res = await lookupPackage(ecosystem, name.trim(), version.trim() || undefined)
      if (res.status === 'error') {
        setError(res.message || res.error || 'lookup failed')
        setResult(null)
      } else {
        setResult(res)
      }
    } catch {
      setError('Could not reach the ReachGraph backend. Is scripts/server.py running on :8081?')
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  const pkg = result?.package
  const blast = result?.blast_radius
  const dependents = result?.dependents

  return (
    <div className="data-screen">
      <aside className="data-sidebar">
        <div>
          <h1 className="data-title">Package blast radius</h1>
          <p className="data-subtitle">
            Enter an npm or PyPI package name. We resolve its real registry metadata, scrape who
            actually depends on it off GitHub, and store that graph in HydraDB.
          </p>
        </div>

        <form className="field-group" onSubmit={onSubmit}>
          <div className="field-group">
            <span className="field-label">Ecosystem</span>
            <div className="eco-toggle">
              <button type="button" className={ecosystem === 'npm' ? 'active' : ''} onClick={() => setEcosystem('npm')}>
                npm
              </button>
              <button type="button" className={ecosystem === 'pypi' ? 'active' : ''} onClick={() => setEcosystem('pypi')}>
                PyPI
              </button>
            </div>
          </div>

          <div className="field-group">
            <span className="field-label">Package name</span>
            <input
              className="text-input"
              placeholder={ecosystem === 'npm' ? 'e.g. chalk' : 'e.g. requests'}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div className="field-group">
            <span className="field-label">Version (optional)</span>
            <input
              className="text-input"
              placeholder="latest"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
            />
          </div>

          <button className="primary-btn" type="submit" disabled={loading || !name.trim()}>
            {loading ? 'Tracing blast radius…' : 'Compute blast radius'}
          </button>
        </form>

        {error && <div className="error-box">{error}</div>}

        {pkg && (
          <div className="info-card">
            <div className="info-row">
              <span className="k">package</span>
              <span className="v">{pkg.name}</span>
            </div>
            <div className="info-row">
              <span className="k">ecosystem</span>
              <span className="v">{pkg.ecosystem}</span>
            </div>
            <div className="info-row">
              <span className="k">version</span>
              <span className="v">{pkg.version ?? '—'}</span>
            </div>
            <div className="info-row">
              <span className="k">repository</span>
              <span className="v">{pkg.repository ?? 'unknown'}</span>
            </div>
          </div>
        )}

        {blast && (
          <div className="stat-grid">
            <div className="stat-card">
              <div className="value">{blast.total_reached}</div>
              <div className="label">nodes reached</div>
            </div>
            <div className="stat-card">
              <div className="value">{blast.max_depth}</div>
              <div className="label">max depth</div>
            </div>
            <div className="stat-card">
              <div className="value">{dependents?.shown ?? 0}</div>
              <div className="label">dependents shown</div>
            </div>
            <div className="stat-card">
              <div className="value">{dependents?.known_total ?? '—'}</div>
              <div className="label">known on deps.dev</div>
            </div>
          </div>
        )}
      </aside>

      <div className="graph-pane">
        {result?.graph ? (
          <>
            <GraphView graph={result.graph} sourceKey={blast?.source_key} onNodeClick={setSelected} />
            {selected && (
              <div className="node-detail">
                <button className="close" onClick={() => setSelected(null)}>
                  ×
                </button>
                <div className="k">{selected}</div>
              </div>
            )}
          </>
        ) : (
          <div className="graph-empty">
            {loading
              ? 'Resolving metadata, scraping dependents, computing blast radius…'
              : 'Look up a package to see its blast radius in 3D.'}
          </div>
        )}
      </div>
    </div>
  )
}
