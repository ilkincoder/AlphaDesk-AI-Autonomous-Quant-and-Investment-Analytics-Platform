/** The conversations *this browser* has started, and which one is open.
 *
 * Kept in `localStorage`, and the panel says so. The server owns every conversation; this
 * list owns only what has been started from here, so it cannot know about one begun in another
 * browser or another machine. Presenting it as "your conversations" would be a claim the client
 * cannot make -- and a reader who trusted it would conclude a conversation had been deleted
 * when it had only been opened somewhere else.
 *
 * Reopening one works because the server can restore any conversation whose id you have. That
 * is why an id is enough, and why nothing here needs a list endpoint.
 */

import type { StoredConversation } from '../conversations'
import { Button } from './Button'

export function ConversationHistory({
  open,
  onClose,
  conversations,
  activeId,
  onSelect,
  onNew,
  busy,
}: {
  open: boolean
  onClose: () => void
  conversations: StoredConversation[]
  activeId: string | null
  onSelect: (id: string) => void
  onNew: () => void
  busy: boolean
}) {
  if (!open) return null

  return (
    <aside
      aria-label="Conversation history"
      className="w-full shrink-0 border-b border-line bg-sidebar p-4 min-[900px]:w-[260px] min-[900px]:border-b-0 min-[900px]:border-r"
    >
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-heading font-medium text-ink">History</h2>
        <Button onClick={onClose} aria-label="Hide conversation history">
          Hide
        </Button>
      </div>

      <p className="mt-2 text-label text-faint">
        Conversations started in this browser. The server keeps them all; this list cannot see
        ones opened elsewhere.
      </p>

      <Button className="mt-3 w-full justify-center" onClick={onNew} disabled={busy}>
        New conversation
      </Button>

      <ul className="mt-3 space-y-1">
        {conversations.map((item) => {
          const isActive = item.id === activeId
          return (
            <li key={item.id}>
              <button
                type="button"
                aria-current={isActive ? 'true' : undefined}
                disabled={busy && !isActive}
                onClick={() => onSelect(item.id)}
                className={`w-full rounded-md px-3 py-2 text-left text-note transition-colors ${
                  isActive
                    ? 'bg-selected font-medium text-accent-ink'
                    : 'text-dim hover:bg-line/40 hover:text-ink'
                }`}
              >
                <span className="line-clamp-2">{item.title}</span>
                <span className="mt-0.5 block text-label text-faint">
                  {new Date(item.updatedAt).toLocaleString()}
                </span>
              </button>
            </li>
          )
        })}
        {conversations.length === 0 && (
          <li className="px-3 py-2 text-label text-faint">
            Nothing yet. Ask a question and it will appear here.
          </li>
        )}
      </ul>
    </aside>
  )
}
