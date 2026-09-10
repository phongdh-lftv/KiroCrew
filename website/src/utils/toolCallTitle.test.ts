import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  classifyToolCall,
  deriveToolCallTitle,
  formatRawCommand,
  humanizeToolName,
  mcpIdentityFromTitle,
  relDisplayPath,
  type ToolAction,
  type ToolCallTitleInput,
} from './toolCallTitle'
import { shortDisplayPath, tokenizeShell } from './shellCommandParse'

/**
 * The TypeScript half of the tool-call title conformance suite.
 *
 * Reads the SAME fixture as `test/test_tool_call_title.py`. The two
 * implementations (this module for the dashboard, `kiro_crew/tool_call_title.py`
 * for the Slack / Discord renderers) must agree on every case's ACTION and its
 * English TITLE, or a user sees one label in the dashboard and another in the
 * channel for the same call. The fixture is the contract; each side asserts it
 * in its own idiom.
 */
const FIXTURE = path.resolve(__dirname, '../../../test/fixtures/tool_call_titles.json')

type Case = {
  name: string
  input: ToolCallTitleInput
  expect: { action: ToolAction | null; more: number; title: string; kind: string; derived: boolean }
}

const cases: Case[] = JSON.parse(readFileSync(FIXTURE, 'utf8')).cases

describe('toolCallTitle fixture conformance', () => {
  it('has at least the corpus-shaped cases', () => {
    expect(cases.length).toBeGreaterThan(50)
  })

  for (const c of cases) {
    it(c.name, () => {
      const classified = classifyToolCall(c.input)
      if (c.expect.action === null) {
        expect(classified).toBeNull()
      } else {
        expect(classified?.action).toEqual(c.expect.action)
        expect(classified?.more).toBe(c.expect.more)
      }
      const derived = deriveToolCallTitle(c.input)
      expect(derived.title).toBe(c.expect.title)
      expect(derived.kind).toBe(c.expect.kind)
      expect(derived.derived).toBe(c.expect.derived)
    })
  }
})

describe('tokenizeShell', () => {
  it('splits words, connectors and newlines', () => {
    expect(tokenizeShell('ls -la && cat a\ngrep x | wc')).toEqual([
      { word: 'ls' }, { word: '-la' }, { op: '&&' }, { word: 'cat' }, { word: 'a' }, { op: ';' },
      { word: 'grep' }, { word: 'x' }, { op: '|' }, { word: 'wc' },
    ])
  })

  it('joins quoted concatenations into one word', () => {
    expect(tokenizeShell(`rg -g"*.py" 'a b'`)).toEqual([{ word: 'rg' }, { word: '-g*.py' }, { word: 'a b' }])
  })

  it.each([
    ['$VAR', 'ls $HOME'],
    ['substitution', 'echo $(date)'],
    ['redirect', 'ls > out.txt'],
    ['append redirect', 'ls >> out.txt'],
    ['input redirect', 'wc < a.txt'],
    ['heredoc', "cat <<'EOF'\nx\nEOF"],
    ['glob', 'ls *.py'],
    ['tilde', 'cat ~/a'],
    ['subshell', '(ls)'],
    ['background', 'sleep 1 &'],
    ['backtick', 'echo `date`'],
    ['unterminated quote', "echo 'abc"],
    ['expansion in double quotes', 'echo "$X"'],
    ['comment', 'ls # list'],
  ])('rejects %s', (_label, script) => {
    expect(tokenizeShell(script)).toBeNull()
  })

  it('strips stderr-silencing redirects only', () => {
    expect(tokenizeShell('ls x 2>/dev/null')).toEqual([{ word: 'ls' }, { word: 'x' }])
    expect(tokenizeShell('ls x 2>&1')).toEqual([{ word: 'ls' }, { word: 'x' }])
    expect(tokenizeShell('ls x 2>err.txt')).toBeNull()
  })
})

describe('shortDisplayPath', () => {
  it.each([
    ['webview/src', 'webview'],
    ['foo/src/', 'foo'],
    ['packages/app/node_modules/', 'app'],
    ['src', 'src'],
    ['/a/b/c.ts', 'c.ts'],
    ['.', '.'],
    ['C:\\x\\y.txt', 'y.txt'],
  ])('%s -> %s', (input, expected) => {
    expect(shortDisplayPath(input)).toBe(expected)
  })
})

describe('relDisplayPath', () => {
  it('is relative under cwd, basename at cwd, parent/basename elsewhere', () => {
    expect(relDisplayPath('/p/src/a.ts', '/p')).toBe('src/a.ts')
    expect(relDisplayPath('/p', '/p')).toBe('p')
    expect(relDisplayPath('/q/r/s/t.ts', '/p')).toBe('s/t.ts')
    expect(relDisplayPath('/q/t.ts')).toBe('q/t.ts')
    expect(relDisplayPath('~/n/t.md')).toBe('~/n/t.md')
  })
})

describe('mcpIdentityFromTitle', () => {
  it.each([
    ['@kirocrew-core/session_send', 'kirocrew-core', 'session_send'],
    ['Running: @kirocrew-dashboard/chat_folder_tree', 'kirocrew-dashboard', 'chat_folder_tree'],
    ['kirocrew-core___wait', 'kirocrew-core', 'wait'],
    ['mcp__github__list_prs', 'github', 'list_prs'],
  ])('%s', (title, server, tool) => {
    expect(mcpIdentityFromTitle(title)).toEqual({ server, tool })
  })

  it('does not match a shell title or a plain phrase', () => {
    expect(mcpIdentityFromTitle('Running: ls -la')).toBeUndefined()
    expect(mcpIdentityFromTitle('Reading a.rs:1-20')).toBeUndefined()
    expect(mcpIdentityFromTitle('user@host/path')).toBeUndefined()
  })
})

describe('humanizeToolName', () => {
  it.each([
    ['session_send', 'Session send'],
    ['artifact-folder-create', 'Artifact folder create'],
    ['ChorusDocRead', 'Chorus doc read'],
    ['wait', 'Wait'],
  ])('%s -> %s', (name, expected) => {
    expect(humanizeToolName(name)).toBe(expected)
  })
})

describe('formatRawCommand', () => {
  it('keeps a short single line verbatim', () => {
    expect(formatRawCommand('ls -la')).toBe('ls -la')
  })

  it('marks further lines with an ellipsis', () => {
    expect(formatRawCommand('cat > x <<EOF\nbody\nEOF')).toBe('cat > x <<EOF …')
  })

  it('cuts a long line on a word boundary at or before 80 characters', () => {
    const words = Array.from({ length: 30 }, (_, i) => `word${i}`).join(' ')
    const out = formatRawCommand(words)
    expect(out.endsWith('…')).toBe(true)
    expect(out.length).toBeLessThanOrEqual(81)
    expect(words.startsWith(out.slice(0, -1))).toBe(true)
    expect(out.slice(0, -1).endsWith(' ')).toBe(false)
  })

  it('collapses internal whitespace', () => {
    expect(formatRawCommand('ls   -la\t src')).toBe('ls -la src')
  })
})

describe('deriveToolCallTitle rawTitle', () => {
  it('keeps the verbatim command for a shell call', () => {
    const d = deriveToolCallTitle({ kind: 'execute', title: 'shell', rawInput: { command: 'ls -la src' } })
    expect(d.rawTitle).toBe('ls -la src')
    expect(d.title).toBe('List files in src')
  })

  it('keeps the incoming title for a non-shell call', () => {
    const d = deriveToolCallTitle({ kind: 'other', title: '@kirocrew-core/session_send', rawInput: { target: 'x' } })
    expect(d.rawTitle).toBe('@kirocrew-core/session_send')
  })
})
