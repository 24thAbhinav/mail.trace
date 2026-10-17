import { NavLink, Outlet } from 'react-router-dom'
import { LayoutDashboard, Settings } from 'lucide-react'
import ThemeToggle from './ThemeToggle'

const navClass = ({ isActive }: { isActive: boolean }) =>
  isActive ? 'side-link active' : 'side-link'

export default function Layout() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-top">
          <span className="brand-mark" aria-hidden>
            MT
          </span>
          <span className="brand-name">mail.trace</span>
        </div>
        <nav className="side-nav" aria-label="Primary">
          <NavLink to="/" end className={navClass}>
            <span className="icon" aria-hidden>
              <LayoutDashboard size={17} />
            </span>
            Dashboard
          </NavLink>
          <NavLink to="/settings" className={navClass}>
            <span className="icon" aria-hidden>
              <Settings size={17} />
            </span>
            Settings
          </NavLink>
        </nav>
        <div className="sidebar-foot">
          <ThemeToggle />
        </div>
      </aside>
      <main className="content">
        <div className="content-inner">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
