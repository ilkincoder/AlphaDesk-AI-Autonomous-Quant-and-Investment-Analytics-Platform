/** The page: asking, restoring, retrying, and the mistakes that cost money.
 *
 * The retry tests are the ones worth having. Every other behaviour here degrades the page; a
 * client that regenerates a `request_id` after a timeout spends a second analysis on a question
 * that was already answered, and does it silently.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { aConversation, aHistory, aTurn, jsonResponse, on, routeFetch, sseResponse } from '../testing'
import { AnalysisPage } from './AnalysisPage'

/** What the page would have sent, parsed. */
function bodies(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.body)
    .map(([, init]) => JSON.parse((init as RequestInit).body as string))
}

function sseDone(turn: ReturnType<typeof aTurn>) {
  return sseResponse([
    { event: 'routing', destination: 'module1_analysis', symbol: 'NVDA' },
    { event: 'composing' },
    { event: 'done', turn },
  ])
}

async function renderPage() {
  render(<AnalysisPage />)
  await waitFor(() => expect(screen.getByLabelText('Ask about a company')).toBeDefined())
}

async function ask(question: string) {
  fireEvent.change(screen.getByLabelText('Ask about a company'), {
    target: { value: question },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Send' }))
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('AnalysisPage — starting and restoring', () => {
  it('creates a conversation on first load, and no more than one', async () => {
    const fetchMock = routeFetch([
      on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
      on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
    ])
    vi.stubGlobal('fetch', fetchMock)

    await renderPage()

    const creates = fetchMock.mock.calls.filter(
      ([url, init]) =>
        String(url).includes('/analysis/conversations') &&
        (init as RequestInit | undefined)?.method === 'POST',
    )
    // StrictMode runs the boot effect twice in development; the in-flight guard is what stops
    // one page load creating two conversations.
    expect(creates).toHaveLength(1)
    expect(window.localStorage.getItem('alphadesk.analysis.activeConversation')).toBe('conv-1')
  })

  it('restores the stored conversation instead of creating one', async () => {
    window.localStorage.setItem('alphadesk.analysis.activeConversation', 'conv-7')
    const fetchMock = routeFetch([
      on('GET', '/analysis/conversations/conv-7', () =>
        jsonResponse(
          aHistory([aTurn({ user_message: 'An earlier question.' })], {
            conversation_id: 'conv-7',
          }),
        ),
      ),
    ])
    vi.stubGlobal('fetch', fetchMock)

    await renderPage()

    expect(await screen.findByText('An earlier question.')).toBeDefined()
    const creates = fetchMock.mock.calls.filter(
      ([, init]) => (init as RequestInit | undefined)?.method === 'POST',
    )
    expect(creates).toHaveLength(0)
  })

  it('restores the answer with its citations and limitations, not just its text', async () => {
    window.localStorage.setItem('alphadesk.analysis.activeConversation', 'conv-7')
    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('GET', '/analysis/conversations/conv-7', () =>
          jsonResponse(
            aHistory(
              [aTurn({ answer: 'It rose [E1].', limitations: ['insufficient_coverage'] })],
              { conversation_id: 'conv-7' },
            ),
          ),
        ),
      ]),
    )

    await renderPage()

    expect(await screen.findByText(/Sources and evidence/)).toBeDefined()
    expect(screen.getByText('insufficient_coverage')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Show evidence E1' })).toBeDefined()
  })

  it('offers a new conversation when the stored one is gone', async () => {
    window.localStorage.setItem('alphadesk.analysis.activeConversation', 'conv-gone')
    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('GET', '/analysis/conversations/conv-gone', () => jsonResponse({ detail: 'no conversation' }, 404)),
      ]),
    )

    render(<AnalysisPage />)

    // No composer here: a conversation that is not on the server cannot be asked in.
    expect(await screen.findByText(/not on the server/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeDefined()
    expect(screen.queryByLabelText('Ask about a company')).toBeNull()
  })
})

describe('AnalysisPage — asking', () => {
  it('shows the question immediately and then the server’s answer', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
        on('GET', '/analysis/conversations/conv-1', () =>
          jsonResponse(aHistory([aTurn({ user_message: 'How is NVDA doing?' })])),
        ),
        on('POST', '/analysis/chat/stream', () => sseDone(aTurn())),
      ]),
    )

    await renderPage()
    await ask('How is NVDA doing?')

    expect(await screen.findByText(/NVDA rose slightly/)).toBeDefined()
  })

  it('reports what the run is doing, from the events it actually sent', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
        on('GET', '/analysis/conversations/conv-1', () =>
          jsonResponse(aHistory([aTurn({ user_message: 'How is NVDA doing?' })])),
        ),
        on('POST', '/analysis/chat/stream', () =>
          sseResponse([
            { event: 'routing', destination: 'module1_analysis', symbol: 'NVDA' },
            { event: 'tool', tool: 'market_insider_analysis', status: 'ok' },
            { event: 'composing' },
            { event: 'done', turn: aTurn() },
          ]),
        ),
      ]),
    )

    await renderPage()
    await ask('How is NVDA doing?')

    // Finished by now, so the progress is gone and the answer is present -- but the tool name
    // survives in the answer's own details, which is where a reader checks it.
    const details = await screen.findByText('Tool details and usage')
    expect(within(details.closest('details') as HTMLElement).getByText(/market_insider_analysis/))
      .toBeDefined()
  })

  it('clears the composer once the question is answered', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
        on('GET', '/analysis/conversations/conv-1', () =>
          jsonResponse(aHistory([aTurn({ user_message: 'How is NVDA doing?' })])),
        ),
        on('POST', '/analysis/chat/stream', () => sseDone(aTurn())),
      ]),
    )

    await renderPage()
    await ask('How is NVDA doing?')
    await screen.findByText(/NVDA rose slightly/)

    expect((screen.getByLabelText('Ask about a company') as HTMLTextAreaElement).value).toBe('')
  })

  it('sends a clarification reply to the same conversation, with a new request id', async () => {
    const asked = aTurn({
      turn_id: 'turn-1',
      request_id: 'req-1',
      status: 'clarification_needed',
      answer: 'Which period should I look at?',
      resolved: null,
    })
    const answered = aTurn({
      turn_id: 'turn-2',
      request_id: 'req-2',
      sequence: 2,
      user_message: 'August 6 through September 17, 2026.',
      answer: 'Over that window NVDA rose slightly [E1].',
    })
    const fetchMock = routeFetch([
      on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
      on('GET', '/analysis/conversations/conv-1', () =>
        jsonResponse(aHistory(answered.request_id === 'req-2' ? [asked, answered] : [asked])),
      ),
      on('POST', '/analysis/chat/stream', () => sseDone(answered)),
    ])
    vi.stubGlobal('fetch', fetchMock)
    window.localStorage.setItem(
      'alphadesk.analysis.conversations',
      JSON.stringify([{ id: 'conv-1', title: 'x', updatedAt: '2026-09-19T00:00:00Z' }]),
    )

    await renderPage()
    await ask('Compare NVDA price movement and insider activity.')
    expect(await screen.findByText('Which period should I look at?')).toBeDefined()

    await ask('August 6 through September 17, 2026.')
    expect(await screen.findByText(/Over that window/)).toBeDefined()

    const sent = bodies(fetchMock).filter((body) => 'message' in body)
    expect(sent).toHaveLength(2)
    // Both to the same conversation, with different ids -- the second is a new question.
    expect(sent[0].conversation_id).toBe('conv-1')
    expect(sent[1].conversation_id).toBe('conv-1')
    expect(sent[0].request_id).not.toBe(sent[1].request_id)
  })

  it('discards an answer that arrives after the page moved to another conversation', async () => {
    let release!: () => void
    const held = new Promise<void>((resolve) => {
      release = resolve
    })

    const fetchMock = routeFetch([
      on('POST', '/analysis/conversations', () => jsonResponse(aConversation('conv-new'), 201)),
      on('GET', '/analysis/conversations/conv-1', () =>
        jsonResponse(aHistory([aTurn({ user_message: 'The first conversation.' })])),
      ),
      on('GET', '/analysis/conversations/conv-new', () => jsonResponse(aHistory([]))),
      on('POST', '/analysis/chat/stream', async () => {
        await held
        return sseDone(aTurn({ answer: 'AN ANSWER FROM THE OLD CONVERSATION' }))
      }),
    ])
    vi.stubGlobal('fetch', fetchMock)
    window.localStorage.setItem('alphadesk.analysis.activeConversation', 'conv-1')
    window.localStorage.setItem(
      'alphadesk.analysis.conversations',
      JSON.stringify([
        { id: 'conv-1', title: 'first', updatedAt: '2026-09-19T00:00:00Z' },
        { id: 'conv-new', title: 'new', updatedAt: '2026-09-19T00:00:00Z' },
      ]),
    )

    await renderPage()
    await screen.findByText('The first conversation.')
    await ask('A question in the old conversation.')

    // Move on before the answer arrives.
    fireEvent.click(screen.getByRole('button', { name: 'History' }))
    fireEvent.click(screen.getByRole('button', { name: /new/ }))

    release?.()

    await waitFor(() => expect(screen.queryByText(/AN ANSWER FROM THE OLD/)).toBeNull())
  })
})

describe('AnalysisPage — failures and retries', () => {
  it('keeps the question in the box when the request fails', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
        on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
        on('POST', '/analysis/chat/stream', () => {
          throw new TypeError('network down')
        }),
      ]),
    )

    await renderPage()
    await ask('A question that will fail.')

    await waitFor(() =>
      expect((screen.getByLabelText('Ask about a company') as HTMLTextAreaElement).value).toBe(
        'A question that will fail.',
      ),
    )
  })

  it('does not send a second request on its own after a failure', async () => {
    const fetchMock = routeFetch([
      on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
      on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
      on('POST', '/analysis/chat/stream', () => {
        throw new TypeError('network down')
      }),
    ])
    vi.stubGlobal('fetch', fetchMock)

    await renderPage()
    await ask('A question that will fail.')

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]) => String(url).includes('/analysis/chat/stream')),
      ).toHaveLength(1),
    )
  })

  it('retries with the identical request id and payload, not a new one', async () => {
    const fetchMock = routeFetch([
      on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
      on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
      on('POST', '/analysis/chat/stream', () => {
        throw new TypeError('network down')
      }),
    ])
    vi.stubGlobal('fetch', fetchMock)

    await renderPage()
    await ask('A question that will fail.')
    await screen.findByText('Send that request again')

    fireEvent.click(screen.getByRole('button', { name: 'Send that request again' }))

    const streams = bodies(fetchMock).filter((body) => 'message' in body)
    expect(streams).toHaveLength(2)
    // The whole point: a timeout does not prove the analysis failed, so the retry must be the
    // same request -- not a second question that happens to read the same.
    expect(streams[1].request_id).toBe(streams[0].request_id)
    expect(streams[1]).toEqual(streams[0])
  })

  it('offers a new request only as an explicit second choice', async () => {
    const fetchMock = routeFetch([
      on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
      on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
      on('POST', '/analysis/chat/stream', () => {
        throw new TypeError('network down')
      }),
    ])
    vi.stubGlobal('fetch', fetchMock)

    await renderPage()
    await ask('A question that will fail.')
    await screen.findByText('Ask as a new request')

    fireEvent.click(screen.getByRole('button', { name: 'Ask as a new request' }))

    const streams = bodies(fetchMock).filter((body) => 'message' in body)
    expect(streams[1].request_id).not.toBe(streams[0].request_id)
  })

  it('reconciles an interrupted submission on reload instead of sending it again', async () => {
    window.localStorage.setItem('alphadesk.analysis.activeConversation', 'conv-1')
    window.localStorage.setItem(
      'alphadesk.analysis.pending',
      JSON.stringify({ conversationId: 'conv-1', requestId: 'req-inflight', message: 'Half sent.' }),
    )
    // The server has no such turn: the submission never reached it.
    const fetchMock = routeFetch([
      on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
    ])
    vi.stubGlobal('fetch', fetchMock)

    await renderPage()

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]) => String(url).includes('/analysis/chat')),
      ).toHaveLength(0),
    )
  })

  it('shows a 409 as a readable conflict and keeps the question', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
        on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
        on('POST', '/analysis/chat/stream', () =>
          jsonResponse({ detail: 'another turn in this conversation is still running' }, 409),
        ),
      ]),
    )

    await renderPage()
    await ask('A question while another is running.')

    expect(await screen.findByText(/still running/)).toBeDefined()
    expect((screen.getByLabelText('Ask about a company') as HTMLTextAreaElement).value).toBe(
      'A question while another is running.',
    )
  })

  it('shows a 422 using the backend’s own words', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
        on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
        on('POST', '/analysis/chat/stream', () =>
          jsonResponse({ detail: 'message: String should have at most 2000 characters' }, 422),
        ),
      ]),
    )

    await renderPage()
    await ask('A question.')

    expect(await screen.findByText(/at most 2000 characters/)).toBeDefined()
  })

  it('polls rather than re-asking when the server says it is still running', async () => {
    const done = aTurn({ request_id: 'req-1', status: 'completed' })
    const fetchMock = routeFetch([
      on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
      on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
      on('POST', '/analysis/chat/stream', () =>
        jsonResponse({ status: 'processing', retry_after_seconds: 15 }, 202),
      ),
    ])
    // After the 202, the poll starts asking for the conversation; give it the finished turn.
    let polls = 0
    const inner = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (url: RequestInfo | URL, init?: RequestInit) => {
      if (String(url).includes('/analysis/conversations/conv-1') && polls++ > 0) {
        return jsonResponse(aHistory([done]))
      }
      return inner(url, init)
    })
    vi.stubGlobal('fetch', fetchMock)

    await renderPage()
    await ask('A slow question.')

    expect(await screen.findByText(/Still running on the server/)).toBeDefined()
    // No second submission was made.
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]) => String(url).includes('/analysis/chat/stream')),
      ).toHaveLength(1),
    )
  })
})

describe('AnalysisPage — history', () => {
  it('pages older messages in front without duplicating any', async () => {
    // A conversation longer than one page: 25 turns, 20 per page. The API pages oldest-first,
    // so opening on offset 0 would show turns 1..20 and the newest answer would be missing.
    const turn = (n: number) =>
      aTurn({
        turn_id: `turn-${n}`,
        sequence: n,
        request_id: `r${n}`,
        user_message: `Question ${n}.`,
      })
    const oldestPage = Array.from({ length: 20 }, (_, i) => turn(i + 1))
    const newestPage = Array.from({ length: 20 }, (_, i) => turn(i + 6))

    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('GET', '/analysis/conversations/conv-1', (url) =>
          url.includes('offset=5')
            ? jsonResponse(aHistory(newestPage, { total_turns: 25, offset: 5 }))
            : jsonResponse(aHistory(oldestPage, { total_turns: 25, offset: 0 })),
        ),
      ]),
    )
    window.localStorage.setItem('alphadesk.analysis.activeConversation', 'conv-1')

    await renderPage()

    // It opened on the newest page, not the oldest.
    expect(await screen.findByText('Question 25.')).toBeDefined()
    expect(screen.queryByText('Question 1.')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Load earlier messages' }))

    expect(await screen.findByText('Question 1.')).toBeDefined()

    // In order, and each question exactly once: turn 6-20 arrived twice and must not appear
    // twice.
    const questions = screen
      .getAllByText(/^Question \d+\.$/)
      .map((el) => el.textContent as string)
    expect(questions).toEqual(
      Array.from({ length: 25 }, (_, i) => `Question ${i + 1}.`),
    )
  })

  it('starts a fresh conversation without touching the stored list', async () => {
    const fetchMock = routeFetch([
      on('POST', '/analysis/conversations', () => jsonResponse(aConversation('conv-2'), 201)),
      on('GET', '/analysis/conversations/conv-1', () =>
        jsonResponse(aHistory([aTurn({ user_message: 'The first conversation.' })])),
      ),
      on('GET', '/analysis/conversations/conv-2', () => jsonResponse(aHistory([]))),
    ])
    vi.stubGlobal('fetch', fetchMock)
    window.localStorage.setItem('alphadesk.analysis.activeConversation', 'conv-1')
    window.localStorage.setItem(
      'alphadesk.analysis.conversations',
      JSON.stringify([{ id: 'conv-1', title: 'first', updatedAt: '2026-09-19T00:00:00Z' }]),
    )

    await renderPage()
    await screen.findByText('The first conversation.')

    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }))

    await waitFor(() =>
      expect(window.localStorage.getItem('alphadesk.analysis.activeConversation')).toBe('conv-2'),
    )
    // Both are still remembered: nothing on the server was deleted.
    const stored = JSON.parse(
      window.localStorage.getItem('alphadesk.analysis.conversations') ?? '[]',
    ) as { id: string }[]
    expect(stored.map((item) => item.id)).toEqual(['conv-2', 'conv-1'])
  })

  it('says the history is this browser’s, not the server’s', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch([
        on('POST', '/analysis/conversations', () => jsonResponse(aConversation(), 201)),
        on('GET', '/analysis/conversations/conv-1', () => jsonResponse(aHistory([]))),
      ]),
    )

    await renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'History' }))

    expect(screen.getByText(/cannot see ones opened elsewhere/)).toBeDefined()
  })
})
