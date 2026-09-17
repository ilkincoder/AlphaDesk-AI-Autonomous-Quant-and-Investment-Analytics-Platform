/** Navigation icons.
 *
 * Nine of them, all drawn on the same 16x16 grid with a 1.5px stroke, so they read as
 * one set rather than nine borrowed drawings. Each inherits `currentColor`, so a row's
 * icon colours itself from the row's text colour and no icon needs a variant.
 */

import type { ReactNode } from 'react'

export function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      // Decorative: the row already has a text label, so announcing the icon too
      // would just make every item read twice.
      aria-hidden="true"
      className="shrink-0"
    >
      {children}
    </svg>
  )
}

export function DashboardIcon() {
  return (
    <Icon>
      <rect x="2" y="2" width="5" height="5" rx="1" />
      <rect x="9" y="2" width="5" height="5" rx="1" />
      <rect x="2" y="9" width="5" height="5" rx="1" />
      <rect x="9" y="9" width="5" height="5" rx="1" />
    </Icon>
  )
}

export function AnalysisIcon() {
  return (
    <Icon>
      <path d="M2.5 2.5v11h11" />
      <path d="M5.5 10.5V8" />
      <path d="M8 10.5V5" />
      <path d="M10.5 10.5V6.5" />
    </Icon>
  )
}

export function PortfolioIcon() {
  return (
    <Icon>
      <rect x="2" y="5" width="12" height="8.5" rx="1.5" />
      <path d="M6 5V3.5A1.5 1.5 0 0 1 7.5 2h1A1.5 1.5 0 0 1 10 3.5V5" />
    </Icon>
  )
}

export function WhatIfIcon() {
  return (
    <Icon>
      <circle cx="8" cy="8" r="5.5" />
      {/* A question mark: the arc, the stem, then the dot. */}
      <path d="M6.4 6.3a1.6 1.6 0 0 1 3.2 0v.2c0 1-.9 1.5-1.6 2.1v1" />
      <path d="M8 11.3h.01" />
    </Icon>
  )
}

export function StrategiesIcon() {
  return (
    <Icon>
      <circle cx="8" cy="8" r="5.5" />
      <circle cx="8" cy="8" r="1.75" />
    </Icon>
  )
}

export function BacktestIcon() {
  return (
    <Icon>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 5v3.4l2.2 1.3" />
    </Icon>
  )
}

export function OrdersIcon() {
  return (
    <Icon>
      <path d="M3.5 4h9" />
      <path d="M3.5 8h9" />
      <path d="M3.5 12h5" />
    </Icon>
  )
}

export function WatchlistIcon() {
  return (
    <Icon>
      <path d="M1.5 8S3.9 4 8 4s6.5 4 6.5 4-2.4 4-6.5 4S1.5 8 1.5 8Z" />
      <circle cx="8" cy="8" r="1.75" />
    </Icon>
  )
}

export function DataNewsIcon() {
  return (
    <Icon>
      <ellipse cx="8" cy="4" rx="5" ry="2" />
      <path d="M3 4v8c0 1.1 2.24 2 5 2s5-.9 5-2V4" />
      <path d="M3 8c0 1.1 2.24 2 5 2s5-.9 5-2" />
    </Icon>
  )
}

export function SettingsIcon() {
  return (
    <Icon>
      <path d="M2.5 5.5h11" />
      <path d="M2.5 10.5h11" />
      <circle cx="6" cy="5.5" r="1.6" />
      <circle cx="10" cy="10.5" r="1.6" />
    </Icon>
  )
}
