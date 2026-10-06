/**
 * Chat with a headless remote crew, from the crew's own row under Remote Crew.
 *
 * A remote Kiro Crew GATEWAY gets embedded as its own dashboard. A headless
 * crew -- the Fargate lane and the Lambda MicroVM lane both run one -- has no
 * dashboard to embed: it serves a single turn route and nothing else. Until now
 * the row offered a copyable URL and the user was on their own with curl.
 *
 * WHAT THIS DOES NOT HOLD
 * The crew's control secret never reaches this component. The browser posts
 * same-origin to `POST /api/instances/{id}/crew-turn` with its own dashboard
 * token; the gateway reads the crew's secret from the owner's Secrets Manager
 * and is the one that talks to the crew. So there is no secret in a prop, in
 * component state, or in a network tab here -- which is also why there is no
 * "paste your secret" field.
 *
 * THE THREAD ID IS THE CONVERSATION
 * The crew serializes per slot and restores that slot's transcript before each
 * turn, so sending the same thread id after a suspend and resume CONTINUES the
 * conversation rather than starting one. It is generated once per mounted row
 * and shown, because "same thread" is the property a user checks after a
 * suspend, and a hidden id cannot be checked.
 */

import { Loader2, Send } from 'lucide-react'
import { useCallback, useMemo, useRef, useState } from 'react'

import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'

type Role = 'user' | 'assistant'

interface Turn {
  role: Role
  text: string
  /** Set while the assistant's text is still arriving. */
  streaming?: boolean
}

/** One SSE `data:` payload from the crew, reduced to the text it adds.
 *
 * The crew answers OpenAI-shaped chunks, so a frame's new text is at
 * `choices[0].delta.content`. Anything else in a frame is ignored rather than
 * rendered: a frame this function does not understand must not become visible
 * noise in a conversation.
 */
function deltaText(frame: string): string {
  if (!frame || frame === '[DONE]') return ''
  try {
    const obj = JSON.parse(frame) as {
      choices?: { delta?: { content?: string }; message?: { content?: string } }[]
      error?: { code?: string }
    }
    if (obj.error) return ''
    const choice = obj.choices?.[0]
    return choice?.delta?.content ?? choice?.message?.content ?? ''
  } catch {
    return ''
  }
}

/** The error code a frame carries, if it carries one. */
function frameError(frame: string): string {
  try {
    const obj = JSON.parse(frame) as { error?: { code?: string; detail?: string } }
    if (!obj.error) return ''
    // The CODE first, for the reason the refusal path gives: a frame carries
    // prose too, and reading it first means `readable` never matches a code and
    // every error arrives as the same generic line.
    return obj.error.code || obj.error.detail || 'crew_error'
  } catch {
    return ''
  }
}

/**
 * A sentence for a code the gateway sends, or the code itself.
 *
 * A bare `crew_unreachable` or `HTTP 502` tells a reader that something failed
 * and nothing about what to do next, and this is the one notice they get on
 * every failed send. The known codes are the ones this pane's own route
 * produces, so each has a next step; anything else falls through unchanged
 * rather than being flattened into a generic line that hides a real detail.
 */
function readable(detail: string): string {
  const unreachable = i18nT('pages.settings.headlessCrewChat.unreachable')
  const known: Record<string, string> = {
    crew_unreachable: unreachable,
    crew_error: unreachable,
    crew_refused: unreachable,
    crew_forbidden: unreachable,
    crew_not_connected: i18nT('pages.settings.headlessCrewChat.connect_first'),
    instance_unknown: i18nT('pages.settings.headlessCrewChat.connect_first'),
    instances_unavailable: unreachable,
    crew_secret_unavailable: i18nT('pages.settings.headlessCrewChat.secret_unavailable'),
    control_forbidden: i18nT('pages.settings.headlessCrewChat.secret_unavailable'),
    // A suspended crew the gateway could not wake. Its own line, because the next
    // step differs from an unreachable one: the crew exists and is reachable, it
    // just did not come back, so retrying is worth something here.
    crew_resume_failed: i18nT('pages.settings.headlessCrewChat.resume_failed'),
    // The reader's OWN input, and the only code in this map they can act on
    // directly. Flattening it into "the crew did not answer" would send them
    // looking at the crew for a limit on their message.
    message_too_large: i18nT('pages.settings.headlessCrewChat.message_too_large'),
    bad_request: unreachable,
  }
  // The FALLBACK is the one that says what to do, not the raw value. A code this
  // map does not know, or a bare `HTTP 502`, tells a reader that something failed
  // and nothing else -- and this is the only notice they get on a failed send.
  return known[detail] ?? unreachable
}

export function HeadlessCrewChat({
  instanceId,
  crewName,
  connected,
}: {
  instanceId: string
  crewName: string
  connected: boolean
}) {
  // One thread per mounted row. Deliberately NOT persisted: a thread id that
  // outlived the crew would address a conversation the crew no longer holds, and
  // the user would read an empty reply as the crew having forgotten them.
  // Built from the INSTANCE ID, not the display name, and reduced to the
  // characters an agent name may carry. A crew is registered as
  // `Kiro Crew Cloud (<tag>)` and any crew can be renamed with a space, so a
  // display name in the slot id is an id the crew backend refuses outright --
  // its `_AGENT_NAME_RE` allows no spaces or brackets and caps the length -- and
  // every turn comes back 400.
  const thread = useMemo(() => {
    const safe = instanceId.replace(/[^A-Za-z0-9-]+/g, '-').replace(/^-+|-+$/g, '')
    const suffix = Math.random().toString(36).slice(2, 10)
    return `dashboard-${safe.slice(0, 48) || 'crew'}-${suffix}`
  }, [instanceId])
  const [turns, setTurns] = useState<Turn[]>([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const log = useRef<HTMLDivElement | null>(null)

  const scrollDown = useCallback(() => {
    const el = log.current
    if (el) el.scrollTop = el.scrollHeight
  }, [])

  const send = useCallback(async () => {
    const message = draft.trim()
    if (!message || busy) return
    setDraft('')
    setError('')
    setBusy(true)
    setTurns(prev => [...prev, { role: 'user', text: message }, { role: 'assistant', text: '', streaming: true }])
    const appendToLast = (chunk: string) => {
      setTurns(prev => {
        const next = prev.slice()
        const last = next[next.length - 1]
        if (last && last.role === 'assistant') next[next.length - 1] = { ...last, text: last.text + chunk }
        return next
      })
      scrollDown()
    }
    try {
      const resp = await fetch(`/api/instances/${encodeURIComponent(instanceId)}/crew-turn`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ thread, message, stream: true }),
      })
      if (!resp.ok || !resp.body) {
        // A refusal arrives as JSON before the stream starts, so its own detail
        // is the most useful thing to show -- it says which step is incomplete
        // (not connected, secret unreadable) rather than "it failed".
        let detail = `HTTP ${resp.status}`
        let code = ''
        try {
          const body = (await resp.json()) as { detail?: string; code?: string }
          code = body.code || ''
          detail = body.detail || code || detail
        } catch {
          /* keep the status */
        }
        // The CODE first. Every refusal body carries prose as well, so reading
        // the detail first means the code map never matches and `connect first`
        // and `credential unreadable` both arrive as the generic unreachable
        // line -- losing exactly the next step the reader needs.
        setError(readable(code || detail))
        setTurns(prev => prev.slice(0, -1))
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let sawText = false
      let sawError = false
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // SSE frames are separated by a blank line. Hold an incomplete tail.
        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''
        for (const part of parts) {
          for (const line of part.split('\n')) {
            if (!line.startsWith('data:')) continue
            const frame = line.slice(5).trim()
            const failed = frameError(frame)
            if (failed) {
              setError(readable(failed))
              sawError = true
              continue
            }
            const text = deltaText(frame)
            if (text) {
              sawText = true
              appendToLast(text)
            }
          }
        }
      }
      if (!sawText && !sawError) {
        // An empty answer is not a successful turn. Said plainly rather than
        // leaving a blank assistant bubble, which reads as the crew ignoring the
        // message.
        //
        // Skipped when an error FRAME already arrived: the stream carries the
        // error and then `[DONE]`, so text was never seen -- and replacing a real
        // reason with "the reply carried no text" is strictly less information.
        setError(i18nT('pages.settings.headlessCrewChat.empty_reply'))
      }
    } catch {
      setError(i18nT('pages.settings.headlessCrewChat.unreachable'))
    } finally {
      setBusy(false)
      setTurns(prev => {
        const next = prev.slice()
        const last = next[next.length - 1]
        if (last && last.streaming) next[next.length - 1] = { ...last, streaming: false }
        return next
      })
      scrollDown()
    }
  }, [busy, draft, instanceId, scrollDown, thread])

  return (
    <div className="mt-3" data-testid="headless-crew-chat">
      <div className="flex items-baseline justify-between mb-1">
        <div className="text-[11px] uppercase tracking-[.08em] text-muted">
          {i18nT('pages.settings.headlessCrewChat.title')}
        </div>
        {/* The thread id, shown because "the same thread survived the suspend" is
            what a user verifies, and an invisible id cannot be verified. LABELLED,
            because an unlabelled internal string is one a reader cannot place: it
            looks like a fault code rather than the thing that proves continuity. */}
        <code
          className="font-mono text-[11px] text-muted"
          title={i18nT('pages.settings.headlessCrewChat.thread_label')}
          data-testid="headless-crew-thread"
        >
          {i18nT('pages.settings.headlessCrewChat.thread_label')}: {thread}
        </code>
      </div>
      <div
        ref={log}
        className="bg-bg-elevated border border-border rounded-md p-3 max-h-64 overflow-y-auto space-y-2"
        data-testid="headless-crew-log"
      >
        {turns.length === 0 ? (
          <p className="text-[12px] text-muted">
            {i18nT('pages.settings.headlessCrewChat.empty_state', { name: crewName })}
          </p>
        ) : (
          turns.map((t, i) => (
            <div key={i} className="text-[13px]" data-testid={`headless-crew-turn-${t.role}`}>
              <span className="text-[11px] uppercase tracking-[.08em] text-muted mr-2">
                {t.role === 'user'
                  ? i18nT('pages.settings.headlessCrewChat.you')
                  : crewName}
              </span>
              <span className="whitespace-pre-wrap text-card-fg">{t.text}</span>
              {t.streaming ? (
                <Loader2 size={12} className="inline-block ml-1 animate-spin text-muted" />
              ) : null}
            </div>
          ))
        )}
      </div>
      {error ? (
        <ErrorNotice
          variant="inline"
          className="mt-1.5"
          message={error}
          // askAgent ON: a failed turn destroys nothing -- the message was not
          // delivered and nothing was written -- so the hand-off is safe, and a
          // crew that will not answer is exactly the case a reader cannot
          // diagnose from this pane alone.
          askAgent
          testId="headless-crew-chat-error"
        />
      ) : null}
      <div className="flex items-center gap-2 mt-2">
        <input
          className="flex-1 min-w-0 bg-bg-elevated border border-border rounded-md px-3 py-1.5 text-[13px]"
          value={draft}
          disabled={!connected || busy}
          placeholder={
            connected
              ? i18nT('pages.settings.headlessCrewChat.placeholder')
              : i18nT('pages.settings.headlessCrewChat.connect_first')
          }
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void send()
            }
          }}
          data-testid="headless-crew-input"
          aria-label={i18nT('pages.settings.headlessCrewChat.placeholder')}
        />
        <button
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-border text-[13px] disabled:opacity-50"
          disabled={!connected || busy || !draft.trim()}
          onClick={() => void send()}
          data-testid="headless-crew-send"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
          {i18nT('pages.settings.headlessCrewChat.send')}
        </button>
      </div>
    </div>
  )
}
