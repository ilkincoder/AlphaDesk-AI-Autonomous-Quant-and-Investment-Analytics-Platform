/** Shell navigation: what is reachable, what is not, and what the mobile drawer does
 *  with focus. */

import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { Sidebar } from './Sidebar'
import type { PageId } from './Sidebar'

function renderSidebar(current: PageId = 'portfolio', onNavigate = vi.fn()) {
  render(<Sidebar current={current} onNavigate={onNavigate} />)
  return onNavigate
}

/** The nav rows in DOM order. */
function navLabels() {
  const nav = screen.getByRole('navigation', { name: 'Main' })
  return within(nav)
    .getAllByRole('listitem')
    .map((item) => item.textContent)
}

describe('Sidebar', () => {
  it('lists every destination, with News & Trading directly below What if?', () => {
    renderSidebar()

    expect(navLabels()).toEqual([
      'Dashboard',
      'Analysis',
      'Portfolio',
      'What if?',
      'News & Trading',
      'Strategies',
      'Backtest',
      'Orders',
      'Watchlist',
      'Settings',
    ])
  })

  it('marks the current page and disables the six that have no page', () => {
    renderSidebar()

    const portfolio = screen.getByRole('button', { name: 'Portfolio' })
    expect(portfolio.getAttribute('aria-current')).toBe('page')
    expect(portfolio.hasAttribute('disabled')).toBe(false)

    // Ten destinations, four built. The six without a page behind them are genuinely
    // disabled rather than links that navigate nowhere.
    expect(screen.getAllByTitle('Coming soon')).toHaveLength(6)
    expect(screen.getByRole('button', { name: 'Dashboard' }).hasAttribute('disabled')).toBe(true)
  })

  it('enables News & Trading, and reports it as the chosen destination', () => {
    const onNavigate = renderSidebar('news')

    const news = screen.getByRole('button', { name: 'News & Trading' })
    expect(news.hasAttribute('disabled')).toBe(false)
    expect(news.getAttribute('aria-current')).toBe('page')

    fireEvent.click(news)
    expect(onNavigate).toHaveBeenCalledWith('news')
  })

  it('enables Analysis, and marks it current when it is the page', () => {
    renderSidebar('analysis')

    const analysis = screen.getByRole('button', { name: 'Analysis' })
    expect(analysis.hasAttribute('disabled')).toBe(false)
    expect(analysis.getAttribute('aria-current')).toBe('page')
    // Portfolio is enabled too; only one destination is current.
    expect(screen.getByRole('button', { name: 'Portfolio' }).getAttribute('aria-current')).toBeNull()
  })

  it('reports Analysis as the chosen destination', () => {
    const onNavigate = renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: 'Analysis' }))

    expect(onNavigate).toHaveBeenCalledWith('analysis')
  })

  it('highlights the current page, not merely an enabled one', () => {
    renderSidebar('what-if')

    expect(screen.getByRole('button', { name: 'What if?' }).getAttribute('aria-current')).toBe(
      'page',
    )
    // Every built destination is enabled; only one is current.
    expect(screen.getByRole('button', { name: 'Portfolio' }).getAttribute('aria-current')).toBeNull()
  })

  it('reports the destination that was chosen', () => {
    const onNavigate = renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: 'What if?' }))

    expect(onNavigate).toHaveBeenCalledWith('what-if')
  })

  it('does not navigate from a disabled destination', () => {
    const onNavigate = renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: 'Dashboard' }))

    expect(onNavigate).not.toHaveBeenCalled()
  })

  it('names the demo portfolio and its currency, and invents no user', () => {
    renderSidebar()

    expect(screen.getAllByText('Demo portfolio').length).toBeGreaterThan(0)
    expect(screen.getAllByText('USD').length).toBeGreaterThan(0)
  })

  it('keeps the drawer out of the DOM until it is opened', () => {
    renderSidebar()

    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('button', { name: 'Open navigation menu' })).toBeTruthy()
  })

  it('closes the drawer on Escape and gives focus back to the menu button', () => {
    renderSidebar()

    const menuButton = screen.getByRole('button', { name: 'Open navigation menu' })
    fireEvent.click(menuButton)

    const drawer = screen.getByRole('dialog')
    expect(within(drawer).getByRole('button', { name: 'Portfolio' })).toBeTruthy()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('dialog')).toBeNull()
    // Without this, a keyboard user would be dropped back at the top of the document.
    expect(document.activeElement).toBe(menuButton)
  })

  it('closes the drawer after a destination is chosen in it', () => {
    const onNavigate = renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: 'Open navigation menu' }))
    const drawer = screen.getByRole('dialog')
    fireEvent.click(within(drawer).getByRole('button', { name: 'What if?' }))

    expect(onNavigate).toHaveBeenCalledWith('what-if')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('closes the drawer from its own close button', () => {
    renderSidebar()

    fireEvent.click(screen.getByRole('button', { name: 'Open navigation menu' }))
    fireEvent.click(screen.getByRole('button', { name: 'Close navigation menu' }))

    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('does not steal focus on first render', () => {
    renderSidebar()

    // The focus-restoration effect must not fire on mount, or the page would open
    // with focus parked on the menu button.
    expect(document.activeElement).toBe(document.body)
  })
})
