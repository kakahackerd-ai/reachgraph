import { Link, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import './Shell.css'

const TABS = [
  { to: '/npm', label: 'Package' },
  { to: '/repo', label: 'Repository' },
]

export default function Shell({ children }: { children: ReactNode }) {
  const location = useLocation()
  return (
    <div className="shell">
      <header className="shell-header">
        <Link to="/" className="shell-logo">
          <span className="shell-logo-mark" />
          ReachGraph
        </Link>
        <nav className="shell-nav">
          {TABS.map((tab) => (
            <Link key={tab.to} to={tab.to} className={location.pathname === tab.to ? 'active' : ''}>
              {tab.label}
            </Link>
          ))}
        </nav>
      </header>
      <main className="shell-main">{children}</main>
    </div>
  )
}
