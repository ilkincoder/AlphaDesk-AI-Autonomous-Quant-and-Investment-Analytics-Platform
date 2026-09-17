import { describe, expect, it } from 'vitest'

import {
  directionOf,
  formatCurrency,
  formatPercent,
  formatShares,
  formatSignedPercent,
  isZeroAmount,
} from './format'

describe('formatCurrency', () => {
  it('groups thousands and keeps two decimal places', () => {
    expect(formatCurrency('16100.00')).toBe('$16,100.00')
    expect(formatCurrency('1000.00')).toBe('$1,000.00')
    expect(formatCurrency('10000.00')).toBe('$10,000.00')
  })

  it('leaves values under a thousand alone', () => {
    expect(formatCurrency('200.00')).toBe('$200.00')
    expect(formatCurrency('0.00')).toBe('$0.00')
  })

  it('groups every third digit in longer values', () => {
    expect(formatCurrency('1234567.89')).toBe('$1,234,567.89')
  })

  it('pads a value that arrives with fewer than two decimals', () => {
    expect(formatCurrency('5')).toBe('$5.00')
  })

  it('shows more decimals rather than silently truncating them', () => {
    // Money fields are always 2dp today. If one ever is not, showing the digits is
    // safer than rounding them away without saying so.
    expect(formatCurrency('1.2345')).toBe('$1.2345')
  })

  it('keeps a negative sign outside the symbol', () => {
    expect(formatCurrency('-250.50')).toBe('-$250.50')
  })
})

describe('formatShares', () => {
  it('drops the six trailing zeros the column stores', () => {
    expect(formatShares('5.000000')).toBe('5')
    expect(formatShares('10.000000')).toBe('10')
  })

  it('keeps a fraction that means something', () => {
    expect(formatShares('3.500000')).toBe('3.5')
    expect(formatShares('0.333333')).toBe('0.333333')
    expect(formatShares('2.250000')).toBe('2.25')
  })

  it('handles a value with no decimal point at all', () => {
    expect(formatShares('7')).toBe('7')
  })

  it('does not turn a zero quantity into an empty cell', () => {
    expect(formatShares('0.000000')).toBe('0')
  })
})

describe('formatPercent', () => {
  it('appends a percent sign to a value from the API', () => {
    expect(formatPercent('6.21')).toBe('6.21%')
    expect(formatPercent('62.11')).toBe('62.11%')
  })

  it('shows a dash for null, which is absent rather than zero', () => {
    expect(formatPercent(null)).toBe('—')
  })
})

describe('formatSignedPercent', () => {
  it('keeps a minus and adds a plus', () => {
    expect(formatSignedPercent('-10')).toBe('-10%')
    expect(formatSignedPercent('10')).toBe('+10%')
    expect(formatSignedPercent('-7.5')).toBe('-7.5%')
  })

  it('does not put a plus on no change', () => {
    expect(formatSignedPercent('0')).toBe('0%')
    expect(formatSignedPercent('0.00')).toBe('0%')
  })
})

describe('isZeroAmount', () => {
  it('reads zero off the digits, however it is written', () => {
    expect(isZeroAmount('0')).toBe(true)
    expect(isZeroAmount('0.00')).toBe(true)
    // A minus sign in front of nothing is still nothing.
    expect(isZeroAmount('-0.00')).toBe(true)
  })

  it('is false as soon as any digit is non-zero', () => {
    expect(isZeroAmount('0.01')).toBe(false)
    expect(isZeroAmount('-0.01')).toBe(false)
    expect(isZeroAmount('150.00')).toBe(false)
  })
})

describe('directionOf', () => {
  it('names the direction in words', () => {
    expect(directionOf('-150.00')).toBe('decrease')
    expect(directionOf('150.00')).toBe('increase')
    expect(directionOf('0.00')).toBe('no change')
    expect(directionOf('-0.00')).toBe('no change')
  })
})
