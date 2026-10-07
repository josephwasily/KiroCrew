import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { INHERIT_MODEL } from '../../components/crew/useCrewEditor'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import { modelSupportsEffort } from '../../lib/effort'
import { useAppDispatch } from '../../store'
import { changeApprovalMode } from '../../store/dashboardSlice'
import { EffortField, Field, ModelField } from '../KiroCrewAgentsPage'

/** The approval modes a crewmate's record may pin, in label order; YOLO is process-global. */
const MODES = ['normal', 'trust_reads', 'trust'] as const
type Setting = 'approval_mode' | 'model' | 'reasoning_effort'

/**
 * The crewmate's own settings on its profile card: permission, model and
 * effort. They live here, not in the chat composer. Each pick writes the crew
 * record (what every later thread opens with) and then the live DM slot, so the
 * open thread follows at once instead of keeping an older per-session pick.
 * An unset permission reads as Trust, the default the thread opens with.
 */
export default function CrewProfileSettings({ member, slotKey }: { member: MemberRosterRow; slotKey?: string | null }) {
  const { t } = useTranslation()
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Partial<Record<Setting, string>>>({})
  const [error, setError] = useState('')
  const models = useAvailableModelsQuery({ enabled: true })
  const { data: resolved } = useQuery({
    queryKey: ['agent-resolved-model', member.name],
    queryFn: () => api.agentResolvedModel(member.name),
  })
  const stored = {
    approval_mode: String(member.approval_mode || 'trust'),
    model: member.model || INHERIT_MODEL,
    reasoning_effort: String(member.reasoning_effort || ''),
  }
  const value = (k: Setting) => draft[k] ?? stored[k]
  const model = value('model')
  const effortCapable = modelSupportsEffort(model === INHERIT_MODEL ? resolved?.model : model)
  const modelOptions = [INHERIT_MODEL, ...(models.data || []).map((m) => m.name).filter((n) => n && n !== INHERIT_MODEL)]

  const save = async (key: Setting, next: string) => {
    setDraft((d) => ({ ...d, [key]: next }))
    setError('')
    // The record spells "inherit" as ''; the picker spells it INHERIT_MODEL.
    const wire = key === 'model' && next === INHERIT_MODEL ? '' : next
    try {
      await api.updateKirocrewAgent(member.name, { [key]: wire })
      if (slotKey && key === 'approval_mode') await dispatch(changeApprovalMode({ mode: wire, slot: slotKey })).unwrap()
      else if (slotKey && key === 'model') await api.chatSlotModel(slotKey, wire)
      else if (slotKey) await api.chatSlotReasoningEffort(slotKey, wire)
    } catch (e) {
      setDraft((d) => ({ ...d, [key]: undefined }))
      setError(e instanceof Error ? e.message : String((e as { message?: string })?.message ?? e))
    } finally {
      void queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: ['agent-resolved-model', member.name] })
      void queryClient.invalidateQueries({ queryKey: ['kirocrew-agents'] })
    }
  }

  return (
    <section className="rounded-2xl border border-border bg-bg px-3.5 py-3 flex flex-col gap-3" data-testid="crew-profile-settings">
      <Field label={t('pages.membersPage.profile_permissions')}>
        <SimpleSelect
          options={[...MODES]}
          optionLabels={[t('components.approvalModePicker.normal_label'), t('components.approvalModePicker.reads_label'), t('components.approvalModePicker.trust_label')]}
          value={value('approval_mode')}
          onChange={(v) => void save('approval_mode', v)}
          aria-label={t('pages.membersPage.profile_permissions')}
        />
      </Field>
      <ModelField options={modelOptions} value={model} onChange={(v) => void save('model', v)} />
      {(effortCapable || !!value('reasoning_effort')) && (
        <EffortField value={value('reasoning_effort')} onChange={(v) => void save('reasoning_effort', v)} />
      )}
      <ErrorNotice message={error || null} testId="crew-profile-settings-error" />
    </section>
  )
}
