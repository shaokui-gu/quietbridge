#!/usr/bin/env node
/**
 * Minimal fake QuietBridge MCP server used only by the bundle's `node --test`
 * suite. It speaks newline-delimited JSON-RPC on stdin/stdout, records every
 * message under `<QUIETBRIDGE_CONFIG_DIR>`, and behaves according to a mode file
 * so tests never need Python, a real connector, a helper, or the network.
 *
 * Mode file: `<QUIETBRIDGE_CONFIG_DIR>/fake-mode` (default `normal`):
 *   normal          - echo tools/call results
 *   crash-on-call   - exit without a response on the first tools/call
 *   crash-on-start  - exit before reading anything
 *   list-changed    - emit notifications/tools/list_changed after initialize
 *                     and add one extra tool to later tools/list responses
 *
 * Crash targeting: `<QUIETBRIDGE_CONFIG_DIR>/fake-target-agent` may contain the
 * QUIETBRIDGE_HOST_AGENT_ID that should crash; other Agents stay healthy.
 */
import { appendFileSync, existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import readline from 'node:readline'

const configDir = process.env.QUIETBRIDGE_CONFIG_DIR || '.'
const agentId = process.env.QUIETBRIDGE_HOST_AGENT_ID || 'unknown-agent'
const logPath = path.join(configDir, `fake-connector-${agentId}.jsonl`)

const readSmall = (name, fallback = '') => {
	try {
		return readFileSync(path.join(configDir, name), 'utf8').trim()
	} catch {
		return fallback
	}
}

const mode = readSmall('fake-mode', 'normal')
const targetAgent = readSmall('fake-target-agent', '')
const crashes = mode === 'crash-on-call' || mode === 'crash-on-start'
const appliesToThisAgent = !targetAgent || targetAgent === agentId

const log = (entry) => {
	try {
		appendFileSync(logPath, `${JSON.stringify(entry)}\n`)
	} catch {
		/* logging must never change behavior */
	}
}

const send = (message) => {
	try {
		process.stdout.write(`${JSON.stringify(message)}\n`)
	} catch {
		/* stdout closed */
	}
}

const baseTools = [
	{
		name: 'quietbridge_task',
		description: '开始、查询或结束一个 QuietBridge 界面任务。',
		inputSchema: {
			$schema: 'http://json-schema.org/draft-07/schema#',
			$defs: { Action: { type: 'string', enum: ['begin', 'status', 'end'] } },
			type: 'object',
			properties: {
				action: { $ref: '#/$defs/Action', description: '任务动作' },
				targetSurface: {
					type: 'string',
					format: 'quietbridge-surface',
					enum: ['web_content', 'browser_ui', 'native_dialog', 'desktop_app'],
				},
				waitSeconds: { type: 'number', minimum: 0, maximum: 120, default: 1 },
				nested: {
					anyOf: [
						{ type: 'object', properties: { x: { type: 'string' } }, required: ['x'] },
						{ type: 'string' },
					],
				},
			},
			required: ['action'],
			additionalProperties: false,
		},
	},
	{
		name: 'quietbridge_observe',
		description: '观察当前目标并返回可读状态。',
		inputSchema: {
			type: 'object',
			properties: {
				target: { type: ['string', 'null'], description: '目标提示' },
				detail: { type: 'boolean', default: false },
			},
			additionalProperties: true,
		},
	},
]

const extraTool = {
	name: 'quietbridge_extra',
	description: '仅在 list_changed 重同步后出现的工具。',
	inputSchema: { type: 'object', properties: {} },
}

let listRequests = 0
let listChangedSent = false
let crashed = false

const respond = (id, result) => send({ jsonrpc: '2.0', id, result })
const respondError = (id, code, message) => send({ jsonrpc: '2.0', id, error: { code, message } })
const notify = (method, params) => send({ jsonrpc: '2.0', method, ...(params === undefined ? {} : { params }) })

const shutdown = (reason) => {
	log({ method: 'shutdown', reason })
	process.exit(0)
}

process.on('SIGTERM', () => shutdown('SIGTERM'))
process.on('SIGINT', () => shutdown('SIGINT'))

log({
	method: 'env',
	host: process.env.QUIETBRIDGE_HOST ?? '',
	sessionId: process.env.QUIETBRIDGE_HOST_SESSION_ID ?? '',
	agentId: process.env.QUIETBRIDGE_HOST_AGENT_ID ?? '',
	helperPath: process.env.QUIETBRIDGE_HELPER_PATH ?? '',
	configDir: process.env.QUIETBRIDGE_CONFIG_DIR ?? '',
})

if (mode === 'crash-on-start' && appliesToThisAgent) {
	log({ method: 'exit', reason: 'crash-on-start' })
	process.exit(7)
}

const callTool = (id, params) => {
	const name = params?.name
	const args = params?.arguments && typeof params.arguments === 'object' ? params.arguments : {}
	const tool = [...baseTools, extraTool].find((entry) => entry.name === name)
	if (tool === undefined) {
		respondError(id, -32602, `unknown tool: ${String(name)}`)
		return
	}
	if (mode === 'crash-on-call' && appliesToThisAgent && !crashed) {
		crashed = true
		log({ method: 'exit', reason: 'crash-on-call', tool: name })
		process.exit(9)
	}
	const modeArg = typeof args.__mode === 'string' ? args.__mode : ''
	if (modeArg === 'slow') {
		// Never answer; a cancellation must arrive and a late duplicate result is
		// emitted below when the notification is observed.
		setTimeout(() => {
			// Fallback so an un-cancelled slow call cannot wedge a test forever.
			respond(id, { content: [{ type: 'text', text: 'slow-result' }] })
		}, 4000)
		return
	}
	if (modeArg === 'image') {
		respond(id, {
			content: [
				{ type: 'text', text: '截图已获取（演示）' },
				{ type: 'image', mimeType: 'image/png', data: 'aGVsbG8taW1hZ2UtYnl0ZXM=' },
				{ type: 'resource_link', name: '示例页面', uri: 'https://example.test/page' },
				{ type: 'audio', mimeType: 'audio/wav', data: 'YXVkaW8=' },
			],
			structuredContent: { ok: true, screenshot: true },
		})
		return
	}
	if (modeArg === 'error') {
		respond(id, {
			isError: true,
			content: [{ type: 'text', text: 'EXPECTED_FAILURE: 目标不允许该操作' }],
			structuredContent: { ok: false, errorCode: 'EXPECTED_FAILURE' },
		})
		return
	}
	if (modeArg === 'big') {
		respond(id, {
			content: [{ type: 'text', text: 'x'.repeat(300000) }],
			structuredContent: { ok: true },
		})
		return
	}
	respond(id, {
		content: [{ type: 'text', text: `echo:${name}:${JSON.stringify(args)}` }],
		structuredContent: { ok: true, tool: name, echo: args },
	})
}

const handle = (message) => {
	if (typeof message?.method === 'string') {
		log({ method: message.method, id: message.id ?? null, params: message.params ?? null })
		switch (message.method) {
			case 'initialize':
				respond(message.id, {
					protocolVersion: message.params?.protocolVersion ?? '2024-11-05',
					capabilities: { tools: { listChanged: false } },
					serverInfo: { name: 'fake-quietbridge', version: '0.0.0' },
					instructions: 'fake connector for tests',
				})
				return
			case 'notifications/initialized':
				if (mode === 'list-changed' && !listChangedSent) {
					listChangedSent = true
					setTimeout(() => notify('notifications/tools/list_changed'), 30)
				}
				return
			case 'notifications/cancelled': {
				const requestId = message.params?.requestId
				log({ method: 'cancel-received', requestId })
				// Deliberately answer late so the client must discard it.
				setTimeout(() => {
					respond(requestId, { content: [{ type: 'text', text: 'LATE_RESULT_SHOULD_BE_IGNORED' }] })
				}, 50)
				return
			}
			case 'ping':
				respond(message.id, {})
				return
			case 'tools/list': {
				listRequests += 1
				const tools =
					mode === 'list-changed' && listRequests > 1 ? [...baseTools, extraTool] : [...baseTools]
				respond(message.id, { tools })
				return
			}
			case 'tools/call':
				callTool(message.id, message.params)
				return
			case 'resources/list':
				respond(message.id, { resources: [] })
				return
			case 'prompts/list':
				respond(message.id, { prompts: [] })
				return
			default:
				if (message.id !== undefined) respondError(message.id, -32601, `Method not found: ${message.method}`)
		}
		return
	}
	if (message?.id !== undefined) respondError(message.id, -32600, 'invalid request')
}

const reader = readline.createInterface({ input: process.stdin })
reader.on('line', (line) => {
	const text = line.trim()
	if (text === '') return
	let message
	try {
		message = JSON.parse(text)
	} catch {
		log({ method: 'parse-error' })
		return
	}
	if (Array.isArray(message)) for (const entry of message) handle(entry)
	else handle(message)
})
reader.on('close', () => shutdown('stdin-end'))

process.stdin.on('error', () => shutdown('stdin-error'))
