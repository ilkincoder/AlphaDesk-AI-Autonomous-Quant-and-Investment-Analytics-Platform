/** The composer's four rules, each of which is a way a send button annoys somebody. */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { MAX_MESSAGE_LENGTH } from '../analysisApi'
import { ChatComposer } from './ChatComposer'

function renderComposer(overrides: Partial<Parameters<typeof ChatComposer>[0]> = {}) {
  const props = {
    draft: '',
    onDraftChange: vi.fn(),
    onSend: vi.fn(),
    disabled: false,
    busy: false,
    ...overrides,
  }
  render(<ChatComposer {...props} />)
  return props
}

function textarea() {
  return screen.getByLabelText('Ask about a company') as HTMLTextAreaElement
}

describe('ChatComposer', () => {
  it('sends on Enter', () => {
    const props = renderComposer({ draft: 'How is NVDA doing?' })

    fireEvent.keyDown(textarea(), { key: 'Enter' })

    expect(props.onSend).toHaveBeenCalled()
  })

  it('starts a new line on Shift+Enter rather than sending', () => {
    const props = renderComposer({ draft: 'A question' })

    fireEvent.keyDown(textarea(), { key: 'Enter', shiftKey: true })

    expect(props.onSend).not.toHaveBeenCalled()
  })

  it('does not send while an IME composition is in progress', () => {
    // Enter commits a candidate in Japanese and Chinese input. Sending there would submit a
    // half-written question -- and spend a model call on it.
    const props = renderComposer({ draft: 'こんに' })

    fireEvent.compositionStart(textarea())
    fireEvent.keyDown(textarea(), { key: 'Enter' })

    expect(props.onSend).not.toHaveBeenCalled()

    fireEvent.compositionEnd(textarea())
    fireEvent.keyDown(textarea(), { key: 'Enter' })
    expect(props.onSend).toHaveBeenCalled()
  })

  it('does not send an empty or whitespace-only question', () => {
    for (const draft of ['', '   ', '\n\t ']) {
      // Cleaned up between iterations: Testing Library clears the document after each *test*,
      // and this loop renders more than one.
      cleanup()
      const props = renderComposer({ draft })

      fireEvent.keyDown(textarea(), { key: 'Enter' })

      expect(props.onSend).not.toHaveBeenCalled()
      expect(
        (screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled,
      ).toBe(true)
    }
  })

  it('refuses a message past the backend’s own limit, and says so', () => {
    const props = renderComposer({ draft: 'x'.repeat(MAX_MESSAGE_LENGTH + 1) })

    fireEvent.keyDown(textarea(), { key: 'Enter' })

    expect(props.onSend).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent).toMatch(/too long/)
  })

  it('allows a message exactly at the limit', () => {
    const props = renderComposer({ draft: 'x'.repeat(MAX_MESSAGE_LENGTH) })

    fireEvent.keyDown(textarea(), { key: 'Enter' })

    expect(props.onSend).toHaveBeenCalled()
  })

  it('submits from the button as well', () => {
    const props = renderComposer({ draft: 'A question' })

    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    expect(props.onSend).toHaveBeenCalled()
  })

  it('is disabled while a turn is running, and says what it is doing', () => {
    renderComposer({ draft: 'A question', disabled: true, busy: true })

    const button = screen.getByRole('button', { name: 'Analyzing…' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })

  it('reports every keystroke to the caller, so the draft is never held here', () => {
    const props = renderComposer()

    fireEvent.change(textarea(), { target: { value: 'half a question' } })

    expect(props.onDraftChange).toHaveBeenCalledWith('half a question')
  })
})
