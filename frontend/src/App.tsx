import { useState } from 'react'

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
 * Which page is showing is plain state, not a URL. There is no router: two destinations
 * do not justify the dependency or the rework, and the cost is only that a refresh
 * returns to Portfolio and the What if? page cannot be linked to.
 */
export function App() {
  const [page, setPage] = useState<PageId>('portfolio')

  return (
    <div className="flex min-h-screen flex-col bg-page min-[900px]:flex-row">
      <Sidebar current={page} onNavigate={setPage} />
      {/* min-w-0 so the content column can be narrower than its contents, which is
          what lets the holdings table scroll inside its card. */}
      <main className="min-w-0 flex-1 p-6">
        {page === 'portfolio' ? <PortfolioPage /> : <WhatIfPage />}
      </main>
    </div>
  )
}
