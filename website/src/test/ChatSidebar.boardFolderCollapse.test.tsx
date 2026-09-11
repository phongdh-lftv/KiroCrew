/**
 * Board view (tag-columns) renders the same root folders once per column, but
 * a folder's `collapsed` field is one server-persisted flag. Collapsing a
 * folder from one column must NOT collapse its copies in the other columns:
 * each (column, folder) pair keeps a client-local override layered over the
 * server flag.
 *
 * Load-bearing assertions:
 *   (1) toggling a folder in column A leaves column B's copy untouched;
 *   (2) the toggle never writes the server flag (no updateChatFolder call);
 *   (3) overrides survive a remount via localStorage;
 *   (4) a column with no override follows the server default.
 *   (5) emptiness joins that default — an empty folder starts collapsed in every
 *       column, and opening it in one column still leaves the others alone;
 *   (6) a folder that arrives after the first load is exempt from (5), so a
 *       just-created folder does not read as a failed create.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatTag, TagColumn, ChatFolder } from '../types'

// Render framer-motion elements as plain DOM because jsdom cannot run projection.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  updateChatFolder: vi.fn(),
  chatFolders: vi.fn(),
  chatTags: vi.fn(),
  tagColumns: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const BLOCKED = '11111111-1111-1111-1111-111111111111'
const REVIEW = '22222222-2222-2222-2222-222222222222'
const COL_A = 'col-aaaa'
const COL_B = 'col-bbbb'
const FOLDER_ID = 'folder-zzzz'
// Every fixture below files a subfolder under the subject folder, so it is not
// EMPTY. An empty folder collapses on its own in both views (auto-collapse),
// which would make the per-column default `collapsed` here and test a different
// thing than these cases are about. The subfolder is the cheapest way to hold
// the subject fixed: board columns read it through `deepChildren`, so no slot or
// tag wiring is needed.
const SUBFOLDER_ID = 'folder-sub'
const withChild = (folder: ChatFolder): ChatFolder[] =>
  [folder, { id: SUBFOLDER_ID, name: 'Sub', parent_id: FOLDER_ID, order: 0 }]

/** What `api.chatFolders()` currently answers, for the one case that needs a
 *  folder to ARRIVE after mount rather than be there on the first render. */
let served: ChatFolder[] = []

const tags: ChatTag[] = [
  { id: BLOCKED, name: 'Blocked', color: '#e11', order: 0, status: true },
  { id: REVIEW, name: 'Review', color: '#1a1', order: 1, status: true },
]
const columns: TagColumn[] = [
  { id: COL_A, name: 'Planned/Blocked', tag_ids: [BLOCKED], mode: 'any', order: 0 },
  { id: COL_B, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 1 },
]

function renderSidebar(folderData: ChatFolder[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folderData)
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store, qc }
}

function folderHeader(container: HTMLElement, columnId: string): HTMLElement {
  const block = container.querySelector(`[data-testid="col-${columnId}-folder-${FOLDER_ID}"]`) as HTMLElement
  expect(block).toBeTruthy()
  return block.querySelector('[role="button"][aria-expanded]') as HTMLElement
}

beforeEach(() => {
  localStorage.clear()
  served = []
  mocks.chatFolders.mockImplementation(() => Promise.resolve(served))
  // Board view only exists while these two answer non-empty. The Proxy default
  // resolves an unmocked method to [], so a refetch mid-test would drop the
  // columns and take every folder block with them.
  mocks.chatTags.mockImplementation(() => Promise.resolve(tags))
  mocks.tagColumns.mockImplementation(() => Promise.resolve(columns))
  mocks.updateChatFolder.mockResolvedValue({ ok: true })
})
afterEach(() => vi.clearAllMocks())

describe('board view: per-column folder collapse', () => {
  it('collapsing a folder in one column leaves the other column expanded', () => {
    const { container } = renderSidebar(withChild({ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }))
    const headerA = folderHeader(container, COL_A)
    const headerB = folderHeader(container, COL_B)
    expect(headerA.getAttribute('aria-expanded')).toBe('true')
    expect(headerB.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(headerA)

    expect(folderHeader(container, COL_A).getAttribute('aria-expanded')).toBe('false')
    // The load-bearing assertion: column B's copy did not follow.
    expect(folderHeader(container, COL_B).getAttribute('aria-expanded')).toBe('true')
  })

  it('never writes the server collapsed flag from a board toggle', () => {
    const { container } = renderSidebar(withChild({ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }))
    fireEvent.click(folderHeader(container, COL_A))
    fireEvent.click(folderHeader(container, COL_B))
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('persists per-column state across a remount', () => {
    const folderData: ChatFolder[] = withChild({ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false })
    const first = renderSidebar(folderData)
    fireEvent.click(folderHeader(first.container, COL_A))
    first.unmount()

    const second = renderSidebar(folderData)
    expect(folderHeader(second.container, COL_A).getAttribute('aria-expanded')).toBe('false')
    expect(folderHeader(second.container, COL_B).getAttribute('aria-expanded')).toBe('true')
  })

  it('a column without an override follows the server default', () => {
    // Server says collapsed; expanding in A must not expand B, whose state is
    // still the server flag.
    const { container } = renderSidebar(withChild({ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: true }))
    expect(folderHeader(container, COL_A).getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(folderHeader(container, COL_A))
    expect(folderHeader(container, COL_A).getAttribute('aria-expanded')).toBe('true')
    expect(folderHeader(container, COL_B).getAttribute('aria-expanded')).toBe('false')
  })

  it('an empty folder starts collapsed in every column and still opens per column', () => {
    // No subfolder and no slot: the folder has nothing to show in any column, so
    // emptiness is the per-column DEFAULT even though the server flag says
    // expanded. Opening it in A must leave B on that default.
    const { container } = renderSidebar([{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }])
    expect(folderHeader(container, COL_A).getAttribute('aria-expanded')).toBe('false')
    expect(folderHeader(container, COL_B).getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(folderHeader(container, COL_A))

    expect(folderHeader(container, COL_A).getAttribute('aria-expanded')).toBe('true')
    expect(folderHeader(container, COL_B).getAttribute('aria-expanded')).toBe('false')
    // Emptiness is a display default, never a write — the stored flag is
    // untouched, exactly as for a normal board toggle.
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('does not persist an empty folder expansion across a remount', () => {
    // The tree forgets an empty folder you opened by hand, on purpose: still
    // empty next session is still noise. A durable localStorage override here
    // would instead reopen it on every future reload, so a flip against an
    // emptiness default writes nothing. A NON-empty folder's toggle still
    // persists (the case above).
    const folderData: ChatFolder[] = [{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }]
    const first = renderSidebar(folderData)
    expect(folderHeader(first.container, COL_A).getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(folderHeader(first.container, COL_A))
    expect(folderHeader(first.container, COL_A).getAttribute('aria-expanded')).toBe('true')
    expect(Object.keys(localStorage).filter(k => k.startsWith('kc-board-folder-collapsed:'))).toEqual([])
    first.unmount()

    const second = renderSidebar(folderData)
    expect(folderHeader(second.container, COL_A).getAttribute('aria-expanded')).toBe('false')
  })

  it('keeps the action cluster visible on an empty column row', () => {
    // List-view parity: the row's own cluster stops hiding on an empty folder,
    // so a collapsed empty row is not left with no visible control.
    const clusterOf = (container: HTMLElement, columnId: string) =>
      container.querySelector(`[data-testid="col-${columnId}-folder-${FOLDER_ID}-new-chat"]`)?.parentElement as HTMLElement
    const { container } = renderSidebar([{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }])
    expect(clusterOf(container, COL_A).className).not.toContain('opacity-0')
    expect(clusterOf(container, COL_B).className).not.toContain('opacity-0')

    // A folder with content keeps the hover-only cluster.
    const withSub = renderSidebar(withChild({ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }))
    expect(clusterOf(withSub.container, COL_A).className).toContain('opacity-0')
  })

  it('leaves a just-created empty folder open in its columns', async () => {
    // A folder that arrives AFTER the first load is one somebody just made.
    // Emptiness must not collapse it, or the create reads as having failed —
    // the same exemption the tree gives, reaching board view through the
    // per-column default.
    const { container, qc } = renderSidebar([])
    served = [{ id: FOLDER_ID, name: 'CDF', order: 0, collapsed: false }]
    await act(async () => { await qc.invalidateQueries({ queryKey: ['chat-folders'] }) })

    await waitFor(() => expect(container.querySelector(`[data-testid="col-${COL_A}-folder-${FOLDER_ID}"]`)).toBeTruthy())
    expect(folderHeader(container, COL_A).getAttribute('aria-expanded')).toBe('true')
    expect(folderHeader(container, COL_B).getAttribute('aria-expanded')).toBe('true')
  })
})
