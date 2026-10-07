import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'

type Call = { path: string; body: Record<string, unknown>; token?: string }
type Sim = {
  calls: Call[]
  submitted: { text: string; context?: readonly string[] }[]
  stored: Record<string, unknown>
  status: (string | undefined)[]
  clock: ReturnType<typeof mock.clock>
}
type Behaviour = { isHelperUp?: boolean; screen?: boolean; accessibility?: boolean; context?: string; failCleanups?: number }

const turn = { answer: 'ok', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer' } as const

function world(on: On, stored: Record<string, unknown> = {}, b: Behaviour = {}): Sim {
  let cleanupCalls = 0
  const sim: Sim = { calls: [], submitted: [], stored: { ...stored }, status: [], clock: mock.clock(on) }
  mock.env(on, { HOME: '/home/test' })
  on('session.cwd', async () => ({ value: '/work' }))
  on('store.get', async (_$, e) => ({ value: sim.stored[e.key] }))
  on('store.set', async (_$, e) => {
    sim.stored[e.key] = e.value
    return { value: undefined }
  })
  on('ui.status', async (_$, e) => {
    sim.status.push(e.text)
    return { value: undefined }
  })
  on('ui.log', async () => ({ value: undefined }))
  on('command.register', async (_$, e) => ({ value: { command: e.name } }))
  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  on('turn.complete', async (_$, e) => ({ text: e.answer }))
  on('prompt.submit', async (_$, e) => {
    sim.submitted.push({ text: e.text, context: e.context })
    return { text: e.text }
  })
  on('process.spawn', async function* () {
    if (b.isHelperUp === false) return { value: { code: 1, signal: null } }
    yield { stream: 'stdout' as const, text: '{"port": 7777, "token": "tok123"}\n' }
    await sim.clock.sleep(3_600_000)
    return { value: { code: 0, signal: null } }
  })
  on('http.fetch', async (_$, e) => {
    const path = new URL(e.url).pathname
    const body = e.init?.body ? (JSON.parse(e.init.body) as Record<string, unknown>) : {}
    const headers = (e.init?.headers ?? {}) as Record<string, string>
    sim.calls.push({ path, body, token: headers['x-look-token'] })
    let payload: unknown = { ok: true }
    if (path === '/status') payload = { ok: true, screen: b.screen ?? true, accessibility: b.accessibility ?? true, mic_in_use: false, recordings: 0 }
    if (path === '/cleanup' && (b.failCleanups ?? 0) > cleanupCalls++) payload = { ok: false }
    if (path === '/bundle') payload = { ok: true, context: b.context ?? 'POINTER CONTEXT', dir: body['dir'], files: [] }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(payload) } }
  })
  return sim
}

const start = ($: Engine) => $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
const run = ($: Engine, args: string) =>
  $.command.run({ command: 'look', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
const say = ($: Engine, text: string, kind: 'composer' | 'plugin' = 'composer') =>
  $.prompt.submit(kind === 'composer' ? { text, wait: false, origin: { kind: 'composer' } } : { text, wait: false, origin: { kind: 'plugin', name: 'x' } })
const calls = (sim: Sim, path: string) => sim.calls.filter(c => c.path === path)

test('/look on starts the helper, remembers the choice, and sweeps old folders', async ($, on) => {
  const sim = world(on)
  await start($)
  const r = await run($, 'on')
  expect(r.text).toContain('Pointer context on')
  expect(sim.stored['enabled']).toBe(true)
  expect(calls(sim, '/sweep')[0]?.body['root']).toBe('/work/.claude/lookat')
  expect(sim.status.at(-1)).toBe('look: on')
})

test('missing macOS permissions are named, and shown in the status line', async ($, on) => {
  const sim = world(on, {}, { screen: false })
  await start($)
  const r = await run($, 'on')
  expect(r.text).toContain('Screen Recording')
  expect(sim.status.at(-1)).toContain('needs Screen Recording')
})

test('a prompt with a pointing word gets the pointer context added', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'make this bigger')
  expect(calls(sim, '/bundle').length).toBe(1)
  const body = calls(sim, '/bundle')[0]!.body
  expect(body['text']).toBe('make this bigger')
  expect(String(body['dir'])).toMatch(/^\/work\/\.claude\/lookat\/[a-z0-9-]+$/)
  expect(typeof body['submit_at']).toBe('number')
  expect(sim.submitted.at(-1)?.context).toEqual(['POINTER CONTEXT'])
})

test('a prompt with no pointing word is left alone by default', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'run the tests')
  expect(calls(sim, '/bundle')).toEqual([])
  expect(sim.submitted.at(-1)?.context).toBeUndefined()
})

test('with onlyWithPointingWords off, every prompt gets context', { options: { onlyWithPointingWords: false } }, async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'run the tests')
  expect(calls(sim, '/bundle').length).toBe(1)
})

test('switched off: nothing is added', async ($, on) => {
  const sim = world(on, {})
  await start($)
  await say($, 'make this bigger')
  expect(calls(sim, '/bundle')).toEqual([])
})

test('a prompt a plugin sent is never given pointer context', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'make this bigger', 'plugin')
  expect(calls(sim, '/bundle')).toEqual([])
})

test('the folder is deleted after a successful answer, not before', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'fix this')
  const dir = calls(sim, '/bundle')[0]!.body['dir']
  expect(calls(sim, '/cleanup')).toEqual([])
  await $.turn.complete(turn)
  expect(calls(sim, '/cleanup').map(c => c.body['dir'])).toEqual([dir])
  expect(calls(sim, '/cleanup')[0]?.body['root']).toBe('/work/.claude/lookat')
  await $.turn.complete(turn)
  expect(calls(sim, '/cleanup').length).toBe(1)
})

test('an interrupted turn keeps the folder for a retry', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'fix this')
  await $.turn.complete({ ...turn, reason: 'aborted', isAborted: true })
  expect(calls(sim, '/cleanup')).toEqual([])
})

test('a subagent turn does not trigger cleanup', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'fix this')
  await $.turn.complete({ ...turn, agentId: 'sub1' })
  expect(calls(sim, '/cleanup')).toEqual([])
})

test('when the helper has nothing to add, the prompt goes through unchanged and nothing is cleaned', async ($, on) => {
  const sim = world(on, { enabled: true }, { context: '' })
  await start($)
  await say($, 'fix this')
  expect(sim.submitted.at(-1)?.context).toBeUndefined()
  await $.turn.complete(turn)
  expect(calls(sim, '/cleanup')).toEqual([])
})

test('if the helper cannot start, the prompt still goes through', async ($, on) => {
  const sim = world(on, { enabled: true }, { isHelperUp: false })
  await start($)
  await say($, 'fix this')
  expect(sim.submitted.at(-1)?.text).toBe('fix this')
  expect(sim.submitted.at(-1)?.context).toBeUndefined()
})

test('/look off stops adding context', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  expect((await run($, 'off')).text).toBe('Pointer context off.')
  expect(sim.stored['enabled']).toBe(false)
  await say($, 'fix this')
  expect(calls(sim, '/bundle')).toEqual([])
})

test('a cleanup the helper refuses is tried again at the next answer, then not again', async ($, on) => {
  const sim = world(on, { enabled: true }, { failCleanups: 1 })
  await start($)
  await say($, 'fix this')
  await $.turn.complete(turn)
  expect(calls(sim, '/cleanup').length).toBe(1)
  await $.turn.complete(turn)
  expect(calls(sim, '/cleanup').length).toBe(2)
  await $.turn.complete(turn)
  expect(calls(sim, '/cleanup').length).toBe(2)
})

test('after three refusals the mod gives up on that folder', async ($, on) => {
  const sim = world(on, { enabled: true }, { failCleanups: 99 })
  await start($)
  await say($, 'fix this')
  for (let i = 0; i < 6; i++) await $.turn.complete(turn)
  expect(calls(sim, '/cleanup').length).toBe(3)
})

test('every call to the helper carries its secret token', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'fix this')
  await $.turn.complete(turn)
  expect(sim.calls.length).toBeGreaterThan(2)
  expect(sim.calls.every(c => c.token === 'tok123')).toBe(true)
})

test('a successful answer also sweeps old folders the list lost', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'fix this')
  await $.turn.complete(turn)
  const sweeps = calls(sim, '/sweep')
  expect(sweeps.at(-1)?.body['older_s']).toBe(900)
  expect(sweeps.at(-1)?.body['root']).toBe('/work/.claude/lookat')
})

test('a session that starts with the mod on sweeps leftovers older than an hour', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await sim.clock.settle()
  expect(calls(sim, '/sweep').some(c => c.body['older_s'] === 3600)).toBe(true)
})

test('/look off removes every folder at once', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'fix this')
  await run($, 'off')
  expect(calls(sim, '/sweep').some(c => c.body['older_s'] === 0)).toBe(true)
  await $.turn.complete(turn)
  expect(calls(sim, '/cleanup')).toEqual([])
})

test('the helper is told to work, with the folder it may use, when the mod turns on', async ($, on) => {
  const sim = world(on)
  await start($)
  await run($, 'on')
  const enables = calls(sim, '/enable')
  expect(enables.at(-1)?.body).toEqual({ on: true, screenshots: true, root: '/work/.claude/lookat' })
})

test('/look off tells the helper to stop and drop what it holds', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await run($, 'off')
  expect(calls(sim, '/enable').at(-1)?.body['on']).toBe(false)
})

test('a session that starts with the mod on switches the helper on at its first use', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await sim.clock.settle()
  expect(calls(sim, '/enable').some(c => c.body['on'] === true)).toBe(true)
})

test('with screenshots turned off the helper is told so', { options: { screenshots: false } }, async ($, on) => {
  const sim = world(on)
  await start($)
  await run($, 'on')
  expect(calls(sim, '/enable').at(-1)?.body['screenshots']).toBe(false)
})

test('a prompt sent while a turn runs keeps its folder when the first turn ends', async ($, on) => {
  const sim = world(on, { enabled: true })
  await start($)
  await say($, 'fix this')
  await sim.clock.advance(5000)
  await say($, 'and fix that')
  const dirs = calls(sim, '/bundle').map(c => c.body['dir'])
  await $.turn.complete({ ...turn, durationMs: 6000 })
  expect(calls(sim, '/cleanup').map(c => c.body['dir'])).toEqual([dirs[0]])
  await $.turn.complete({ ...turn, durationMs: 1000 })
  expect(calls(sim, '/cleanup').map(c => c.body['dir'])).toEqual([dirs[0], dirs[1]])
})
