/**
 * Cancellation, timeout, and late-response handling for tools/call.
 */
import assert from 'node:assert/strict'
import path from 'node:path'
import test from 'node:test'

import { apply } from '../index.js'
import {
	makeAgent,
	makeConfig,
	makeConfigDir,
	makeExecution,
	makeHarness,
	readJsonLines,
	removeConfigDir,
	waitFor,
} from './helpers.mjs'

/** Start the plugin + one Agent and return both plus the fake connector log. */
async function setup(t, overrides = {}) {
	const configDir = makeConfigDir()
	const harness = makeHarness()
	const agent = makeAgent('session-cccccccc-0000-0000-0000-000000000003')
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir, overrides))
	await harness.ctx.emitAsync('agent/created', { agent: agent.agent })
	return { harness, agent, configDir, log: path.join(configDir, `fake-connector-${agent.agent.id}.jsonl`) }
}

test('abort sends notifications/cancelled and discards the late response', async (t) => {
	const { agent, log } = await setup(t)
	const task = agent.tools.get('quietbridge_task')
	const controller = new AbortController()
	const execution = makeExecution(agent, { signal: controller.signal })
	const pending = task.execute({ action: 'begin', __mode: 'slow' }, execution)
	await waitFor(
		() => readJsonLines(log).some((entry) => entry.method === 'tools/call'),
		{ label: 'fake connector receives tools/call' },
	)
	controller.abort()
	await assert.rejects(pending, /取消/)
	await waitFor(
		() => readJsonLines(log).some((entry) => entry.method === 'cancel-received'),
		{ label: 'notifications/cancelled reaches the connector' },
	)
	const call = readJsonLines(log).find((entry) => entry.method === 'tools/call')
	const cancelled = readJsonLines(log).find((entry) => entry.method === 'cancel-received')
	assert.equal(cancelled.requestId, call.id)
	// The connector answers that cancelled request 50ms later; the client must
	// discard it and keep serving later calls.
	await new Promise((resolve) => setTimeout(resolve, 250))
	const value = await task.execute({ action: 'status' }, makeExecution(agent))
	assert.match(value.content[0].text, /^echo:quietbridge_task:/)
	assert.ok(!JSON.stringify(value).includes('LATE_RESULT_SHOULD_BE_IGNORED'))
})

test('an already-aborted signal fails before writing a tools/call', async (t) => {
	const { agent, log } = await setup(t)
	const task = agent.tools.get('quietbridge_task')
	const controller = new AbortController()
	controller.abort()
	await assert.rejects(task.execute({ action: 'begin' }, makeExecution(agent, { signal: controller.signal })), /取消/)
	const calls = readJsonLines(log).filter((entry) => entry.method === 'tools/call')
	assert.equal(calls.length, 0)
})

test('tools/call timeout rejects with a non-replayable error and keeps the connector usable', async (t) => {
	const { agent } = await setup(t, { requestTimeoutMs: 300, startupTimeoutMs: 2000 })
	const task = agent.tools.get('quietbridge_task')
	const started = Date.now()
	await assert.rejects(task.execute({ action: 'begin', __mode: 'slow' }, makeExecution(agent)), /超时/)
	assert.ok(Date.now() - started >= 250)
	const value = await task.execute({ action: 'status' }, makeExecution(agent))
	assert.match(value.content[0].text, /^echo:quietbridge_task:/)
})
