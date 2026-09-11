/**
 * Screenshot + video probe for empty-folder auto-collapse in the chat sidebar.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend). The fixture is the shape that
 * motivated the change: one working subfolder plus thirteen empty sibling
 * subfolders, each of which used to render its own "New chat in <name>"
 * placeholder row and push the one real session off the top of the list.
 *
 * Frames written, per run:
 *   <prefix>-01-empty-folders     list view, the whole tree at rest
 *   <prefix>-02-clicked-open      list view, one empty folder clicked open
 *   <prefix>-03-board             board (tag-columns) view, same fixture
 *   <prefix>-04-open-close.webm   list view, the open/close transition
 *
 * The just-created-folder exemption is NOT shot here: making a folder arrive
 * after the first load means driving the create dialog, since this harness has
 * no gateway to push one. It is covered by tests in both views instead
 * ("exempts a folder that appears after the first load" in
 * ChatSidebar.emptyFolderAutoCollapse.test.tsx, and "leaves a just-created
 * empty folder open in its columns" in ChatSidebar.boardFolderCollapse.test.tsx).
 *
 * The point is the delta, so run it against this branch (after) and against
 * origin/main (before):
 *   node scripts/capture-empty-folder-collapse.mjs ../temp-screenshots/empty-folder-collapse after
 *
 * Usage: node scripts/capture-empty-folder-collapse.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, rmSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/empty-folder-collapse'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const ROOT = 'pipeline'
const EMPTY_NAMES = [
  'agents', 'cron', 'security', 'core', 'apps', 'skills', 'packaging',
  'channels', 'gateway', 'area-cron', 'area-agents', 'area-core', 'area-tests',
]
const baseFolders = [
  { id: ROOT, name: 'pipeline-issue-fix', order: 0, collapsed: false },
  { id: 'dashboard', name: 'dashboard', order: 0, collapsed: false, parent_id: ROOT },
  ...EMPTY_NAMES.map((name, i) => ({ id: name, name, order: i + 1, collapsed: false, parent_id: ROOT })),
]

// Exactly one real session, filed in the only subfolder that holds work. The
// tag keeps it in a board column so the board frame is not an empty board.
const TAG = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
const slots = [{
  key: 's1', title: 'Worker · #7627 Settings Display toggle for user', messages: 12,
  running: false, agent: 'kirocrew', created: '2026-09-07T01:00:00Z',
  last_ts: '2026-09-07T21:14:00Z', folder_id: 'dashboard', tags: [TAG],
}]
const tags = [{ id: TAG, name: 'Working', color: '#1a1', order: 0, status: true }]
const columns = [{ id: 'col-working', name: 'Working', tag_ids: [TAG], mode: 'any', order: 0 }]

/** Crop the sidebar's session/folder panel out of the viewport. */
async function clipFor(page, anchorId) {
  const anchor = page.locator(`[data-testid="folder-collapse-${anchorId}"]`)
  const box = (await anchor.count()) ? await anchor.first().boundingBox() : null
  const x = box ? Math.max(0, box.x - 44) : 470
  return { x, y: 118, width: Math.min(1400 - x, 380), height: 1000 }
}

async function newPage(context, folders, { board = false } = {}) {
  const page = await context.newPage()
  // The stub clears localStorage in its own init script, and Playwright does not
  // order separately registered init scripts, so the board flag has to ride
  // `localStorageEntries` rather than an addInitScript of our own.
  await stubDashboardApi(page, {
    folders, slots, tags, columns,
    localStorageEntries: board ? { 'mc-chat-config': JSON.stringify({ tagColumnsEnabled: true }) } : null,
  })
  logPageProblems(page)
  return page
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const shared = { viewport: { width: 1400, height: 1250 }, deviceScaleFactor: 2 }
  const context = await browser.newContext(shared)
  const page = await newPage(context, baseFolders)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const clip = await clipFor(page, ROOT)
  await page.screenshot({ path: `${OUT}/${PREFIX}-01-empty-folders.png`, clip })
  console.log('wrote', `${OUT}/${PREFIX}-01-empty-folders.png`)

  // Frame 2: the escape hatch. Clicking an empty folder still opens it and
  // reveals its "New chat in <name>" row, so nothing became unreachable.
  const core = page.locator('[data-testid="folder-collapse-core"]')
  if (await core.count()) {
    await core.first().click()
    await page.waitForTimeout(400)
    await page.screenshot({ path: `${OUT}/${PREFIX}-02-clicked-open.png`, clip })
    console.log('wrote', `${OUT}/${PREFIX}-02-clicked-open.png`)
  }

  // Frame 3: board (tag-columns) view. The setting is localStorage-backed, so
  // seed it on the origin before the SPA boots.
  const boardContext = await browser.newContext(shared)
  const board = await newPage(boardContext, baseFolders, { board: true })
  await board.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await board.waitForTimeout(2600)
  // Board rows carry a column-scoped test id, not the list view's
  // `folder-collapse-<id>`, so the crop anchors on that instead.
  const boardRow = board.locator(`[data-testid="col-${columns[0].id}-folder-${ROOT}"]`)
  const boardBox = (await boardRow.count()) ? await boardRow.first().boundingBox() : null
  const bx = boardBox ? Math.max(0, boardBox.x - 24) : 470
  await board.screenshot({ path: `${OUT}/${PREFIX}-03-board.png`, clip: { x: bx, y: 96, width: Math.min(1400 - bx, 420), height: 940 } })
  console.log('wrote', `${OUT}/${PREFIX}-03-board.png`)
  await board.close()
  await boardContext.close()

  // Frame 5: the open/close transition, which a still cannot show. Playwright
  // writes the video on context close, under a name it picks, so the file is
  // renamed afterwards.
  const videoDir = join(OUT, `.video-${PREFIX}`)
  rmSync(videoDir, { recursive: true, force: true })
  const videoContext = await browser.newContext({ ...shared, deviceScaleFactor: 1, recordVideo: { dir: videoDir, size: { width: 700, height: 900 } } })
  const clip5 = await (async () => {
    const vp = await newPage(videoContext, baseFolders)
    await vp.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
    await vp.waitForTimeout(2600)
    const target = vp.locator('[data-testid="folder-collapse-core"]')
    for (let i = 0; i < 2; i++) {
      await vp.waitForTimeout(700)
      await target.first().click()
      await vp.waitForTimeout(700)
      await target.first().click()
    }
    await vp.waitForTimeout(800)
    await vp.close()
    return null
  })()
  void clip5
  await videoContext.close()
  const recorded = readdirSync(videoDir).filter(f => f.endsWith('.webm'))
  if (recorded.length) {
    renameSync(join(videoDir, recorded[0]), `${OUT}/${PREFIX}-04-open-close.webm`)
    console.log('wrote', `${OUT}/${PREFIX}-04-open-close.webm`)
  }
  rmSync(videoDir, { recursive: true, force: true })

  await context.close()
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
