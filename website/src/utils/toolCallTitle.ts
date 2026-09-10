/**
 * Human-readable tool-call titles, derived client-side from the call's own
 * arguments.
 *
 * ACP defines `tool_call.title` as "human-readable title describing what the
 * tool is doing", and every mainstream adapter (codex-acp, claude-code-acp,
 * opencode) fills it deterministically from the arguments — `Read file 'x'`,
 * `Search for 'foo' in src`. Ours do not: kiro-cli puts the raw command in it
 * (and degrades to the bare tool name `shell` on replay), KAS sends a fixed
 * `Run Command`, and an MCP call arrives as `@server/tool`. This module closes
 * that gap without a protocol change, so live and replayed rows read the same.
 *
 * ## Shape
 *
 *   classifyToolCall(input) -> ToolAction | null      structure, language-neutral
 *   renderToolAction(action) -> string                i18n template
 *   deriveToolCallTitle(input) -> DerivedToolTitle    the two, plus fallbacks
 *
 * The split is load-bearing: `src/kiro_crew/tool_call_title.py` is a
 * line-for-line mirror of `classifyToolCall` for the messaging renderers, and
 * `test/fixtures/tool_call_titles.json` pins both implementations to one case
 * table (action + English title). Change a rule here and the fixture fails on
 * whichever side you forgot.
 *
 * ## Shell rules
 *
 * Live in `shellCommandParse.ts` — a port of Codex's `parse_command.rs` that
 * accepts only plain-word scripts, classifies each segment (Read / List files /
 * Search / Git / package-manager / print / GitHub CLI / curl) and returns null
 * when any segment stays Unknown, so the caller shows the raw command rather
 * than a half-templated title (claude-agent-acp#1068 is the user backlash that
 * rule prevents). That module holds CLI syntax only and is a named boundary in
 * `eslint.i18n.config.js`; every string a person reads is rendered HERE through
 * `i18nT`.
 *
 * The model-written `__tool_use_purpose` is NOT read here — it stays the
 * separate purpose line (see `toolPurpose.ts`), and the verbatim command stays
 * available as `rawTitle` for tooltips and the expanded header.
 *
 * ## Why the client derives, when the gateway sees every frame
 *
 * The gateway could stamp a title onto each tool_call frame instead. It does
 * not, for three reasons that only hold on the client: the title is rendered
 * in the user's CURRENT locale (a stamped English string would need
 * re-stamping on a language switch); paths are shortened against the slot's
 * CURRENT project directory; and a persisted row from before this change, or
 * from a replay whose title degraded to `shell`, heals on render with no data
 * migration, because the derivation runs from the persisted arguments. The
 * Python mirror serves the channels, which have no client — it is a second
 * renderer of the same rules, not the place to consolidate them.
 *
 * Stamping the language-neutral `ToolAction` (rather than a string) would
 * answer the first two points but not the third, and it adds two more: rows
 * the gateway never authored — kiro-cli's own `session/load` replay behind
 * `dashboard.replay_from_acp`, and the app-SDK embed's frames — would carry
 * no stamp and need this classifier anyway; and every history page served
 * to a pre-change row would run the classifier on the gateway at serve time
 * instead of once at render. One classifier per side, pinned to one fixture,
 * is the maintainer's chosen trade (#9988); revisit when PR 2's
 * server-declared MCP titles land.
 */

import { i18nT } from '../i18n/t'

import { classifyShellCommand, hostOf } from './shellCommandParse'
import type { ToolAction } from './toolAction'

export type { ToolAction } from './toolAction'

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type ToolCallTitleInput = {
  /** Trusted programmatic tool name (`fs_read`, `execute_bash`, `session_send`)
   *  when the transport supplied one; `''`/undefined otherwise. */
  toolName?: string
  /** ACP tool kind (`execute`, `read`, `edit`, `search`, `fetch`, `other`, …). */
  kind?: string
  /** The call's arguments: a parsed object, a JSON string, or — for persisted
   *  edit rows — a unified diff string. */
  rawInput?: unknown
  /** The title the transport sent (raw command, `@server/tool`, `Run Command`,
   *  kiro-cli's own `Reading x.rs:1-20`, or the degraded replay stub `shell`). */
  title?: string
  /** Resolved shell signal from the transport, when the caller holds one. */
  isShell?: boolean
  /** MCP server that served the call (trusted `_meta.kiro.mcpServerName`). */
  mcpServer?: string
  /** Working directory used to shorten file paths in native-tool titles. */
  cwd?: string
}

export type ClassifiedToolCall = {
  action: ToolAction
  /** Further parsed actions folded into "and N more" (shell chains, multi-op reads). */
  more: number
  /** ACP-style kind for the icon; refined for shell reads/searches. */
  kind: string
}

export type DerivedToolTitle = {
  /** Localized display title. */
  title: string
  /** ACP-style kind for the row icon. */
  kind: string
  /** The verbatim command / incoming title, for tooltips and the expanded header. */
  rawTitle: string
  /** True when a template applied; false when `title` is the (formatted) raw title. */
  derived: boolean
}

// ---------------------------------------------------------------------------
// Native tools (fs_read / fs_write / grep / glob / web_fetch / KAS read/edit/write)
// ---------------------------------------------------------------------------

/** Programmatic names of the native file / fetch tools across kiro-cli, KAS and
 *  claude-agent-acp. Transport identifiers matched exactly, not copy. */
const NATIVE_TOOL_NAME_RE = /^(?:fs_read|fs_write|read|write|edit|glob|grep|web_fetch|web_search|Read|Write|Edit|Glob|Grep|WebFetch|WebSearch)$/
const MAX_MCP_ARG_CHARS = 60
const MAX_RAW_TITLE_CHARS = 80
/** `gh` nouns that read as English only in their initialism form. */
const GH_NOUN_DISPLAY: Record<string, string> = { pr: 'PR' }
const MCP_SALIENT_KEYS = ['target', 'path', 'file_path', 'url', 'query', 'pattern', 'name', 'title', 'message', 'task', 'slug', 'job_id', 'session_key', 'id']

function asRecord(v: unknown): Record<string, unknown> | undefined {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined
}

function str(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined
}

function num(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

/** Parse `rawInput` into an args object when it is one (or a JSON string of one). */
export function parseToolArgs(rawInput: unknown): Record<string, unknown> | undefined {
  const rec = asRecord(rawInput)
  if (rec) return rec
  if (typeof rawInput === 'string') {
    const s = rawInput.trim()
    if (!s.startsWith('{')) return undefined
    try {
      return asRecord(JSON.parse(s))
    } catch {
      return undefined
    }
  }
  return undefined
}

/** Path relative to `cwd` when under it; else `parent/basename`; a home-anchored
 *  path keeps its `~/…` form. The full path stays in the tooltip. */
export function relDisplayPath(path: string, cwd?: string): string {
  const p = path.replace(/\\/g, '/')
  const parts = p.replace(/\/+$/, '').split('/').filter(Boolean)
  if (cwd) {
    const base = cwd.replace(/\\/g, '/').replace(/\/+$/, '')
    if (base && p.startsWith(base + '/')) return p.slice(base.length + 1)
    if (base && p.replace(/\/+$/, '') === base && parts.length) return parts[parts.length - 1]
  }
  if (p.startsWith('~/')) return p
  if (parts.length <= 2) return parts.join('/') || p
  return parts.slice(-2).join('/')
}

/** Path named by a persisted unified-diff input (`+++ b/path` / `+++ path`). */
function diffTargetPath(input: string): string | undefined {
  const m = input.match(/^\+\+\+ (?:b\/)?(.+?)\s*$/m)
  const p = m?.[1]
  return p && p !== '/dev/null' ? p : undefined
}

function classifyNative(inp: ToolCallTitleInput): ClassifiedToolCall | null {
  const kind = inp.kind || ''
  const tool = inp.toolName || ''
  const args = parseToolArgs(inp.rawInput)
  const rel = (p: string) => relDisplayPath(p, inp.cwd)
  const title = inp.title || ''

  // -- read ---------------------------------------------------------------
  if (kind === 'read' || tool === 'fs_read' || tool === 'read' || tool === 'Read') {
    if (!args) return null
    const ops = Array.isArray(args.operations) ? (args.operations as unknown[]).map(asRecord).filter(Boolean) as Record<string, unknown>[] : undefined
    if (ops && ops.length > 0) {
      const first = ops[0]
      const path = str(first.path)
      if (!path) return null
      const distinct = new Set(ops.map(o => str(o.path)).filter(Boolean)).size
      const more = Math.max(0, distinct - 1)
      const mode = str(first.mode)
      if (mode === 'Directory') return { action: { type: 'list_files', path: rel(path) }, more, kind: 'read' }
      if (mode === 'Image') return { action: { type: 'view_image', path: rel(path) }, more, kind: 'read' }
      if (mode === 'Search') {
        const q = str(first.pattern) ?? str(first.query)
        return q ? { action: { type: 'search', query: q, path: rel(path) }, more, kind: 'search' } : null
      }
      const from = num(first.offset) ?? num(first.start_line)
      const limit = num(first.limit)
      const to = num(first.end_line) ?? (from !== undefined && limit !== undefined ? from + limit - 1 : undefined)
      const action: ToolAction = from !== undefined && to !== undefined && more === 0
        ? { type: 'read', path: rel(path), from, to }
        : { type: 'read', path: rel(path) }
      return { action, more, kind: 'read' }
    }
    const path = str(args.path) ?? str(args.file_path)
    if (!path) return null
    const from = num(args.offset)
    const limit = num(args.limit)
    const action: ToolAction = from !== undefined && limit !== undefined
      ? { type: 'read', path: rel(path), from, to: from + limit - 1 }
      : { type: 'read', path: rel(path) }
    return { action, more: 0, kind: 'read' }
  }

  // -- edit / create --------------------------------------------------------
  if (kind === 'edit' || tool === 'fs_write' || tool === 'write' || tool === 'edit' || tool === 'Write' || tool === 'Edit') {
    if (args) {
      const path = str(args.path) ?? str(args.file_path)
      if (!path) return null
      const cmd = str(args.command)
      const isCreate = cmd === 'create' || tool === 'write' || tool === 'Write' || (cmd === undefined && args.content !== undefined && args.oldStr === undefined && args.old_string === undefined)
      return { action: { type: isCreate ? 'create' : 'edit', path: rel(path) }, more: 0, kind: 'edit' }
    }
    if (typeof inp.rawInput === 'string' && inp.rawInput.startsWith('---')) {
      const path = diffTargetPath(inp.rawInput)
      if (!path) return null
      const isCreate = /^--- \/dev\/null\s*$/m.test(inp.rawInput)
      return { action: { type: isCreate ? 'create' : 'edit', path: rel(path) }, more: 0, kind: 'edit' }
    }
    return null
  }

  // -- search (grep / glob) -------------------------------------------------
  if (kind === 'search' || tool === 'grep' || tool === 'glob' || tool === 'Grep' || tool === 'Glob') {
    if (!args) return null
    const pattern = str(args.pattern)
    if (!pattern) return null
    const path = str(args.path)
    const isGlob = tool === 'glob' || tool === 'Glob' || (tool === '' && args.include === undefined && /^Finding\b/.test(title))
    const action: ToolAction = isGlob
      ? { type: 'find_files', pattern, ...(path ? { path: rel(path) } : {}) }
      : { type: 'search', query: pattern, ...(path ? { path: rel(path) } : {}) }
    return { action, more: 0, kind: 'search' }
  }

  // -- fetch / web search ---------------------------------------------------
  if (kind === 'fetch' || tool === 'web_fetch' || tool === 'web_search' || tool === 'WebFetch' || tool === 'WebSearch') {
    if (!args) return null
    const url = str(args.url)
    if (url) {
      const host = hostOf(url)
      return host ? { action: { type: 'fetch', host }, more: 0, kind: 'fetch' } : null
    }
    const query = str(args.query)
    return query ? { action: { type: 'search_web', query }, more: 0, kind: 'fetch' } : null
  }
  return null
}

// ---------------------------------------------------------------------------
// MCP tools
// ---------------------------------------------------------------------------

const MCP_TITLE_RES: RegExp[] = [
  /^(?:Running:\s*)?@([^/\s]+)\/(\S+)$/,
  /^([a-z0-9][\w.-]*)___(\w+)$/i,
  /^mcp__(.+?)__(\w+)$/,
]

/** `(server, tool)` named by an MCP-shaped title, or undefined. */
export function mcpIdentityFromTitle(title: string): { server: string; tool: string } | undefined {
  const t = (title || '').trim()
  for (const re of MCP_TITLE_RES) {
    const m = t.match(re)
    if (m) return { server: m[1], tool: m[2] }
  }
  return undefined
}

/** `session_send` / `artifact-folder-create` / `readSessionTail` -> `Session send` … */
export function humanizeToolName(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_\-.]+/g, ' ')
    .trim()
    .toLowerCase()
  if (!words) return name
  return words[0].toUpperCase() + words.slice(1)
}

function oneLine(s: string, max: number): string {
  const flat = s.replace(/\s+/g, ' ').trim()
  if (flat.length <= max) return flat
  const cut = flat.lastIndexOf(' ', max)
  return (cut >= Math.floor(max / 2) ? flat.slice(0, cut) : flat.slice(0, max)).trimEnd() + '…'
}

/** The first short string argument worth naming in an MCP title. */
export function salientMcpArg(args: Record<string, unknown> | undefined): string | undefined {
  if (!args) return undefined
  const pick = (v: unknown) => {
    const s = str(v)
    return s ? oneLine(s, MAX_MCP_ARG_CHARS) : undefined
  }
  for (const k of MCP_SALIENT_KEYS) {
    if (k in args) {
      const s = pick(args[k])
      if (s) return s
    }
  }
  for (const [k, v] of Object.entries(args)) {
    if (k.startsWith('__') || k === '_meta') continue
    const s = pick(v)
    if (s) return s
  }
  return undefined
}

function classifyMcp(inp: ToolCallTitleInput): ClassifiedToolCall | null {
  const fromTitle = mcpIdentityFromTitle(inp.title || '')
  const tool = inp.toolName || fromTitle?.tool
  if (!tool) return null
  if (!inp.mcpServer && !fromTitle) return null
  const args = parseToolArgs(inp.rawInput)
  const arg = salientMcpArg(args)
  return {
    action: { type: 'mcp', tool, ...(arg ? { arg } : {}) },
    more: 0,
    kind: inp.kind && inp.kind !== 'unknown' ? inp.kind : 'other',
  }
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

/** Titles a transport sends for a shell call when it has no command to show: the
 *  replay stub (`shell`), KAS's fixed display name, claude-agent-acp's
 *  pre-refinement placeholder. Transport identifiers matched exactly, not copy. */
const SHELL_STUB_TITLE_RE = /^(?:shell|execute_bash|Run Command|Terminal|bash|Bash)$/
const SHELL_TOOL_NAME_RE = /^(?:execute_bash|shell|bash|Bash|execute|terminal)$/

function shellCommandOf(inp: ToolCallTitleInput): string | undefined {
  const args = parseToolArgs(inp.rawInput)
  return str(args?.command)
}

function isShellCall(inp: ToolCallTitleInput): boolean {
  if (inp.isShell || inp.kind === 'execute') return true
  if (inp.toolName && SHELL_TOOL_NAME_RE.test(inp.toolName)) return true
  const title = (inp.title || '').trim()
  const kindUnknown = !inp.kind || inp.kind === 'unknown'
  if (!kindUnknown) return false
  // kiro-cli's live shell title carries the command itself (`Running: <cmd>`);
  // an MCP call is `Running: @server/tool` and is not a shell.
  if (title.startsWith('Running:') && !title.startsWith('Running: @')) return true
  return shellCommandOf(inp) !== undefined && SHELL_STUB_TITLE_RE.test(title)
}

/**
 * Structure first: which action this call performs, language-neutral.
 * Returns null when nothing better than the incoming title can be said.
 */
export function classifyToolCall(inp: ToolCallTitleInput): ClassifiedToolCall | null {
  if (isShellCall(inp)) {
    const cmd = shellCommandOf(inp) ?? shellCommandFromTitle(inp.title)
    if (!cmd) return null
    const shell = classifyShellCommand(cmd)
    if (!shell) return null
    const kind = shell.action.type === 'read' || shell.action.type === 'list_files' ? 'read'
      : shell.action.type === 'search' || shell.action.type === 'find_files' ? 'search'
        : 'execute'
    return { ...shell, kind }
  }
  const nativeKind = inp.kind === 'read' || inp.kind === 'edit' || inp.kind === 'search' || inp.kind === 'fetch'
  if (nativeKind || (inp.toolName && NATIVE_TOOL_NAME_RE.test(inp.toolName))) {
    const native = classifyNative(inp)
    if (native) return native
    // An MCP tool can report a native kind (a server-side `read`); fall through.
    if (!inp.mcpServer && !mcpIdentityFromTitle(inp.title || '')) return null
  }
  return classifyMcp(inp)
}

/**
 * True when a transport title says something on its own — false for an empty
 * title and for the stubs a shell tool sends when it has no command to show
 * (`shell` on replay, KAS's `Run Command`, claude-agent-acp's `Terminal`). The
 * raw-titles preference keeps an informative title on the row and lets the
 * derived title replace only these.
 */
export function isInformativeTitle(title: string | undefined): boolean {
  const t = (title || '').trim()
  return t.length > 0 && !SHELL_STUB_TITLE_RE.test(t)
}

/** The command a live kiro-cli title carries (`Running: <cmd>` or the bare command). */
function shellCommandFromTitle(title: string | undefined): string | undefined {
  const t = (title || '').trim()
  if (!t || SHELL_STUB_TITLE_RE.test(t)) return undefined
  return t.replace(/^Running:\s*/, '')
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

/** English-neutral render of one action through the i18n catalog. */
export function renderToolAction(action: ToolAction): string {
  switch (action.type) {
    case 'read':
      return action.from !== undefined && action.to !== undefined
        ? i18nT('utils.toolCallTitle.read_range', { path: action.path, from: action.from, to: action.to })
        : i18nT('utils.toolCallTitle.read', { path: action.path })
    case 'list_files':
      return action.path ? i18nT('utils.toolCallTitle.list_files_in', { path: action.path }) : i18nT('utils.toolCallTitle.list_files')
    case 'search':
      return action.path ? i18nT('utils.toolCallTitle.search_in', { query: action.query, path: action.path }) : i18nT('utils.toolCallTitle.search', { query: action.query })
    case 'find_files':
      return action.path ? i18nT('utils.toolCallTitle.find_files_in', { pattern: action.pattern, path: action.path }) : i18nT('utils.toolCallTitle.find_files', { pattern: action.pattern })
    case 'git':
      return action.path ? i18nT('utils.toolCallTitle.git_path', { sub: action.sub, path: action.path }) : i18nT('utils.toolCallTitle.git', { sub: action.sub })
    case 'install':
      return i18nT('utils.toolCallTitle.install_dependencies')
    case 'test':
      return i18nT('utils.toolCallTitle.run_tests')
    case 'script':
      return i18nT('utils.toolCallTitle.run_script', { name: action.name })
    case 'build':
      return i18nT('utils.toolCallTitle.build')
    case 'lint':
      return i18nT('utils.toolCallTitle.lint')
    case 'print':
      return i18nT('utils.toolCallTitle.print')
    case 'github': {
      const noun = GH_NOUN_DISPLAY[action.noun] ?? action.noun
      return action.target
        ? i18nT('utils.toolCallTitle.github_target', { noun, verb: action.verb, target: action.target })
        : i18nT('utils.toolCallTitle.github', { noun, verb: action.verb })
    }
    case 'github_api':
      return i18nT('utils.toolCallTitle.github_api', { path: action.path })
    case 'fetch':
      return action.method ? i18nT('utils.toolCallTitle.request', { method: action.method, host: action.host }) : i18nT('utils.toolCallTitle.fetch', { host: action.host })
    case 'search_web':
      return i18nT('utils.toolCallTitle.search_web', { query: action.query })
    case 'edit':
      return i18nT('utils.toolCallTitle.edit', { path: action.path })
    case 'create':
      return i18nT('utils.toolCallTitle.create', { path: action.path })
    case 'view_image':
      return i18nT('utils.toolCallTitle.view_image', { path: action.path })
    case 'mcp': {
      const label = humanizeToolName(action.tool)
      return action.arg ? i18nT('utils.toolCallTitle.mcp_with_arg', { title: label, arg: action.arg }) : label
    }
    case 'unknown':
      return action.cmd
  }
}

/** Raw-command fallback: first line, whitespace collapsed, ≤ 80 chars on a word boundary. */
export function formatRawCommand(cmd: string): string {
  const lines = cmd.split('\n').map(l => l.trim()).filter(Boolean)
  if (lines.length === 0) return ''
  const first = lines[0].replace(/\s+/g, ' ')
  const multi = lines.length > 1
  if (first.length <= MAX_RAW_TITLE_CHARS) return multi ? first + ' …' : first
  const cut = first.lastIndexOf(' ', MAX_RAW_TITLE_CHARS)
  return (cut >= 40 ? first.slice(0, cut) : first.slice(0, MAX_RAW_TITLE_CHARS)).trimEnd() + '…'
}

/**
 * The title a tool-call row should show, plus what to keep for the tooltip.
 *
 * Live and replayed rows go through this same function: a replayed shell row
 * whose title degraded to `shell` is recomputed from `rawInput.command`.
 */
export function deriveToolCallTitle(inp: ToolCallTitleInput): DerivedToolTitle {
  const incoming = (inp.title || '').trim()
  const shell = isShellCall(inp)
  const cmd = shell ? (shellCommandOf(inp) ?? shellCommandFromTitle(incoming)) : undefined
  const rawTitle = cmd ?? incoming
  const kindIn = inp.kind && inp.kind !== 'unknown' ? inp.kind : (shell ? 'execute' : 'other')
  const classified = classifyToolCall(inp)
  if (classified) {
    let title = renderToolAction(classified.action)
    if (classified.more > 0) title = i18nT('utils.toolCallTitle.and_more', { title, count: classified.more })
    return { title, kind: classified.kind, rawTitle, derived: true }
  }
  if (shell && cmd) return { title: formatRawCommand(cmd), kind: 'execute', rawTitle, derived: false }
  return { title: incoming, kind: kindIn, rawTitle, derived: false }
}
