/** Isolated capture entry for the crew webview's DOCKED mint-failure state.
 *
 * WHY ISOLATED: the docked card renders its own `ErrorNotice` (the shipped
 * `webview_docked_error` string plus the shared "Ask the agent" hand-off and a
 * "Try again" control) when the panel read succeeds -- so the card is up -- but
 * the docked document mint fails. UX review asks for a screenshot of exactly
 * this state; no existing capture entry drives it (the docked summary entry
 * stays on the healthy path and the error entry shows the EXPANDED failure).
 *
 * WHAT IS FAITHFUL: the real `CrewWebview`, its real React Query read through
 * the real `api.memberPanel` layer, the real `useSandboxDoc` mint, the real
 * `ErrorNotice`, and the real `webview_docked_error` / `webview_retry` catalog
 * strings. Only the fetch boundary is stubbed: the panel read answers from a
 * fixture carrying `docked_height` so the docked frame is attempted, and the
 * `/api/sandbox-doc` mint answers HTTP 500 so the docked failure branch (with
 * no prior URL) is the one that renders.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import CrewWebview from '../src/pages/members/CrewWebview'
import { PANEL_HTML, PANEL_META } from './crewWebviewFixture'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLUG = 'research'
const MEMBER = 'research'

const json = (body: unknown, status = 200) =>
  Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )

/** The panel read carries a `docked_height`, so the card tries to mint a docked
 *  frame; the mint answers HTTP 500, so `useSandboxDoc` fails with no live URL
 *  and the `crew-webview-docked-error` notice renders. Every other read answers
 *  empty so no code path hangs on an absent gateway. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  if (url.includes('/api/sandbox-doc')) return json({ error: 'mint unavailable' }, 500)
  if (url.includes('/panel'))
    return json({ html: PANEL_HTML, panel: { ...PANEL_META, docked_height: 320 } })
  return json({})
}

// retry:false so the mint error branch renders promptly for the camera rather
// than after React Query's default back-off ladder.
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  return (
    <div
      style={{
        width: 320,
        height: '100vh',
        marginLeft: 'auto',
        borderLeft: '1px solid var(--border)',
        display: 'flex',
        flexDirection: 'column',
        padding: 16,
      }}
      className="bg-bg text-text"
    >
      <div className="text-[11px] font-semibold tracking-wide text-muted mb-1.5">
        Research crew
      </div>
      <CrewWebview slug={SLUG} member={MEMBER} />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
