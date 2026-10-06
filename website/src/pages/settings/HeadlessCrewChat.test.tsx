/**
 * The pane mounts, and the row's own condition is the one the gateway publishes.
 *
 * WHAT THIS EXISTS TO CATCH, because it already happened: the pane did not
 * render on a pod for a crew whose `/api/instances` row carried
 * `headless_crew: true`, and nothing failed. The cause was not the mount
 * condition -- it was that this component did not COMPILE under
 * `tsconfig.app.json` (a default export imported as named, and `i18nT` imported
 * from `../../i18n` instead of `../../i18n/t`). `npm run build` runs that
 * typecheck first, so the build failed, the published bundle stayed at the
 * previous revision, and the dashboard served a version of the panel that had
 * never heard of the pane. A probe reported `paneNodes: 0` beside
 * `headless_crew: true` and read like a logic bug.
 *
 * Importing and RENDERING the component in a test is what makes that
 * impossible to miss: a module that cannot compile cannot be imported, so the
 * suite reddens where the build would have, instead of a stale bundle quietly
 * passing for a fresh one.
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { HeadlessCrewChat } from './HeadlessCrewChat'

vi.mock('../../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}:${JSON.stringify(vars)}` : key,
}))

describe('HeadlessCrewChat', () => {
  it('mounts and carries the test id the dashboard probe looks for', () => {
    render(<HeadlessCrewChat instanceId="l2crew" crewName="l2crew" connected={false} />)
    expect(screen.getByTestId('headless-crew-chat')).toBeTruthy()
    expect(screen.getByTestId('headless-crew-log')).toBeTruthy()
    expect(screen.getByTestId('headless-crew-input')).toBeTruthy()
    expect(screen.getByTestId('headless-crew-send')).toBeTruthy()
  })

  it('shows the thread id, because that is what a user checks after a suspend', () => {
    render(<HeadlessCrewChat instanceId="l2crew" crewName="l2crew" connected />)
    const shown = screen.getByTestId('headless-crew-thread').textContent || ''
    // LABELLED, so a reader can place it: an unlabelled internal string looks
    // like a fault code rather than the thing that proves continuity. Asserted as
    // the i18n KEY, which is what `i18nT` returns with no catalog loaded -- and a
    // stronger check than the English, because a hardcoded label would not match.
    expect(shown).toContain('pages.settings.headlessCrewChat.thread_label')
    // Generated per mount and scoped to the crew, so two rows cannot address one
    // conversation. The shape matters more than the value: an empty one would
    // make the crew mint its own slot and the "same thread" check meaningless.
    const thread = shown.split('thread_label: ').slice(1).join('')
    expect(thread.startsWith('dashboard-l2crew-')).toBe(true)
    expect(thread.length).toBeGreaterThan('dashboard-l2crew-'.length)
  })

  it('refuses to send while the crew is disconnected', () => {
    render(<HeadlessCrewChat instanceId="l2crew" crewName="l2crew" connected={false} />)
    expect((screen.getByTestId('headless-crew-input') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByTestId('headless-crew-send') as HTMLButtonElement).disabled).toBe(true)
  })

  it('still refuses to send when connected but the draft is empty', () => {
    // The pane is offered before the crew is connected ON PURPOSE -- a pane that
    // only appears once connected gives a user no way to see that chatting is
    // what this crew offers -- so "connected" must not by itself enable Send.
    render(<HeadlessCrewChat instanceId="l2crew" crewName="l2crew" connected />)
    expect((screen.getByTestId('headless-crew-input') as HTMLInputElement).disabled).toBe(false)
    expect((screen.getByTestId('headless-crew-send') as HTMLButtonElement).disabled).toBe(true)
  })
})
