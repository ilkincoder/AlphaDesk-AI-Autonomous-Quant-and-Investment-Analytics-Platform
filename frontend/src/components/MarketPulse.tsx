/** The screen the News page shows while a rebalance proposal is running.
 *
 * It **covers** the page underneath rather than replacing it, because the page underneath is
 * what is doing the work. The content stays mounted and runs below; this sits on top of it
 * until the wait ends.
 *
 * It waits a moment before appearing -- see `useWorkScreen` in `NewsPage` -- because the
 * alternative is a working page, and a rejection that comes back in 80ms should not flash the
 * whole application dark.
 *
 * **Nothing here is data.** The trace is one fixed path, drawn to look like a market without
 * being one: it has no axis, no scale, no value, and no up or down that means anything. A
 * sparkline computed from the portfolio would be a claim about it, on a screen that is
 * showing nothing yet -- so this is decorative, marked `aria-hidden`, and the only words it
 * offers a screen reader are the ones saying what is happening.
 *
 * The animations are in `index.css`, where the line and the dot are driven by the same 3s
 * clock so the dot cannot drift off the revealed end.
 */

/** How long the screen takes to fade once the wait is over.
 *
 * The caller holds the screen mounted for this long after the wait ends so the transition has
 * something to run on. Kept in step with the `duration-200` below by being the same number.
 */
export const PULSE_FADE_MS = 200

/** The trace, exactly as drawn here and again as the dot's path in `index.css`.
 *
 * One shape rather than two keeps the dot on the line by construction instead of by the two
 * being kept in step by hand.
 */
const MARKET_TRACE_PATH =
  'M 0 145 C 12 140 18 129 28 132 S 43 110 54 118 S 70 128 82 111 S 97 107 109 89 ' +
  'S 123 80 136 98 S 149 93 161 111 S 177 106 190 119 S 205 125 217 132 S 231 120 244 112 ' +
  'S 257 116 270 96 S 284 87 296 100 S 310 80 322 86 S 335 58 347 72 S 361 50 374 51 ' +
  'S 387 69 399 82 S 411 64 424 88 S 438 109 452 97 S 467 85 481 80 S 500 61 520 60'

export function MarketPulse({
  message,
  leaving = false,
}: {
  /** What the wait is for. One line, because that is the slot the composition has for it:
   *  the screen names the thing being waited on and nothing else. */
  message: string
  leaving?: boolean
}) {
  return (
    <div
      // Above the shell, and above the sidebar's mobile drawer (z-40), which is the only
      // other overlay in the application.
      //
      // `h-[100dvh]` as well as `inset-0`: a fixed element is laid out against the *layout*
      // viewport, which on a phone is taller than the part of it on screen -- so a footer
      // pinned to the container's bottom lands behind the browser's own chrome. `dvh` is the
      // height actually visible, which is what the footer is pinned to. On a desktop, where
      // nothing collapses, the two are the same number.
      className={`fixed inset-0 z-50 h-[100dvh] overflow-hidden bg-page transition-opacity duration-200 ${
        leaving ? 'opacity-0' : 'opacity-100'
      }`}
    >
      {/* One centred column, so the vertical rhythm is a handful of gaps to tune rather than
          five positions to keep in agreement with each other. */}
      <div className="flex h-full flex-col items-center justify-center px-6 pb-7">
        {/* Not an `h1`: the page underneath has one, and a second would compete with it for
            the same role on a screen that has not decided what the reader is looking at. */}
        <p className="text-center text-[36px] font-semibold leading-[1.1] tracking-tight text-[#F4F5FA] min-[700px]:text-[58px]">
          AlphaDesk <span className="text-accent">AI</span>
        </p>

        <svg
          viewBox="0 0 520 180"
          role="presentation"
          aria-hidden="true"
          className="mt-9 h-auto w-[min(520px,calc(100vw-48px))] min-[700px]:mt-[38px]"
        >
          <defs>
            {/* The grid fades on a radial mask, so it dissolves toward every edge at once and
                never draws the rectangle it is clipped to. */}
            <radialGradient id="splash-grid-fade" cx="50%" cy="50%" r="62%">
              <stop offset="0%" stopColor="#ffffff" stopOpacity="0.9" />
              <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
            </radialGradient>
            <mask id="splash-grid-mask">
              <rect width="520" height="180" fill="url(#splash-grid-fade)" />
            </mask>
            <pattern id="splash-grid" width="30" height="30" patternUnits="userSpaceOnUse">
              <path
                d="M 30 0 L 0 0 L 0 30"
                fill="none"
                stroke="#3b4b63"
                strokeWidth="1"
                opacity="0.5"
              />
            </pattern>

            {/* Dim at the tail, brightest at the head: the trace is read left to right. */}
            <linearGradient id="splash-trace" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor="#6d28d9" stopOpacity="0.35" />
              <stop offset="45%" stopColor="#8b5cf6" stopOpacity="0.9" />
              <stop offset="100%" stopColor="#c4b5fd" stopOpacity="1" />
            </linearGradient>
          </defs>

          <rect width="520" height="180" fill="url(#splash-grid)" mask="url(#splash-grid-mask)" />

          <path
            d={MARKET_TRACE_PATH}
            fill="none"
            stroke="url(#splash-trace)"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            // A dash of 1 is the whole path at this `pathLength`, so the offset animated in
            // `index.css` is the revealed fraction and no length has to be measured in the
            // browser.
            pathLength="1"
            className="market-pulse-line"
            // A glow, not a bloom: enough to read the head of the line as the live end of it.
            style={{ filter: 'drop-shadow(0 0 5px rgba(139, 92, 246, 0.55))' }}
          />

          {/* One dot, on the same path as the line and revealed by the same clock. */}
          <circle
            r="5"
            fill="#f4f5fa"
            className="market-pulse-dot"
            style={{ filter: 'drop-shadow(0 0 7px rgba(167, 139, 250, 0.9))' }}
          />
        </svg>

        <p
          // A status rather than an alert: it is what the screen is doing, not something that
          // has gone wrong, and it should not interrupt. It is announced once, when it
          // appears -- the animation is `aria-hidden` and cycles in silence.
          role="status"
          className="mt-7 text-center text-[19px] font-normal text-[#D5DBE7] min-[700px]:mt-[28px] min-[700px]:text-[28px]"
        >
          {message}
        </p>

        <div
          aria-hidden="true"
          className="mt-7 h-[6px] w-[220px] overflow-hidden rounded-full border border-[#232f42] bg-[#141c29] min-[700px]:w-[270px]"
        >
          <div className="market-pulse-accent h-full w-[95px] rounded-full bg-accent" />
        </div>
      </div>

      {/* Outside the centred column, because it is not part of the composition: it is pinned
          to the bottom of the screen, and a short window takes the space out of the gap above
          it rather than moving the footer up. The safe-area inset is added to the 48px rather
          than replacing it -- on a phone with a home indicator that area is under this text,
          and 48px from the glass is not 48px from the bottom of the screen. */}
      <p className="absolute bottom-[calc(3rem+env(safe-area-inset-bottom))] left-0 w-full text-center text-[12px] tracking-[2px] text-[#748198]">
        Investment Analytics
      </p>
    </div>
  )
}
