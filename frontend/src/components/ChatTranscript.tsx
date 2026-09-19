/** The messages, the empty state, and the two things that keep reading comfortable.
 *
 * **Auto-scroll only when the reader is already at the bottom.** Jumping to the newest message
 * while somebody is reading the middle of a long answer is the behaviour that makes a chat page
 * feel hostile. If they have scrolled up, a button offers the move instead of forcing it.
 *
 * **One announcement per answer.** A live region that re-read the whole transcript every time
 * a turn arrived would be unusable with a screen reader; the region says only that an answer
 * has arrived.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import type { Turn } from '../analysisApi'
import { AssistantAnswer } from './AssistantAnswer'
import { Card } from './Card'

/** How close to the bottom counts as "at the bottom", in pixels. */
const NEAR_BOTTOM = 80

function UserMessage({ children }: { children: string }) {
  return (
    <div className="flex justify-end">
      {/* Restrained purple, and no avatar or name: the backend has no notion of who is asking,
          and inventing an identity for the reader would be a claim the system cannot make. */}
      <p className="max-w-[80%] whitespace-pre-wrap break-words rounded-card rounded-br-sm border border-accent/40 bg-selected px-4 py-2.5 text-body text-ink">
        {children}
      </p>
    </div>
  )
}

function EmptyState() {
  return (
    <Card>
      <p className="text-heading font-medium text-ink">Ask about a company</p>
      <p className="mt-2 max-w-[70ch] text-note text-dim">
        Questions are answered from what this system has stored: reported insider transactions,
        daily prices, financial figures from filings, the text of SEC filings, and your demo
        portfolio&apos;s holdings.
      </p>
      <p className="mt-2 max-w-[70ch] text-note text-faint">
        It does not trade, approve orders, or backtest — those are later work. What it can
        answer depends on which companies have been ingested and imported here, so a question
        about a company with no stored data will say so rather than answer from memory. There is
        no live market feed.
      </p>
    </Card>
  )
}

export function ChatTranscript({
  turns,
  pendingQuestion,
  busy,
  progress,
  statusNote,
  onCheckStatus,
  showCheckStatus,
}: {
  turns: Turn[]
  /** The message being sent, shown before the server has answered. */
  pendingQuestion: string | null
  busy: boolean
  /** What the run has reported so far, in order. Each is something that happened. */
  progress: string[]
  statusNote: string | null
  onCheckStatus: () => void
  showCheckStatus: boolean
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const [atBottom, setAtBottom] = useState(true)

  const scrollToBottom = useCallback(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight })
    setAtBottom(true)
  }, [])

  // Keep the reader in place unless they were already following along.
  useEffect(() => {
    if (atBottom) scrollToBottom()
  }, [turns.length, pendingQuestion, progress.length, atBottom, scrollToBottom])

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scroller}
        onScroll={(event) => {
          const el = event.currentTarget
          setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM)
        }}
        className="h-full space-y-4 overflow-y-auto p-6"
      >
        {turns.length === 0 && !pendingQuestion ? <EmptyState /> : null}

        {turns.map((turn) => (
          <div key={turn.turn_id} className="space-y-4">
            <UserMessage>{turn.user_message ?? turn.question ?? ''}</UserMessage>
            <AssistantAnswer turn={turn} />
          </div>
        ))}

        {pendingQuestion && (
          <>
            <UserMessage>{pendingQuestion}</UserMessage>
            <Card>
              {/* "Analyzing…" and nothing else invented. What follows are events the backend
                  actually emitted, in the order it emitted them. */}
              <p className="text-body text-dim" aria-live="polite">
                {busy ? 'Analyzing…' : 'Waiting…'}
              </p>
              {progress.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {progress.map((line, index) => (
                    <li key={`${line}-${index}`} className="text-label text-faint">
                      {line}
                    </li>
                  ))}
                </ul>
              )}
              {statusNote && <p className="mt-2 text-label text-dim">{statusNote}</p>}
              {showCheckStatus && (
                <button
                  type="button"
                  onClick={onCheckStatus}
                  className="mt-2 text-label text-accent-ink underline underline-offset-2"
                >
                  Check request status
                </button>
              )}
            </Card>
          </>
        )}
      </div>

      {!atBottom && (
        <button
          type="button"
          onClick={scrollToBottom}
          className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-md border border-line bg-panel px-3 py-1.5 text-label text-ink shadow-card"
        >
          New message
        </button>
      )}
    </div>
  )
}
