import type { ComponentPropsWithRef } from 'react'

/** Two weights and two colours, and that is the whole system.
 *
 * `secondary` is the default and covers every existing usage: menu, Refresh, Retry.
 * `primary` is the filled purple, for a genuine call to action.
 *
 * A variant prop rather than a className override, because two Tailwind utilities of
 * the same kind resolve by stylesheet order, not by the order they appear in the class
 * attribute — the same hazard documented in States.tsx.
 */
const VARIANTS = {
  secondary:
    'border-line bg-panel text-ink hover:border-accent hover:text-accent-ink ' +
    'disabled:hover:border-line disabled:hover:text-ink',
  primary:
    'border-accent-strong bg-accent-strong text-white hover:border-accent hover:bg-accent ' +
    'disabled:hover:border-accent-strong disabled:hover:bg-accent-strong',
}

/** A small control. `ComponentPropsWithRef` rather than `ButtonHTMLAttributes` because
 *  React 19 passes `ref` as an ordinary prop, and the menu button needs one to restore
 *  focus to. */
export function Button({
  className = '',
  variant = 'secondary',
  ...props
}: ComponentPropsWithRef<'button'> & { variant?: keyof typeof VARIANTS }) {
  return (
    <button
      // Not "submit": there is no form, and the default type would submit one anyway
      // if a form is ever added around it.
      type="button"
      {...props}
      className={`inline-flex h-8 items-center gap-1.5 rounded-md border px-3 text-label font-medium transition-colors disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
    />
  )
}
