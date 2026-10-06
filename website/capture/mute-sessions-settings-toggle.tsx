/**
 * Isolated capture entry for the "mute sessions opened by other sessions" toggle
 * this PR adds to Settings > Notifications, plus its save-failure notice.
 *
 * WHY ISOLATED: the delta is one new SettingsToggle on NotificationsPanel (and,
 * when a save is rejected, an ErrorNotice beneath it); the rest of the panel
 * (sound presets, per-category rows, the ChannelsSection that fetches sources
 * against a live gateway) is unchanged and would only add noise. So this mounts
 * the REAL `SettingsSection` / `SettingsCard` / `SettingsToggle` / `ErrorNotice`
 * with the SAME i18n keys and the SAME `label`/`hint` the shipped panel passes
 * (NotificationsPanel.tsx), and nothing else.
 *
 * Panes are STACKED (not side by side) so a wide hint never clips. Three states:
 * OFF (the default), ON (after enabling), and SAVE-FAILED (localStorage blocked:
 * the toggle stays on its prior value and the `askAgent` ErrorNotice shows the
 * failure instead of a silent no-op). `?theme=dark|light` selects the theme.
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import { SettingsSection, SettingsCard, SettingsToggle } from '../src/components/settings'
import ErrorNotice from '../src/components/ErrorNotice'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

/** The exact toggle this PR adds to NotificationsPanel, rendered with a fixed
 *  `checked` so each pane is one deterministic state. When `failed`, the same
 *  ErrorNotice the panel shows on a rejected save is rendered beneath it. */
function MuteToggle({ checked, failed = false }: { checked: boolean; failed?: boolean }) {
  return (
    <SettingsSection title={i18nT('pages.settings.notificationsPanel.desktop_alerts')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.notificationsPanel.mute_sessions_opened_by_other_sessions')}
          hint={i18nT('pages.settings.notificationsPanel.mute_sessions_opened_by_other_sessions_description')}
          checked={checked}
          onChange={() => {}}
        />
        {failed && (
          <ErrorNotice
            message={i18nT('pages.settings.notificationsPanel.mute_sessions_opened_save_failed')}
            askAgent
          />
        )}
      </SettingsCard>
    </SettingsSection>
  )
}

function Pane({ checked, failed, label, caption }: { checked: boolean; failed?: boolean; label: string; caption: string }) {
  return (
    <div className="flex flex-col gap-2 w-[560px]" data-capture-pane>
      <div className="text-text text-[13px] font-medium">{label}</div>
      <div className="text-muted text-[11px] leading-snug">{caption}</div>
      <MuteToggle checked={checked} failed={failed} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <div className="bg-bg p-6 flex flex-col gap-8 items-start min-h-screen" data-capture-root>
    <Pane
      checked={false}
      label="Default — off"
      caption="Sessions another session opened keep their chime, toast and unread badge."
    />
    <Pane
      checked
      label="Enabled"
      caption="Any session opened by another session (conductor workers, and also cron, app and import sessions) is silenced for attention; the opener keeps its signals and approvals still notify."
    />
    <Pane
      checked={false}
      failed
      label="Save failed (local storage blocked)"
      caption="The toggle keeps its prior value and the failure is surfaced through ErrorNotice, instead of a silent no-op where the switch and the attention gate could disagree."
    />
  </div>,
)
