import { useCallback, useState } from 'react'

import { AnalysisPage } from './components/AnalysisPage'
import { NewsPage } from './components/NewsPage'
import { PortfolioPage } from './components/PortfolioPage'
import { Sidebar } from './components/Sidebar'
import type { PageId } from './components/Sidebar'
import { WhatIfPage } from './components/WhatIfPage'

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

  const navigate = useCallback((next: PageId) => {
    setPage(next)
    setVisited((current) =>
      current.has(next) ? current : new Set([...current, next]),
    )
  }, [])

  const showing = (id: PageId) => (page === id ? 'contents' : 'none')

  return (
    <div className="flex min-h-screen flex-col bg-page min-[900px]:flex-row">
      <Sidebar current={page} onNavigate={navigate} />

      {/* min-h-0 so the content column can be narrower and shorter than its contents, which
          is what lets a table or a transcript scroll inside its own container. */}
      <main className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 flex-col p-6" style={{ display: showing('portfolio') }}>
          {/* `active` is what the page polls on. It stays mounted while hidden, so
              mounting is not the same event as arriving here. */}
          {visited.has('portfolio') && <PortfolioPage active={page === 'portfolio'} />}
        </div>

        <div className="min-h-0 flex-1 flex-col p-6" style={{ display: showing('what-if') }}>
          {visited.has('what-if') && <WhatIfPage />}
        </div>

        <div className="min-h-0 flex-1 flex-col p-6" style={{ display: showing('news') }}>
          {visited.has('news') && <NewsPage active={page === 'news'} />}
        </div>

        <div className="min-h-0 flex-1 flex-col" style={{ display: showing('analysis') }}>
          {visited.has('analysis') && <AnalysisPage />}
        </div>
      </main>
    </div>
  )
}
