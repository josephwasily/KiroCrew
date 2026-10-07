/**
 * The ONE "mate greeting on open" seam of the Crewmates page: what a crewmate
 * says when the user opens its chat, picked once per open.
 *
 * Today it has one kind, `warm`: the user comes back while the crewmate is in
 * the middle of a goal, and the chat opens on where that goal stands (what
 * finished, what is in progress, what needs a look) and the next step.
 * A cold-start welcome is a second kind of the same union, picked in the same
 * hook, so the page never shows two greetings for one open.
 *
 * Built from the crewmate's own work ledger (`GET /api/crew-board`, the masked
 * read the Crew board already uses, through the same query key), never from a
 * model call: the status is already recorded, so reading it is cheap and says
 * the same thing every time.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import { crewBoardQueryKey, type WorkBoardItem, type WorkBoardResponse } from '../../api/crewBoard'
import { retryPolicy } from '../../api/queryClient'
import { partitionBoardRows } from '../crewBoardRows'

/** Why an open item needs a look, most urgent first. */
export type ResumeReason = 'question' | 'blocked' | 'quiet' | 'done'

export interface ResumeItem {
  title: string
  reason: ResumeReason
  /** For a question: the worker's own last report, which is where it asks. */
  detail?: string
}

export interface MateResume {
  goal: string
  /** Closed items: the Crew board's Finished band. */
  finished: number
  /** Open items that need no look, split by whether a worker is running them.
   *  Every open item is in `running`, `idle` or `attention`. */
  running: number
  idle: number
  /** Open items that need a look, most urgent first. */
  attention: ResumeItem[]
  /** The next step, picked from the most urgent item (`attention[0]`). */
  next:
    | { kind: 'attention'; item: ResumeItem }
    | { kind: 'wait' }
    | { kind: 'continue'; title: string }
  /** What the card says, as one string: a return to the same card soon after
   *  it was shown says nothing new, so it is not shown again. */
  fingerprint: string
}

export type MateGreeting = { kind: 'warm'; slot: string; resume: MateResume }

const REASON_RANK: Record<ResumeReason, number> = { question: 0, blocked: 1, quiet: 2, done: 3 }

/** Precedence follows the board's own row label (`rowKindLabelKey`): an
 *  orphaned item has no one left to answer it, so it reads as gone quiet even
 *  while it carries a question. */
function reasonOf(item: WorkBoardItem): ResumeReason | null {
  if (item.orphaned) return 'quiet'
  if (item.outstanding) return 'question'
  if (item.stale) return 'quiet'
  if (item.status === 'blocked') return 'blocked'
  if (item.status === 'done') return 'done'
  return null
}

/** The warm greeting for a board, or null when no goal is in flight: a board
 *  whose every item is closed is a goal that ended, not one to resume. */
export function summarizeResume(board: WorkBoardResponse): MateResume | null {
  const { ruling, working, finished } = partitionBoardRows(board.items)
  const open = [...ruling, ...working]
  if (open.length === 0) return null
  const attention: ResumeItem[] = []
  let running = 0
  let idle = 0
  for (const item of open) {
    const reason = reasonOf(item)
    if (reason === 'question' && item.summary) attention.push({ title: item.title, reason, detail: item.summary })
    else if (reason) attention.push({ title: item.title, reason })
    else if (item.alive === 'running') running += 1
    else idle += 1
  }
  attention.sort((a, b) => REASON_RANK[a.reason] - REASON_RANK[b.reason])
  // Nothing needs a look: wait only when every open item has a worker running;
  // an idle one is work nobody is doing, so the step is to pick it up.
  const idleItem = open.find((i) => !reasonOf(i) && i.alive !== 'running')
  const next: MateResume['next'] = attention.length
    ? { kind: 'attention', item: attention[0] }
    : idleItem
      ? { kind: 'continue', title: idleItem.title }
      : { kind: 'wait' }
  const said = { goal: board.conductor.goal, finished: finished.length, running, idle, attention, next }
  // Built from what the card shows rather than from the item fields, so any
  // change a reader would see (a worker dying, an item going idle) is a change.
  return { ...said, fingerprint: JSON.stringify(said) }
}

/** How long a shown, unchanged status stays quiet on the next open. Long
 *  enough that hopping between crewmates, or a reconnect re-confirming the
 *  thread, does not bring back a card the user just dismissed; short enough
 *  that coming back after a break shows it again. */
export const RESUME_REPEAT_MS = 15 * 60_000

const SEEN_PREFIX = 'kc-mate-resume-seen-'

function seenRecently(slot: string, fingerprint: string, now: number): boolean {
  try {
    const raw = sessionStorage.getItem(SEEN_PREFIX + slot)
    if (!raw) return false
    const seen = JSON.parse(raw) as { fp?: unknown; at?: unknown }
    return seen.fp === fingerprint && typeof seen.at === 'number' && now - seen.at < RESUME_REPEAT_MS
  } catch {
    return false
  }
}

function markSeen(slot: string, fingerprint: string, now: number): void {
  try {
    sessionStorage.setItem(SEEN_PREFIX + slot, JSON.stringify({ fp: fingerprint, at: now }))
  } catch {
    // Storage full or blocked: the greeting may repeat on the next open, which
    // is the harmless side.
  }
}

/** A read that failed for a reason other than "this crewmate has no ledger". */
export interface MateGreetingFailure {
  slot: string
  error: unknown
}

/** A 404 is the crewmate that never ran a goal: an answer, not a failure, so it
 *  is not retried. Anything else takes the dashboard's shared retry ladder. */
const retryUnlessNoLedger = (failureCount: number, error: unknown): boolean =>
  !isNotFoundError(error) && retryPolicy(failureCount, error)

/**
 * The greeting for the crewmate chat open on `slotKey`.
 *
 * Fires once per open: when `slotKey` turns to a slot (the thread confirmed),
 * not on every render. A slot going blank and back (a reconnect's re-confirm)
 * is a new open, and the seen-record above keeps it from repeating. Nothing is
 * read while the crewmate is mid-turn (`idle` false): its own reply is about to
 * say where things stand. A turn starting takes a shown greeting down.
 *
 * The read goes through the Crew board's query key with `staleTime: 0`, so a
 * board cached by the menu or page is refreshed, never shown as this return's
 * status. It is enabled only while this open waits on it, so it never polls.
 */
export function useMateGreeting(slotKey: string, idle: boolean): {
  greeting: MateGreeting | null
  failure: MateGreetingFailure | null
  dismiss: () => void
} {
  const [greeting, setGreeting] = useState<MateGreeting | null>(null)
  const [failure, setFailure] = useState<MateGreetingFailure | null>(null)
  // The slot whose open is waiting on its read.
  const [armed, setArmed] = useState('')
  const handled = useRef('')
  useEffect(() => {
    if (!slotKey) {
      handled.current = ''
      return
    }
    if (handled.current === slotKey) return
    handled.current = slotKey
    setGreeting((g) => (g?.slot === slotKey ? g : null))
    setFailure(null)
    // An open that begins mid-turn reads nothing, and is not re-armed when the
    // turn ends: this open is handled.
    setArmed(idle ? slotKey : '')
  }, [slotKey, idle])
  useEffect(() => {
    if (idle) return
    setGreeting(null)
    setArmed('')
  }, [idle])

  const armedSlot = armed === slotKey ? armed : ''
  const read = useQuery({
    queryKey: crewBoardQueryKey(armedSlot),
    queryFn: () => api.crewBoard(armedSlot),
    enabled: !!armedSlot,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: retryUnlessNoLedger,
  })
  // Decided once the read settles. A cached board is stale under
  // `staleTime: 0`, so enabling the read reports it fetching at once and the
  // old data never decides this open.
  const { data, error, fetchStatus } = read
  useEffect(() => {
    if (!armedSlot || fetchStatus !== 'idle') return
    if (error) {
      setArmed('')
      // No ledger: the chat simply opens without a greeting.
      if (!isNotFoundError(error)) setFailure({ slot: armedSlot, error })
      return
    }
    if (!data) return
    setArmed('')
    const resume = summarizeResume(data)
    const now = Date.now()
    if (!resume || seenRecently(armedSlot, resume.fingerprint, now)) return
    markSeen(armedSlot, resume.fingerprint, now)
    setGreeting({ kind: 'warm', slot: armedSlot, resume })
  }, [armedSlot, data, error, fetchStatus])

  const dismiss = useCallback(() => {
    setGreeting(null)
    setFailure(null)
  }, [])
  return {
    greeting: greeting && greeting.slot === slotKey ? greeting : null,
    failure: failure && failure.slot === slotKey ? failure : null,
    dismiss,
  }
}
