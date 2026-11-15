/**
 * Cleanup, tool-list re-sync, and activation-time Agent adoption.
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
	registeredToolNames,
	removeConfigDir,
	waitFor,
	writeMode,
} from './helpers.mjs'

test('plugin unload closes every connector, removes tools, and writes agent-disposed', async (t) => {
	const configDir = makeConfigDir()
	const harness = makeHarness()
	const agent = makeAgent('session-55555555-0000-0000-0000-00000000000b')
	t.after(() => removeConfigDir(configDir))
	apply(harness.ctx, makeConfig(configDir))
	await harness.ctx.emitAsync('agent/created', { agent: agent.agent })
	assert.deepEqual(registeredToolNames(agent), ['quietbridge_observe', 'quietbridge_task'])
	const tool = agent.tools.get('quietbridge_task')

	await harness.disposePlugin()

	assert.deepEqual(registeredToolNames(agent), [])
	await assert.rejects(tool.execute({ action: 'begin' }, makeExecution(agent)), /连接器/)
	await waitFor(
		() =>
			readJsonLines(path.join(configDir, `fake-connector-${agent.agent.id}.jsonl`)).some(
				(entry) => entry.method === 'shutdown',
			),
		{ label: 'connector shutdown after plugin unload' },
	)
	await waitFor(
		() => readJsonLines(path.join(configDir, 'fake-lifecycle.jsonl')).some((entry) => entry.event === 'agent-disposed'),
		{ label: 'agent-disposed after plugin unload' },
	)
	assert.equal(harness.errors.length, 0)
})

test('Agent-scope effect release is the early teardown owner (double owner)', { timeout: 10000 }, async (t) => {
	const configDir = makeConfigDir()
	const harness = makeHarness()
	const agent = makeAgent('session-14141414-0000-0000-0000-000000000010')
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir))
	await harness.ctx.emitAsync('agent/created', { agent: agent.agent })
	assert.deepEqual(registeredToolNames(agent), ['quietbridge_observe', 'quietbridge_task'])
	const agentEffectCleanup = agent.agentCtx.effectCleanups[0]
	assert.equal(typeof agentEffectCleanup, 'function')
	// Release order in DSH: agent.ctx effects run before agent/disposed.
	await agentEffectCleanup()
	assert.deepEqual(registeredToolNames(agent), [])
	await waitFor(
		() =>
			readJsonLines(path.join(configDir, `fake-connector-${agent.agent.id}.jsonl`)).some(
				(entry) => entry.method === 'shutdown',
			),
		{ label: 'connector shutdown from agent-scope effect' },
	)
	await waitFor(
		() =>
			readJsonLines(path.join(configDir, 'fake-lifecycle.jsonl')).some(
				(entry) => entry.event === 'agent-disposed',
			),
		{ label: 'agent-disposed from agent-scope effect' },
	)
	// A late agent/disposed event must be idempotent.
	await harness.ctx.emitAsync('agent/disposed', { agent: agent.agent })
})

test('connector spawn is capped and extra Agents degrade to no tools', async (t) => {
	const configDir = makeConfigDir()
	const harness = makeHarness()
	const first = makeAgent('session-12121212-0000-0000-0000-00000000000e')
	const second = makeAgent('session-13131313-0000-0000-0000-00000000000f')
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir, { maxConnectors: 1 }))
	await harness.ctx.emitAsync('agent/created', { agent: first.agent })
	await harness.ctx.emitAsync('agent/created', { agent: second.agent })
	assert.deepEqual(registeredToolNames(first), ['quietbridge_observe', 'quietbridge_task'])
	assert.deepEqual(registeredToolNames(second), [])
	assert.ok(harness.warns.some((line) => /上限/.test(line)))
	// The first Agent still owns the only live connector.
	const value = await first.tools.get('quietbridge_task').execute({ action: 'status' }, makeExecution(first))
	assert.match(value.content[0].text, /^echo:quietbridge_task:/)
})

test('notifications/tools/list_changed re-syncs the agent tool set', async (t) => {
	const configDir = makeConfigDir()
	writeMode(configDir, 'fake-mode', 'list-changed')
	const harness = makeHarness()
	const agent = makeAgent('session-44444444-0000-0000-0000-00000000000c')
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir))
	await harness.ctx.emitAsync('agent/created', { agent: agent.agent })
	assert.deepEqual(registeredToolNames(agent), ['quietbridge_observe', 'quietbridge_task'])
	await waitFor(() => agent.tools.has('quietbridge_extra'), { label: 'list_changed re-sync' })
	assert.deepEqual(registeredToolNames(agent), ['quietbridge_extra', 'quietbridge_observe', 'quietbridge_task'])
	// The old generation was disposed and replaced, not duplicated.
	const value = await agent.tools.get('quietbridge_task').execute({ action: 'status' }, makeExecution(agent))
	assert.match(value.content[0].text, /^echo:quietbridge_task:/)
})

test('activation adopts Agents that already exist (profile reload)', async (t) => {
	const configDir = makeConfigDir()
	const harness = makeHarness()
	const agent = makeAgent('session-33333333-0000-0000-0000-00000000000d')
	harness.existingAgents.push(agent.agent)
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir))
	await waitFor(() => agent.tools.size === 2, { label: 'existing Agent tool registration' })
	assert.deepEqual(registeredToolNames(agent), ['quietbridge_observe', 'quietbridge_task'])
	const value = await agent.tools.get('quietbridge_task').execute({ action: 'status' }, makeExecution(agent))
	assert.match(value.content[0].text, /^echo:quietbridge_task:/)
	await waitFor(
		() => readJsonLines(path.join(configDir, 'fake-lifecycle.jsonl')).some((entry) => entry.event === 'agent-created'),
		{ label: 'adopted Agent agent-created lifecycle event' },
	)
})
