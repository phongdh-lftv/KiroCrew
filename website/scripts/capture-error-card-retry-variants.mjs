/**
 * Screenshot runner for capture/error-card-retry-variants.html (#9932 evidence).
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6830 --strictPort
 *   node scripts/capture-error-card-retry-variants.mjs http://127.0.0.1:6830 <outdir>
 *
 * Frames every gateway "please retry" row as the real ErrorCard re-speaks it,
 * in both themes. Before framing it asserts, per variant, that the card text
 * carries the Resume verb and NOT the word "retry" — so a frame cannot
 * photograph a banner that still contradicts the button beside it — and that
 * the unknown-string row is rendered verbatim (the fallback still holds).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6830'
const OUT = process.argv[3] || '../temp-screenshots/resume-vs-continue'
mkdirSync(OUT, { recursive: true })

const RESUME_VARIANTS = [
  'connection lost',
  'connection lost + exit code',
  'session busy',
  'turn stalled',
  'tool stalled',
  'backend hiccup',
  'settled',
]

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 780, height: 1100 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/error-card-retry-variants.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    await page.locator('[data-testid="capture-root"]').waitFor({ timeout: 10000 })
    for (const v of RESUME_VARIANTS) {
      const card = page.locator(`[data-variant="${v}"] [data-testid="error-card"]`)
      await card.waitFor({ timeout: 10000 })
      const text = (await card.innerText()).replace(/\s+/g, ' ')
      if (!/resume/i.test(text)) throw new Error(`${v}: banner lacks the Resume verb — "${text}"`)
      if (/retry/i.test(text)) throw new Error(`${v}: banner still says "retry" — "${text}"`)
      if (v === 'connection lost + exit code' && !text.includes('(exit 1)')) {
        throw new Error(`${v}: exit-code detail lost — "${text}"`)
      }
      const button = page.locator(`[data-variant="${v}"] [data-testid="error-card-continue"]`)
      const wantButton = v !== 'settled'
      if ((await button.count()) !== (wantButton ? 1 : 0)) {
        throw new Error(`${v}: expected ${wantButton ? 'a' : 'no'} Resume button`)
      }
    }
    const unknown = page.locator('[data-variant="unknown"] [data-testid="error-card"]')
    const unknownText = (await unknown.innerText()).replace(/\s+/g, ' ')
    if (!unknownText.includes('⟳ Something this build has no copy for — please retry.')) {
      throw new Error(`unknown-string fallback broken — "${unknownText}"`)
    }
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-testid="capture-root"]').screenshot({
      path: `${OUT}/error-card-retry-variants-${theme}.png`,
    })
    console.log(`${theme}: ${RESUME_VARIANTS.length} variants speak Resume, fallback verbatim — OK`)
  } catch (e) {
    console.error(`${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
