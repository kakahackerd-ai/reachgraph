import { Link } from 'react-router-dom'
import AmbientNetwork from '../scenes/AmbientNetwork'
import './IntroScreen.css'

export default function IntroScreen() {
  return (
    <div className="intro">
      <AmbientNetwork />
      <div className="intro-content">
        <div className="intro-eyebrow">supply-chain blast radius, in 3D</div>
        <h1 className="intro-title">
          If this dependency
          <br />
          breaks, what does it <span className="accent">reach</span>?
        </h1>
        <p className="intro-sub">
          ReachGraph traces real dependency graphs through a live graph database and shows you exactly
          who&nbsp;— and what&nbsp;— is downstream.
        </p>

        <div className="intro-cards">
          <Link to="/npm" className="intro-card">
            <div className="intro-card-badge cyan">01</div>
            <h2>Package blast radius</h2>
            <p>
              Enter an npm or PyPI package. We pull its real registry metadata, scrape the packages
              that actually depend on it, and render the reach as a 3D graph.
            </p>
            <span className="intro-card-cta">Look up a package →</span>
          </Link>

          <Link to="/repo" className="intro-card">
            <div className="intro-card-badge violet">02</div>
            <h2>Repository blast radius</h2>
            <p>
              Enter a GitHub repo — monorepos included. We clone it, discover every manifest, build its
              dependency graph, and let you pick one dependency to see exactly what it reaches inside
              the repo.
            </p>
            <span className="intro-card-cta">Scan a repository →</span>
          </Link>
        </div>

        <div className="intro-legend">
          <span className="legend-title">reading the graph</span>
          <span className="legend-item">
            <i className="dot" style={{ background: '#f8fafc' }} /> source you searched
          </span>
          <span className="legend-item">
            <i className="dot" style={{ background: '#22d3ee' }} /> package
          </span>
          <span className="legend-item">
            <i className="dot" style={{ background: '#a78bfa' }} /> application / repo
          </span>
          <span className="legend-item">
            <i className="dot" style={{ background: '#fb923c' }} /> file
          </span>
          <span className="legend-item">click a node for details · drag to rotate · scroll to zoom</span>
        </div>
      </div>
    </div>
  )
}
