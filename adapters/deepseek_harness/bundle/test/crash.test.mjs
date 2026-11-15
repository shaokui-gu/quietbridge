/**
 * Connector crash isolation: structured errors, no replay, other Agents healthy.
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

test('a crashed per-Agent connector fails its calls once and never replays input', async (t) => {
	const configDir = makeConfigDir()
	writeMode(configDir, 'fake-mode', 'crash-on-call')
	const crashing = makeAgent('session-dddddddd-0000-0000-0000-000000000004')
	writeMode(configDir, 'fake-target-agent', crashing.agent.id)
	const healthy = makeAgent('session-eeeeeeee-0000-0000-0000-000000000005')
	const harness = makeHarness()
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir))

	await harness.ctx.emitAsync('agent/created', { agent: crashing.agent })
	await harness.ctx.emitAsync('agent/created', { agent: healthy.agent })
	assert.deepEqual(registeredToolNames(crashing), ['quietbridge_observe', 'quietbridge_task'])
	assert.deepEqual(registeredToolNames(healthy), ['quietbridge_observe', 'quietbridge_task'])

	const crashingTool = crashing.tools.get('quietbridge_task')
	await assert.rejects(crashingTool.execute({ action: 'begin' }, makeExecution(crashing)), (error) => {
		assert.match(error.message, /连接器|CONNECTOR/i)
		assert.match(error.message, /不要重放|未执行/)
		return true
	})

	// Second call must fail locally: input from the crashed process is never resent.
	await assert.rejects(crashingTool.execute({ action: 'begin' }, makeExecution(crashing)), (error) => {
		assert.match(error.message, /连接器不可用/)
		return true
	})
	const crashingLog = readJsonLines(path.join(configDir, `fake-connector-${crashing.agent.id}.jsonl`))
	assert.equal(crashingLog.filter((entry) => entry.method === 'tools/call').length, 1, 'input must not be replayed')
	assert.ok(crashingLog.some((entry) => entry.method === 'exit'))

	// The other Agent keeps working.
	const healthyValue = await healthy.tools
		.get('quietbridge_task')
		.execute({ action: 'status' }, makeExecution(healthy))
	assert.match(healthyValue.content[0].text, /^echo:quietbridge_task:/)
	assert.deepEqual(registeredToolNames(healthy), ['quietbridge_observe', 'quietbridge_task'])
})

test('a connector that dies during startup is contained and registers no tools', async (t) => {
	const configDir = makeConfigDir()
	writeMode(configDir, 'fake-mode', 'crash-on-start')
	const agent = makeAgent('session-ffffffff-0000-0000-0000-000000000006')
	const harness = makeHarness()
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir))

	// A serial agent/created listener must never reject; startup failure is logged.
	await harness.ctx.emitAsync('agent/created', { agent: agent.agent })
	assert.deepEqual(registeredToolNames(agent), [])
	assert.ok(
		harness.warns.some((line) => /启动失败/.test(line)),
		`expected a startup warning, saw: ${harness.warns.join('\n')}`,
	)
	assert.deepEqual(harness.errors, [])
	// Teardown must still work after a failed start.
	await harness.ctx.emitAsync('agent/disposed', { agent: agent.agent })
})

test('disposing one Agent tears down only its connector and writes agent-disposed', async (t) => {
	const configDir = makeConfigDir()
	const first = makeAgent('session-99999999-0000-0000-0000-000000000007')
	const second = makeAgent('session-88888888-0000-0000-0000-000000000008')
	const harness = makeHarness()
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir))
	await harness.ctx.emitAsync('agent/created', { agent: first.agent })
	await harness.ctx.emitAsync('agent/created', { agent: second.agent })

	await harness.ctx.emitAsync('agent/disposed', { agent: first.agent })
	await waitFor(
		() =>
			readJsonLines(path.join(configDir, `fake-connector-${first.agent.id}.jsonl`)).some(
				(entry) => entry.method === 'shutdown',
			),
		{ label: 'first connector shutdown' },
	)
	assert.deepEqual(registeredToolNames(first), [])
	await waitFor(
		() =>
			readJsonLines(path.join(configDir, 'fake-lifecycle.jsonl')).some(
				(entry) => entry.event === 'agent-disposed' && entry.agentId === first.agent.id,
			),
		{ label: 'agent-disposed lifecycle event' },
	)

	// The surviving Agent still has tools and a live connector.
	const value = await second.tools.get('quietbridge_task').execute({ action: 'status' }, makeExecution(second))
	assert.match(value.content[0].text, /^echo:quietbridge_task:/)
})
