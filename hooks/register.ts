import type { EngineInterface as Engine, Register } from 'claude-code'

const STORE_KEY = 'enabled'
const STARTUP_MS = 120_000
const POINTING = /\b(this|that|these|those|here|there|circled|circle|highlighted|selected|outlined|boxed)\b/i

let uvPath = 'uv'
let onlyWithPointingWords = true
let useScreenshots = true
let isEnabled = false
// the helper prints a secret on its first line; every call to it must send that secret back
let helperToken = ''
const SWEEP_AFTER_ANSWER_S = 900
let server: Promise<string | undefined> | undefined
// bundle folders waiting to be deleted after the answer
let pending: { dir: string; root: string; tries: number; at: number }[] = []
const MAX_TRIES = 3
const MAX_PENDING = 50
const TURN_START_SLACK_MS = 2000

type Status = { ok: boolean; screen: boolean; accessibility: boolean; mic_in_use: boolean; recordings: number }

function showStatus($: Engine, text: string | undefined): void {
  $.ui.status(text)
}

// The helper runs for the life of the module and ends with it.
async function runHelper(
  $: Engine,
  candidates: readonly string[],
  index: number,
  onReady: (base: string | undefined) => void,
): Promise<void> {
  const uv = candidates[index]
  if (uv === undefined) return onReady(undefined)
  const script = `${$.plugin.root}/helper/pointer_helper.py`
  const child = $.process.spawn({ argv: [uv, 'run', '--script', script], cwd: `${$.plugin.root}/helper` })
  let tail = ''
  let hasStarted = false
  try {
    for await (const { stream, text } of child) {
      if (stream === 'stderr') {
        tail = (tail + text).slice(-400)
        continue
      }
      const match = text.match(/"port"\s*:\s*(\d+)/)
      const secret = text.match(/"token"\s*:\s*"([^"]+)"/)
      if (match && secret && !hasStarted) {
        hasStarted = true
        helperToken = secret[1] ?? ''
        onReady(`http://127.0.0.1:${match[1]}`)
      }
    }
  } catch {
    if (!hasStarted) return runHelper($, candidates, index + 1, onReady)
  }
  server = undefined
  if (!hasStarted) {
    $.ui.log(`point-and-speak: helper exited before it was ready. ${tail.trim()}`, { to: 'debug' })
    onReady(undefined)
  }
}

async function launch($: Engine): Promise<string | undefined> {
  const home = await $.env.get('HOME')
  const candidates = [uvPath, ...(home ? [`${home}/.local/bin/uv`] : []), '/opt/homebrew/bin/uv']
  return new Promise<string | undefined>(resolve => {
    void runHelper($, candidates, 0, resolve)
    void $.clock.after(STARTUP_MS, () => resolve(undefined))
  })
}

function ensureHelper($: Engine): Promise<string | undefined> {
  if (!server) {
    const started = launch($).then(async base => {
      if (base === undefined && server === started) server = undefined
      if (base !== undefined) await syncHelper($, base)
      return base
    })
    server = started
  }
  return server
}

async function call($: Engine, base: string, path: string, body?: unknown): Promise<Record<string, unknown> | undefined> {
  try {
    const res = await $.http.fetch(`${base}${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'content-type': 'application/json', 'x-look-token': helperToken },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    return res.ok ? (JSON.parse(res.text) as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

async function refreshStatus($: Engine): Promise<string> {
  const base = await ensureHelper($)
  if (!base) {
    showStatus($, 'look: helper not running')
    return 'The helper did not start. Is `uv` installed?'
  }
  const s = (await call($, base, '/status')) as Status | undefined
  if (!s) {
    showStatus($, 'look: helper not answering')
    return 'The helper did not answer.'
  }
  const missing = [s.screen ? '' : 'Screen Recording', s.accessibility ? '' : 'Accessibility'].filter(Boolean)
  showStatus($, missing.length ? `look: on, needs ${missing.join(' + ')}` : 'look: on')
  return missing.length
    ? `On, but macOS permission is missing: ${missing.join(' and ')}. Without it the screenshot and the element under the pointer are left out. Grant it to your terminal app in System Settings, Privacy & Security.`
    : 'On. Screen Recording and Accessibility are granted.'
}

// The helper does nothing until told to. This tells it: on or off, screenshots or not, and the one folder it may use.
async function syncHelper($: Engine, base: string): Promise<void> {
  await call($, base, '/enable', { on: isEnabled, screenshots: useScreenshots, root: `${await $.session.cwd()}/.claude/lookat` })
}

function newId(now: number): string {
  return `${Math.floor(now / 1000).toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`
}

async function enable($: Engine): Promise<string> {
  isEnabled = true
  await $.store.set(STORE_KEY, true)
  showStatus($, 'look: starting')
  const note = await refreshStatus($)
  const root = `${await $.session.cwd()}/.claude/lookat`
  const base = await ensureHelper($)
  if (base) {
    await syncHelper($, base)
    await call($, base, '/sweep', { root, older_s: 3600 })
  }
  return `Pointer context on. Dictate with /voice (or type) while you hover over what you mean. ${note}`
}

async function sweepOld($: Engine, olderS: number): Promise<void> {
  const base = server ? await server : undefined
  if (!base) return
  await call($, base, '/sweep', { root: `${await $.session.cwd()}/.claude/lookat`, older_s: olderS })
}

async function disable($: Engine): Promise<string> {
  await sweepOld($, 0)
  pending = []
  isEnabled = false
  const base = server ? await server : undefined
  if (base) await syncHelper($, base) // the helper stops sampling and drops what it holds
  await $.store.set(STORE_KEY, false)
  showStatus($, undefined)
  return 'Pointer context off.'
}

// Deletes the folders of the prompt whose turn just ended. A prompt submitted after that turn began is still waiting,
// so its folder stays. A folder also stays on the list if the helper is down or refuses, and is tried again at the
// next answer, up to MAX_TRIES times (the later sweeps catch the rest).
async function cleanup($: Engine, turnStart: number): Promise<void> {
  if (pending.length === 0 && !isEnabled) return
  const base = server ? await server : undefined
  if (!base) return
  const due = pending.filter(p => p.at <= turnStart + TURN_START_SLACK_MS)
  pending = pending.filter(p => p.at > turnStart + TURN_START_SLACK_MS)
  const left: typeof pending = []
  for (const p of due) {
    const out = await call($, base, '/cleanup', { dir: p.dir, root: p.root })
    if (out?.ok !== true && p.tries + 1 < MAX_TRIES) left.push({ ...p, tries: p.tries + 1 })
  }
  pending = [...left, ...pending].slice(-MAX_PENDING)
  // folders the list lost (a reload, a crash, a cancelled prompt) are removed once they are old
  if (isEnabled) await sweepOld($, SWEEP_AFTER_ANSWER_S)
}

export const register: Register = (on, options) => {
  uvPath = String(options.uvPath ?? uvPath)
  onlyWithPointingWords = options.onlyWithPointingWords !== false
  useScreenshots = options.screenshots !== false

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'look',
      description: 'Pointer context for voice or typed prompts. /look on | off | status',
      argumentHint: 'on|off|status',
    })
    if ((await $.store.get(STORE_KEY)) === true) {
      isEnabled = true
      showStatus($, 'look: starting')
      void refreshStatus($)
        .then(() => sweepOld($, 3600))
        .catch(() => undefined)
    }
    return next(e)
  })

  on('command.run', { command: 'look' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    if (arg === 'off') return { text: await disable($) }
    if (arg === 'status') {
      return { text: isEnabled ? await refreshStatus($) : 'Pointer context is off. /look on turns it on.' }
    }
    return { text: await enable($) }
  })

  // add where the pointer was, as hidden context beside the person's own words
  on('prompt.submit', async ($, e, next) => {
    if (!isEnabled || e.origin.kind !== 'composer' || !e.text.trim()) return next(e)
    if (onlyWithPointingWords && !POINTING.test(e.text)) return next(e)
    const base = await ensureHelper($)
    if (!base) return next(e)
    const now = await $.clock.now()
    const root = `${await $.session.cwd()}/.claude/lookat`
    const dir = `${root}/${newId(now)}`
    const out = await call($, base, '/bundle', { text: e.text, submit_at: now / 1000, dir })
    const context = typeof out?.context === 'string' ? out.context : ''
    if (!context) return next(e)
    pending = [...pending, { dir, root, tries: 0, at: now }].slice(-MAX_PENDING)
    return next({ ...e, context: [...(e.context ?? []), context] })
  }).catch(($, e, next) => next(e))

  // the answer is in: delete the screenshots and crops
  on('turn.complete', async ($, e, next) => {
    const result = await next(e)
    if (e.reason === 'answer' && e.agentId === undefined) await cleanup($, (await $.clock.now()) - e.durationMs)
    return result
  })
}
