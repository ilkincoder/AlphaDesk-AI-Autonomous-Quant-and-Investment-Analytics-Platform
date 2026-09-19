/** This browser's conversations, remembered across a reload.
 *
 * The **server** owns every conversation; this file owns only which ones this browser has
 * started and which is open. That split matters. A list kept here can be lost by clearing site
 * data, and it can never know about a conversation started somewhere else -- so the panel that
 * reads it says so, rather than presenting itself as the list of conversations that exist.
 *
 * Every read and write is guarded. `localStorage` throws in a private window with site data
 * blocked, and an unguarded call at module load or in an effect would take the page down with
 * it: a chat that cannot remember where it was should still be able to chat.
 */

export type StoredConversation = {
  id: string
  title: string
  updatedAt: string
}

const ACTIVE_KEY = 'alphadesk.analysis.activeConversation'
const LIST_KEY = 'alphadesk.analysis.conversations'

/** How many conversations are remembered. Enough to be useful, bounded so a long-lived
 *  browser cannot grow this without limit. */
export const MAX_STORED_CONVERSATIONS = 20

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, value)
  } catch {
    // Storage is unavailable or full. The page keeps working; it just will not remember.
  }
}

export function activeConversationId(): string | null {
  return read(ACTIVE_KEY)
}

export function setActiveConversationId(id: string | null): void {
  write(ACTIVE_KEY, id)
}

export function listConversations(): StoredConversation[] {
  const raw = read(LIST_KEY)
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (item): item is StoredConversation =>
        typeof item === 'object' &&
        item !== null &&
        typeof (item as StoredConversation).id === 'string',
    )
  } catch {
    // Corrupt or written by an older version. An empty list is better than a crash.
    return []
  }
}

/** Remember a conversation, most recent first, and make it the active one. */
export function rememberConversation(id: string, title: string): StoredConversation[] {
  const now = new Date().toISOString()
  const others = listConversations().filter((item) => item.id !== id)
  const next = [{ id, title: title.slice(0, 120), updatedAt: now }, ...others].slice(
    0,
    MAX_STORED_CONVERSATIONS,
  )
  write(LIST_KEY, JSON.stringify(next))
  setActiveConversationId(id)
  return next
}

const PENDING_KEY = 'alphadesk.analysis.pending'

/** A submission whose outcome is not yet known.
 *
 * Written **before** the request is sent and cleared only once the server has answered, so a
 * browser that is closed, reloaded or killed mid-request can still reconcile. The `requestId`
 * is the whole point: reconciling means asking the server about *that* request, and retrying
 * means sending *that* request again. Generating a fresh id after a timeout would turn one
 * question into two paid analyses.
 */
export type PendingSubmission = {
  conversationId: string
  requestId: string
  message: string
}

export function pendingSubmission(): PendingSubmission | null {
  const raw = read(PENDING_KEY)
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      typeof (parsed as PendingSubmission).conversationId === 'string' &&
      typeof (parsed as PendingSubmission).requestId === 'string' &&
      typeof (parsed as PendingSubmission).message === 'string'
    ) {
      return parsed as PendingSubmission
    }
  } catch {
    // Corrupt. Treated as nothing pending, which is the recoverable direction.
  }
  return null
}

export function setPendingSubmission(value: PendingSubmission | null): void {
  write(PENDING_KEY, value === null ? null : JSON.stringify(value))
}

/** The first line of a message, for the history list. */
export function titleFor(message: string): string {
  const line = message.trim().split('\n', 1)[0] ?? ''
  return line.length > 0 ? line : 'New conversation'
}
