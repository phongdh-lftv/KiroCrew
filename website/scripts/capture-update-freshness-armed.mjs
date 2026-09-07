/**
 * Evidence capture for the update-freshness PR's one new user-visible surface:
 * the armed panel in Settings > About, in both themes.
 *
 * The scene is the whole point of the change. The panel's offer comes from a
 * verdict up to 12 hours old; arming re-checks the feed so the approval installs
 * the NEWEST build. When a release published in between, the button the user just
 * clicked and the panel they are looking at name different versions — so the
 * capture drives exactly that: offer v0.4.7, arm answers v0.4.8.
 *
 * Two frames per theme, because the offer is the only thing that makes the armed
 * frame legible:
 *   offer-<theme>.png  — "Update to v0.4.7"
 *   armed-<theme>.png  — the armed version line (v0.4.8) AND the note explaining
 *                        why the number moved
 *
 * Runs against a Vite dev server with every /api/* request answered inline —
 * gateway-free. Copy of the technique in capture-version-display-fold.mjs.
 *
 * Usage:
 *   npx vite --port 5199 &            # or `npm run dev -- --port 5199`
 *   node scripts/capture-update-freshness-armed.mjs [viteBase] [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const base = process.argv[2] || 'http://127.0.0.1:5199'
const outDir = process.argv[3] || '../temp-screenshots/update-freshness'
mkdirSync(outDir, { recursive: true })

const OFFERED = '0.4.7' // what the stale verdict advertises
const ARMED = '0.4.8' // what the arm's own re-check found, and what installs

// Escape every regex metacharacter, not just the dots a version string happens to
// carry: a partial escape leaves a backslash in the input able to change what the
// pattern matches, so the button lookup could silently miss and the capture would
// screenshot the wrong panel.
const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

// Read the copy from the CATALOG, so a key rename fails the capture loudly
// instead of silently screenshotting a panel missing the line under test.
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const about = manual.pages.settings.aboutPanel
for (const key of ['update_to_version', 'armed_version_changed', 'armed_run_on_host']) {
  if (!about[key]) throw new Error(`catalog key ${key} missing from en.manual.json — renamed?`)
}

const STATUS = {
  uptime: '2h', start_time: 0, sessions: 1, messages: 3, cron_jobs: 0,
  lessons: 0, subagents: 0, no_crons: false, branch: '', commit: '',
  release_channel: 'stable', version: '0.4.6', version_display: '0.4.6',
  update_available: true, update_can_apply: false, update_can_arm: true,
  update_check_status: 'succeeded', update_command: 'kirocrew update',
  update_latest_version: OFFERED, update_latest_version_display: OFFERED,
  update_channel: 'stable', update_managed_by: 'kirocrew',
  update_commits_ahead: 0, update_commits_behind: 0,
}

const browser = await chromium.launch()

async function scene(theme) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 2 })
  await ctx.addInitScript(mode => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-theme', mode)
  }, theme)
  const p = await ctx.newPage()
  await p.route('**/*', route => {
    const u = new URL(route.request().url())
    if (!u.pathname.startsWith('/api/')) return route.continue()
    const json = body => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (u.pathname === '/api/status') return json(STATUS)
    if (u.pathname === '/api/update/arm') {
      // The re-check the PR adds: the gateway arms what the feed serves NOW.
      return json({
        ok: true, armed: true, request_id: 'r1',
        version: ARMED, version_display: ARMED,
        expires_in: 600, approve_command: 'kirocrew update approve r1',
      })
    }
    if (u.pathname.startsWith('/api/update/check')) {
      return json({
        check_status: 'succeeded', update_available: true, error_code: null,
        latest_version: OFFERED, latest_version_display: OFFERED,
        channel: 'stable', managed_by: 'kirocrew', can_apply: false, can_arm: true,
        update_command: 'kirocrew update', current_version: '0.4.6',
        commits_ahead: 0, commits_behind: 0,
      })
    }
    if (u.pathname.startsWith('/api/changelog')) return json({ content: '' })
    if (u.pathname.startsWith('/api/models')) return json({ models: [] })
    if (u.pathname.startsWith('/api/instances')) return json({ active: false, instances: [], warm_set_cap: 0, sso: {} })
    if (u.pathname.startsWith('/api/kiro-prerequisite')) return json({ ready: true, initial_setup_complete: true, setup_allowed: true })
    // List-shaped endpoints crash the app when handed `{}` (e.g.
    // pendingApprovals.filter): answer every array consumer with [].
    if (/approvals|sessions|crons|lessons|skills|notifications|artifacts|apps\b/.test(u.pathname)) return json([])
    return json({})
  })

  await p.goto(`${base}/settings/about`, { waitUntil: 'networkidle' })
  // The proactive update popup opens over the page; dismiss it to reach About.
  const remind = p.getByRole('button', { name: /remind me tomorrow/i }).first()
  if (await remind.isVisible().catch(() => false)) {
    await remind.click()
    await p.waitForTimeout(500)
  }

  const offer = p.getByTestId('in-app-update')
  await offer.waitFor({ state: 'visible', timeout: 20_000 })
  await offer.scrollIntoViewIfNeeded()
  await p.waitForTimeout(300)
  await p.screenshot({ path: `${outDir}/offer-${theme}.png` })
  console.log(`captured ${outDir}/offer-${theme}.png`)

  await p.getByRole('button', { name: new RegExp(`update to v${escapeRe(OFFERED)}`, 'i') }).click()
  const armed = p.getByTestId('in-app-update-armed')
  await armed.waitFor({ state: 'visible', timeout: 20_000 })
  // Fail loudly rather than shipping a frame that does not show the fix.
  await p.getByTestId('armed-version-changed').waitFor({ state: 'visible', timeout: 10_000 })
  await armed.scrollIntoViewIfNeeded()
  await p.waitForTimeout(300)
  await p.screenshot({ path: `${outDir}/armed-${theme}.png` })
  console.log(`captured ${outDir}/armed-${theme}.png`)

  await ctx.close()
}

await scene('light')
await scene('dark')
await browser.close()
