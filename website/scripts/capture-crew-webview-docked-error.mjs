/**
 * Isolated capture for the crew webview's DOCKED mint-failure notice.
 *
 * WHY THIS EXISTS: the docked card renders the shared `ErrorNotice` with the
 * shipped `webview_docked_error` string and the "Ask the agent" hand-off when
 * its document mint fails. UX review asks for a screenshot of that exact state
 * and no script produced one -- the docked summary script stays on the healthy
 * path and the error script shoots the EXPANDED failure. This drives the
 * ISOLATED capture entry (website/capture/crew-webview-docked-error.html),
 * where the panel read succeeds and the mint answers HTTP 500, so the shot
 * shows the docked notice AND the `crew-webview-docked-retry` "Try again"
 * control.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6838 --strictPort    # in another shell (website/)
 *   node scripts/capture-crew-webview-docked-error.mjs http://127.0.0.1:6838 ../temp-screenshots/crew-webview
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6838'
const OUT = process.argv[3] || '../temp-screenshots/crew-webview'
mkdirSync(OUT, { recursive: true })

// A right-dock-sized frame: the width the drawer occupies beside the members list.
const VIEWPORT = { width: 360, height: 480 }

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 2 })
page.on('pageerror', e => {
  console.error('pageerror:', e.message)
  failures++
})
await page.goto(`${BASE}/capture/crew-webview-docked-error.html?theme=dark`, {
  waitUntil: 'networkidle',
})

const notice = page.locator('[data-testid="crew-webview-docked-error"]')
await notice.waitFor({ state: 'visible', timeout: 15000 })
check('the docked mint-failure notice shows', await notice.isVisible())

// Through the shared error surface, so it carries role=alert and the hand-off.
check('the notice is an alert', (await notice.getAttribute('role')) === 'alert')
check(
  'the notice offers the hand-off and retry',
  (await notice.locator('button, a').count()) >= 1 &&
    (await page.locator('[data-testid="crew-webview-docked-retry"]').count()) === 1,
)

await page.waitForTimeout(150)
await page.screenshot({ path: `${OUT}/05-docked-error-state.png` })

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}/05-docked-error-state.png`)
