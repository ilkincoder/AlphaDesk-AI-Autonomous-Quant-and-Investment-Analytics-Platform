import type { ReactNode } from 'react'

/** The panel every block on the page sits in.
 *
 * 10px radius, hairline border, 20px padding, and a shadow you have to look for. The
 * point of a shared wrapper is that those four numbers are decided once, so two cards
 * cannot drift apart.
 */
export function Card({
  children,
  className = '',
}: {
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`rounded-card border border-line bg-panel p-5 shadow-card ${className}`}>
      {children}
    </section>
  )
}
