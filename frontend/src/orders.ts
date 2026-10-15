/** What a batch and its orders say to a reader, decided once for the whole application.
 *
 * Two screens now report the same execution: the Orders page, which is where a batch is read in
 * full, and the News & Trading page, which reports the batch an approval just opened without
 * leaving the news. A second copy of this vocabulary is how one status comes to be called two
 * things on two screens -- and these are the sentences a reader uses to decide whether their
 * money moved, so the two must not drift.
 *
 * Nothing here reads the wire values as anything but what they are. `partial` and `halted` are
 * not failures of the application: they mean the batch stopped, and why is in the batch's own
 * notes. Every label is plain language, and none of them is a colour on its own.
 */

import type { ExecutionBatch, OrderIntent } from './api'

/** How often a page re-reads while a batch is still working.
 *
 * The execution itself does not depend on this: the server follows the batch on its own thread
 * whether or not a page is open. This only decides how quickly a screen catches up.
 */
export const POLL_INTERVAL_MS = 4000

/** The statuses a batch stops at.
 *
 * From `backend/app/orders.py`, which is where the worker decides them. A batch that is `pending`
 * or `submitting` is still being worked on by a thread this application does not control, so a
 * screen waiting on one is waiting on something real.
 */
const SETTLED_BATCH_STATUSES = ['completed', 'partial', 'halted']

/** True when nothing further will happen to this batch without a person doing something. */
export function isSettled(batch: ExecutionBatch): boolean {
  return SETTLED_BATCH_STATUSES.includes(batch.status)
}

/** What a batch's status says to a reader. */
export function batchLabel(batch: ExecutionBatch): string {
  switch (batch.status) {
    case 'completed':
      return 'Completed'
    case 'partial':
      return 'Partly completed'
    case 'halted':
      return 'Stopped'
    case 'submitting':
      return 'Working'
    default:
      return 'Waiting'
  }
}

export function batchColour(batch: ExecutionBatch): string {
  if (batch.status === 'completed') return 'text-positive'
  if (batch.status === 'halted' || batch.status === 'partial') return 'text-danger'
  return 'text-accent-ink'
}

/** What one order's status says, including the two that mean "not known".
 *
 * `unknown` is an unanswered submission and `not_found` is one the broker has no record of.
 * Both are shown as what they are rather than as a failure, because an order that may exist
 * must not be reported as one that did not happen.
 */
export function intentLabel(intent: OrderIntent): string {
  switch (intent.status) {
    case 'filled':
      return 'Filled'
    case 'partially_filled':
      return 'Partly filled'
    case 'accepted':
      return 'Accepted, not yet filled'
    case 'submitting':
      return 'Being submitted'
    case 'planned':
      return 'Not sent yet'
    case 'rejected':
      return 'Rejected by the broker'
    case 'canceled':
      return 'Canceled'
    case 'expired':
      return 'Expired unfilled'
    case 'halted':
      return 'Stopped, never sent'
    case 'unknown':
      return 'Unanswered — outcome not known'
    case 'not_found':
      return 'Never confirmed by the broker'
    default:
      return intent.status
  }
}

export function intentColour(intent: OrderIntent): string {
  if (intent.status === 'filled') return 'text-positive'
  if (['rejected', 'canceled', 'expired', 'halted'].includes(intent.status)) return 'text-danger'
  if (['unknown', 'not_found'].includes(intent.status)) return 'text-danger'
  return 'text-accent-ink'
}
