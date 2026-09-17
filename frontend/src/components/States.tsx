/** Everything the page shows when it is not showing data.
 *
 * Split from the cards themselves so that "what the page looks like while loading"
 * is one file to read, and so no state can be quietly faked inside a card component.
 */

import type { ReactNode } from 'react'

import { Button } from './Button'
import { Card } from './Card'

function Skeleton({ className }: { className: string }) {
  return <div className={`animate-pulse rounded bg-line ${className}`} />
}

/** Placeholders shaped like the two cards they stand in for.
 *
 * Matching the real layout matters: if the skeleton were a spinner, every load would
 * end with the page jumping as a different shape appeared.
 */
export function LoadingCards() {
  return (
    <div
      aria-busy="true"
      className="grid grid-cols-1 gap-4 min-[1100px]:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]"
    >
      <Card className="min-[1100px]:min-h-[400px]">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="mt-3 h-8 w-40" />
        <Skeleton className="mt-2 h-3 w-32" />
        <Skeleton className="mt-6 h-3 w-full" />
        <Skeleton className="mt-3 h-3 w-5/6" />
      </Card>
      <Card className="min-[1100px]:min-h-[400px]">
        <Skeleton className="h-4 w-20" />
        {[0, 1, 2, 3].map((row) => (
          <Skeleton key={row} className="mt-4 h-6 w-full" />
        ))}
      </Card>
      {/* One announcement for the whole thing, rather than eight anonymous grey boxes. */}
      <span className="sr-only" role="status">
        Loading portfolio data
      </span>
    </div>
  )
}

/** A request failed and there is nothing to show. Always offers a way back. */
export function ErrorNotice({
  title,
  message,
  onRetry,
}: {
  title: string
  message: string
  onRetry: () => void
}) {
  return (
    <Card>
      {/* The red edge lives on an inner element rather than on the Card itself. Two
          Tailwind border-colour utilities on one element resolve by stylesheet order,
          not class order, so overriding Card's own border from the outside is a
          coin-flip; a nested element has nothing to fight with. */}
      <div role="alert" className="border-l-2 border-l-danger pl-3">
        <p className="text-heading font-medium text-ink">{title}</p>
        <p className="mt-1 text-note text-dim">{message}</p>
      </div>
      <Button className="mt-4" onClick={onRetry}>
        Retry
      </Button>
    </Card>
  )
}

/** Something needs saying, but the page is still usable: a note, not an error page. */
export function Banner({ children }: { children: ReactNode }) {
  // role="status" rather than "alert": this accompanies values that are already on
  // screen, so it should be announced politely, not interrupt.
  return (
    <div
      role="status"
      className="rounded-card border border-accent/40 bg-selected px-4 py-3 text-note text-accent-ink"
    >
      {children}
    </div>
  )
}
