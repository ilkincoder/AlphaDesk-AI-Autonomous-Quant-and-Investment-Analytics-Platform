/** Display formatting for values that arrive as decimal strings.
 *
 * All of it is string manipulation, deliberately. Day 3 sends money as JSON *strings*
 * so that a client cannot reintroduce the binary floating-point error the NUMERIC
 * columns exist to avoid — and `Number("16100.00")` would reintroduce it immediately.
 * These functions punctuate the digits they were handed instead of parsing them.
 *
 * None of this is valuation maths: every number displayed is calculated in Python.
 */

/** Shown wherever a value is genuinely absent, so a blank cell is never ambiguous. */
export const PLACEHOLDER = '—'

/** `"1000.00"` → `"$1,000.00"`.
 *
 * The scale is not forced to two digits here. Every money field the API returns
 * already has exactly two, so the output is two, but a value with more decimals
 * would be shown as it is rather than silently truncated.
 */
export function formatCurrency(value: string): string {
  const negative = value.startsWith('-')
  const digits = negative ? value.slice(1) : value
  const [whole, fraction = ''] = digits.split('.')

  // Insert a comma before every group of three digits that is not the first group.
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')

  return `${negative ? '-' : ''}$${grouped}.${fraction.padEnd(2, '0')}`
}

/** `"5.000000"` → `"5"`, `"3.500000"` → `"3.5"`, `"0.333333"` stays put.
 *
 * The column stores six decimal places, so trailing zeros are almost always noise.
 * A fraction someone actually holds is not noise, so it is never rounded away.
 */
export function formatShares(quantity: string): string {
  if (!quantity.includes('.')) return quantity
  return quantity.replace(/0+$/, '').replace(/\.$/, '')
}

/** `"6.21"` → `"6.21%"`. A null percentage is absent, not zero, so it shows a dash. */
export function formatPercent(value: string | null): string {
  return value === null ? PLACEHOLDER : `${value}%`
}

/** `"-10"` → `"-10%"`, `"10"` → `"+10%"`, `"0"` → `"0%"`.
 *
 * A signed percentage is a direction, so the plus is worth showing — the difference
 * between a rise and a fall should not rest on the reader noticing a missing minus.
 */
export function formatSignedPercent(value: string): string {
  if (isZeroAmount(value)) return '0%'
  return value.startsWith('-') ? `${value}%` : `+${value}%`
}

/** True for `"0"`, `"0.00"` and `"-0.00"`.
 *
 * Read off the digits rather than parsed: a minus sign in front of nothing is not a
 * change, and `Number("-0.00")` is not the only thing that would have to be trusted.
 */
export function isZeroAmount(value: string): boolean {
  return !/[1-9]/.test(value)
}

/** Which way a change went, as a word.
 *
 * Returned separately from any colour so that the direction survives without it. Red and
 * green are the least reliable way to say "down" and "up" — to a colour-blind reader
 * they are the same shade, and to a screen reader they are nothing at all.
 */
export function directionOf(value: string): 'increase' | 'decrease' | 'no change' {
  if (isZeroAmount(value)) return 'no change'
  return value.startsWith('-') ? 'decrease' : 'increase'
}
