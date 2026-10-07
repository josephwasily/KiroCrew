import { safeSetItem } from './safeStorage'
import { mergeRecoveredDraft } from './chatDrafts'
/**
 * Paste-token utilities.
 *
 * Large pastes into the chat input are collapsed into inline tokens of the
 * form `⌜ Paste #N · M lines ⌟` so the textarea stays readable. Pairing a
 * token occurrence in text to its backing PasteBlock is by the block's stable
 * `id`, carried in the token as an ENTIRELY INVISIBLE run of zero-width code
 * points right after `#N` (see {@link encodeIdInvisible}). Every character of
 * the carried id is zero-width, so the token renders byte-for-byte as
 * `[ Paste #N · M lines ]` to the user — nothing of the id is visible — while
 * the regex still recovers a durable key. `seq` restarts at 1 once a send
 * clears the blocks, so it is a display number only, never the addressing key.
 *
 * Legacy tokens in the old `#N`-only form (no invisible id) are still resolved
 * by seq as a migration fallback, so sent transcripts, per-slot drafts and the
 * paste-draft store written before this change keep working.
 *
 * Seq numbers are stable once assigned — if the user deletes token #2 and
 * pastes again, the new block is assigned a fresh seq (max+1), not renumbered.
 */

/** A collapsed paste block stored alongside the input/message. */
export interface PasteBlock {
  id: string       // unique id (React key AND the token's invisible addressing key)
  seq: number      // display-only per-session number visible in the token (`#N`)
  lines: number    // line count displayed in the token (`M lines`)
  content: string  // original pasted text
}

export const PASTE_THRESHOLD_LINES = 3
export const PASTE_THRESHOLD_CHARS = 200

// Zero-width code points used to carry the id invisibly. All three render as
// nothing and have zero advance width, so the token the user reads is
// unchanged, and they survive the app's own round-trips (localStorage drafts,
// the sent transcript, re-collapse). The id bits ride on U+200B (ZERO WIDTH
// SPACE = bit 0) and U+200C (ZERO WIDTH NON-JOINER = bit 1); U+2063 (INVISIBLE
// SEPARATOR) fences the run so the regex can bound it unambiguously. The run
// sits right after `#N` and before ` · M lines ]`, never trailing, so
// trailing-whitespace trimming never touches it.
const PASTE_ID_FENCE = '\u2063'
const PASTE_ID_BIT0 = '\u200b'
const PASTE_ID_BIT1 = '\u200c'

/** Encode an ASCII id as a fenced, fully zero-width run: each char's code point
 *  (0–127) as 7 bits MSB-first over {@link PASTE_ID_BIT0}/{@link PASTE_ID_BIT1},
 *  wrapped in {@link PASTE_ID_FENCE}. `makePasteId` emits base-36 (`[a-z0-9]`),
 *  all ASCII. Returns '' for an empty id so the token stays legacy-shaped. */
export function encodeIdInvisible(id: string): string {
  if (!id) return ''
  let bits = ''
  for (let i = 0; i < id.length; i++) {
    const code = id.charCodeAt(i) & 0x7f
    for (let b = 6; b >= 0; b--) bits += (code >> b) & 1 ? PASTE_ID_BIT1 : PASTE_ID_BIT0
  }
  return `${PASTE_ID_FENCE}${bits}${PASTE_ID_FENCE}`
}

/** Inverse of {@link encodeIdInvisible}: decode a bare bit run (no fences) back
 *  to the ASCII id. Returns '' when the run is not a whole number of 7-bit
 *  groups (corruption) so a malformed token resolves to nothing rather than a
 *  wrong block. */
export function decodeIdInvisible(bits: string): string {
  if (!bits || bits.length % 7 !== 0) return ''
  let out = ''
  for (let i = 0; i < bits.length; i += 7) {
    let code = 0
    for (let b = 0; b < 7; b++) {
      const ch = bits[i + b]
      if (ch === PASTE_ID_BIT1) code = (code << 1) | 1
      else if (ch === PASTE_ID_BIT0) code = code << 1
      else return ''
    }
    out += String.fromCharCode(code)
  }
  return out
}

/**
 * Global regex for token occurrences. (1)=seq, (2)=the invisible id bit run
 * (optional — absent on legacy `#N`-only tokens), (3)=lines. The id run sits
 * between two U+2063 fences right after `#N`; the fences are matched but not
 * captured. Decode group 2 with {@link decodeIdInvisible}.
 */
export const PASTE_TOKEN_REGEX = /\[ Paste #(\d+)(?:\u2063([\u200b\u200c]+)\u2063)? · (\d+) lines \]/g

export function formatToken(block: PasteBlock): string {
  return `[ Paste #${block.seq}${encodeIdInvisible(block.id)} · ${block.lines} lines ]`
}

export function shouldCollapse(text: string): boolean {
  if (!text) return false
  return countLines(text) >= PASTE_THRESHOLD_LINES || text.length >= PASTE_THRESHOLD_CHARS
}

export function countLines(text: string): number {
  if (!text) return 0
  return text.split('\n').length
}

/** React-only id; not embedded in token. */
export function makePasteId(): string {
  const t = Date.now().toString(36)
  const r = Math.floor(Math.random() * 1296).toString(36).padStart(2, '0')
  return `${t}${r}`
}

/** Next seq for a new paste = max existing + 1, starting at 1. */
export function nextSeq(blocks: PasteBlock[]): number {
  let max = 0
  for (const b of blocks) { if (b.seq > max) max = b.seq }
  return max + 1
}

/**
 * Re-sequence `carried` blocks whose `seq` is already taken by `used`, and rewrite
 * their markers in `text` to match.
 *
 * Markers resolve by `seq` alone, so re-using a seq makes two blocks collapse onto
 * one on expansion — one blob's content is sent twice and the other is dropped.
 * Rewriting must therefore happen in a SINGLE right-to-left pass over the located
 * ranges: a naive per-block `split/join` re-matches markers an earlier iteration
 * just emitted (the needle `[ Paste #N · M lines ]` collides whenever two blocks
 * share a line count), which cascades every marker onto the last block.
 *
 * `used` is mutated to include the assigned seqs. Blocks keep their identity and
 * content; only `seq` changes, and only when it has to.
 */
export function remapCarriedBlocks(
  text: string,
  carried: PasteBlock[],
  used: Set<number>,
): { text: string; blocks: PasteBlock[] } {
  let free = 0
  for (const v of used) if (v > free) free = v
  free += 1
  const remap = new Map<number, number>()
  const blocks: PasteBlock[] = []
  for (const b of carried) {
    // Re-derive `free` against `used` on every allocation. A block that KEEPS its
    // seq also lands in `used`, so a single max()-seed goes stale the moment a kept
    // seq is >= free — and the next block needing a new seq would be handed one the
    // kept block already holds, recreating the duplicate this function exists to
    // prevent. (Reachable when the live list has a seq gap, e.g. after a paste chip
    // is deleted, then a second failed recovery runs.)
    while (used.has(free)) free++
    const seq = used.has(b.seq) ? free++ : b.seq
    used.add(seq)
    if (seq !== b.seq) remap.set(b.seq, seq)
    blocks.push(seq === b.seq ? b : { ...b, seq })
  }
  if (!remap.size) return { text, blocks }
  // Right-to-left so each splice leaves the earlier ranges' offsets valid, and so
  // no marker written by this pass is ever re-examined.
  let out = text
  const ranges = findTokenRanges(text, carried)
  for (let i = ranges.length - 1; i >= 0; i--) {
    const { start, end, block } = ranges[i]
    const mapped = remap.get(block.seq)
    if (mapped === undefined) continue
    out = out.slice(0, start) + formatToken({ ...block, seq: mapped }) + out.slice(end)
  }
  return { text: out, blocks }
}

/**
 * Bring the blocks behind a payload being handed back (`carried`, the ones its
 * tokens in `text` point at) into a composer that already holds `kept` blocks.
 *
 * The one owner of the recovery rule every composer host applies: a carried
 * block the composer already holds (same id — the clear had not flushed when
 * the payload was captured, or an undo put it back) is not added twice AND its
 * token is dropped from the payload text, since the composer already shows
 * that token — appending a second one would make the retry send the paste
 * twice. The rest go through {@link remapCarriedBlocks} so a colliding `seq`
 * gets a fresh number and its token in `text` is rewritten. Returns the text to
 * merge (`text`, held tokens stripped), the same payload with every token still
 * in place (`full`, for the exact-duplicate test — see {@link mergeCarriedDraft})
 * and the full block list to install (kept first, then the carried ones).
 */
export interface CarriedPastes { text: string; full: string; pastes: PasteBlock[] }

export function carryPastes(text: string, carried: PasteBlock[], kept: PasteBlock[]): CarriedPastes {
  if (!carried.length) return { text, full: text, pastes: kept }
  const keptIds = new Set(kept.map(b => b.id))
  const fresh = carried.filter(b => !keptIds.has(b.id))
  let payload = text
  // Strip a held block's token from the payload so the composer never shows it
  // twice. A new-form token carries the block id, so the strip below resolves
  // it precisely. A LEGACY token (`#N`, no id) can only match by seq and is
  // ambiguous when a fresh carried block shares that seq — attributing it to
  // the held block would wrongly swallow the fresh one's token, so a held block
  // whose seq a fresh block also claims is left in place (its legacy token then
  // flows through the re-sequencing path with the fresh blocks).
  const freshSeqs = new Set(fresh.map(b => b.seq))
  const held = carried.filter(b => keptIds.has(b.id) && !freshSeqs.has(b.seq))
  if (held.length) {
    // Right-to-left, so each splice leaves the earlier ranges' offsets valid.
    const ranges = findTokenRanges(payload, held)
    for (let i = ranges.length - 1; i >= 0; i--) {
      const { start, end } = ranges[i]
      // Take the token's own line break with it, so no blank line is left behind.
      const eatNewline = payload[end] === '\n' ? 1 : 0
      payload = payload.slice(0, start) + payload.slice(end + eatNewline)
    }
  }
  // `remapCarriedBlocks` mutates the seq set it is given, so each pass gets its
  // own copy and both assign the same numbers.
  const keptSeqs = kept.map(b => b.seq)
  const { text: remapped, blocks } = remapCarriedBlocks(payload, fresh, new Set(keptSeqs))
  const full = payload === text ? remapped : remapCarriedBlocks(text, fresh, new Set(keptSeqs)).text
  return { text: remapped, full, pastes: [...kept, ...blocks] }
}

/**
 * Put a carried payload back into a composer's text under the shared recovery
 * rule. The exact-duplicate test runs against the payload WITH its tokens
 * (`full`): an undo that put the whole payload back — tokens included — is the
 * case the equality exists for, and the stripped text alone would never equal
 * it. Anything else appends the stripped text, so a held block's token is not
 * shown twice and a retry cannot send the paste twice.
 */
export function mergeCarriedDraft(keep: string | null | undefined, carried: CarriedPastes): string {
  const existing = keep ?? ''
  if (existing.trim() && existing.trim() === carried.full.trim()) return existing
  return mergeRecoveredDraft(existing, carried.text)
}

/**
 * Ranges for each token that resolves to a block in `blocks`, in document order.
 *
 * A token is resolved by its invisible `id` fence when present — the primary
 * key — so a recalled message's token for block A never resolves to a different
 * draft's block B even when both display `#1` (seq restarts at 1 after a send).
 * A new-form token whose id names no live block does NOT resolve; it stays
 * literal text rather than mis-pairing.
 *
 * A legacy token with no id fence (`#N`, written before id-addressing) falls
 * back to `seq`, but only when exactly one live block owns that seq. Several
 * live blocks sharing the seq make a bare legacy token ambiguous, so it is left
 * unresolved rather than guessed — guessing is the wrong-content splice this
 * change exists to prevent.
 */
export function findTokenRanges(
  text: string,
  blocks: PasteBlock[],
): Array<{ start: number; end: number; block: PasteBlock }> {
  if (!text || !blocks.length) return []
  const byId = new Map(blocks.map(b => [b.id, b]))
  const bySeq = new Map<number, PasteBlock[]>()
  for (const b of blocks) {
    const arr = bySeq.get(b.seq)
    if (arr) arr.push(b); else bySeq.set(b.seq, [b])
  }
  const out: Array<{ start: number; end: number; block: PasteBlock }> = []
  PASTE_TOKEN_REGEX.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = PASTE_TOKEN_REGEX.exec(text)) !== null) {
    let block: PasteBlock | undefined
    if (m[2] !== undefined) {
      // The token carries a fenced id run. Resolve by the decoded id ONLY — a
      // run that decodes to nothing (corrupted/garbled, e.g. clipboard text
      // altered outside the app) must stay unresolved, NOT fall back to seq:
      // the seq fallback could pair it to a live same-seq block and splice that
      // block's content in. The seq fallback is for LEGACY tokens that never
      // carried an id, which is `m[2] === undefined`.
      const id = decodeIdInvisible(m[2])
      if (id) block = byId.get(id)
    } else {
      // Legacy `#N`-only token (no id run): resolve by seq, but only when
      // exactly one live block owns it; an ambiguous seq is left unresolved.
      const candidates = bySeq.get(Number(m[1]))
      if (candidates && candidates.length === 1) block = candidates[0]
    }
    if (block) out.push({ start: m.index, end: m.index + m[0].length, block })
  }
  return out
}

export function tokenRangeAt(
  text: string,
  blocks: PasteBlock[],
  caret: number,
): { start: number; end: number; block: PasteBlock } | null {
  for (const r of findTokenRanges(text, blocks)) {
    if (caret >= r.start && caret <= r.end) return r
  }
  return null
}

export function pruneBlocks(text: string, blocks: PasteBlock[]): PasteBlock[] {
  if (!blocks.length) return blocks
  const survivors = new Set(findTokenRanges(text, blocks).map(r => r.block.id))
  const next = blocks.filter(b => survivors.has(b.id))
  return next.length === blocks.length ? blocks : next
}

export function expandAll(text: string, blocks: PasteBlock[]): string {
  if (!text || !blocks.length) return text
  const ranges = findTokenRanges(text, blocks)
  if (!ranges.length) return text
  let out = text
  for (let i = ranges.length - 1; i >= 0; i--) {
    const r = ranges[i]
    out = out.slice(0, r.start) + r.block.content + out.slice(r.end)
  }
  return out
}

/**
 * Inverse of {@link expandAll}: given fully-expanded content and its backing
 * blocks, substitute each block's verbatim content back to its
 * `[ Paste #N · M lines ]` token.
 *
 * This is the render-time safety net for history load. The backend stores and
 * re-serves the EXPANDED content (what the LLM saw) alongside `meta.pastes`.
 * `mergePreservedPastes` only re-collapses when it can match against an
 * in-memory optimistic bubble or a localStorage side-table entry — both of
 * which are absent on a fresh tab or after the side-table evicts the entry.
 * When that happens a multi-hundred-KB paste is otherwise handed raw to the
 * markdown renderer, which parses + lays out tens of thousands of lines on the
 * main thread and freezes the tab. Because the
 * blocks travel with the message, re-collapse can be derived deterministically
 * from `content` + `meta.pastes` with no external state.
 *
 * Replaces the FIRST non-overlapping occurrence of each block in document
 * order, so repeated sends of the same paste each collapse to their own token.
 * A block whose content is not found verbatim is skipped (that region renders
 * as-is). Returns `content` unchanged when nothing matched.
 */
export function recollapsePastes(content: string, blocks: PasteBlock[]): string {
  if (!content || !blocks.length) return content
  interface Hit { start: number; end: number; block: PasteBlock }
  const hits: Hit[] = []
  const claimed: Array<[number, number]> = []
  // First occurrence of `needle` not already claimed by an earlier block
  // (handles the rare case where one paste's content is a substring of
  // another's). Returns -1 when every occurrence overlaps a claim or none exist.
  const firstUnclaimed = (needle: string): number => {
    if (!needle) return -1
    let from = 0
    while (from <= content.length) {
      const idx = content.indexOf(needle, from)
      if (idx < 0) return -1
      if (!claimed.some(([s, e]) => idx < e && idx + needle.length > s)) return idx
      from = idx + 1
    }
    return -1
  }
  for (const b of blocks) {
    if (!b.content) continue
    // Prefer a verbatim match. Fall back to the trailing-whitespace-trimmed
    // block content: the backend strips trailing whitespace from the stored
    // message (mergePreservedPastes keys on trimEnd() for the same reason), so
    // a paste that was the LAST thing in the message loses its own trailing
    // newline/spaces and won't match verbatim — without the fallback the huge
    // paste falls through to the raw markdown renderer and the freeze it guards
    // against is not prevented for that shape. Verbatim is tried fully first so
    // an interior block (whose trailing whitespace is preserved) is unaffected.
    const trimmed = b.content.trimEnd()
    let needle = b.content
    let idx = firstUnclaimed(needle)
    if (idx < 0 && trimmed && trimmed !== b.content) {
      needle = trimmed
      idx = firstUnclaimed(needle)
    }
    if (idx < 0) continue
    const end = idx + needle.length
    hits.push({ start: idx, end, block: b })
    claimed.push([idx, end])
  }
  if (!hits.length) return content
  hits.sort((a, b) => a.start - b.start)
  let out = ''
  let pos = 0
  for (const h of hits) {
    if (h.start < pos) continue // defensive: an overlap survived the claim check
    out += content.slice(pos, h.start) + formatToken(h.block)
    pos = h.end
  }
  out += content.slice(pos)
  return out
}

/**
 * Merge preserved paste state from `existing` onto `incoming` (from backend
 * refresh). For each user message in `existing` with `meta.pastes`, the
 * tokenized content + pastes are re-applied to the matching incoming user
 * message — matched by expansion equality (`expandAll(old.content, old.pastes)
 * === new.content`). Consumed FIFO so repeated sends don't collide.
 *
 * Falls back to `readStoredPaste(incoming.content)` for messages that have no
 * in-memory counterpart (e.g. after page reload or chat switch) — this reads
 * from the localStorage side table populated by `saveStoredPaste`.
 *
 * Why: the backend only sees/stores the LLM-facing expanded text. Without
 * this merge, the user bubble would "expand" to full text as soon as the
 * refreshSlot after chat_done replaces the optimistic message.
 */
export function mergePreservedPastes<M extends { role: string; content: string; meta?: Record<string, unknown> }>(
  existing: M[],
  incoming: M[],
): M[] {
  const preserved: Array<{ content: string; pastes: PasteBlock[]; expanded: string; files: string[] | null }> = []
  for (const m of existing) {
    const pastes = (m.meta?.pastes as PasteBlock[] | undefined) || []
    if (m.role === 'user' && pastes.length) {
      const files = (m.meta?.files as string[] | undefined) ?? null
      // Normalize trailing whitespace — the backend strips it before storing,
      // so our expanded text (which may have a trailing newline/space from the
      // token + newline pattern) won't match the incoming content byte-for-byte.
      preserved.push({ content: m.content, pastes, expanded: expandAll(m.content, pastes).trimEnd(), files })
    }
  }
  const queue = preserved.slice()
  // A backend-served user message that carries its own `meta.pastes` but whose
  // content is still fully expanded (no `[ Paste #N ]` token) needs fallback 3
  // (self-contained re-collapse) even when there is no optimistic bubble and no
  // side-table hit — so it must NOT be short-circuited away.
  const needsSelfCollapse = (m: M): boolean => {
    if (m.role !== 'user') return false
    const own = (m.meta?.pastes as PasteBlock[] | undefined) || []
    return own.length > 0 && findTokenRanges(m.content, own).length === 0
  }
  // Short-circuit: if no existing user messages have paste metadata AND no
  // incoming user message has a matching entry in the localStorage side table
  // AND none needs self-contained re-collapse, return the `incoming` array
  // reference unchanged. This preserves reference equality for callers that use
  // Object.is / toBe checks, and avoids an unnecessary array allocation in the
  // common no-pastes case.
  if (
    !queue.length &&
    !incoming.some(m => m.role === 'user' && readStoredPaste(m.content.trimEnd())) &&
    !incoming.some(needsSelfCollapse)
  ) {
    return incoming
  }
  return incoming.map(m => {
    if (m.role !== 'user') return m
    // 1) In-memory preservation (optimistic bubble still present)
    if (queue.length) {
      // Compare against trimEnd()'d incoming content — backend strips trailing
      // whitespace on storage, so our expanded text (pre-strip) wouldn't match.
      const incomingTrimmed = m.content.trimEnd()
      const idx = queue.findIndex(p => p.expanded === incomingTrimmed)
      if (idx >= 0) {
        const match = queue.splice(idx, 1)[0]
        const newMeta: Record<string, unknown> = { ...m.meta, pastes: match.pastes }
        // meta.files is lost on the backend-served message — preserve it
        // from the existing optimistic bubble so file chips stay clickable.
        if (match.files && match.files.length) newMeta.files = match.files
        return { ...m, content: match.content, meta: newMeta }
      }
    }
    // 2) localStorage side table (survives refresh/chat-switch)
    const stored = readStoredPaste(m.content.trimEnd())
    if (stored) {
      const newMeta: Record<string, unknown> = { ...m.meta, pastes: stored.pastes }
      if (stored.files && stored.files.length) newMeta.files = stored.files
      return { ...m, content: stored.displayTxt, meta: newMeta }
    }
    // 3) Self-contained re-collapse. The backend re-serves `meta.pastes`
    // alongside the fully-expanded content, so when neither the optimistic
    // bubble nor the side table can re-collapse (fresh tab, evicted entry),
    // fold the message's own blocks back into `[ Paste #N ]` tokens. Without
    // this a huge paste stays expanded in state and the virtualizer measures /
    // the renderer parses hundreds of KB on the main thread, freezing the tab.
    const ownPastes = (m.meta?.pastes as PasteBlock[] | undefined) || []
    if (ownPastes.length && !findTokenRanges(m.content, ownPastes).length) {
      const collapsed = recollapsePastes(m.content, ownPastes)
      if (collapsed !== m.content) return { ...m, content: collapsed }
    }
    return m
  })
}

/* ---- localStorage side table: content-addressed paste preservation ---- */

export const STORE_KEY = 'mc-paste-store-v1'
export const STORE_CAP = 200
// Discard entries not touched within this window. Mirrors the 30-day draft
// TTL (DRAFT_TTL_MS in chatDrafts) so sent-paste rehydration data ages out on
// the same schedule as the unsent drafts it complements.
export const STORE_TTL_MS = 30 * 24 * 60 * 60 * 1000
// Byte ceiling for the serialized store. The STORE_CAP entry count alone does
// NOT bound size — 200 large pastes (logs, files, transcripts) can reach ~5 MB
// and exhaust the localStorage quota, after which every other setItem (e.g.
// saveChatConfig) throws QuotaExceededError and silently breaks the UI. A
// byte-aware LRU keeps only the newest entries that fit. Matches the
// DRAFT_MAX_STORE_BYTES budget the slot-draft stores adopt in.
export const STORE_MAX_BYTES = 2 * 1024 * 1024

// `seq` is a monotonic insertion counter used as the recency tiebreaker.
// `savedAt` (wall-clock ms) is too coarse: a burst of pastes within the same
// millisecond all share a savedAt, and a stable sort would then preserve
// insertion order (oldest-first) — floating the OLDEST entries to the front of
// a "newest-first" sort and evicting newer ones. `seq` is strictly increasing
// per write (derived as max(existing)+1, so it survives reloads), giving an
// unambiguous recency order under sub-millisecond writes.
interface StoredPaste { displayTxt: string; pastes: PasteBlock[]; files?: string[]; savedAt: number; seq?: number }
type Store = Record<string, StoredPaste>

/** True if a stored entry is structurally valid and within the TTL window. */
function isFresh(v: StoredPaste, cutoff: number): boolean {
  return !!v && typeof v.savedAt === 'number' && v.savedAt >= cutoff
}

/** Next monotonic insertion seq = max existing + 1 (1 when empty). Derived from
 *  the store itself so it stays monotonic across page reloads without a
 *  module-level counter that would reset to 0 and collide with persisted seqs. */
function nextStoreSeq(store: Store): number {
  let max = 0
  for (const v of Object.values(store)) {
    if (typeof v.seq === 'number' && v.seq > max) max = v.seq
  }
  return max + 1
}

function readStore(): Store {
  if (typeof localStorage === 'undefined') return {}
  try {
    const raw = localStorage.getItem(STORE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (typeof parsed !== 'object' || !parsed) return {}
    // Drop entries past the TTL so stale paste content is never rehydrated;
    // the physical removal happens on the next writeStore.
    const cutoff = Date.now() - STORE_TTL_MS
    const fresh: Store = {}
    for (const [k, v] of Object.entries(parsed as Store)) {
      if (isFresh(v, cutoff)) fresh[k] = v
    }
    return fresh
  } catch {
    return {}
  }
}

function writeStore(store: Store): void {
  if (typeof localStorage === 'undefined') return
  // Bound the store on three axes, newest-first so the most recent pastes
  // always survive: (1) drop TTL-expired entries, (2) cap entry count, and
  // (3) cap total serialized bytes via a byte-aware LRU. The newest entry is
  // never evicted (the count > 0 guard), even if it alone exceeds the budget.
  const cutoff = Date.now() - STORE_TTL_MS
  const entries = Object.entries(store)
    .filter(([, v]) => isFresh(v, cutoff))
    // Newest first. Tiebreak same-millisecond savedAt by the monotonic `seq`
    // so a burst of writes orders by true insertion recency, not stable-sort
    // insertion order (which would float the oldest entry to the front).
    .sort((a, b) => (b[1].savedAt - a[1].savedAt) || ((b[1].seq ?? 0) - (a[1].seq ?? 0)))
  const kept: Store = {}
  let bytes = 2 // enclosing "{}"
  let count = 0
  for (const [k, v] of entries) {
    if (count >= STORE_CAP) break
    // Approx serialized contribution of this entry: "key":value plus comma.
    const entryBytes = JSON.stringify(k).length + 1 + JSON.stringify(v).length + 1
    if (count > 0 && bytes + entryBytes > STORE_MAX_BYTES) break
    kept[k] = v
    bytes += entryBytes
    count++
  }
  try {
    safeSetItem(STORE_KEY, JSON.stringify(kept))
  } catch { /* quota exceeded or storage unavailable — ignore */ }
}

/** Persist paste tokenization for a message so it survives refresh/chat switch.
 *  Keyed by the fully-expanded content (what the backend stores).
 *  Stores `files` alongside so @-file chips stay clickable after refresh. */
export function saveStoredPaste(
  expandedContent: string,
  displayTxt: string,
  pastes: PasteBlock[],
  files?: string[],
): void {
  if (!pastes.length || !expandedContent) return
  const store = readStore()
  // Key by trimEnd() to match what the backend stores (it strips trailing whitespace).
  const key = expandedContent.trimEnd()
  // Compute seq BEFORE inserting so a re-save of an existing key still advances
  // its recency. Delete-then-reinsert isn't needed — eviction sorts on seq.
  const seq = nextStoreSeq(store)
  store[key] = {
    displayTxt,
    pastes,
    ...(files && files.length ? { files } : {}),
    savedAt: Date.now(),
    seq,
  }
  writeStore(store)
}

/** Look up persisted paste tokenization by expanded content. Returns null if absent. */
export function readStoredPaste(expandedContent: string): StoredPaste | null {
  if (!expandedContent) return null
  const store = readStore()
  return store[expandedContent] ?? null
}
