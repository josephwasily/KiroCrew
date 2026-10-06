/**
 * "Mute the sessions a conductor opens".
 *
 * A goal conductor opens a new session per work item, and each worker chimes
 * when its turn ends. The user wants to hear from the conductor, not from every
 * worker it spawns -- and the worker's first chime fires before the user can
 * reach that row's kebab menu, so a per-row mute cannot cover it. The
 * mute has to exist before the worker's session does.
 *
 * The rule is set ON the creating session (`slot.mutes_opened`, the user's own
 * decision, never an agent's) and READ off the durable `created_by` chain: a
 * session is muted when any session UP its `created_by` chain carries the flag.
 * Deriving from the persisted `created_by` -- not from process-local lineage --
 * is what makes the rule survive a gateway restart (acceptance criterion 4).
 *
 * There is also an optional global preference (Settings -> Notifications): mute
 * ANY session whose `created_by` is non-empty, i.e. any session opened by
 * another session regardless of a per-conductor flag.
 *
 * The creator itself is never muted by this rule -- its `created_by` points at
 * the user's own tab (empty) or at a session that does not carry the flag, so
 * walking its chain finds nothing. That is the point: the conductor's reports
 * are the signal the user asked for.
 */
import type { ChatSlot } from '../types'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/** localStorage key for the global "mute sessions opened by other sessions"
 *  preference. Absent -- or anything but `'1'` -- means off, so a corrupt or
 *  half-written value degrades to the default (off) rather than silence a
 *  user never asked for. Lives in localStorage beside the other per-device
 *  notification toggles in `chatCompleteNotify.ts`. */
export const MUTE_OPENED_GLOBAL_KEY = 'mc-mute-sessions-opened-by-sessions'

/** Whether the user turned on the global "mute any session opened by another
 *  session" preference. Default OFF. */
export function loadMuteOpenedGlobal(): boolean {
  return safeGetItem(MUTE_OPENED_GLOBAL_KEY) === '1'
}

/** Persist the global preference. Returns whether the write landed -- a
 *  disabled or full localStorage returns false, so the caller can keep the
 *  toggle on its prior value and surface the failure rather than paint a state
 *  the attention gate never actually reads. */
export function saveMuteOpenedGlobal(on: boolean): boolean {
  return safeSetItem(MUTE_OPENED_GLOBAL_KEY, on ? '1' : '0')
}

/**
 * Whether `slotKey`'s attention is muted by the "mute sessions it opens" rule.
 *
 * Walks the `created_by` chain upward from the slot's CREATOR (never the slot
 * itself -- a creator setting the flag mutes what it opens, not itself) and
 * returns true as soon as an ancestor carries `mutes_opened`. With the global
 * preference on, any slot that was opened by another session (non-empty
 * `created_by`) is muted.
 *
 * Guards: a `seen` set breaks a `created_by` cycle a corrupted history could
 * introduce, and a hard depth cap bounds a pathological chain. A slot not found
 * in `slots` ends the walk (its ancestry is unknown, so nothing to mute on).
 *
 * The per-row override (acceptance criterion 6) belongs to the per-row mute
 * feature, which has
 * not landed; when it does, its per-row unmute is checked here BEFORE the chain
 * walk so a single worker can be exempted. Left as a documented seam, not a
 * live read, so this change does not depend on an unlanded one.
 */
export function isSlotMutedByCreator(
  slots: readonly ChatSlot[],
  slotKey: string | undefined | null,
): boolean {
  if (!slotKey) return false
  const byKey = new Map<string, ChatSlot>()
  for (const s of slots) byKey.set(s.key, s)
  const self = byKey.get(slotKey)
  if (!self) return false
  // The birth-time creator. Empty for a person's own tab, a fork or a restore
  // -- none of which this rule touches.
  const firstCreator = self.created_by || ''
  if (!firstCreator) return false
  // Global preference: opened-by-another-session is enough, no flag needed.
  if (loadMuteOpenedGlobal()) return true
  const seen = new Set<string>([slotKey])
  let cursor = firstCreator
  for (let depth = 0; cursor && depth < 64; depth++) {
    if (seen.has(cursor)) break
    seen.add(cursor)
    const ancestor = byKey.get(cursor)
    if (!ancestor) break
    if (ancestor.mutes_opened) return true
    cursor = ancestor.created_by || ''
  }
  return false
}
