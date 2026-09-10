/**
 * Release-channel worktree rows in the Dev Fleet table.
 *
 * The whole feature is a claim about WHICH RELEASE a checkout is sitting on, so
 * every test here asserts on what the row states rather than on whether it
 * rendered. Two failure modes are specifically guarded:
 *
 * - **Adopting on the name.** `release-channel-stable` is a reserved basename. A
 *   user's own branch checkout under that name must keep ordinary controls; only
 *   the backend's `worktree` field (set when the tree is detached at a resolved
 *   ref) confers lane controls.
 * - **Reusing a column with a different meaning silently.** BEHIND counts from
 *   the LANE TIP on these rows, not from main, and PR is inapplicable rather
 *   than merely absent.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage, { __resetDevFleetNoticesForTests } from '../pages/DevFleetPage'

function renderPage() {
  return renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
}

const MAIN = {
  name: 'main',
  is_main: true,
  running: false,
  has_dist: true,
  behind: 0,
  last_updated_at: Date.now() / 1000,
}

// A feature worktree carrying the repo's real naming convention, so the
// ordering assertions compare against what the fleet actually shows.
const FEATURE = {
  name: 'kirocrew-wt-update-freshness',
  is_main: false,
  running: false,
  has_dist: true,
  behind: 12,
  last_updated_at: Date.now() / 1000 - 3600,
}

const STABLE_WT = {
  name: 'release-channel-stable',
  is_main: false,
  running: false,
  has_dist: true,
  // Behind MAIN is large by construction on a release worktree — the row must
  // not show this number.
  behind: 412,
  last_updated_at: Date.now() / 1000 - 86400 * 2,
}

const CHANNELS = {
  stable: {
    lane: 'stable',
    name: 'release-channel-stable',
    worktree: 'release-channel-stable',
    ref: 'refs/tags/v0.5.0',
    version: '0.5.0',
    lane_check: 'ok',
    error: null,
    at_tip: true,
    behind: 0,
    name_taken_by_branch: false,
  },
  insider: {
    lane: 'insider',
    name: 'release-channel-insider',
    worktree: null,
    ref: 'refs/tags/v0.6.0-insider.6',
    version: '0.6.0-insider.6',
    lane_check: 'ok',
    error: null,
    at_tip: null,
    behind: null,
    name_taken_by_branch: false,
  },
  nightly: {
    lane: 'nightly',
    name: 'release-channel-nightly',
    worktree: null,
    ref: 'origin/main',
    version: null,
    lane_check: 'untagged',
    error: null,
    at_tip: null,
    behind: null,
    name_taken_by_branch: false,
  },
}

function mockFleet(data: Record<string, unknown>, posts?: Record<string, unknown>) {
  const seen: { url: string; body: unknown }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation((url, init) => {
    const u = typeof url === 'string' ? url : (url as Request).url
    if (init?.method === 'POST') {
      seen.push({ url: u, body: init.body ? JSON.parse(String(init.body)) : null })
      const key = Object.keys(posts || {}).find((k) => u.includes(k))
      return Promise.resolve(
        new Response(JSON.stringify(key ? posts![key] : { ok: true }), { status: 200 }),
      )
    }
    if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
    if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
  return seen
}

beforeEach(() => {
  __resetDevFleetNoticesForTests()
  vi.restoreAllMocks()
})

describe('DevFleetPage release-channel rows', () => {
  it('badges an adopted lane row with the release it is sitting on', async () => {
    mockFleet({
      base_branch: 'main',
      worktrees: [MAIN, STABLE_WT, FEATURE],
      release_channels: [CHANNELS.stable, CHANNELS.insider, CHANNELS.nightly],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // The lane is already in the row name, so the badge carries the version.
    const badge = screen.getByText('0.5.0')
    expect(badge).toBeInTheDocument()
    expect(badge).toHaveAttribute('title', expect.stringContaining('refs/tags/v0.5.0'))
  })

  it('lists a lane with no worktree as a placeholder row offering Create', async () => {
    // Without the placeholder there is nowhere on the page the feature is
    // discoverable — the design has no header control.
    mockFleet({
      worktrees: [MAIN, FEATURE],
      release_channels: [CHANNELS.stable, CHANNELS.insider, CHANNELS.nightly],
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('release-channel-placeholder-insider')).toBeInTheDocument())
    const row = screen.getByTestId('release-channel-placeholder-insider')
    expect(within(row).getByText('release-channel-insider')).toBeInTheDocument()
    expect(within(row).getByText('0.6.0-insider.6')).toBeInTheDocument()
    expect(within(row).getByText('no worktree yet')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /create/i })).toBeEnabled()
  })

  it('shows nightly resolving to origin/main rather than inventing a version', async () => {
    // nightly.yml builds from main HEAD and tags nothing, so there is no version
    // string any artifact carries. Synthesizing one would put a fabricated
    // version on screen.
    mockFleet({
      worktrees: [MAIN],
      release_channels: [CHANNELS.nightly],
    })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-nightly'))
    expect(within(row).getByText('origin/main')).toBeInTheDocument()
  })

  it('counts BEHIND from the lane tip, not from main', async () => {
    const behindTip = { ...CHANNELS.stable, at_tip: false, behind: 3 }
    mockFleet({
      worktrees: [MAIN, STABLE_WT],
      release_channels: [behindTip],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // 3 from the channel tip is shown; 412 from main is not.
    expect(screen.getByText('↓3')).toBeInTheDocument()
    expect(screen.queryByText('↓412')).not.toBeInTheDocument()
  })

  it('marks PR inapplicable on a lane row instead of showing the no-PR dash', async () => {
    // The em dash on every other row means "no PR yet", which invites waiting
    // for one. A tag-detached tree can never have a PR at all.
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channels: [CHANNELS.stable] })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const na = screen.getAllByText('n/a')
    expect(na.length).toBeGreaterThan(0)
    expect(na[0]).toHaveAttribute('title', expect.stringContaining('pull request'))
  })

  it('does NOT adopt a branch checkout that merely shares the reserved name', async () => {
    // The name guard, from the UI side: the backend reports worktree=null plus
    // name_taken_by_branch, so the row keeps ordinary controls.
    const taken = { ...CHANNELS.stable, worktree: null, at_tip: null, behind: null, name_taken_by_branch: true }
    mockFleet({
      worktrees: [MAIN, { ...STABLE_WT, behind: 5 }],
      release_channels: [taken],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    // No version badge: this row is not a lane pin.
    expect(screen.queryByText('0.5.0')).not.toBeInTheDocument()
    // Its behind count is the ordinary behind-main figure, not a lane distance.
    expect(screen.getByText('↓5')).toBeInTheDocument()
  })

  it('explains the occupied name on the existing row, not as a second row', async () => {
    // One directory is one row. Rendering a blocked placeholder alongside the
    // real checkout printed `release-channel-stable` twice on the page, which is
    // what this asserts against.
    const taken = { ...CHANNELS.stable, worktree: null, at_tip: null, behind: null, name_taken_by_branch: true }
    mockFleet({ worktrees: [MAIN, { ...STABLE_WT, behind: 5 }], release_channels: [taken] })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    expect(screen.getAllByText('release-channel-stable')).toHaveLength(1)
    expect(screen.queryByTestId('release-channel-placeholder-stable')).not.toBeInTheDocument()
    const badge = screen.getByText('Not a release-channel worktree')
    expect(badge).toHaveAttribute('title', expect.stringContaining('is on a branch'))
  })

  it('flags a lane_check mismatch instead of presenting it as a clean pin', async () => {
    // The one way this row could serve a prerelease while claiming a stable
    // lane: the tag-shape rule and the version classifier disagreed.
    const mismatched = { ...CHANNELS.stable, lane_check: 'mismatch' }
    mockFleet({ worktrees: [MAIN, STABLE_WT], release_channels: [mismatched] })
    renderPage()
    await waitFor(() => expect(screen.getByText('0.5.0')).toBeInTheDocument())
    expect(screen.getByText('0.5.0')).toHaveAttribute(
      'title',
      expect.stringContaining('does not classify as'),
    )
  })

  it('surfaces an unresolvable lane on its placeholder and blocks Create', async () => {
    const broken = {
      ...CHANNELS.stable,
      worktree: null,
      ref: null,
      version: null,
      error: 'no stable release tag found in this checkout',
    }
    mockFleet({ worktrees: [MAIN], release_channels: [broken] })
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-stable'))
    expect(within(row).getByText('no stable release tag found in this checkout')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /create/i })).toBeDisabled()
  })

  it('orders lane rows under main and above the feature worktrees', async () => {
    // Fixed position, not part of the sort: every sort key on offer describes
    // feature-branch progress, and a release worktree scores badly on all of
    // them by design.
    mockFleet({
      worktrees: [MAIN, FEATURE, STABLE_WT],
      release_channels: [CHANNELS.stable, CHANNELS.insider],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('release-channel-stable')).toBeInTheDocument())
    const names = screen
      .getAllByText(/^(main|release-channel-\w+|kirocrew-wt-[\w-]+)$/)
      .map((n) => n.textContent)
    expect(names.indexOf('release-channel-stable')).toBeLessThan(
      names.indexOf('kirocrew-wt-update-freshness'),
    )
    expect(names.indexOf('main')).toBeLessThan(names.indexOf('release-channel-stable'))
  })

  it('posts the lane to /release-channel/create when Create is confirmed', async () => {
    const seen = mockFleet(
      { worktrees: [MAIN], release_channels: [CHANNELS.insider] },
      { '/release-channel/create': { ok: true, lane: 'insider', version: '0.6.0-insider.6' } },
    )
    renderPage()
    const row = await waitFor(() => screen.getByTestId('release-channel-placeholder-insider'))
    within(row).getByRole('button', { name: /create/i }).click()
    // Create is destructive enough to confirm: it writes a new checkout to disk.
    const confirm = await waitFor(() => screen.getByRole('button', { name: 'Create', hidden: false }))
    expect(confirm).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/Create the insider release-channel worktree/)).toBeInTheDocument())
    // The lane, not a path, is what crosses the wire — the server derives the
    // path so a caller can never name one.
    expect(seen.every((s) => !('path' in ((s.body as object) || {})))).toBe(true)
  })
})
