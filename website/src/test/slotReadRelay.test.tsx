/**
 * Cross-window unread-badge sync: the `slot_read` relay.
 *
 * Read marks were window-local (Redux + localStorage), so reading a session
 * in one dashboard window left the sidebar bubble lit in every other one.
 * These specs pin the three legs of the fix:
 *
 *  - the relay module's per-slot throttle (leading send, one coalesced
 *    trailing send, never a dropped final read),
 *  - the socket wiring: an inbound `slot_read` frame clears the local badge
 *    and never echoes back out; the arrival branch relays a read only for
 *    this window's visible active slot,
 *  - the read-gesture sites: `switchSlot` relays the slot it just read.
 *
 * Harness mirrors UseWebSocketCoverage: the hook dispatches through the
 * Provider store but reads `activeSlot` off the singleton store, so tests
 * prime both and reset both.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import chatReducer, { setActiveSlot, clearMessages, switchSlot } from '../store/chatSlice'
import dashboardReducer, { addSlotOptimistic, removeSlotOptimistic, markSlotRead, markSlotUnread, remoteSlotRead, restoreManualUnread, MANUAL_UNREAD } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { bindSlotReadSender, emitSlotRead, flushSlotRead, _resetSlotReadRelayForTest } from '../lib/slotReadRelay'

/** Flip jsdom's document.hidden and fire the visibilitychange the hook listens for. */
const setDocumentHidden = (v: boolean) => {
  Object.defineProperty(document, 'hidden', { value: v, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

const ACTIVE = 'slot-active'
const BACKGROUND = 'slot-background'

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })
  constructor(public url: string) { WS_INSTANCES.push(this) }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: unknown) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

describe('slotReadRelay module throttle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    _resetSlotReadRelayForTest()
  })
  afterEach(() => {
    _resetSlotReadRelayForTest()
    vi.useRealTimers()
  })

  it('sends the first read immediately (leading edge)', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')
    expect(sent).toEqual(['k1'])
  })

  it('coalesces a burst into exactly one trailing send carrying the newest watermark', () => {
    const sent: Array<[string, string | undefined]> = []
    bindSlotReadSender((s, ts) => sent.push([s, ts]))
    emitSlotRead('k1', '2026-01-01T00:00:01Z')
    emitSlotRead('k1', '2026-01-01T00:00:03Z')
    emitSlotRead('k1', '2026-01-01T00:00:02Z')
    expect(sent).toEqual([['k1', '2026-01-01T00:00:01Z']])   // burst suppressed…
    vi.advanceTimersByTime(1_000)
    // …but the LAST read still lands, watermarked at the NEWEST ts seen.
    expect(sent).toEqual([['k1', '2026-01-01T00:00:01Z'], ['k1', '2026-01-01T00:00:03Z']])
    vi.advanceTimersByTime(5_000)
    expect(sent.length).toBe(2)           // trailing send does not self-perpetuate
  })

  it('a quiet window with no repeat sends nothing at its end', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')
    vi.advanceTimersByTime(5_000)
    expect(sent).toEqual(['k1'])
  })

  it('throttles per slot, not globally', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')
    emitSlotRead('k2')
    expect(sent).toEqual(['k1', 'k2'])
  })

  it('is a safe no-op unbound and for an empty key', () => {
    expect(() => emitSlotRead('k1')).not.toThrow()
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('')
    expect(sent).toEqual([])
  })

  it('flush sends a pending trailing relay immediately and disarms its timer', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')
    emitSlotRead('k1')          // leading sent + trailing pending
    flushSlotRead('k1')
    expect(sent).toEqual(['k1', 'k1'])
    vi.advanceTimersByTime(5_000)
    expect(sent).toEqual(['k1', 'k1'])  // timer disarmed: no third send
  })

  it('flush of a quiet window sends nothing extra', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')          // leading only, no repeat
    flushSlotRead('k1')
    vi.advanceTimersByTime(5_000)
    expect(sent).toEqual(['k1'])
  })

  it('a targeted flush leaves other slots pending', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1'); emitSlotRead('k1')
    emitSlotRead('k2'); emitSlotRead('k2')
    flushSlotRead('k1')
    expect(sent).toEqual(['k1', 'k2', 'k1'])
    vi.advanceTimersByTime(1_000)
    expect(sent).toEqual(['k1', 'k2', 'k1', 'k2'])  // k2 trailing untouched
  })
})

describe('slot_read over the dashboard socket', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    _resetSlotReadRelayForTest()
    WS_INSTANCES.length = 0
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    _resetSlotReadRelayForTest()
    setDocumentHidden(false)
    vi.unstubAllGlobals()
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mount() {
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { ...hook, ws }
  }

  const dash = () => testStore.getState().dashboard
  const sentReadFrames = (ws: MockWebSocket) =>
    ws.send.mock.calls.map(c => c[0] as string).filter(f => f.includes('"slot_read"'))

  it('an inbound slot_read frame retires a badge its watermark covers', () => {
    const { ws } = mount()
    testStore.dispatch(markSlotUnread({ slot: BACKGROUND, ts: '2026-01-01T00:00:05Z' }))
    expect(dash().unreadSlots).toContain(BACKGROUND)
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: BACKGROUND, read_ts: '2026-01-01T00:00:09Z' } }) })
    expect(dash().unreadSlots).not.toContain(BACKGROUND)
  })

  it('an inbound slot_read older than the badge keeps it lit (watermark)', () => {
    const { ws } = mount()
    // The F1 race: A read message N (ts 5) and relayed; N+1 (ts 9) badged this
    // window before the relay landed. The stale relay must not clear N+1.
    testStore.dispatch(markSlotUnread({ slot: BACKGROUND, ts: '2026-01-01T00:00:09Z' }))
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: BACKGROUND, read_ts: '2026-01-01T00:00:05Z' } }) })
    expect(dash().unreadSlots).toContain(BACKGROUND)
  })

  it('a manual mark-as-unread is never cleared by a remote read', () => {
    const { ws } = mount()
    testStore.dispatch(markSlotUnread(BACKGROUND))   // string form = manual reminder
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: BACKGROUND, read_ts: '2099-12-31T23:59:59Z' } }) })
    expect(dash().unreadSlots).toContain(BACKGROUND)
  })

  it('a boot-restored badge (no watermark recorded) accepts any relayed read', () => {
    // unreadSince is deliberately not persisted, so a badge restored from
    // localStorage has no watermark: any relayed read clears it, even one
    // with no read_ts of its own.
    const boot = { ...dashboardReducer(undefined, { type: '@@INIT' }), unreadSlots: ['restored-slot'] }
    const cleared = dashboardReducer(boot, remoteSlotRead({ slot: 'restored-slot', readTs: undefined }))
    expect(cleared.unreadSlots).not.toContain('restored-slot')
  })

  it('an inbound slot_read never echoes back out (no relay loop)', () => {
    const { ws } = mount()
    testStore.dispatch(markSlotUnread({ slot: BACKGROUND, ts: '2026-01-01T00:00:05Z' }))
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: BACKGROUND, read_ts: '2026-01-01T00:00:09Z' } }) })
    expect(sentReadFrames(ws)).toEqual([])
  })

  it('a malformed slot_read frame is ignored', () => {
    const { ws } = mount()
    testStore.dispatch(markSlotUnread({ slot: BACKGROUND, ts: '2026-01-01T00:00:05Z' }))
    act(() => { ws.simulateMessage({ type: 'slot_read', data: {} }) })
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: 42 } }) })
    expect(dash().unreadSlots).toContain(BACKGROUND)
  })

  it('a message landing in the visible active slot relays a read', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
    })
    expect(sentReadFrames(ws)).toEqual([JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:00Z' })])
  })

  it('a message landing in the active slot on a NON-chat route relays nothing', () => {
    // chat.activeSlot survives navigating to Settings; a visible tab there
    // must not broadcast a read for a transcript it is not rendering.
    const { ws } = mount()
    window.history.pushState({}, '', '/settings')
    try {
      act(() => {
        ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
      })
      expect(sentReadFrames(ws)).toEqual([])
    } finally {
      window.history.pushState({}, '', '/')
    }
  })

  it('a message landing in a background slot badges it and relays nothing', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: BACKGROUND, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
    })
    expect(dash().unreadSlots).toContain(BACKGROUND)
    expect(sentReadFrames(ws)).toEqual([])
  })

  it('switchSlot relays the read of the slot it opens', async () => {
    const { ws } = mount()
    await act(async () => { await testStore.dispatch(switchSlot(BACKGROUND) as never) })
    // No slot metadata in this store, so no last_ts exists: the relay goes out
    // WITHOUT a watermark rather than minting client time (receivers then
    // apply their conservative default).
    const frames = sentReadFrames(ws).map(f => JSON.parse(f) as { type: string; slot: string; read_ts?: string })
    expect(frames.some(f => f.slot === BACKGROUND && f.read_ts === undefined)).toBe(true)
  })

  it('switching away flushes the outgoing slot\'s pending trailing relay', () => {
    const { ws } = mount()
    act(() => {
      // Two arrivals in the visible active slot: leading frame + pending trailing.
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'a', ts: '2026-09-10T00:00:00Z' } })
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'b', ts: '2026-09-10T00:00:01Z' } })
    })
    expect(sentReadFrames(ws)).toEqual([JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:00Z' })])
    // The slot stops being visible-active: the coalesced trailing read goes
    // out NOW (watermarked at the newest arrival), so no timer survives to
    // wipe a later re-badge.
    act(() => { globalStore.dispatch(setActiveSlot(BACKGROUND)) })
    expect(sentReadFrames(ws)).toEqual([
      JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:00Z' }),
      JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:01Z' }),
    ])
  })

  it('reveal relays the slot that is active NOW, never a stale hidden arrival', () => {
    const { ws } = mount()
    act(() => { setDocumentHidden(true) })
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
    })
    act(() => { globalStore.dispatch(setActiveSlot(BACKGROUND)) })
    act(() => { setDocumentHidden(false) })
    // The reveal read names what the user now sees. This store holds no slot
    // metadata, so no last_ts exists: the frame goes out without a watermark
    // rather than minting client time (receivers apply their conservative
    // default and keep watermarked badges lit).
    const frames = sentReadFrames(ws).map(f => JSON.parse(f) as { slot: string; read_ts?: string })
    expect(frames.length).toBe(1)
    expect(frames[0].slot).toBe(BACKGROUND)
    expect(frames[0].read_ts).toBeUndefined()
  })

  it('markSlotRead is a persistence no-op for a key that is not unread', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem')
    const before = dashboardReducer(undefined, { type: '@@INIT' })
    spy.mockClear()
    const after = dashboardReducer(before, markSlotRead('never-unread'))
    expect(after.unreadSlots).toEqual(before.unreadSlots)
    expect(spy).not.toHaveBeenCalled()             // echo fan-in writes nothing
    spy.mockRestore()
  })

  it('a message-lit badge without any actual ts accepts any relayed read', () => {
    // No frame ts and no slot last_ts: nothing is recorded (client time is
    // never minted), so a relayed read — even watermark-less — clears it.
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    st = dashboardReducer(st, markSlotUnread({ slot: 'no-ts-slot' }))
    expect(st.unreadSince['no-ts-slot']).toBeUndefined()
    st = dashboardReducer(st, remoteSlotRead({ slot: 'no-ts-slot', readTs: undefined }))
    expect(st.unreadSlots).not.toContain('no-ts-slot')
  })

  it('compares watermarks as instants, not strings (mixed-offset timestamps)', () => {
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    // since = 00:00Z written as +02:00; a read at 01:00Z covers it even though
    // the lexical comparison would say otherwise.
    st = dashboardReducer(st, markSlotUnread({ slot: 'tz-slot', ts: '2026-01-01T02:00:00+02:00' }))
    st = dashboardReducer(st, remoteSlotRead({ slot: 'tz-slot', readTs: '2026-01-01T01:00:00Z' }))
    expect(st.unreadSlots).not.toContain('tz-slot')
    // …and an unparseable watermark can never clear a badge.
    st = dashboardReducer(st, markSlotUnread({ slot: 'tz-slot', ts: '2026-01-01T05:00:00Z' }))
    st = dashboardReducer(st, remoteSlotRead({ slot: 'tz-slot', readTs: 'not-a-timestamp' }))
    expect(st.unreadSlots).toContain('tz-slot')
  })

  it('a manual reminder survives reload: the sentinel is persisted and restored', () => {
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    st = dashboardReducer(st, markSlotUnread('remember-me'))
    const persisted = JSON.parse(sessionStorage.getItem('mc-unread-manual') ?? '[]') as string[]
    expect(persisted).toContain('remember-me')          // written at mark time
    dashboardReducer(st, markSlotRead('remember-me'))
    const cleared = JSON.parse(sessionStorage.getItem('mc-unread-manual') ?? '[]') as string[]
    expect(cleared).not.toContain('remember-me')        // pruned on local read
    // The store is per-window (sessionStorage), so a sibling window writing its
    // own sentinel set cannot clobber this one: nothing lands in the shared
    // localStorage at all.
    expect(localStorage.getItem('mc-unread-manual')).toBeNull()
  })

  it('persisting is a per-slot delta: sibling-window keys survive every write site', () => {
    const stored = () => JSON.parse(localStorage.getItem('mc-unread-slots') ?? '[]') as string[]
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    // A sibling window persisted Y; this window's memory has never seen it.
    localStorage.setItem('mc-unread-slots', JSON.stringify(['sibling-Y']))
    st = dashboardReducer(st, markSlotUnread({ slot: 'mine-X', ts: '2026-01-01T00:00:01Z' }))
    expect(stored().sort()).toEqual(['mine-X', 'sibling-Y'])   // add composes
    st = dashboardReducer(st, remoteSlotRead({ slot: 'mine-X', readTs: '2026-01-01T00:00:09Z' }))
    expect(stored()).toEqual(['sibling-Y'])                    // remote clear removes only its slot
    st = dashboardReducer(st, markSlotUnread({ slot: 'mine-X', ts: '2026-01-01T00:00:10Z' }))
    st = dashboardReducer(st, markSlotRead('mine-X'))
    expect(stored()).toEqual(['sibling-Y'])                    // local read removes only its slot
    st = dashboardReducer(st, markSlotUnread({ slot: 'mine-X', ts: '2026-01-01T00:00:11Z' }))
    dashboardReducer(st, removeSlotOptimistic('mine-X'))
    expect(stored()).toEqual(['sibling-Y'])                    // slot removal removes only its slot
  })

  it('an orphan manual sentinel is pruned at boot, not restored as an invisible shield', () => {
    // This window persisted a sentinel for X, then a sibling window cleared X
    // from the shared unread list while this tab was unloaded: the badge is
    // gone, so restoring the sentinel would silently shield X from every
    // future remote clear.
    sessionStorage.setItem('mc-unread-manual', JSON.stringify(['orphan-X', 'alive-Z']))
    localStorage.setItem('mc-unread-slots', JSON.stringify(['alive-Z']))
    const restored = restoreManualUnread()
    expect(restored).toEqual({ 'alive-Z': MANUAL_UNREAD })
    // The prune is written back, so the orphan cannot resurrect on the next boot.
    expect(JSON.parse(sessionStorage.getItem('mc-unread-manual') ?? '[]')).toEqual(['alive-Z'])
  })

  it('reducer-level watermark rules: manual sentinel and newest-ts retention', () => {
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    st = dashboardReducer(st, markSlotUnread('manual-slot'))                              // manual
    expect(st.unreadSince['manual-slot']).toBe(MANUAL_UNREAD)
    st = dashboardReducer(st, markSlotUnread({ slot: 'msg-slot', ts: '2026-01-01T00:00:09Z' }))
    st = dashboardReducer(st, markSlotUnread({ slot: 'msg-slot', ts: '2026-01-01T00:00:04Z' }))  // older arrival
    expect(st.unreadSince['msg-slot']).toBe('2026-01-01T00:00:09Z')                       // newest wins
    st = dashboardReducer(st, remoteSlotRead({ slot: 'manual-slot', readTs: '2099-01-01T00:00:00Z' }))
    expect(st.unreadSlots).toContain('manual-slot')                                       // shielded
    st = dashboardReducer(st, remoteSlotRead({ slot: 'msg-slot', readTs: '2026-01-01T00:00:08Z' }))
    expect(st.unreadSlots).toContain('msg-slot')                                          // older read keeps badge
    st = dashboardReducer(st, remoteSlotRead({ slot: 'msg-slot', readTs: '2026-01-01T00:00:09Z' }))
    expect(st.unreadSlots).not.toContain('msg-slot')                                      // covering read clears
    expect(st.unreadSince['msg-slot']).toBeUndefined()
  })
})

describe('hidden-tab reveal relay (single store end-to-end)', () => {
  // The hook dispatches through the Provider store and the relay reads the
  // singleton; in the app they are the same object. These specs mount the
  // Provider ON the singleton so the flush -> last_ts -> reveal-watermark
  // chain runs against one store, as it does in production.
  beforeEach(() => {
    vi.clearAllMocks()
    _resetSlotReadRelayForTest()
    WS_INSTANCES.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    globalStore.dispatch(addSlotOptimistic({ key: ACTIVE, title: ACTIVE, messages: 0, running: false } as ChatSlot))
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    _resetSlotReadRelayForTest()
    setDocumentHidden(false)
    vi.unstubAllGlobals()
    globalStore.dispatch(removeSlotOptimistic(ACTIVE))
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function singleStoreWrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: globalStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mountOnSingleton() {
    const hook = renderHook(() => useWebSocket(), { wrapper: singleStoreWrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { ...hook, ws }
  }

  const sentReadFrames = (ws: MockWebSocket) =>
    ws.send.mock.calls.map(c => c[0] as string).filter(f => f.includes('"slot_read"'))

  it('a hidden-tab arrival relays nothing; reveal relays the read at post-flush last_ts', () => {
    const { ws } = mountOnSingleton()
    act(() => { setDocumentHidden(true) })
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
    })
    expect(sentReadFrames(ws)).toEqual([])         // hidden window isn't reading
    act(() => { setDocumentHidden(false) })        // …returning to it IS
    // Reveal flushes the buffered recency bump (rAF is parked in hidden tabs)
    // and relays the active slot at the flushed last_ts — the arrival's own ts.
    expect(sentReadFrames(ws)).toEqual([JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:00Z' })])
  })

  it('a timestamp-less chat_done while hidden cannot regress the reveal watermark', () => {
    const { ws } = mountOnSingleton()
    act(() => { setDocumentHidden(true) })
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:05Z' } })
    })
    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })   // no ts on the frame
    act(() => { setDocumentHidden(false) })
    // No per-arrival state exists for the ts-less chat_done to overwrite: the
    // reveal reads the slot's own post-flush last_ts, which the reducer keeps
    // monotonic, so the relay carries the newest arrival's ts.
    expect(sentReadFrames(ws)).toEqual([JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:05Z' })])
  })
})
