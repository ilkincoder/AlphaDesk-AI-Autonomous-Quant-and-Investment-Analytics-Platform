/** One allocation, as a ring with a numeric legend.
 *
 * Hand-drawn SVG rather than a charting library. A donut is a circle with a `stroke-dasharray`
 * per segment, and the alternative is a dependency that ships a layout engine to draw one ring
 * — the existing `AllocationBar` already established that this application draws its own
 * allocation charts from the API's own percentages, and this follows it rather than adding a
 * second way to draw the same thing.
 *
 * **One ring is one complete allocation.** The percentages are the API's own strings, used as
 * segment lengths on a 100-unit circumference, and nothing here adds, scales or normalises
 * them. Two rings side by side are two allocations of the same portfolio at different times,
 * and they must never be combined into one — they need not even share a denominator, because
 * the "before" side is a share of the broker's equity and the "after" side is not.
 *
 * The legend is the accessible reading of the ring: every percentage is text, so the chart
 * itself is `aria-hidden` and carries no information a reader cannot get from the list.
 */

import { nothingToAllocate } from '../allocation'
import type { AllocationAsset } from '../allocation'
import { formatPercent } from '../format'

/** The radius that makes the circumference exactly 100, so a percentage is a dash length. */
const RADIUS = 15.9155

export function AllocationDonut({
  label,
  assets,
  caption,
}: {
  label: string
  assets: AllocationAsset[]
  caption?: string
}) {
  const empty = nothingToAllocate(assets)

  return (
    <div className="min-w-0">
      <p className="text-label font-medium text-ink">{label}</p>

      {empty ? (
        // No denominator, so there is no share to draw. Saying so beats an empty ring, which
        // would read as a portfolio that had lost everything.
        <p className="mt-2 text-label text-faint">
          Allocation unavailable for a zero-value portfolio.
        </p>
      ) : (
        <>
          <svg
            viewBox="0 0 40 40"
            width="104"
            height="104"
            className="mt-2 block"
            aria-hidden="true"
          >
            <g transform="rotate(-90 20 20)">
              <circle
                cx="20"
                cy="20"
                r={RADIUS}
                fill="none"
                stroke="var(--color-line)"
                strokeWidth="6"
              />
              {segmentsOf(assets).map((segment) => (
                <circle
                  key={segment.key}
                  cx="20"
                  cy="20"
                  r={RADIUS}
                  fill="none"
                  stroke={segment.colour}
                  strokeWidth="6"
                  // Each segment starts where the one before it ended. `dasharray` is the
                  // visible length followed by a gap long enough to reach the start again.
                  strokeDasharray={`${segment.length} ${100 - segment.length}`}
                  strokeDashoffset={`${-segment.offset}`}
                />
              ))}
            </g>
          </svg>
          {caption !== undefined && (
            <p className="mt-1 text-label text-faint">{caption}</p>
          )}
        </>
      )}

      <ul className="mt-3 space-y-1.5">
        {assets.map((asset) => (
          <li key={asset.key} className="flex items-center gap-2 text-label">
            <span
              className="size-2 shrink-0 rounded-full"
              style={{ backgroundColor: asset.colour }}
              aria-hidden="true"
            />
            <span className="text-dim">{asset.label}</span>
            <span className="ml-auto tabular-nums text-ink">
              {formatPercent(asset.percent)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The segments with their starting offsets, in drawing order.
 *
 * A percentage that cannot be read as a number is drawn as zero rather than throwing: the
 * ring is decorative and the legend beside it carries the value the API actually sent.
 */
function segmentsOf(assets: AllocationAsset[]) {
  let offset = 0
  return assets.map((asset) => {
    const length = clamp(Number(asset.percent ?? 0))
    const segment = { key: asset.key, colour: asset.colour, length, offset }
    offset += length
    return segment
  })
}

function clamp(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0
  return Math.min(value, 100)
}
