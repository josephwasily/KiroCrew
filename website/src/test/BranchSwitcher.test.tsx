import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18next, initI18n } from '../i18n/all'
import type { GitBranchList, GitBranchRow } from '../api/client/files'

const H = vi.hoisted(() => ({
  api: {
    projectGitBranches: vi.fn(),
    projectGitSwitch: vi.fn(),
  },
}))

vi.mock('../api/client', () => ({ api: H.api }))

import BranchSwitcher, { isValidNewBranchName } from '../components/BranchSwitcher'

const PROJECT = '/workspace/project'

function row(name: string, extra: Partial<GitBranchRow> = {}): GitBranchRow {
  return {
    name,
    date: '2026-10-01T00:00:00Z',
    author: 'Ada',
    subject: `work on ${name}`,
    switchable: true,
    ...extra,
  }
}

const LIST: GitBranchList = {
  repo: true,
  current: 'main',
  local: [row('main', { current: true }), row('feature/login', { ahead: 2 }), row('secret', { switchable: false })],
  remote: [row('origin/review-fix')],
}

function mount(props: { switchBlocked?: boolean; detached?: boolean; branch?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  render(
    <QueryClientProvider client={qc}>
      <BranchSwitcher projectDir={PROJECT} branch={props.branch ?? 'main'} switchBlocked={props.switchBlocked} detached={props.detached} />
    </QueryClientProvider>,
  )
  return { invalidate }
}

async function openPicker() {
  fireEvent.click(screen.getByTestId('branch-switcher-trigger'))
  await screen.findByText('feature/login')
}

function apiError(code: string, extra: Record<string, unknown> = {}) {
  return Object.assign(new Error(code), { body: JSON.stringify({ error: 'x', code, ...extra }) })
}

beforeEach(async () => {
  await initI18n()
  await i18next.changeLanguage('en')
  H.api.projectGitBranches.mockReset().mockResolvedValue(LIST)
  H.api.projectGitSwitch.mockReset().mockResolvedValue({ ok: true, branch: 'feature/login' })
})

describe('BranchSwitcher', () => {
  it('narrows to the viewport when 360px does not fit beside the gutters', async () => {
    const original = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 320 })
    try {
      mount()
      await openPicker()
      const pop = screen.getByTestId('branch-switcher')
      expect(pop.style.width).toBe('304px')
      expect(Number.parseFloat(pop.style.left) + 304).toBeLessThanOrEqual(320 - 8)
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: original })
    }
  })

  it('reads the branch list only once opened', async () => {
    mount()
    expect(screen.getByTestId('branch-switcher-trigger')).toHaveTextContent('main')
    expect(H.api.projectGitBranches).not.toHaveBeenCalled()
    await openPicker()
    expect(H.api.projectGitBranches).toHaveBeenCalledWith(PROJECT)
    expect(screen.getByText('Local')).toBeInTheDocument()
    expect(screen.getByText('Remote')).toBeInTheDocument()
  })

  it('switches to a local branch and refreshes every working-tree view', async () => {
    const { invalidate } = mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('feature/login'))
    await waitFor(() => expect(screen.queryByTestId('branch-switcher')).toBeNull())
    expect(H.api.projectGitSwitch).toHaveBeenCalledWith({ path: PROJECT, branch: 'feature/login' })
    const keys = invalidate.mock.calls.map(c => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
    for (const k of [['git-status', PROJECT], ['git-log', PROJECT], ['git-branches', PROJECT], ['project-tree', PROJECT], ['project-git']]) {
      expect(keys).toContain(JSON.stringify(k))
    }
  })

  it('checks out a remote branch as a tracking branch with its short name', async () => {
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('origin/review-fix'))
    await waitFor(() => expect(H.api.projectGitSwitch).toHaveBeenCalled())
    expect(H.api.projectGitSwitch).toHaveBeenCalledWith({ path: PROJECT, branch: 'review-fix', track: 'origin/review-fix' })
  })

  it('offers to create a typed name that does not exist, and Enter picks the only match', async () => {
    mount()
    await openPicker()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'feat/new-thing' } })
    expect(screen.getByTestId('branch-create')).toHaveTextContent('Create branch “feat/new-thing”')
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(H.api.projectGitSwitch).toHaveBeenCalledWith({ path: PROJECT, branch: 'feat/new-thing', create: true }))
  })

  it('does not offer creation for an invalid name', async () => {
    mount()
    await openPicker()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'bad..name' } })
    expect(screen.queryByTestId('branch-create')).toBeNull()
    expect(screen.getByText('Not a valid branch name')).toBeInTheDocument()
  })

  it('never switches to the current branch or a redacted one', async () => {
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('secret'))
    fireEvent.mouseDown(screen.getAllByText('main').find(el => el.closest('[role="option"]'))!)
    expect(H.api.projectGitSwitch).not.toHaveBeenCalled()
  })

  it('shows the localized reason when uncommitted changes block the switch and stays open', async () => {
    H.api.projectGitSwitch.mockRejectedValue(apiError('git_switch_dirty'))
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('feature/login'))
    const notice = await screen.findByTestId('branch-switcher-error')
    // Names the branch the switch targeted, so the reader can tell it failed.
    expect(notice).toHaveTextContent("Couldn't switch to feature/login")
    expect(notice).toHaveTextContent('Not switched: your uncommitted changes would be overwritten. Commit or stash them, then switch.')
    expect(screen.getByTestId('branch-switcher')).toBeInTheDocument()
    // The "never overwritten" line would contradict the refusal beside it.
    expect(screen.queryByTestId('branch-switcher-safety')).toBeNull()
  })

  it('brings the safety line back once the refusal is cleared by typing', async () => {
    H.api.projectGitSwitch.mockRejectedValue(apiError('git_switch_dirty'))
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('feature/login'))
    await screen.findByTestId('branch-switcher-error')
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'f' } })
    expect(screen.getByTestId('branch-switcher-safety')).toBeInTheDocument()
  })

  it('confirms a landed switch beside the trigger and announces it politely', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      mount()
      const status = screen.getByTestId('branch-switcher-status')
      expect(status).toHaveAttribute('role', 'status')
      expect(status).toHaveTextContent('')
      await openPicker()
      fireEvent.mouseDown(screen.getByText('feature/login'))
      await waitFor(() => expect(screen.queryByTestId('branch-switcher')).toBeNull())
      const note = await screen.findByTestId('branch-switcher-note')
      expect(note).toHaveAttribute('data-note', 'switched')
      expect(note).toHaveTextContent('Switched to feature/login')
      expect(status).toHaveTextContent('Switched to feature/login')
      await vi.advanceTimersByTimeAsync(4100)
      expect(screen.queryByTestId('branch-switcher-note')).toBeNull()
      expect(status).toHaveTextContent('')
    } finally {
      vi.useRealTimers()
    }
  })

  it('confirms nothing when the switch is refused', async () => {
    H.api.projectGitSwitch.mockRejectedValue(apiError('git_switch_dirty'))
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('feature/login'))
    await screen.findByTestId('branch-switcher-error')
    expect(screen.queryByTestId('branch-switcher-note')).toBeNull()
    expect(screen.getByTestId('branch-switcher-status')).toHaveTextContent('')
  })

  it('says another running session blocked the switch when the server refuses it', async () => {
    H.api.projectGitSwitch.mockRejectedValue(apiError('git_switch_session_busy'))
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('feature/login'))
    const notice = await screen.findByTestId('branch-switcher-error')
    expect(notice).toHaveTextContent("Couldn't switch to feature/login")
    expect(notice).toHaveTextContent('Not switched: another session working in this repository is still running. Wait for it to finish, then switch.')
  })

  it('names the local branch a remote row would create when the switch fails', async () => {
    H.api.projectGitSwitch.mockRejectedValue(apiError('git_switch_dirty'))
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('origin/review-fix'))
    expect(await screen.findByTestId('branch-switcher-error')).toHaveTextContent("Couldn't switch to review-fix")
  })

  it('names the branch a failed create was for', async () => {
    H.api.projectGitSwitch.mockRejectedValue(apiError('git_branch_exists'))
    mount()
    await openPicker()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'feat/new-thing' } })
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    const notice = await screen.findByTestId('branch-switcher-error')
    expect(notice).toHaveTextContent("Couldn't create branch feat/new-thing")
    expect(notice).toHaveTextContent('A branch with that name already exists.')
  })

  it('falls back to git’s own line for an uncoded failure', async () => {
    H.api.projectGitSwitch.mockRejectedValue(apiError('git_switch_failed', { detail: 'fatal: something odd' }))
    mount()
    await openPicker()
    fireEvent.mouseDown(screen.getByText('feature/login'))
    const notice = await screen.findByTestId('branch-switcher-error')
    expect(notice).toHaveTextContent("Couldn't switch to feature/login")
    expect(notice).toHaveTextContent('fatal: something odd')
  })

  it('explains a filter-driver block and disables every row', async () => {
    H.api.projectGitBranches.mockResolvedValue({ ...LIST, switchBlocked: 'filter' })
    mount()
    await openPicker()
    // Leads with the outcome and the next step; the reason follows.
    expect(screen.getByTestId('branch-switcher-blocked')).toHaveTextContent(
      'Switching is off for this repository. Use a terminal to switch. Its Git settings name a filter program that would run on checkout.')
    fireEvent.mouseDown(screen.getByText('feature/login'))
    expect(H.api.projectGitSwitch).not.toHaveBeenCalled()
  })

  it('mutes every row while a filter driver blocks switching', async () => {
    H.api.projectGitBranches.mockResolvedValue({ ...LIST, switchBlocked: 'filter' })
    mount()
    await openPicker()
    fireEvent.mouseEnter(screen.getByText('feature/login').closest('[role="option"]')!)
    for (const opt of screen.getAllByRole('option')) {
      expect(opt).toHaveAttribute('aria-disabled', 'true')
      expect(opt).toHaveAttribute('data-muted', 'true')
      expect(opt.className).toContain('opacity-50')
      // No pointer or keyboard highlight on a row that cannot be chosen.
      expect(opt.className).not.toContain('bg-bg-hover')
    }
  })

  it('keeps rows at full strength when switching is allowed', async () => {
    mount()
    await openPicker()
    const row = screen.getByText('feature/login').closest('[role="option"]')!
    fireEvent.mouseEnter(row)
    expect(row).not.toHaveAttribute('data-muted')
    expect(row.className).toContain('bg-bg-hover')
  })

  it('says above the list what a switch does and that uncommitted changes are never overwritten', async () => {
    mount()
    await openPicker()
    const hint = screen.getByTestId('branch-switcher-safety')
    expect(hint).toHaveTextContent("Switching updates this project's files to that branch. Uncommitted changes are never overwritten.")
    // Sits above the list, so it is read before any row.
    expect(hint.compareDocumentPosition(screen.getByRole('listbox')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('leaves the safety line out when a filter driver blocks switching', async () => {
    H.api.projectGitBranches.mockResolvedValue({ ...LIST, switchBlocked: 'filter' })
    mount()
    await openPicker()
    expect(screen.queryByTestId('branch-switcher-safety')).toBeNull()
  })

  it('says in words what the ahead/behind arrows count', async () => {
    H.api.projectGitBranches.mockResolvedValue({
      ...LIST,
      local: [row('main', { current: true }), row('feature/login', { ahead: 2, behind: 1 }), row('solo', { ahead: 1 })],
    })
    mount()
    await openPicker()
    const [both, one] = screen.getAllByTestId('branch-ahead-behind')
    expect(both).toHaveAttribute('title', '2 commits ahead of upstream, 1 commit behind upstream')
    expect(one).toHaveAttribute('title', '1 commit ahead of upstream')
    // The arrows are hidden from assistive tech; the words are read instead.
    expect(both.querySelector('[aria-hidden="true"]')).toHaveTextContent('↑2 ↓1')
    expect(both.querySelector('.sr-only')).toHaveTextContent('2 commits ahead of upstream, 1 commit behind upstream')
  })

  it('leaves out the Remote heading when no remote branch is listed', async () => {
    H.api.projectGitBranches.mockResolvedValue({ ...LIST, remote: [] })
    mount()
    await openPicker()
    expect(screen.getByText('Local')).toBeInTheDocument()
    expect(screen.queryByText('Remote')).toBeNull()
  })

  it('closes on Escape', async () => {
    mount()
    await openPicker()
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('branch-switcher')).toBeNull())
  })
})

describe('BranchSwitcher while switching is blocked', () => {
  let originalClipboard: PropertyDescriptor | undefined
  const stubClipboard = () => {
    originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    return writeText
  }
  afterEach(() => {
    if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
    else delete (navigator as { clipboard?: unknown }).clipboard
    originalClipboard = undefined
  })

  it('copies the branch name instead of opening the picker', async () => {
    const writeText = stubClipboard()
    mount({ switchBlocked: true })
    const trigger = screen.getByTestId('branch-switcher-trigger')
    expect(trigger).toBeEnabled()
    expect(trigger).toHaveAttribute('data-mode', 'copy')
    expect(trigger).toHaveAccessibleName('Copy branch name main')
    expect(trigger).toHaveAccessibleDescription('Copy branch name. Stop the current response to switch branch.')
    // The reason is drawn visibly, not left to a delayed native tooltip.
    expect(trigger).not.toHaveAttribute('title')
    expect(trigger).not.toHaveAttribute('aria-haspopup')
    fireEvent.click(trigger)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('main'))
    await waitFor(() => expect(trigger).toHaveAccessibleName('Copied branch name main'))
    // Touch has no hover, so the copy itself shows how to switch.
    expect(screen.getByTestId('branch-switcher-note')).toHaveTextContent('Copied. Stop the current response to switch branch.')
    expect(screen.queryByTestId('branch-switcher')).toBeNull()
    expect(H.api.projectGitBranches).not.toHaveBeenCalled()
  })

  it('calls a detached HEAD a commit', async () => {
    const writeText = stubClipboard()
    mount({ switchBlocked: true, detached: true, branch: 'a1b2c3d' })
    const trigger = screen.getByTestId('branch-switcher-trigger')
    expect(trigger).toHaveAccessibleName('Copy commit a1b2c3d')
    expect(trigger).toHaveAccessibleDescription('Copy commit. Stop the current response to switch branch.')
    fireEvent.click(trigger)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('a1b2c3d'))
  })

  it('shows the reason visibly on hover and on keyboard focus', () => {
    mount({ switchBlocked: true })
    const trigger = screen.getByTestId('branch-switcher-trigger')
    expect(screen.queryByTestId('branch-switcher-note')).toBeNull()
    fireEvent.mouseEnter(trigger)
    expect(screen.getByTestId('branch-switcher-note')).toHaveTextContent('Copy branch name. Stop the current response to switch branch.')
    expect(screen.getByTestId('branch-switcher-note')).toHaveAttribute('data-note', 'busy')
    fireEvent.mouseLeave(trigger)
    expect(screen.queryByTestId('branch-switcher-note')).toBeNull()
    fireEvent.focus(trigger)
    expect(screen.getByTestId('branch-switcher-note')).toBeInTheDocument()
    fireEvent.blur(trigger)
    expect(screen.queryByTestId('branch-switcher-note')).toBeNull()
  })

  it('shows no busy note while switching is allowed', () => {
    mount()
    fireEvent.mouseEnter(screen.getByTestId('branch-switcher-trigger'))
    expect(screen.queryByTestId('branch-switcher-note')).toBeNull()
  })

  it('withholds the confirmation and shows an error notice when the copy fails', async () => {
    originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    const writeText = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const originalExecCommand = Object.getOwnPropertyDescriptor(document, 'execCommand')
    const execCommand = vi.fn().mockReturnValue(false)
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })
    try {
      mount({ switchBlocked: true })
      const trigger = screen.getByTestId('branch-switcher-trigger')
      fireEvent.click(trigger)
      await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'))
      expect(trigger).toHaveAccessibleName('Copy branch name main')
      const notice = await screen.findByTestId('branch-switcher-copy-error')
      expect(notice).toHaveAttribute('role', 'alert')
      expect(notice).toHaveTextContent("Couldn't copy main to the clipboard.")
      // No agent hand-off: it would move the chat away from the composer draft.
      expect(notice.querySelector('a, button')).toBeNull()
      expect(screen.getByTestId('branch-switcher-note')).toHaveAttribute('data-note', 'copy_failed')
    } finally {
      if (originalExecCommand) Object.defineProperty(document, 'execCommand', originalExecCommand)
      else delete (document as { execCommand?: unknown }).execCommand
    }
  })

  it('clears the copy failure on its own', async () => {
    originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) }, configurable: true })
    const originalExecCommand = Object.getOwnPropertyDescriptor(document, 'execCommand')
    Object.defineProperty(document, 'execCommand', { value: vi.fn().mockReturnValue(false), configurable: true })
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      mount({ switchBlocked: true })
      fireEvent.click(screen.getByTestId('branch-switcher-trigger'))
      await screen.findByTestId('branch-switcher-copy-error')
      await vi.advanceTimersByTimeAsync(8100)
      expect(screen.queryByTestId('branch-switcher-copy-error')).toBeNull()
    } finally {
      vi.useRealTimers()
      if (originalExecCommand) Object.defineProperty(document, 'execCommand', originalExecCommand)
      else delete (document as { execCommand?: unknown }).execCommand
    }
  })

  it('opens the picker again once switching is allowed', async () => {
    mount()
    const trigger = screen.getByTestId('branch-switcher-trigger')
    expect(trigger).toHaveAttribute('data-mode', 'switch')
    expect(trigger).toHaveAccessibleName('Switch branch (current: main)')
    await openPicker()
    expect(screen.getByTestId('branch-switcher')).toBeInTheDocument()
  })
})

describe('isValidNewBranchName', () => {
  it.each(['main', 'feat/x', 'release-1.2', 'a_b'])('accepts %s', name => {
    expect(isValidNewBranchName(name)).toBe(true)
  })
  it.each(['', '-x', 'a..b', 'x.lock', 'HEAD', 'feat/', 'a b', 'feat/CON', 'a~1'])('rejects %s', name => {
    expect(isValidNewBranchName(name)).toBe(false)
  })
})

describe('BranchSwitcher keyboard start', () => {
  it('highlights the first switchable row, not the checked-out branch', async () => {
    mount()
    await openPicker()
    const selected = screen.getAllByRole('option').find(el => el.getAttribute('aria-selected') === 'true')
    expect(selected).toHaveTextContent('feature/login')
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'Enter' })
    await waitFor(() => expect(H.api.projectGitSwitch).toHaveBeenCalledWith({ path: PROJECT, branch: 'feature/login' }))
  })
})
