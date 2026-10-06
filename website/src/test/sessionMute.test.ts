import { describe, it, expect, beforeEach } from 'vitest'
import {
  isSlotMutedByCreator,
  loadMuteOpenedGlobal,
  saveMuteOpenedGlobal,
  MUTE_OPENED_GLOBAL_KEY,
} from '../hooks/sessionMute'
import type { ChatSlot } from '../types'

/** Minimal ChatSlot factory — only the fields the mute walk reads. */
function slot(key: string, createdBy = '', mutesOpened = false): ChatSlot {
  return { key, messages: 0, running: false, created_by: createdBy, mutes_opened: mutesOpened } as ChatSlot
}

describe('isSlotMutedByCreator', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it("a person's own tab (empty created_by) is never muted", () => {
    const slots = [slot('conductor', '', true), slot('tab', '')]
    expect(isSlotMutedByCreator(slots, 'tab')).toBe(false)
  })

  it('the creator itself is never muted by its own flag', () => {
    const slots = [slot('conductor', '', true), slot('worker', 'conductor')]
    expect(isSlotMutedByCreator(slots, 'conductor')).toBe(false)
  })

  it('a worker opened by a flagged conductor is muted', () => {
    const slots = [slot('conductor', '', true), slot('worker', 'conductor')]
    expect(isSlotMutedByCreator(slots, 'worker')).toBe(true)
  })

  it('a worker whose conductor is NOT flagged is not muted', () => {
    const slots = [slot('conductor', '', false), slot('worker', 'conductor')]
    expect(isSlotMutedByCreator(slots, 'worker')).toBe(false)
  })

  it('a nested worker is muted when any ANCESTOR carries the flag', () => {
    const slots = [
      slot('top', '', true),
      slot('sub', 'top', false),
      slot('leaf', 'sub', false),
    ]
    expect(isSlotMutedByCreator(slots, 'leaf')).toBe(true)
  })

  it('a created_by cycle terminates and does not throw', () => {
    const slots = [slot('a', 'b', false), slot('b', 'a', false)]
    expect(isSlotMutedByCreator(slots, 'a')).toBe(false)
  })

  it('an unknown slot key is not muted', () => {
    expect(isSlotMutedByCreator([slot('x', '')], 'missing')).toBe(false)
    expect(isSlotMutedByCreator([], null)).toBe(false)
    expect(isSlotMutedByCreator([], undefined)).toBe(false)
  })

  it('global preference mutes ANY session opened by another session, flag or not', () => {
    saveMuteOpenedGlobal(true)
    const slots = [slot('conductor', '', false), slot('worker', 'conductor', false)]
    expect(isSlotMutedByCreator(slots, 'worker')).toBe(true)
    // but still never the creator / own tab
    expect(isSlotMutedByCreator(slots, 'conductor')).toBe(false)
    expect(isSlotMutedByCreator([slot('tab', '')], 'tab')).toBe(false)
  })
})

describe('mute-opened global preference', () => {
  beforeEach(() => localStorage.clear())

  it('defaults off and round-trips', () => {
    expect(loadMuteOpenedGlobal()).toBe(false)
    saveMuteOpenedGlobal(true)
    expect(localStorage.getItem(MUTE_OPENED_GLOBAL_KEY)).toBe('1')
    expect(loadMuteOpenedGlobal()).toBe(true)
    saveMuteOpenedGlobal(false)
    expect(loadMuteOpenedGlobal()).toBe(false)
  })

  it('reports whether the write landed, so a failed save is not swallowed', () => {
    // A successful write returns true.
    expect(saveMuteOpenedGlobal(true)).toBe(true)
    // A storage that throws (disabled / quota) returns false and the caller
    // keeps the toggle on its prior value rather than painting an unsaved one.
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('quota', 'QuotaExceededError')
    })
    try {
      expect(saveMuteOpenedGlobal(false)).toBe(false)
    } finally {
      spy.mockRestore()
    }
  })
})
