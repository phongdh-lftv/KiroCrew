/**
 * Empty folders auto-collapse in the sidebar tree.
 *
 * A folder holding no session and no subfolder has nothing in its body but the
 * "New chat in <name>" affordance, so an expanded one spends a full row on
 * nothing. With twenty area folders that is most of the sidebar's height, and
 * the sessions that DO exist fall below the fold. Such a folder therefore
 * renders collapsed and one click opens it.
 *
 * The four load-bearing properties, in order of what breaks if they regress:
 *   (1) an empty folder present at load renders collapsed;
 *   (2) a folder holding a session is untouched — auto-collapse must not hide
 *       real work;
 *   (3) the click that opens an empty folder is CLIENT-LOCAL: it writes no
 *       `collapsed` PATCH, because the stored flag already reads "expanded" and
 *       writing `true` to give the toggle something to flip would leave the
 *       folder shut once it gains its first session. Clicking again re-collapses;
 *   (4) a folder that appears AFTER the first load is exempt — collapsing a
 *       folder the instant it is created reads as the create having failed;
 *   (5) but a COLD LOAD is not a creation: the folder query resolves after
 *       mount, and reading that arrival as fourteen new folders would exempt the
 *       whole tree.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot } from '../types'
import type { RootState } from '../store'

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
// Legacy list layout: tag columns OFF, so folders render once in the tree.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ updateChatFolder: vi.fn(), chatFolders: vi.fn() }))

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

const EMPTY_FOLDER = 'folder-empty'
const FULL_FOLDER = 'folder-full'
const NEW_FOLDER = 'folder-new'
const SLOT = 'chat-1'

const folders: ChatFolder[] = [
  { id: EMPTY_FOLDER, name: 'Empty', order: 0 },
  { id: FULL_FOLDER, name: 'Full', order: 1 },
]
const slots: ChatSlot[] = [{ key: SLOT, title: 'Worker', messages: 3, running: false, folder_id: FULL_FOLDER }]

/** What `api.chatFolders()` currently answers. The sidebar refetches on its own,
 *  so seeding the cache alone is not enough — a refetch would blank the tree. */
let served: ChatFolder[] = folders

function renderSidebar(seedCache = true, slotList: ChatSlot[] = slots) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: slotList, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  // A real cold load mounts with this query still in flight, so `folders` is []
  // on the first render. Some cases need that path; the rest seed the cache to
  // keep the first paint deterministic.
  if (seedCache) qc.setQueryData(['chat-folders'], served)
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slotList} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, qc }
}

/** The folder row's collapse toggle — the glyph carries the test id, the
 *  surrounding <button> carries the expanded state. */
function toggleOf(container: HTMLElement, folderId: string): HTMLButtonElement {
  const glyph = container.querySelector(`[data-testid="folder-collapse-${folderId}"]`)
  const button = glyph?.closest('button')
  if (!button) throw new Error(`no collapse toggle for ${folderId}`)
  return button as HTMLButtonElement
}

/** FolderBody marks a closed body aria-hidden, which is what actually keeps the
 *  placeholder row out of the accessibility tree. It is a DIRECT child of the
 *  folder block — the header above it holds aria-hidden decorations of its own. */
function bodyHidden(container: HTMLElement, folderId: string): boolean {
  const block = container.querySelector(`[data-folder-drop="${folderId}"]`)
  const body = block?.querySelector(':scope > [aria-hidden]')
  if (!body) throw new Error(`no folder body for ${folderId}`)
  return body.getAttribute('aria-hidden') === 'true'
}

beforeEach(() => {
  localStorage.clear()
  served = folders
  mocks.chatFolders.mockImplementation(() => Promise.resolve(served))
  mocks.updateChatFolder.mockResolvedValue({})
})
afterEach(() => vi.clearAllMocks())

describe('sidebar: empty folders auto-collapse', () => {
  it('collapses a folder with no sessions and leaves a populated one open', () => {
    const { container } = renderSidebar()
    expect(toggleOf(container, EMPTY_FOLDER).getAttribute('aria-expanded')).toBe('false')
    expect(bodyHidden(container, EMPTY_FOLDER)).toBe(true)
    // The session-bearing folder is untouched: auto-collapse must never hide
    // work that exists.
    expect(toggleOf(container, FULL_FOLDER).getAttribute('aria-expanded')).toBe('true')
    expect(bodyHidden(container, FULL_FOLDER)).toBe(false)
  })

  it('opens on click without writing the collapsed flag, and re-collapses', async () => {
    const { container } = renderSidebar()
    await act(async () => { toggleOf(container, EMPTY_FOLDER).click() })
    expect(toggleOf(container, EMPTY_FOLDER).getAttribute('aria-expanded')).toBe('true')
    expect(bodyHidden(container, EMPTY_FOLDER)).toBe(false)
    // No server round-trip: the stored flag already says expanded, so a PATCH
    // here would either be a no-op or (writing `true`) strand the folder shut
    // once it gains a session.
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()

    await act(async () => { toggleOf(container, EMPTY_FOLDER).click() })
    expect(toggleOf(container, EMPTY_FOLDER).getAttribute('aria-expanded')).toBe('false')
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()
  })

  it('keeps the action cluster visible on a collapsed empty row', () => {
    // A collapsed empty folder's body held exactly one thing - the create action
    // - so hiding the body must not leave the row with no visible control at
    // all, or it reads as an inert dead end and its only affordance is found by
    // accident. The row's OWN cluster stops hiding instead: nothing is added, so
    // the two-buttons-per-row cap and the focus order are untouched. A folder
    // that holds sessions keeps the hover-only cluster.
    const { container } = renderSidebar()
    const clusterOf = (id: string) =>
      container.querySelector(`[data-testid="folder-new-chat-${id}"]`)?.parentElement as HTMLElement
    expect(clusterOf(EMPTY_FOLDER).className).not.toContain('opacity-0')
    expect(clusterOf(FULL_FOLDER).className).toContain('opacity-0')
    // Exactly one create control on the HEADER row: the cluster's. A second one
    // there would duplicate its label and its focus stop. (The collapsed body
    // below still holds its own placeholder button, which is aria-hidden and
    // inert while closed, so it is not a second stop.)
    const headerRow = clusterOf(EMPTY_FOLDER).closest('[role="group"]') as HTMLElement
    expect(headerRow.querySelectorAll('[aria-label="New chat in Empty"]').length).toBe(1)
    // The cluster is an absolutely-positioned overlay, so it sits on the count's
    // slot. On an empty row the count reads `0`, which is what the closed empty
    // row already says, so it gives the slot up rather than sitting underneath.
    expect(headerRow.textContent).not.toContain('0')
    const fullRow = clusterOf(FULL_FOLDER).closest('[role="group"]') as HTMLElement
    expect(fullRow.textContent).toContain('1')
  })

  it('never spends the stored collapse flag on an empty folder', async () => {
    // The flag is the user's preference for the folder when it HAS content, so
    // an empty folder must neither read nor write it. Before, opening an empty
    // folder PATCHed `collapsed: false`, and the re-collapse did not put it
    // back - so this exact sequence lost a `true` the user had set, and the
    // folder came back EXPANDED once it was populated again.
    served = [{ id: EMPTY_FOLDER, name: 'Empty', order: 0, collapsed: true }, folders[1]]
    const { container } = renderSidebar()

    // Open it while empty, then close it again.
    await act(async () => { toggleOf(container, EMPTY_FOLDER).click() })
    expect(toggleOf(container, EMPTY_FOLDER).getAttribute('aria-expanded')).toBe('true')
    await act(async () => { toggleOf(container, EMPTY_FOLDER).click() })
    expect(toggleOf(container, EMPTY_FOLDER).getAttribute('aria-expanded')).toBe('false')
    // Nothing was written, so nothing was lost.
    expect(mocks.updateChatFolder).not.toHaveBeenCalled()

    // Populated, the folder reads its stored flag again - and that flag is still
    // the `true` the user set, so it stays collapsed instead of springing open.
    const populated = renderSidebar(true, [...slots, { key: 'chat-2', title: 'Late', messages: 1, running: false, folder_id: EMPTY_FOLDER }])
    expect(toggleOf(populated.container, EMPTY_FOLDER).getAttribute('aria-expanded')).toBe('false')
  })

  it('exempts a folder that appears after the first load', async () => {
    const { container, qc } = renderSidebar()
    expect(container.querySelector(`[data-testid="folder-collapse-${NEW_FOLDER}"]`)).toBeNull()

    await act(async () => {
      served = [...folders, { id: NEW_FOLDER, name: 'Fresh', order: 2 }]
      await qc.invalidateQueries({ queryKey: ['chat-folders'] })
    })

    await waitFor(() => expect(toggleOf(container, NEW_FOLDER).getAttribute('aria-expanded')).toBe('true'))
    expect(bodyHidden(container, NEW_FOLDER)).toBe(false)
    // Pre-existing empty folders are unaffected by the exemption.
    expect(toggleOf(container, EMPTY_FOLDER).getAttribute('aria-expanded')).toBe('false')
  })

  it('collapses on a cold load, where the folder query resolves after mount', async () => {
    // The real app mounts with `folders` still [] and fills the tree a tick
    // later. Read naively, that arrival looks exactly like fourteen folders
    // being created at once, and the new-folder exemption would expand the
    // whole tree — which is what shipped before this case existed. The deferred
    // promise is load-bearing: a queryFn that resolves synchronously lets the
    // mount effect see the finished tree, and the bug hides.
    let release: (() => void) | undefined
    mocks.chatFolders.mockImplementation(() => new Promise<ChatFolder[]>(resolve => { release = () => resolve(served) }))

    const { container } = renderSidebar(false)
    expect(container.querySelector(`[data-testid="folder-collapse-${EMPTY_FOLDER}"]`)).toBeNull()

    await act(async () => { release?.() })
    await waitFor(() => expect(container.querySelector(`[data-testid="folder-collapse-${EMPTY_FOLDER}"]`)).toBeTruthy())

    expect(toggleOf(container, EMPTY_FOLDER).getAttribute('aria-expanded')).toBe('false')
    expect(bodyHidden(container, EMPTY_FOLDER)).toBe(true)
    expect(toggleOf(container, FULL_FOLDER).getAttribute('aria-expanded')).toBe('true')
  })
})
