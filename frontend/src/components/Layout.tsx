import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  Film,
  Sparkles,
  Scissors,
  BarChart2,
  Users,
  Settings,
  Bookmark,
} from 'lucide-react'
import Logo from './Logo'

const nav = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard', mobile: true },
  { to: '/library', icon: Film, label: 'Library', mobile: true },
  { to: '/videoskip', icon: Sparkles, label: 'VideoSkip', mobile: true },
  { to: '/segments', icon: Scissors, label: 'Segments' },
  { to: '/markers', icon: Bookmark, label: 'Markers' },
  { to: '/analytics', icon: BarChart2, label: 'Analytics' },
  { to: '/users', icon: Users, label: 'Users', mobile: true },
  { to: '/settings', icon: Settings, label: 'Settings', mobile: true },
]

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar — desktop only */}
      <aside className="hidden md:flex w-60 flex-shrink-0 bg-[#0e1217] border-r border-plex-border/80 flex-col">
        <div className="px-5 py-5 flex items-center gap-3">
          <Logo size={38} />
          <div className="min-w-0">
            <div className="text-[17px] font-semibold tracking-tight text-white leading-none">
              cleanplex
            </div>
            <div className="mt-1 text-[9px] uppercase tracking-[0.18em] text-gray-500">
              Plex content filter
            </div>
          </div>
        </div>

        <nav className="flex-1 px-3 py-2 space-y-1 overflow-y-auto">
          {nav.map(({ to, icon: Icon, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `group flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-plex-orange/10 text-plex-orange ring-1 ring-inset ring-plex-orange/20'
                    : 'text-gray-400 hover:text-gray-100 hover:bg-white/5'
                }`
              }
            >
              <Icon size={17} className="flex-shrink-0" />
              <span className="truncate">{label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="px-4 py-4 border-t border-plex-border/70 flex items-center justify-between text-[11px] text-gray-600">
          <span>v1.0.8</span>
          <span className="flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 rounded-full bg-green-500/80" />
            LAN
          </span>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto bg-plex-darker">
        <div className="mx-auto w-full max-w-[1500px] p-4 md:p-7 pb-24 md:pb-8">{children}</div>
      </main>

      {/* Bottom nav — mobile only, primary destinations only */}
      <nav className="fixed bottom-0 left-0 right-0 z-50 flex md:hidden bg-[#0e1217]/95 backdrop-blur border-t border-plex-border">
        {nav.filter(item => item.mobile).map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center justify-center gap-1 py-2 text-[10px] font-medium transition-colors ${
                isActive ? 'text-plex-orange' : 'text-gray-500'
              }`
            }
          >
            <Icon size={19} />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  )
}
