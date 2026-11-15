/**
 * End-to-end Agent lifecycle + tool registration against a fake MCP connector.
 */
import assert from 'node:assert/strict'
import path from 'node:path'
import test from 'node:test'

import { apply, __internals } from '../index.js'
import {
	assertDshSupportedSchema,
	makeAgent,
	makeConfig,
	makeConfigDir,
	makeExecution,
	makeHarness,
	readJsonLines,
	registeredToolNames,
	removeConfigDir,
	waitFor,
} from './helpers.mjs'

/** Start the plugin and one Agent; returns the harness and awaited Agent. */
async function startOneAgent(t, overrides = {}) {
	const configDir = makeConfigDir()
	const harness = makeHarness()
	const agent = makeAgent()
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir, overrides))
	await harness.ctx.emitAsync('agent/created', { agent: agent.agent, source: 'startup' })
	return { harness, agent, configDir }
}

test('agent/created spawns one connector and registers every listed tool by raw name', async (t) => {
	const { harness, agent, configDir } = await startOneAgent(t)
	assert.deepEqual(registeredToolNames(agent), ['quietbridge_observe', 'quietbridge_task'])
	assert.deepEqual(harness.errors, [])

	// Trusted identity is injected into the connector environment.
	const log = path.join(configDir, `fake-connector-${agent.agent.id}.jsonl`)
	await waitFor(() => readJsonLines(log).length > 0, { label: 'connector log' })
	const environment = readJsonLines(log)[0]
	assert.equal(environment.method, 'env')
	assert.equal(environment.host, 'deepseek-harness')
	assert.equal(environment.sessionId, agent.agent.session.id)
	assert.equal(environment.agentId, agent.agent.id)
	assert.equal(environment.configDir, configDir)
	assert.equal(environment.helperPath, '/tmp/fake-quietbridge-native')

	// Every input schema is downgraded to the DSH-enforced subset.
	for (const definition of agent.tools.values()) {
		assertDshSupportedSchema(definition.parameters, `${definition.name}.parameters`)
		assertDshSupportedSchema(definition.output.schema, `${definition.name}.output.schema`)
		assert.equal(typeof definition.output.render, 'function')
		assert.equal(typeof definition.execute, 'function')
	}
	const task = agent.tools.get('quietbridge_task')
	assert.equal(task.parameters.required[0], 'action')
	assert.deepEqual(task.parameters.properties.action.enum, ['begin', 'status', 'end'])
	assert.ok(!JSON.stringify(task.parameters).includes('$schema'))
})

test('tools/call round trip returns canonical text content and renders text', async (t) => {
	const { agent } = await startOneAgent(t)
	const task = agent.tools.get('quietbridge_task')
	const value = await task.execute(
		{ action: 'begin', targetSurface: 'web_content' },
		makeExecution(agent),
	)
	assert.ok(Array.isArray(value.content))
	assert.equal(value.content[0].type, 'text')
	assert.match(value.content[0].text, /^echo:quietbridge_task:/)
	assert.deepEqual(value.structuredContent, {
		ok: true,
		tool: 'quietbridge_task',
		echo: { action: 'begin', targetSurface: 'web_content' },
	})
	const rendered = task.output.render({ action: 'begin' }, value)
	assert.equal(rendered.length, 1)
	assert.equal(rendered[0].type, 'text')
	assert.match(rendered[0].text, /echo:quietbridge_task/)
})

test('image/resource blocks are downgraded to readable text and never inline base64', async (t) => {
	const { agent } = await startOneAgent(t)
	const observe = agent.tools.get('quietbridge_observe')
	const value = await observe.execute({ __mode: 'image' }, makeExecution(agent))
	const serialized = JSON.stringify(value)
	assert.ok(!serialized.includes('aGVsbG8taW1hZ2UtYnl0ZXM='), 'image base64 leaked into canonical value')
	assert.ok(!serialized.includes('YXVkaW8='), 'audio base64 leaked into canonical value')
	const rendered = observe.output.render({}, value)
	const text = rendered[0].text
	assert.ok(text.includes('图片'))
	assert.ok(text.includes('资源链接') || text.includes('示例页面'))
	assert.ok(!text.includes('aGVsbG8taW1hZ2UtYnl0ZXM='))
})

test('MCP isError results fail the tool call visibly', async (t) => {
	const { agent } = await startOneAgent(t)
	const task = agent.tools.get('quietbridge_task')
	await assert.rejects(
		task.execute({ action: 'begin', __mode: 'error' }, makeExecution(agent)),
		(error) => {
			assert.match(error.message, /EXPECTED_FAILURE/)
			assert.equal(error.code, 'MCP_TOOL_ERROR')
			return true
		},
	)
})

test('oversized text content is bounded before it reaches render', async (t) => {
	const { agent } = await startOneAgent(t)
	const task = agent.tools.get('quietbridge_task')
	const value = await task.execute({ action: 'status', __mode: 'big' }, makeExecution(agent))
	assert.ok(value.content[0].text.length <= 256 * 1024 + 32)
	const rendered = task.output.render({}, value)
	assert.ok(rendered[0].text.length <= 256 * 1024 + 64)
})

test('each Agent gets an isolated connector and tool scope', async (t) => {
	const configDir = makeConfigDir()
	const harness = makeHarness()
	const first = makeAgent('session-aaaaaaaa-0000-0000-0000-000000000001')
	const second = makeAgent('session-bbbbbbbb-0000-0000-0000-000000000002')
	t.after(async () => {
		await harness.disposePlugin()
		removeConfigDir(configDir)
	})
	apply(harness.ctx, makeConfig(configDir))
	await harness.ctx.emitAsync('agent/created', { agent: first.agent })
	await harness.ctx.emitAsync('agent/created', { agent: second.agent })
	assert.deepEqual(registeredToolNames(first), ['quietbridge_observe', 'quietbridge_task'])
	assert.deepEqual(registeredToolNames(second), ['quietbridge_observe', 'quietbridge_task'])
	assert.notEqual(first.tools.get('quietbridge_task'), second.tools.get('quietbridge_task'))
	await first.tools.get('quietbridge_task').execute({ action: 'status' }, makeExecution(first))
	assert.equal(
		readJsonLines(path.join(configDir, `fake-connector-${first.agent.id}.jsonl`)).length > 0,
		true,
	)
	assert.equal(
		readJsonLines(path.join(configDir, `fake-connector-${second.agent.id}.jsonl`)).length > 0,
		true,
	)
})

test('connector environment never inherits another identity or a worker role', () => {
	const saved = { ...process.env }
	process.env.QUIETBRIDGE_HOST = 'stale-host'
	process.env.QUIETBRIDGE_HOST_SESSION_ID = 'stale-session'
	process.env.QUIETBRIDGE_HOST_AGENT_ID = 'stale-agent'
	process.env.QUIETBRIDGE_SESSION_KEY = 'stale-key'
	process.env.QUIETBRIDGE_ROUTING = 'worker'
	process.env.QUIETBRIDGE_CONNECTOR_REEXEC = '1'
	try {
		const env = __internals.connectorEnvironment(
			{ configDir: '/tmp/quietbridge-cfg', helper: '/tmp/quietbridge-helper', serverName: 'quietbridge' },
			{ sessionId: 'session-1', agentId: 'agent-1' },
		)
		assert.equal(env.QUIETBRIDGE_HOST, 'deepseek-harness')
		assert.equal(env.QUIETBRIDGE_HOST_SESSION_ID, 'session-1')
		assert.equal(env.QUIETBRIDGE_HOST_AGENT_ID, 'agent-1')
		assert.equal(env.QUIETBRIDGE_CONFIG_DIR, '/tmp/quietbridge-cfg')
		assert.equal(env.QUIETBRIDGE_HELPER_PATH, '/tmp/quietbridge-helper')
		assert.equal(env.QUIETBRIDGE_SESSION_KEY, undefined)
		assert.equal(env.QUIETBRIDGE_ROUTING, undefined)
		assert.equal(env.QUIETBRIDGE_CONNECTOR_REEXEC, undefined)
	} finally {
		for (const key of Object.keys(process.env)) {
			if (!(key in saved)) delete process.env[key]
		}
		Object.assign(process.env, saved)
	}
})
