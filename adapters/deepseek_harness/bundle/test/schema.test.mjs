/**
 * Unit tests for schema downgrade, config validation, and result normalization.
 */
import assert from 'node:assert/strict'
import { existsSync, rmSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import { makeConfig, makeHarness } from './helpers.mjs'

import {
	__internals,
	apply,
	isValidToolName,
	mapTurnReason,
	normalizeMcpContent,
	renderToolResult,
	resolveConfig,
	sanitizeToolInputSchema,
	turnIdOf,
} from '../index.js'
import { assertDshSupportedSchema } from './helpers.mjs'

const fakeConnector = '/tmp/fake-bridge/server.py'

test('sanitizeToolInputSchema downgrades MCP vocabulary to the DSH subset', () => {
	const raw = {
		$schema: 'http://json-schema.org/draft-07/schema#',
		$defs: { Action: { type: 'string', enum: ['begin', 'status', 'end'] } },
		type: 'object',
		properties: {
			action: { $ref: '#/$defs/Action', description: '任务动作' },
			targetSurface: { type: 'string', format: 'surface', enum: ['web_content', 'desktop_app'] },
			waitSeconds: { type: 'number', minimum: 0, maximum: 120, default: 1 },
			nested: {
				anyOf: [
					{ type: 'object', properties: { x: { type: 'string' } }, required: ['x'] },
					{ type: 'string' },
				],
			},
			nullable: { type: ['string', 'null'] },
		},
		required: ['action', 'missingProperty'],
		additionalProperties: false,
	}
	const clean = sanitizeToolInputSchema(raw)
	assertDshSupportedSchema(clean)
	assert.equal(clean.type, 'object')
	assert.equal(clean.required.length, 1)
	assert.deepEqual(clean.required, ['action'])
	assert.deepEqual(clean.properties.action, { description: '任务动作', type: 'string', enum: ['begin', 'status', 'end'] })
	assert.equal(clean.properties.waitSeconds.type, 'number')
	assert.equal(clean.properties.waitSeconds.default, 1)
	assert.equal(clean.properties.nullable.type, 'string')
	assert.equal(clean.properties.nested.oneOf.length, 2)
	assert.equal(clean.additionalProperties, false)
	const serialized = JSON.stringify(clean)
	for (const forbidden of ['$schema', '$defs', '$ref', 'format', 'minimum', 'maximum', 'anyOf']) {
		assert.ok(!serialized.includes(`"${forbidden}"`), `sanitized schema must not keep ${forbidden}`)
	}
})

test('sanitizeToolInputSchema approximates allOf and rejects unknown roots safely', () => {
	const allOf = sanitizeToolInputSchema({
		allOf: [
			{ type: 'object', properties: { a: { type: 'string' } }, required: ['a'] },
			{ type: 'object', properties: { b: { type: 'integer' } } },
		],
	})
	assertDshSupportedSchema(allOf)
	assert.equal(allOf.type, 'object')
	assert.ok(allOf.properties.a)
	assert.ok(allOf.properties.b)

	assert.deepEqual(sanitizeToolInputSchema(undefined), {})
	assert.deepEqual(sanitizeToolInputSchema('not a schema'), {})
	assertDshSupportedSchema(sanitizeToolInputSchema({ type: 'object', properties: { loop: { $ref: '#/properties/loop' } } }))
})

test('resolveConfig applies defaults and validates required paths', () => {
	const resolved = resolveConfig({
		connector: fakeConnector,
		configDir: '/tmp/quietbridge-config',
	})
	assert.equal(resolved.python, existsSync('/usr/bin/python3') ? '/usr/bin/python3' : 'python3')
	assert.equal(resolved.serverName, 'quietbridge')
	assert.equal(resolved.requestTimeoutMs, 70000)
	assert.equal(resolved.startupTimeoutMs, 5000)
	assert.equal(resolved.lifecycleTimeoutMs, 5000)
	assert.equal(resolved.maxConnectors, 3)
	assert.equal(resolved.connector, path.resolve(fakeConnector))
	assert.equal(resolved.lifecycleScript, path.resolve('/tmp/fake-bridge/adapters/deepseek_harness/lifecycle.py'))

	assert.throws(() => resolveConfig({ configDir: '/tmp/x' }), /connector/)
	assert.throws(() => resolveConfig({ connector: fakeConnector }), /configDir/)
	assert.throws(() => resolveConfig({ connector: fakeConnector, configDir: '/tmp/x', requestTimeoutMs: 0 }), /requestTimeoutMs/)
	assert.throws(() => resolveConfig({ connector: fakeConnector, configDir: '/tmp/x', serverName: 'bad name' }), /serverName/)
	assert.throws(() => resolveConfig({ connector: fakeConnector, configDir: '/tmp/x', maxConnectors: 33 }), /maxConnectors/)
	assert.equal(
		resolveConfig({ connector: fakeConnector, configDir: '/tmp/x', lifecycleScript: '/tmp/own.py' }).lifecycleScript,
		path.resolve('/tmp/own.py'),
	)
})

test('isValidToolName enforces the agent-scoped tool-name contract', () => {
	assert.equal(isValidToolName('quietbridge_task'), true)
	assert.equal(isValidToolName('quietbridge-observe'), true)
	assert.equal(isValidToolName('quietbridge_task_2'), true)
	assert.equal(isValidToolName('run_code'), true) // reserved is rejected by tools.register, not by this helper
	assert.equal(isValidToolName('a'.repeat(65)), false)
	assert.equal(isValidToolName('quietbridge task'), false)
	assert.equal(isValidToolName(''), false)
	assert.equal(isValidToolName(42), false)
})

test('turn ids and reasons follow the lifecycle contract', () => {
	assert.equal(turnIdOf('session-abc', 3), 'session-abc:3')
	assert.equal(turnIdOf('11111111-2222-3333-4444-555555555555', 1), '11111111-2222-3333-4444-555555555555:1')
	assert.equal(turnIdOf('session-abc', undefined), '')
	assert.equal(mapTurnReason({ kind: 'completed' }), 'completed')
	assert.equal(mapTurnReason({ kind: 'blocked' }), 'completed')
	assert.equal(mapTurnReason({ kind: 'max-tokens' }), 'completed')
	assert.equal(mapTurnReason({ kind: 'aborted', reason: { kind: 'user' } }), 'cancelled')
	assert.equal(mapTurnReason({ kind: 'interrupted' }), 'interrupted')
	assert.equal(mapTurnReason({ kind: 'error' }), 'error')
	assert.equal(mapTurnReason(undefined), 'completed')
})

test('MCP content normalizes to text without base64 or binary payloads', () => {
	const blocks = normalizeMcpContent([
		{ type: 'text', text: 'hello' },
		{ type: 'image', mimeType: 'image/png', data: 'aGVsbG8taW1hZ2UtYnl0ZXM=' },
		{ type: 'audio', mimeType: 'audio/wav', data: 'YXVkaW8=' },
		{ type: 'resource_link', name: '页面', uri: 'https://example.test/' },
		{ type: 'resource', resource: { uri: 'file:///tmp/x', mimeType: 'text/plain', text: 'payload' } },
	])
	assert.ok(blocks.length >= 5)
	const serialized = JSON.stringify(blocks)
	assert.ok(!serialized.includes('aGVsbG8taW1hZ2UtYnl0ZXM='), 'image base64 leaked')
	assert.ok(!serialized.includes('YXVkaW8='), 'audio base64 leaked')
	assert.ok(!serialized.includes('payload'), 'embedded resource text leaked')
	assert.ok(serialized.includes('图片'))
	assert.ok(serialized.includes('资源链接'))
	assert.ok(blocks.every((block) => block.type === 'text'))

	const value = __internals.buildToolValue({
		content: [{ type: 'image', mimeType: 'image/png', data: 'AAAA' }],
		structuredContent: { ok: true, n: 1 },
	})
	assert.deepEqual(value.structuredContent, { ok: true, n: 1 })
	const rendered = renderToolResult(value, 'quietbridge_observe')
	assert.equal(rendered.length, 1)
	assert.equal(rendered[0].type, 'text')
	assert.ok(rendered[0].text.includes('图片'))
	assert.ok(!rendered[0].text.includes('AAAA'))
})

test('renderToolResult falls back to structured content and bounds huge text', () => {
	const empty = renderToolResult({ content: [] }, 'quietbridge_task')
	assert.match(empty[0].text, /无返回内容/)
	const structured = renderToolResult({ content: [], structuredContent: { ok: true } }, 'quietbridge_task')
	assert.ok(structured[0].text.includes('"ok": true'))
	const huge = renderToolResult({ content: [{ type: 'text', text: 'x'.repeat(300000) }] }, 'quietbridge_task')
	assert.ok(huge[0].text.length <= 256 * 1024 + 32)
	assert.ok(huge[0].text.includes('[结果过长，已截断]'))
})

test('apply creates the config directory before connector or lifecycle writes', () => {
	const configDir = path.join(os.tmpdir(), `qb-cfg-${randomUUID()}`)
	assert.equal(existsSync(configDir), false)
	const harness = makeHarness()
	try {
		apply(harness.ctx, makeConfig(configDir))
		assert.equal(existsSync(configDir), true)
	} finally {
		rmSync(configDir, { recursive: true, force: true })
	}
})
