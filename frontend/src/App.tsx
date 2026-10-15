import { useCallback, useEffect, useRef, useState } from 'react'

import { AnalysisPage } from './components/AnalysisPage'
import { BacktestPage } from './components/BacktestPage'
import { DashboardPage } from './components/DashboardPage'
import { NewsPage } from './components/NewsPage'
import { OrdersPage } from './components/OrdersPage'
import { PortfolioPage } from './components/PortfolioPage'
import { Sidebar } from './components/Sidebar'
import type { PageId } from './components/Sidebar'
import { AuroraSplash, AURORA_FADE_MS, AURORA_MINIMUM_MS } from './components/AuroraSplash'

/** The shell: a full-height sidebar beside the current page.
 *
 * Column layout below 900px (where the sidebar becomes a drawer and the menu bar sits
 * above the content) and a row above it. One breakpoint, so the sidebar and its
 * replacement swap at the same width and can never both be absent.
 *
 * Which page is showing is plain state, not a URL. There is no router: four destinations
 * do not justify the dependency or the rework, and the cost is only that a refresh
 * returns to Portfolio and a page cannot be linked to. The Analysis page keeps its own
 * conversation in `localStorage`, so a refresh there restores the conversation even though
 * it does not restore the page.
 *
 * **A visited page stays mounted and is hidden, rather than being unmounted.** A chat that
 * lost its transcript, its draft and its scroll position every time somebody looked at the
 * portfolio would be a chat nobody used twice. `hidden` keeps it in the DOM and out of the
 * accessibility tree, which is what "keep my place" costs.
 */
export function App() {
  const [page, setPage] = useState<PageId>('portfolio')
  const [visited, setVisited] = useState<ReadonlySet<PageId>>(() => new Set(['portfolio']))
  // Which batch the Orders page should mark and scroll to. Set by an approval in News & Trading,
  // which is the one place a batch is created in front of somebody.
  const [focusBatch, setFocusBatch] = useState<string | null>(null)

  /** Whether the startup screen is up, and whether it is on its way out.
   *
   * Two flags rather than one because they are two facts: `booting` is "the screen is still
   * needed", `leaving` is "it is not, and this is it fading". The screen stays mounted through
   * the fade, so unmounting on `booting` alone would cut the transition off on its first frame.
   */
  const [booting, setBooting] = useState(true)
  const [leaving, setLeaving] = useState(false)
  // A ref, because the page reports after *every* read and only the first of them ends the
  // startup screen. Held here rather than in the page: the page does not know which of its
  // reads the reader was actually waiting for, and the shell is the thing that does.
  const booted = useRef(false)

  /** The two things the startup screen is waiting for, and it leaves when *both* have
   *  happened.
   *
   * Kept apart rather than combined into one flag because they answer different questions, and
   * neither is interesting on its own. `answered` is "there is something to show"; the minimum
   * is "the screen has been on long enough to be read". The portfolio read usually comes back
   * in well under a second, so on its own it would take the screen away almost as soon as it
   * appeared -- and the minimum on its own would take the cover off a workspace that is still
   * empty on a slow connection, which is the one thing this screen exists to prevent.
   */
  const [answered, setAnswered] = useState(false)
  const [minimumElapsed, setMinimumElapsed] = useState(false)

  useEffect(() => {
    const timer = window.setTimeout(() => setMinimumElapsed(true), AURORA_MINIMUM_MS)
    return () => window.clearTimeout(timer)
  }, [])

  /** The first page has answered -- either way. */
  const loaded = useCallback(() => {
    if (booted.current) return
    booted.current = true
    setAnswered(true)
  }, [])

  useEffect(() => {
    if (!answered || !minimumElapsed) return
    setLeaving(true)
    // The same span the splash fades over -- it is handed that number by the component, so
    // there is no second copy of it to fall out of step. Unmounting on the same tick as the
    // state that starts the fade would skip it entirely.
    const timer = window.setTimeout(() => setBooting(false), AURORA_FADE_MS)
    return () => window.clearTimeout(timer)
  }, [answered, minimumElapsed])

  const navigate = useCallback((next: PageId) => {
    setPage(next)
    setVisited((current) =>
      current.has(next) ? current : new Set([...current, next]),
    )
  }, [])

  const openOrders = useCallback(
    (batchId: string) => {
      setFocusBatch(batchId)
      navigate('orders')
    },
    [navigate],
  )

  // `flex`, not `contents`. A wrapper with `display: contents` generates no box at all, so
  // its own `p-6` is dropped and every page would sit flush against the sidebar. The
  // Analysis page is not affected: its wrapper has no padding and the page draws its own
  // full-width header.
  const showing = (id: PageId) => (page === id ? 'flex' : 'none')

  return (
    <>
      {/* `inert` while the startup screen is up: the shell is behind it, and a reader must
          not be able to tab into a page they cannot see. */}
      <div
        className="flex min-h-screen flex-col bg-page min-[900px]:flex-row"
        inert={booting}
      >
        <Sidebar current={page} onNavigate={navigate} />

        {/* min-h-0 so the content column can be narrower and shorter than its contents, which
            is what lets a table or a transcript scroll inside its own container. */}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1 flex-col p-6" style={{ display: showing('dashboard') }}>
            {/* No `active` prop: the page reads four sources once on mount and refreshes when
                asked. Nothing here happens on its own, and nothing it calls writes anything. */}
            {visited.has('dashboard') && (
              <DashboardPage onNavigate={navigate} onOpenOrders={openOrders} />
            )}
          </div>

          <div className="min-h-0 flex-1 flex-col p-6" style={{ display: showing('portfolio') }}>
            {/* `active` is what the page polls on. It stays mounted while hidden, so
                mounting is not the same event as arriving here. */}
            {visited.has('portfolio') && (
              <PortfolioPage active={page === 'portfolio'} onLoaded={loaded} />
            )}
          </div>

          <div className="min-h-0 flex-1 flex-col p-6" style={{ display: showing('news') }}>
            {visited.has('news') && (
              <NewsPage active={page === 'news'} onOpenOrders={openOrders} />
            )}
          </div>

          <div className="min-h-0 flex-1 flex-col p-6" style={{ display: showing('backtest') }}>
            {/* No `active` prop: nothing on this page happens on its own. A backtest is run
                when somebody presses Run, and it holds no polling. */}
            {visited.has('backtest') && <BacktestPage />}
          </div>

          <div className="min-h-0 flex-1 flex-col p-6" style={{ display: showing('orders') }}>
            {visited.has('orders') && (
              <OrdersPage active={page === 'orders'} focusBatchId={focusBatch} />
            )}
          </div>

          <div className="min-h-0 flex-1 flex-col" style={{ display: showing('analysis') }}>
            {visited.has('analysis') && <AnalysisPage />}
          </div>
        </main>
      </div>

      {/* Over the shell, not instead of it: the read this waits for is the Portfolio page's
          own, and a shell that returned early would never mount the page that makes it. */}
      {/* `booting` decides whether it is here at all, `leaving` only how it looks on the way
          out: the screen has to stay mounted for the fade to run on, and unmounting it the
          moment readiness is known would cut that fade off on its first frame. */}
      {booting && <AuroraSplash leaving={leaving} />}
    </>
  )
}
