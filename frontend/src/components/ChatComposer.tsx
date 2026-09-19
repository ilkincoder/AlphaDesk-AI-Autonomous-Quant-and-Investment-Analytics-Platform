/** The input, and the rules about when it may be sent.
 *
 * Four of them, and each exists because the alternative is a small daily annoyance:
 *
 * * **Enter sends, Shift+Enter starts a line.** A question about a company often wants a
 *   second line, and a textarea that sends on Enter with no escape is one people learn to
 *   distrust.
 * * **An IME composition is not a send.** Typing Japanese or Chinese fires `Enter` to *commit
 *   a candidate*, not to submit. Without the guard, choosing a character sends a half-written
 *   question -- and it costs a model call.
 * * **Whitespace is not a question.** The backend rejects it, so sending it only spends a
 *   round trip to be told so.
 * * **The draft survives a failure.** The text is owned by the caller, not copied into local
 *   state on send, so a request that fails leaves what was typed exactly where it was.
 */

import { useRef, useState } from 'react'

import { MAX_MESSAGE_LENGTH } from '../analysisApi'
import { Button } from './Button'

export function ChatComposer({
  draft,
  onDraftChange,
  onSend,
  disabled,
  busy,
}: {
  draft: string
  onDraftChange: (value: string) => void
  onSend: () => void
  /** True while this conversation has a turn running: a second submission would be refused. */
  disabled: boolean
  busy: boolean
}) {
  // `isComposing` on the event is not enough on every browser: some fire `keydown` with the
  // flag already cleared and then a final `compositionend`. Tracking it here covers both.
  const composing = useRef(false)
  const [touched, setTouched] = useState(false)

  const trimmed = draft.trim()
  const tooLong = draft.length > MAX_MESSAGE_LENGTH
  const canSend = trimmed.length > 0 && !tooLong && !disabled

  return (
    <form
      className="border-t border-line bg-page p-4"
      onSubmit={(event) => {
        event.preventDefault()
        if (canSend) onSend()
      }}
    >
      <label htmlFor="analysis-question" className="block text-label font-medium text-ink">
        Ask about a company
      </label>
      <p className="mt-0.5 text-label text-faint">
        Enter sends · Shift+Enter starts a new line
      </p>

      <textarea
        id="analysis-question"
        value={draft}
        onChange={(event) => onDraftChange(event.target.value)}
        onBlur={() => setTouched(true)}
        onCompositionStart={() => {
          composing.current = true
        }}
        onCompositionEnd={() => {
          composing.current = false
        }}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || event.shiftKey) return
          if (composing.current || event.nativeEvent.isComposing) return
          event.preventDefault()
          if (canSend) onSend()
        }}
        rows={3}
        // Past the backend's own bound rather than truncated at it: text vanishing as it is
        // typed is worse than being told it is too long.
        aria-invalid={tooLong || (touched && trimmed.length === 0)}
        aria-describedby="analysis-question-help"
        placeholder="Compare NVDA's price movement with insider activity."
        className="mt-2 w-full resize-y rounded-md border border-line bg-panel p-3 text-body text-ink placeholder:text-faint"
      />

      <div className="mt-2 flex items-center justify-between gap-3">
        <p
          id="analysis-question-help"
          role={tooLong ? 'alert' : undefined}
          className={`text-label ${tooLong ? 'text-danger' : 'text-faint'}`}
        >
          {tooLong
            ? `${draft.length} of ${MAX_MESSAGE_LENGTH} characters — this is too long to send.`
            : `${draft.length} of ${MAX_MESSAGE_LENGTH}`}
        </p>
        <Button type="submit" variant="primary" disabled={!canSend}>
          {busy ? 'Analyzing…' : 'Send'}
        </Button>
      </div>
    </form>
  )
}
