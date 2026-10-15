/** The startup screen. Shown once, over the shell, until the workspace has answered.
 *
 * **It covers the workspace rather than replacing it**, and that is not a styling choice: what
 * the screen waits for is the Portfolio page's own first read, so a shell that returned early
 * would never mount the page, never make the read, and never finish. The workspace mounts and
 * loads underneath; this sits on top of it until the read lands.
 *
 * **It is the startup screen and nothing else.** `MarketPulse` is still the work screen the News
 * page puts over a running proposal, and the two share no code: that one is a small mark and a
 * message, this one is a composition, and the only thing they have in common is that both cover
 * the page while something else is happening. Nothing here is reusable for a mid-page wait, and
 * that is deliberate -- see `App` for the flag that decides whether it is here at all.
 *
 * **Nothing here is data.** The chart is one fixed path with fixed candles drawn to look like a
 * market without being one: no axis, no scale, no value, and no up or down that means anything.
 * A sparkline computed from the portfolio would be a claim about it, on a screen that is showing
 * nothing yet. Every drawing here is marked `aria-hidden`, and the only words the screen offers
 * a screen reader are the ones saying what is happening.
 *
 * The animations are in `index.css`, all of them scoped to `.aurora-splash*` so that nothing on
 * any other screen can pick them up.
 */

/** How long the screen takes to fade once the workspace is ready.
 *
 * The caller keeps it mounted for this long after the wait ends, so the transition has
 * something to run on. The same number is handed to the transition inline rather than written
 * into a `duration-*` class as well, so the fade and the unmount cannot come apart. */
export const AURORA_FADE_MS = 400

/** How long the screen stays up, at the least.
 *
 * A floor and not a duration. Two things are being waited for and the screen leaves when both
 * have happened: the workspace answering, and this much time passing. The first alone is not
 * enough -- the portfolio read is usually back in well under a second, and the screen would be
 * a flash of something the reader never got to look at. The second alone is not enough either:
 * a read that takes longer than this keeps the screen up for as long as it takes, because the
 * alternative is taking the cover off a workspace that is still empty.
 *
 * The clock is a timer started at mount rather than elapsed time measured at the answer, so
 * that the whole of the startup screen runs on one clock -- the same one the tests advance. */
export const AURORA_MINIMUM_MS = 3000

/** Candles for the background chart, as drawn on the chart's own 1200x600 grid:
 *  `[x, centreY, height]`.
 *
 * Written out rather than generated. A random chart is a different chart on every load, which
 * is the one thing a decorative market drawing must not be -- it would read as a reading. The
 * centres follow the line above them, so the bars sit behind the curve instead of crossing it.
 */
const AURORA_CANDLES: ReadonlyArray<readonly [number, number, number]> = [
  [30, 331, 34],
  [85, 335, 22],
  [140, 324, 40],
  [195, 317, 26],
  [250, 303, 44],
  [305, 292, 30],
  [360, 300, 20],
  [415, 296, 38],
  [470, 288, 24],
  [525, 277, 46],
  [580, 257, 32],
  [635, 252, 22],
  [690, 262, 40],
  [745, 258, 26],
  [800, 243, 42],
  [855, 232, 28],
  [910, 220, 36],
  [965, 222, 22],
  [1020, 215, 32],
  [1075, 206, 24],
  [1130, 187, 30],
  [1185, 151, 20],
]

/** Points on the line below, in the same 1200x600 grid, lit one after another.
 *
 * They are the curve's own join points, read off the path rather than guessed -- a node that is
 * not on the line is the one thing that would give the drawing away as a decoration. They are
 * drawn as positioned elements rather than as SVG circles because the chart is stretched to the
 * viewport: a circle inside it would be scaled with it and come out an ellipse on every screen
 * whose proportions differ from the grid's.
 */
const AURORA_NODES: ReadonlyArray<readonly [number, number]> = [
  [150, 326],
  [450, 298],
  [750, 260],
  [1050, 212],
]

/** The chart, as one path on the 1200x600 grid.
 *
 * Two things are fixed by construction. It starts at y=330, which is 55% of the grid's height,
 * and ends at y=138, which is 23% -- so on a viewport the grid is stretched to fill, the line
 * runs from just below the middle of the left edge to the upper quarter of the right one, and
 * passes behind the logo on the way because the logo is on the centre line.
 *
 * Every command after the first is an `S`, which mirrors the previous control point, so the
 * joins are smooth by construction rather than by tuning -- which is what makes it read as a
 * price series instead of as a drawing of one.
 */
const AURORA_CHART_PATH =
  'M 0 330 ' +
  'C 60 312, 96 348, 150 326 ' +
  'S 246 268, 300 294 ' +
  'S 396 342, 450 298 ' +
  'S 546 224, 600 250 ' +
  'S 696 302, 750 260 ' +
  'S 846 194, 900 218 ' +
  'S 996 262, 1050 212 ' +
  'S 1146 158, 1200 138'

/** The AlphaDesk mark: the same "A" the sidebar draws on its 24x24 grid, as a filled triangle
 *  rather than as a knockout in a tile.
 *
 * The geometry is the brand's and is not re-drawn here by eye -- see `Wordmark` in `Sidebar`,
 * which cuts the same triangle out of a rounded violet tile. On this screen it is the positive
 * form, because the ring is already the tile. */
function AuroraMark() {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className="aurora-splash__mark h-[58px] w-[58px] min-[700px]:h-[82px] min-[700px]:w-[82px]"
    >
      <defs>
        <linearGradient id="aurora-mark-fill" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#A65BFF" />
          <stop offset="100%" stopColor="#7040E8" />
        </linearGradient>
      </defs>
      <path d="M12 5.5 18.5 18.5H5.5Z" fill="url(#aurora-mark-fill)" />
    </svg>
  )
}

export function AuroraSplash({ leaving = false }: { leaving?: boolean }) {
  return (
    <div
      // Above the shell, and above the sidebar's mobile drawer (z-40), which is the only other
      // overlay in the application.
      //
      // `h-[100dvh]` as well as `inset-0`: a fixed element is laid out against the *layout*
      // viewport, which on a phone is taller than the part of it on screen -- so anything
      // positioned against the container's bottom lands behind the browser's own chrome.
      className={`aurora-splash fixed inset-0 z-50 h-[100dvh] overflow-hidden transition-opacity ${
        leaving ? 'opacity-0' : 'opacity-100'
      }`}
      style={{ transitionDuration: `${AURORA_FADE_MS}ms` }}
    >
      {/* The aurora: three blurred bands drifting across the middle of the screen at different
          speeds. Three rather than one because a single band moves as a single object, and the
          light this is imitating never does. */}
      <div className="absolute inset-0 overflow-hidden" aria-hidden="true">
        <span className="aurora-splash__ribbon aurora-splash__ribbon--one" />
        <span className="aurora-splash__ribbon aurora-splash__ribbon--two" />
        <span className="aurora-splash__ribbon aurora-splash__ribbon--three" />
      </div>

      {/* The chart. Stretched to fill the viewport rather than scaled to fit it, because the
          shape it has to keep is "left edge low, right edge high" and fitting the grid inside
          the viewport would letterbox it into a band. The stroke is `non-scaling` so that a
          stretched chart still has a line of one width on it. */}
      <svg
        viewBox="0 0 1200 600"
        preserveAspectRatio="none"
        aria-hidden="true"
        className="aurora-splash__chart absolute inset-0 h-full w-full"
      >
        <defs>
          <linearGradient id="aurora-chart-stroke" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#8B45FF" stopOpacity="0.35" />
            <stop offset="55%" stopColor="#A65BFF" stopOpacity="0.75" />
            <stop offset="100%" stopColor="#A65BFF" stopOpacity="1" />
          </linearGradient>
        </defs>

        <g className="aurora-splash__chart-ink">
          <g className="aurora-splash__chart-candles">
            {AURORA_CANDLES.map(([x, centre, height]) => (
              <rect
                key={`candle-${x}`}
                x={x}
                y={centre - height / 2}
                width={5}
                height={height}
                rx={1}
                className="aurora-splash__chart-candle"
              />
            ))}
          </g>

          <path
            d={AURORA_CHART_PATH}
            fill="none"
            stroke="url(#aurora-chart-stroke)"
            strokeWidth={1.8}
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
            className="aurora-splash__chart-line"
          />
        </g>
      </svg>

      {/* Lit points on the line, positioned as elements so they stay round on every screen.
          Percentages of the grid: x/1200 and y/600. */}
      <div className="absolute inset-0" aria-hidden="true">
        {AURORA_NODES.map(([x, y], index) => (
          <span
            key={`node-${x}`}
            className={`aurora-splash__node aurora-splash__node--${index + 1}`}
            style={{ left: `${(x / 1200) * 100}%`, top: `${(y / 600) * 100}%` }}
          />
        ))}
      </div>

      {/* The light the aurora is casting on the surface below it, and nothing else: no floor,
          no grid, no horizon line. */}
      <div className="aurora-splash__glow" aria-hidden="true" />

      {/* A design label, not a control and not information: the composition keeps a corner
          quiet without it saying anything. */}
      <p
        aria-hidden="true"
        className="aurora-splash__label absolute left-7 top-7 text-[16px] font-light tracking-wide min-[700px]:text-[17px]"
      >
        03 / Aurora
      </p>

      <div className="relative flex h-full flex-col items-center justify-center px-6">
        <div className="aurora-splash__logo relative h-[160px] w-[160px] min-[700px]:h-[216px] min-[700px]:w-[216px]">
          {/* The ring turns as a whole. The track under the arc is a circle, so rotating it
              moves nothing a reader can see -- which is what lets the rotation be a plain CSS
              transform on the element rather than a transform on the arc inside it. The mark is
              a separate layer precisely so that it does not turn with it.
              `r` is 15.9155 because that makes the circumference 100, so the dash below is the
              arc's percentage of the ring and not a number to be recomputed when it changes. */}
          <svg
            viewBox="0 0 40 40"
            fill="none"
            aria-hidden="true"
            className="aurora-splash__ring absolute inset-0 h-full w-full"
          >
            <defs>
              <linearGradient id="aurora-ring-arc" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stopColor="#7040E8" />
                <stop offset="100%" stopColor="#A65BFF" />
              </linearGradient>
            </defs>
            <circle
              cx="20"
              cy="20"
              r="15.9155"
              strokeWidth="1.1"
              className="aurora-splash__ring-track"
            />
            <circle
              cx="20"
              cy="20"
              r="15.9155"
              stroke="url(#aurora-ring-arc)"
              strokeWidth="1.1"
              strokeLinecap="round"
              // 30 of 100, turned to start at the top so the arc reads as progress rather than
              // as a segment that happens to be somewhere.
              strokeDasharray="30 70"
              transform="rotate(-90 20 20)"
            />
            {/* The head of the arc, on the same circle and behind the same rotation, so the
                two cannot disagree about where the arc ends.
                Placed at the dash's own end before that rotation: a circle's path starts at
                three o'clock and runs clockwise, and 30% of it is 108 degrees, which lands at
                (20 - r*cos72, 20 + r*sin72) -- r being 15.9155. */}
            <circle
              cx="15.08"
              cy="35.14"
              r="1.5"
              className="aurora-splash__ring-head"
              transform="rotate(-90 20 20)"
            />
          </svg>

          <div className="absolute inset-0 flex items-center justify-center">
            <AuroraMark />
          </div>
        </div>

        {/* Not an `h1`: the page underneath has one, and a second would compete with it for the
            same role on a screen that has not decided what the reader is looking at. */}
        <p className="aurora-splash__title mt-10 text-center text-[34px] font-medium leading-[1.1] tracking-tight min-[700px]:mt-12 min-[700px]:text-[50px]">
          AlphaDesk <span className="aurora-splash__title-ai">AI</span>
        </p>

        <p
          // A status rather than an alert: it is what the screen is doing, not something that
          // has gone wrong, and it should not interrupt. It is announced once, when it appears.
          role="status"
          className="aurora-splash__subtitle mt-4 text-center text-[16px] font-normal min-[700px]:text-[21px]"
        >
          Loading your intelligent trading workspace
        </p>

        <div className="mt-7 flex items-center gap-3" aria-hidden="true">
          <span className="aurora-splash__dot aurora-splash__dot--one" />
          <span className="aurora-splash__dot aurora-splash__dot--two" />
          <span className="aurora-splash__dot aurora-splash__dot--three" />
        </div>
      </div>
    </div>
  )
}
