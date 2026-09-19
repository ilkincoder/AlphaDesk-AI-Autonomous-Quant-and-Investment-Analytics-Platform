/** Application shell navigation.
 *
 * Ten destinations, three of which exist. Analysis, Portfolio and What if? are real pages;
 * the other seven are rendered as genuinely disabled buttons with a "Coming soon"
 * description, because a link that navigates nowhere is worse than a link that says it
 * cannot.
 */

import { useEffect, useRef, useState } from 'react'
import type { ComponentType } from 'react'

import { Button } from './Button'
import {
  AnalysisIcon,
  BacktestIcon,
  DashboardIcon,
  DataNewsIcon,
  OrdersIcon,
  PortfolioIcon,
  SettingsIcon,
  StrategiesIcon,
  WatchlistIcon,
  WhatIfIcon,
} from './icons'

/** The pages that exist. Declared here because this file owns the list of destinations;
 *  `App` holds whichever one is current. */
export type PageId = 'analysis' | 'portfolio' | 'what-if'

type NavItem = {
  label: string
  Icon: ComponentType
  /** Present only for destinations that are actually built. Its presence is what makes
   *  a row clickable — there is no separate "enabled" flag to fall out of step with. */
  id?: PageId
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Dashboard', Icon: DashboardIcon },
  { id: 'analysis', label: 'Analysis', Icon: AnalysisIcon },
  { id: 'portfolio', label: 'Portfolio', Icon: PortfolioIcon },
  { id: 'what-if', label: 'What if?', Icon: WhatIfIcon },
  { label: 'Strategies', Icon: StrategiesIcon },
  { label: 'Backtest', Icon: BacktestIcon },
  { label: 'Orders', Icon: OrdersIcon },
  { label: 'Watchlist', Icon: WatchlistIcon },
  { label: 'Data & News', Icon: DataNewsIcon },
  { label: 'Settings', Icon: SettingsIcon },
]

function Wordmark() {
  return (
    <div className="flex items-center gap-2.5">
      <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" className="shrink-0">
        <rect width="24" height="24" rx="6" fill="var(--color-accent)" />
        <path d="M12 5.5 18.5 18.5H5.5Z" fill="var(--color-page)" />
      </svg>
      <span className="text-heading font-semibold tracking-tight text-ink">AlphaDesk AI</span>
    </div>
  )
}

function NavItems({
  current,
  onNavigate,
}: {
  current: PageId
  onNavigate: (page: PageId) => void
}) {
  return (
    <ul className="space-y-0.5">
      {NAV_ITEMS.map(({ id, label, Icon }) => {
        const isCurrent = id === current
        return (
          <li key={label}>
            <button
              type="button"
              disabled={id === undefined}
              // "page", not "true": this is the current destination, not a selection
              // inside a widget.
              aria-current={isCurrent ? 'page' : undefined}
              // A disabled button gets no hover event, so the title is the only way to
              // say why it cannot be pressed. Screen readers expose it as the control's
              // description.
              title={id === undefined ? 'Coming soon' : undefined}
              onClick={id === undefined ? undefined : () => onNavigate(id)}
              className={`flex h-10 w-full items-center gap-2.5 rounded-md px-3 text-body transition-colors ${
                isCurrent
                  ? 'bg-selected font-medium text-accent-ink'
                  : id === undefined
                    ? 'text-faint disabled:opacity-70'
                    : 'text-dim hover:bg-line/40 hover:text-ink'
              }`}
            >
              <Icon />
              <span>{label}</span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

function Footer() {
  return (
    <div className="border-t border-line px-4 py-3">
      <p className="text-note text-ink">Demo portfolio</p>
      <p className="text-label text-faint">USD</p>
    </div>
  )
}

export function Sidebar({
  current,
  onNavigate,
}: {
  current: PageId
  onNavigate: (page: PageId) => void
}) {
  const [open, setOpen] = useState(false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const drawerRef = useRef<HTMLDivElement>(null)
  const wasOpen = useRef(false)

  // Focus in when the drawer opens, back to the button when it closes. The wasOpen
  // guard is load-bearing: without it the closing branch runs on first render too and
  // steals focus to the menu button the moment the page loads.
  useEffect(() => {
    if (open) {
      wasOpen.current = true
      drawerRef.current?.focus()
      return
    }
    if (wasOpen.current) {
      wasOpen.current = false
      menuButtonRef.current?.focus()
    }
  }, [open])

  // Escape closes. Bound to the document rather than the drawer so it still works
  // while focus is somewhere inside it.
  useEffect(() => {
    if (!open) return

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }

    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open])

  // Choosing a destination closes the drawer as well as navigating: leaving it open
  // over the page the user just asked for would hide what they asked to see.
  function navigateFromDrawer(page: PageId) {
    onNavigate(page)
    setOpen(false)
  }

  return (
    <>
      <aside className="hidden border-r border-line bg-sidebar min-[900px]:sticky min-[900px]:top-0 min-[900px]:flex min-[900px]:h-screen min-[900px]:w-[220px] min-[900px]:shrink-0 min-[900px]:flex-col">
        <div className="px-4 py-4">
          <Wordmark />
        </div>
        <nav aria-label="Main" className="flex-1 overflow-y-auto px-3 pb-4">
          <NavItems current={current} onNavigate={onNavigate} />
        </nav>
        <Footer />
      </aside>

      {/* Everything below is the narrow-screen replacement for the aside above. */}
      <div className="flex items-center gap-3 border-b border-line bg-sidebar px-4 py-3 min-[900px]:hidden">
        <Button
          ref={menuButtonRef}
          onClick={() => setOpen(true)}
          aria-label="Open navigation menu"
          aria-expanded={open}
          aria-controls="mobile-navigation"
        >
          <svg
            viewBox="0 0 16 16"
            width="16"
            height="16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />
          </svg>
          Menu
        </Button>
        <Wordmark />
      </div>

      {open && (
        <div className="fixed inset-0 z-40 min-[900px]:hidden">
          {/* Click-away layer. aria-hidden because tapping outside is a convenience,
              not a control anyone should have to find in a screen reader. */}
          <div
            className="absolute inset-0 bg-black/60"
            onClick={() => setOpen(false)}
            aria-hidden="true"
          />
          <div
            id="mobile-navigation"
            ref={drawerRef}
            role="dialog"
            aria-modal="true"
            aria-label="Main navigation"
            // Makes the panel itself focusable, so focus can be moved into the dialog
            // without inventing an element to land on.
            tabIndex={-1}
            className="relative flex h-full w-[260px] flex-col border-r border-line bg-sidebar outline-none"
          >
            <div className="flex items-center justify-between gap-2 px-4 py-4">
              <Wordmark />
              <Button onClick={() => setOpen(false)} aria-label="Close navigation menu">
                <svg
                  viewBox="0 0 16 16"
                  width="16"
                  height="16"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <path d="M4 4l8 8M12 4l-8 8" />
                </svg>
              </Button>
            </div>
            <nav aria-label="Main menu" className="flex-1 overflow-y-auto px-3 pb-4">
              <NavItems current={current} onNavigate={navigateFromDrawer} />
            </nav>
            <Footer />
          </div>
        </div>
      )}
    </>
  )
}
