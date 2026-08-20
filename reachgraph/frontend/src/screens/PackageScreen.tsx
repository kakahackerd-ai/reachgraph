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
    } catch (err) {
      setError(
        err instanceof TypeError
          ? 'Could not reach the ReachGraph backend. Is scripts/server.py running on :8081?'
          : err instanceof Error
            ? err.message
            : 'lookup failed',
      )
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  const pkg = result?.package
  const blast = result?.blast_radius
  const dependents = result?.dependents
  const directPct =
    dependents && dependents.known_total
      ? Math.min(100, Math.max(2, ((dependents.direct_known ?? 0) / dependents.known_total) * 100))
      : 50

  return (
    <div className="stage">
      <div className="stage-graph">
        {result?.graph ? (
          <GraphView graph={result.graph} sourceKey={blast?.source_key} onNodeClick={setSelected} />
        ) : (
          <div className="stage-empty">
            {loading
              ? 'Resolving metadata, scraping dependents, computing blast radius…'
              : 'Look up a package to see its blast radius in 3D.'}
          </div>
        )}
      </div>

      <div className="dock dock-top">
        <div className="dock-top-row">
          <div className="dock-heading">
            <h1>Package blast radius</h1>
            <p>Real registry metadata, real GitHub dependents, walked outward through HydraDB.</p>
          </div>
          <form className="command-form" onSubmit={onSubmit}>
            <div className="eco-toggle">
              <button type="button" className={ecosystem === 'npm' ? 'active' : ''} onClick={() => setEcosystem('npm')}>
                npm
              </button>
              <button type="button" className={ecosystem === 'pypi' ? 'active' : ''} onClick={() => setEcosystem('pypi')}>
                PyPI
              </button>
            </div>
            <input
              className="text-input"
              placeholder={ecosystem === 'npm' ? 'e.g. chalk' : 'e.g. requests'}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <input
              className="text-input"
              style={{ width: 120 }}
              placeholder="version"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
            />
            <button className="primary-btn" type="submit" disabled={loading || !name.trim()}>
              {loading ? 'Tracing…' : 'Trace'}
            </button>
          </form>
        </div>
        {error && <div className="error-box">{error}</div>}
      </div>

      {pkg && (
        <div className="dock-col-left">
          <div className="dock">
            <div className="dock-title">Package</div>
            <div className="info-card" style={{ background: 'transparent', border: 'none', padding: 0 }}>
              <div className="info-row">
                <span className="k">name</span>
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
          </div>

          {blast && (
            <div className="dock">
              <div className="dock-title">Blast radius</div>
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
              {dependents && (
                <>
                  <div className="split-bar" style={{ marginTop: 12 }}>
                    <span style={{ width: `${directPct}%`, background: 'var(--accent)' }} />
                    <span style={{ width: `${100 - directPct}%`, background: 'var(--file)' }} />
                  </div>
                  <div className="split-legend">
                    <span>direct · {dependents.direct_known}</span>
                    <span>transitive · {dependents.indirect_known}</span>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {selected && (
        <div className="dock node-detail">
          <button className="close" onClick={() => setSelected(null)}>
            ×
          </button>
          <div className="k">{selected}</div>
        </div>
      )}
    </div>
  )
}
