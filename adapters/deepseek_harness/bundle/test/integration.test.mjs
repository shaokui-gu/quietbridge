/**
 * Protocol-level integration checks against the real Python lifecycle store.
 *
 * These run only when the repository's `lifecycle.py` and a usable `python3`
 * are present; otherwise they are skipped so the bundle test suite still works
 * on a machine without the Python adapter. No DSH, helper, or network is used.
 */
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, realpathSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const bundleDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = path.resolve(bundleDir, '..', '..', '..')
const lifecycleScript = path.join(repoRoot, 'adapters', 'deepseek_harness', 'lifecycle.py')

const python = (() => {
	for (const candidate of ['python3', 'python']) {
		const probe = spawnSync(candidate, ['--version'], { encoding: 'utf8' })
		if (probe.status === 0) return candidate
	}
	return undefined
})()

const skip = (() => {
	if (python === undefined) return 'python3 is not available'
	try {
		readFileSync(lifecycleScript)
	} catch {
		return 'adapters/deepseek_harness/lifecycle.py is not present'
	}
	return false
})()

/** Run one lifecycle event exactly like the bundle does. */
function runLifecycle(configDir, payload) {
	const run = spawnSync(python, ['-B', lifecycleScript, '--config-dir', configDir], {
		input: `${JSON.stringify(payload)}\n`,
		encoding: 'utf8',
		timeout: 15000,
	})
	assert.equal(run.status, 0, run.stderr)
	return run
}

test('real lifecycle.py accepts the bundle protocol and stores the task receipt', { skip }, () => {
	const configDir = mkdtempSync(path.join(realpathSync(tmpdir()), 'quietbridge-dsh-lifecycle-'))
	const sessionId = '11111111-2222-3333-4444-555555555555'
	const agentId = sessionId
	const turnId = `${sessionId}:3`
	const taskId = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
	const ownerId = 'ffffffff-0000-1111-2222-333333333333'
	try {
		runLifecycle(configDir, { event: 'agent-created', sessionId, agentId })
		runLifecycle(configDir, { event: 'turn-start', sessionId, agentId, turnId })
		runLifecycle(configDir, {
			event: 'tool-result',
			sessionId,
			agentId,
			turnId,
			toolName: 'quietbridge_task',
			toolInput: { action: 'begin', targetSurface: 'web_content' },
			toolResult: {
				content: [{ type: 'text', text: 'SENSITIVE-TRANSCRIPT-MARKER' }],
				structuredContent: {
					state: 'awaiting_window',
					taskId,
					lease: { ownerId, idleSeconds: 120, expiresAt: Date.now() / 1000 + 120 },
				},
			},
		})
		runLifecycle(configDir, { event: 'turn-end', sessionId, agentId, turnId, reason: 'completed' })

		const statePath = path.join(configDir, 'host-lifecycle', 'state.json')
		const state = JSON.parse(readFileSync(statePath, 'utf8'))
		const turns = Object.values(state.turns)
		const turn = turns.find((entry) => entry.host === 'deepseek-harness' && entry.sessionId === sessionId)
		assert.ok(turn, 'turn record missing')
		assert.equal(turn.agentId, agentId)
		assert.equal(turn.requestId, turnId)
		assert.ok(turn.stoppedAt !== undefined, 'completed turn must be stopped')
		assert.equal(turn.reason, 'completed')
		assert.equal(turn.tasks[taskId], ownerId)
		const serialized = JSON.stringify(state)
		assert.ok(!serialized.includes('SENSITIVE-TRANSCRIPT-MARKER'), 'lifecycle store must not retain transcript text')
		assert.ok(!serialized.includes('web_content'), 'lifecycle store must not retain tool arguments')
	} finally {
		rmSync(configDir, { recursive: true, force: true })
	}
})

test('real lifecycle.py turns agent-disposed into a session-end tombstone', { skip }, () => {
	const configDir = mkdtempSync(path.join(realpathSync(tmpdir()), 'quietbridge-dsh-lifecycle-'))
	const sessionId = '22222222-3333-4444-5555-666666666666'
	const agentId = sessionId
	const turnId = `${sessionId}:1`
	try {
		runLifecycle(configDir, { event: 'turn-start', sessionId, agentId, turnId })
		runLifecycle(configDir, { event: 'agent-disposed', sessionId, agentId })
		const state = JSON.parse(readFileSync(path.join(configDir, 'host-lifecycle', 'state.json'), 'utf8'))
		const turns = Object.values(state.turns)
		const stopped = turns.find((entry) => entry.sessionId === sessionId && entry.stoppedAt !== undefined)
		assert.ok(stopped, 'a stopped turn record must exist after agent-disposed')
		assert.equal(stopped.sessionEnded, true)
		assert.ok(turns.some((entry) => entry.sessionId === sessionId && entry.requestId === null && entry.sessionEnded === true))
	} finally {
		rmSync(configDir, { recursive: true, force: true })
	}
})
