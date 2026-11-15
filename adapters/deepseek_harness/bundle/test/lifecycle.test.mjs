/**
 * Lifecycle bridging: session turn events, tool receipts, and best-effort CLI.
 */
import assert from 'node:assert/strict'
import path from 'node:path'
import test from 'node:test'

import { apply } from '../index.js'
import {
	makeAgent,
	makeConfig,
	makeConfigDir,
	makeHarness,
	readJsonLines,
	removeConfigDir,
	waitFor,
	writeMode,
} from './helpers.mjs'

/** Start the plugin and one Agent with a fake lifecycle script. */
async function setup(t, overrides = {}) {
	const configDir = makeConfigDir()
	const harness = makeHarness()
	const agent = makeAgent('session-77777777-0000-0000-0000-000000000009')
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir, overrides))
	await harness.ctx.emitAsync('agent/created', { agent: agent.agent })
	return { harness, agent, configDir, lifecycleLog: path.join(configDir, 'fake-lifecycle.jsonl') }
}

/** Emit one durable Session event for the Agent's session. */
function emitSession(harness, agent, type, data, seq) {
	harness.ctx.emit('session/event', agent.agent.session, {
		type,
		seq,
		time: Date.now(),
		data,
		ignorable: false,
	})
}

test('turn start/end events are forwarded with session-scoped turn ids', async (t) => {
	const { harness, agent, lifecycleLog } = await setup(t)
	const sessionId = agent.agent.session.id
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'agent-created'),
		{ label: 'agent-created lifecycle event' },
	)
	emitSession(harness, agent, 'turn/start', { turn: 4 }, 1)
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'turn-start'),
		{ label: 'turn-start lifecycle event' },
	)
	const started = readJsonLines(lifecycleLog).find((entry) => entry.event === 'turn-start')
	assert.equal(started.turnId, `${sessionId}:4`)
	assert.equal(started.sessionId, sessionId)
	assert.equal(started.agentId, agent.agent.id)

	emitSession(harness, agent, 'turn/end', { turn: 4, reason: { kind: 'aborted', reason: { kind: 'user' } } }, 2)
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'turn-end'),
		{ label: 'turn-end lifecycle event' },
	)
	const ended = readJsonLines(lifecycleLog).find((entry) => entry.event === 'turn-end')
	assert.equal(ended.turnId, `${sessionId}:4`)
	assert.equal(ended.reason, 'cancelled')
})

test('turn/end reasons follow the contract and forked turns stay silent', async (t) => {
	const { harness, agent, lifecycleLog } = await setup(t)
	const sessionId = agent.agent.session.id
	const turnEnds = async () => readJsonLines(lifecycleLog).filter((entry) => entry.event === 'turn-end')

	emitSession(harness, agent, 'turn/start', { turn: 1 }, 1)
	emitSession(harness, agent, 'turn/end', { turn: 1, reason: { kind: 'completed' } }, 2)
	await waitFor(async () => (await turnEnds()).length === 1, { label: 'completed turn end' })
	assert.equal((await turnEnds())[0].reason, 'completed')

	emitSession(harness, agent, 'turn/start', { turn: 2 }, 3)
	emitSession(harness, agent, 'turn/end', { turn: 2, reason: { kind: 'error' } }, 4)
	await waitFor(async () => (await turnEnds()).length === 2, { label: 'error turn end' })
	assert.equal((await turnEnds())[1].reason, 'error')

	emitSession(harness, agent, 'turn/start', { turn: 3 }, 5)
	emitSession(harness, agent, 'turn/end', { turn: 3, reason: { kind: 'forked' } }, 6)
	await new Promise((resolve) => setTimeout(resolve, 200))
	assert.equal((await turnEnds()).length, 2, 'forked turn/end must not be forwarded')

	// Turn ids stay session-scoped even with a bare-UUID session id.
	assert.ok((await turnEnds()).every((entry) => entry.turnId.startsWith(`${sessionId}:`)))
})

test('tool/call + tool/result session events produce one bounded tool-result receipt', async (t) => {
	const { harness, agent, lifecycleLog } = await setup(t)
	const sessionId = agent.agent.session.id
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'agent-created'),
		{ label: 'agent-created lifecycle event' },
	)
	emitSession(harness, agent, 'turn/start', { turn: 7 }, 1)
	emitSession(
		harness,
		agent,
		'tool/call',
		{
			turn: 7,
			step: 1,
			callId: 'call-42',
			name: 'quietbridge_task',
			arguments: { action: 'begin', targetSurface: 'web_content' },
		},
		2,
	)
	emitSession(
		harness,
		agent,
		'tool/result',
		{
			turn: 7,
			step: 1,
			message: {
				source: { kind: 'tool', callId: 'call-42' },
				content: [{ type: 'text', text: 'task begun' }],
				isError: false,
			},
		},
		3,
	)
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'tool-result'),
		{ label: 'tool-result lifecycle event' },
	)
	const receipts = readJsonLines(lifecycleLog).filter((entry) => entry.event === 'tool-result')
	assert.equal(receipts.length, 1)
	assert.equal(receipts[0].turnId, `${sessionId}:7`)
	assert.equal(receipts[0].toolName, 'quietbridge_task')
	assert.deepEqual(receipts[0].toolInput, { action: 'begin', targetSurface: 'web_content' })
	assert.equal(receipts[0].toolResult.content[0].text, 'task begun')
	assert.equal(receipts[0].toolResult.isError, false)

	// Non-QuietBridge tools are ignored.
	emitSession(harness, agent, 'tool/call', { turn: 7, step: 2, callId: 'call-43', name: 'other_tool', arguments: {} }, 4)
	emitSession(
		harness,
		agent,
		'tool/result',
		{
			turn: 7,
			step: 2,
			message: { source: { kind: 'tool', callId: 'call-43' }, content: [], isError: false },
		},
		5,
	)
	await new Promise((resolve) => setTimeout(resolve, 150))
	assert.equal(readJsonLines(lifecycleLog).filter((entry) => entry.event === 'tool-result').length, 1)
})

test('tools/result is a de-duplicated fallback when no turn is available', async (t) => {
	const { harness, agent, lifecycleLog } = await setup(t)
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'agent-created'),
		{ label: 'agent-created lifecycle event' },
	)
	harness.ctx.emit('tools/result', {
		agent: agent.agent,
		name: 'quietbridge_task',
		callId: 'call-fallback',
		arguments: { action: 'status' },
	}, {
		isError: false,
		content: [{ type: 'text', text: 'ok' }],
		value: { content: [{ type: 'text', text: 'ok' }] },
	})
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'tool-result'),
		{ label: 'fallback tool-result lifecycle event' },
	)
	const receipts = readJsonLines(lifecycleLog).filter((entry) => entry.event === 'tool-result')
	assert.equal(receipts.length, 1)
	assert.equal(receipts[0].toolName, 'quietbridge_task')
	assert.equal(receipts[0].turnId, '')
})

test('tools/result forwards the structured task receipt with the matching turn', async (t) => {
	const { harness, agent, lifecycleLog } = await setup(t)
	const sessionId = agent.agent.session.id
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'agent-created'),
		{ label: 'agent-created lifecycle event' },
	)
	emitSession(harness, agent, 'turn/start', { turn: 2 }, 1)
	emitSession(
		harness,
		agent,
		'tool/call',
		{ turn: 2, step: 1, callId: 'call-receipt', name: 'quietbridge_task', arguments: { action: 'begin' } },
		2,
	)
	harness.ctx.emit(
		'tools/result',
		{ agent: agent.agent, name: 'quietbridge_task', callId: 'call-receipt', arguments: { action: 'begin' } },
		{
			isError: false,
			content: [{ type: 'text', text: 'task begun' }],
			value: {
				content: [{ type: 'text', text: 'task begun' }],
				structuredContent: {
					state: 'awaiting_window',
					taskId: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
					lease: { ownerId: 'ffffffff-0000-1111-2222-333333333333' },
				},
			},
		},
	)
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'tool-result'),
		{ label: 'task receipt lifecycle event' },
	)
	const receipt = readJsonLines(lifecycleLog).find((entry) => entry.event === 'tool-result')
	assert.equal(receipt.turnId, `${sessionId}:2`)
	assert.equal(receipt.toolName, 'quietbridge_task')
	assert.deepEqual(receipt.toolInput, { action: 'begin' })
	assert.equal(receipt.toolResult.structuredContent.state, 'awaiting_window')
	assert.equal(receipt.toolResult.structuredContent.taskId, 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
})

test('agent/disposed writes the host session-end lifecycle event', async (t) => {
	const { harness, agent, lifecycleLog } = await setup(t)
	await harness.ctx.emitAsync('agent/disposed', { agent: agent.agent })
	await waitFor(
		() => readJsonLines(lifecycleLog).some((entry) => entry.event === 'agent-disposed'),
		{ label: 'agent-disposed lifecycle event' },
	)
	const disposed = readJsonLines(lifecycleLog).find((entry) => entry.event === 'agent-disposed')
	assert.equal(disposed.sessionId, agent.agent.session.id)
	assert.equal(disposed.agentId, agent.agent.id)
})

test('a hanging lifecycle script times out with a warning and never blocks the host', async (t) => {
	const configDir = makeConfigDir()
	writeMode(configDir, 'fake-lifecycle-mode', 'hang')
	const harness = makeHarness()
	const agent = makeAgent('session-66666666-0000-0000-0000-00000000000a')
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir, { lifecycleTimeoutMs: 200 }))
	await harness.ctx.emitAsync('agent/created', { agent: agent.agent })
	const session = agent.agent.session
	harness.ctx.emit('session/event', agent.agent.session, { type: 'turn/start', seq: 1, time: Date.now(), data: { turn: 1 } })
	await waitFor(() => harness.warns.some((line) => /lifecycle/.test(line) && /超时/.test(line)), {
		label: 'lifecycle timeout warning',
	})
	// The plugin remains responsive and disposable.
	await harness.ctx.emitAsync('agent/disposed', { agent: agent.agent })
	assert.ok(harness.warns.some((line) => /lifecycle/.test(line)))
	assert.ok(typeof session.id === 'string')
})
