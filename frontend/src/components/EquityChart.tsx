/** Two equity curves on one set of axes: the strategy's, and buy-and-hold's.
 *
 * Hand-drawn SVG, for the reason `AllocationDonut` is: a line chart is a polyline and a few
 * labels, and a charting library would be a dependency shipping a layout engine to draw two of
 * them.
 *
 * **The curves are plotted by position, not by date, and that is deliberate.** Both series come
 * from the same run and carry one point per session in the same order, so index *is* the common
 * axis. Plotting by calendar date would put the first two points of every curve at the same x
 * -- the series opens with the initial capital and then with the first session's close, and when
 * the requested start date is itself a session those are the same day -- and draw a vertical
 * stub at the left edge for no reason. The dates are still what the axis is labelled with.
 *
 * **Two lines, told apart by more than colour.** The benchmark is dashed as well as dimmer, and
 * the legend is text. A reader who cannot separate the two purples is not left with one line.
 *
 * The numbers are all in the metrics beside the chart; the chart is `aria-hidden` and says
 * nothing a reader cannot get from them.
 */

import { formatCurrency } from '../format'
import type { BacktestEquityPoint } from '../api'

/** The drawing area, in the SVG's own units. The element scales to its container. */
const WIDTH = 720
const HEIGHT = 240
const PADDING = { top: 12, right: 12, bottom: 26, left: 12 }

const PLOT_WIDTH = WIDTH - PADDING.left - PADDING.right
const PLOT_HEIGHT = HEIGHT - PADDING.top - PADDING.bottom

/** Short axis labels: `"2024-03-04"` → `"4 Mar 24"`.
 *
 * Parsed as a date rather than sliced as a string, because the reader wants a date and not eight
 * characters. `UTC` so a session dated the first of a month is never labelled as the last day of
 * the previous one for a reader behind the exchange.
 */
function shortDate(value: string): string {
  const at = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(at.getTime())) return value
  return at.toLocaleDateString(undefined, {
    day: 'numeric',
    month: 'short',
    year: '2-digit',
    timeZone: 'UTC',
  })
}

export function EquityChart({
  equity,
  benchmark,
  strategyName,
}: {
  equity: BacktestEquityPoint[]
  benchmark: BacktestEquityPoint[]
  strategyName: string
}) {
  if (equity.length < 2) {
    return (
      <p className="text-body text-dim">
        The test covered too few sessions to draw a curve.
      </p>
    )
  }

  // Every value is a decimal string from the API, so it is parsed here and only here -- for
  // geometry, which is a float's job. Nothing displayed goes through this.
  const values = [...equity, ...benchmark].map((point) => Number(point.equity))
  const rawMin = Math.min(...values)
  const rawMax = Math.max(...values)
  // A flat curve would otherwise divide by zero. When the account never moved, the line is
  // drawn through the middle instead of being pinned to an edge.
  const span = rawMax - rawMin || Math.max(rawMax, 1)
  const min = rawMin - span * 0.08
  const max = rawMax + span * 0.08

  const x = (index: number, count: number) =>
    PADDING.left + (count < 2 ? PLOT_WIDTH / 2 : (index / (count - 1)) * PLOT_WIDTH)
  const y = (value: number) =>
    PADDING.top + PLOT_HEIGHT - ((value - min) / (max - min)) * PLOT_HEIGHT

  const line = (series: BacktestEquityPoint[]) =>
    series.map((point, index) => `${x(index, series.length)},${y(Number(point.equity))}`).join(' ')

  // Three gridlines: the top and bottom of the plotted range, and the starting capital, which is
  // the line every return on this page is measured from. A line whose label would land on top of
  // one already placed is dropped rather than drawn over it -- the extremes are the axis and the
  // opening balance is the reference, and a legible two beat an unreadable three.
  const opening = Number(equity[0].equity)
  const placed: number[] = []
  const gridlines = [rawMax, opening, rawMin].filter((value, index, all) => {
    if (all.indexOf(value) !== index) return false
    const at = y(value)
    if (placed.some((other) => Math.abs(other - at) < 14)) return false
    placed.push(at)
    return true
  })

  const last = equity.length - 1

  return (
    <figure className="m-0">
      {/* No fixed height: `w-full` plus the viewBox lets the browser derive one from the aspect
          ratio, so the drawing fills its box at every width. A fixed height letterboxes it as
          soon as the panel is narrower than the viewBox's proportions. */}
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="w-full"
        role="img"
        aria-label={`Equity curves for ${strategyName} and buy-and-hold over ${equity.length} points`}
      >
        {gridlines.map((value) => (
          <g key={value}>
            <line
              x1={PADDING.left}
              x2={WIDTH - PADDING.right}
              y1={y(value)}
              y2={y(value)}
              stroke="var(--color-line)"
              strokeWidth={1}
            />
            {/* Stroked underneath itself in the panel colour, so the label stays readable where
                a curve runs through it. A backing rectangle would need the text measured; a
                halo needs nothing and works wherever the line happens to be. */}
            <text
              x={PADDING.left + 2}
              y={y(value) - 4}
              fontSize={10}
              textAnchor="start"
              fill="var(--color-faint)"
              stroke="var(--color-panel)"
              strokeWidth={3}
              paintOrder="stroke"
            >
              {formatCurrency(value.toFixed(2))}
            </text>
          </g>
        ))}

        {/* The benchmark first, so the strategy's line is not covered by it where they cross. */}
        <polyline
          points={line(benchmark)}
          fill="none"
          stroke="var(--color-cash)"
          strokeWidth={1.5}
          strokeDasharray="5 4"
          strokeLinejoin="round"
        />
        <polyline
          points={line(equity)}
          fill="none"
          stroke="var(--color-accent)"
          strokeWidth={2}
          strokeLinejoin="round"
        />

        <text x={PADDING.left} y={HEIGHT - 8} fontSize={10} textAnchor="start" fill="var(--color-faint)">
          {shortDate(equity[0].on)}
        </text>
        <text
          x={WIDTH - PADDING.right}
          y={HEIGHT - 8}
          fontSize={10}
          textAnchor="end"
          fill="var(--color-faint)"
        >
          {shortDate(equity[last].on)}
        </text>
      </svg>

      <figcaption className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-label text-dim">
        <span className="inline-flex items-center gap-1.5">
          <svg width="18" height="8" aria-hidden="true">
            <line
              x1="0"
              y1="4"
              x2="18"
              y2="4"
              stroke="var(--color-accent)"
              strokeWidth="2"
            />
          </svg>
          {strategyName}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="18" height="8" aria-hidden="true">
            <line
              x1="0"
              y1="4"
              x2="18"
              y2="4"
              stroke="var(--color-cash)"
              strokeWidth="1.5"
              strokeDasharray="5 4"
            />
          </svg>
          Buy and hold
        </span>
      </figcaption>
    </figure>
  )
}
