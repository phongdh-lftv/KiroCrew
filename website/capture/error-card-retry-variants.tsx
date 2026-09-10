/**
 * Evidence sheet for #9932 — every gateway "please retry" error row, re-spoken
 * by the real ErrorCard with the Resume verb, in the resumable shape (button
 * present) so the banner and the button can be read side by side.
 *
 * The left column is the WIRE string the gateway emits (byte-for-byte from
 * chat_runner.py); the card beside it is what the user sees. One row per
 * `pages.chat.errorCard.retry_*` key, plus the settled shape (no button) and
 * an unknown gateway string to show the verbatim fallback still holds.
 *
 *   ?theme=dark|light   ?lang=<locale>
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import { ErrorCard } from '../src/pages/chat/ErrorCard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

/** Gateway wire strings, verbatim (src/kiro_crew/dashboard/chat_runner.py). */
const WIRE: ReadonlyArray<[label: string, content: string]> = [
  ['connection lost', '⟳ Connection lost — please retry.'],
  ['connection lost + exit code', '⟳ Connection lost (exit 1) — please retry.'],
  ['session busy', '⟳ Session busy — please retry.'],
  ['turn stalled', '⟳ Turn stalled — please retry.'],
  ['tool stalled', '⟳ Tool appeared stalled — please retry.'],
  ['backend hiccup', '⟳ Backend hiccup — please retry.'],
]

function Label({ children }: { children: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        opacity: 0.55,
        margin: '18px 0 6px',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      }}
    >
      {children}
    </div>
  )
}

function Wire({ children }: { children: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        opacity: 0.5,
        margin: '0 0 6px',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      }}
    >
      {children}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <div
    data-testid="capture-root"
    style={{ width: 720, padding: '8px 24px 24px', display: 'flex', flexDirection: 'column', gap: 4 }}
  >
    <Label>resumable — banner + Resume button</Label>
    {WIRE.map(([label, content]) => (
      <div key={label} data-variant={label}>
        <Wire>{`wire: ${content}`}</Wire>
        <ErrorCard content={content} onContinue={() => {}} />
      </div>
    ))}
    <Label>settled — same copy, no button</Label>
    <div data-variant="settled">
      <Wire>{`wire: ${WIRE[2][1]}`}</Wire>
      <ErrorCard content={WIRE[2][1]} />
    </div>
    <Label>unknown gateway string — rendered verbatim</Label>
    <div data-variant="unknown">
      <Wire>wire: ⟳ Something this build has no copy for — please retry.</Wire>
      <ErrorCard content="⟳ Something this build has no copy for — please retry." onContinue={() => {}} />
    </div>
  </div>,
)
