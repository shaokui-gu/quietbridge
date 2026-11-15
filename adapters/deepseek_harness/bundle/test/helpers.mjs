/**
 * Test-only fakes for Cordis contexts, DSH Agents, and fixture paths.
 *
 * Deliberately not a `*.test.mjs` file: `node --test` must not treat helpers as
 * test files. Nothing here imports the real DSH runtime.
 */
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const fakeConnectorPath = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures', 'fake-connector.mjs')
export const fakeLifecyclePath = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures', 'fake-lifecycle.mjs')

/** Create one isolated `<QUIETBRIDGE_CONFIG_DIR>` for a test. */
export function makeConfigDir() {
	return mkdtempSync(path.join(tmpdir(), 'quietbridge-dsh-test-'))
}

/** Remove a config dir created by {@link makeConfigDir}. */
export function removeConfigDir(directory) {
	try {
		rmSync(directory, { recursive: true, force: true })
	} catch {
		/* ignore */
	}
}

/** Write one fixture mode file (fake connector / fake lifecycle). */
export function writeMode(configDir, name, value) {
	writeFileSync(path.join(configDir, name), `${value}\n`)
}

/** Read a JSONL log, tolerating a missing file and a partial final line. */
export function readJsonLines(file) {
	try {
		return readFileSync(file, 'utf8')
			.split('\n')
			.filter((line) => line.trim() !== '')
			.map((line) => {
				try {
					return JSON.parse(line)
				} catch {
					return { unparsable: line }
				}
			})
	} catch {
		return []
	}
}

/** Wait until `predicate()` returns truthy, polling with a bounded timeout. */
export async function waitFor(predicate, { timeoutMs = 5000, intervalMs = 25, label = 'condition' } = {}) {
	const deadline = Date.now() + timeoutMs
	for (;;) {
		const value = await predicate()
		if (value) return value
		if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`)
		await new Promise((resolve) => setTimeout(resolve, intervalMs))
	}
}

/**
 * Minimal Cordis-like root context.
 *
 * Supports the exact surface the plugin uses: `ctx.on`, `ctx.effect`,
 * `ctx.get('agents')`, `ctx.logger`, and an async emitter for serial events.
 */
export function makeHarness() {
	const listeners = new Map()
	const effectCleanups = []
	const warns = []
	const errors = []
	const existingAgents = []
	const ctx = {
		logger: {
			debug() {},
			info() {},
			warn(...parts) {
				warns.push(parts.join(' '))
			},
			error(...parts) {
				errors.push(parts.join(' '))
			},
		},
		on(event, listener) {
			const list = listeners.get(event) ?? []
			list.push(listener)
			listeners.set(event, list)
			return () => {
				const index = list.indexOf(listener)
				if (index >= 0) list.splice(index, 1)
			}
		},
		effect(callback) {
			const cleanup = callback()
			effectCleanups.push(cleanup)
			return async () => {
				const index = effectCleanups.indexOf(cleanup)
				if (index >= 0) effectCleanups.splice(index, 1)
				if (typeof cleanup === 'function') return cleanup()
				return undefined
			}
		},
		get(service) {
			if (service === 'agents') return { list: () => [...existingAgents] }
			return undefined
		},
		emit(event, ...args) {
			for (const listener of [...(listeners.get(event) ?? [])]) listener(...args)
		},
		async emitAsync(event, ...args) {
			for (const listener of [...(listeners.get(event) ?? [])]) await listener(...args)
		},
	}
	/** Run every plugin-level cleanup, newest first (like fiber disposal). */
	const disposePlugin = async () => {
		const cleanups = effectCleanups.splice(0).reverse()
		for (const cleanup of cleanups) {
			if (typeof cleanup === 'function') await cleanup()
		}
	}
	return { ctx, warns, errors, existingAgents, disposePlugin }
}

/**
 * Minimal Agent context with an agent-scoped tool registry.
 *
 * Registers tools exactly like `ctx.tools.register`: duplicate names in the same
 * scope throw, and the returned disposer unregisters the tool.
 */
export function makeAgentCtx() {
	const tools = new Map()
	const effectCleanups = []
	const ctx = {
		tools: {
			register(definition) {
				if (tools.has(definition.name)) throw new Error(`duplicate tool ${definition.name}`)
				tools.set(definition.name, definition)
				let disposed = false
				return () => {
					if (disposed) return
					disposed = true
					tools.delete(definition.name)
				}
			},
		},
		effect(callback) {
			const cleanup = callback()
			// Mimic Cordis: a re-entrant dispose() returns the in-flight task.
			let disposing = false
			let task
			const outer = () => {
				if (disposing) return task
				disposing = true
				task = Promise.resolve(typeof cleanup === 'function' ? cleanup() : undefined)
				return task
			}
			effectCleanups.push(outer)
			return outer
		},
		on() {
			return () => {}
		},
	}
	return { ctx, tools, effectCleanups }
}

/** Build one fake DSH Agent: `{id, session: {id}, ctx}`. */
export function makeAgent(agentId = 'session-11111111-2222-3333-4444-555555555555') {
	const agentCtx = makeAgentCtx()
	return {
		agent: { id: agentId, session: { id: agentId }, ctx: agentCtx.ctx },
		tools: agentCtx.tools,
		agentCtx: agentCtx,
	}
}

/** Standard fixture config rooted at one temp config dir. */
export function makeConfig(configDir, overrides = {}) {
	return {
		python: process.execPath,
		connector: fakeConnectorPath,
		helper: '/tmp/fake-quietbridge-native',
		configDir,
		serverName: 'quietbridge',
		requestTimeoutMs: 2000,
		startupTimeoutMs: 2000,
		lifecycleTimeoutMs: 500,
		lifecycleScript: fakeLifecyclePath,
		...overrides,
	}
}

/** Snapshot of all registered tools in one agent scope. */
export function registeredToolNames(agent) {
	return [...agent.tools.keys()].sort()
}

/** Build one execution object like DSH's pipeline passes to `execute`. */
export function makeExecution(agent, { signal } = {}) {
	return {
		agent: agent.agent,
		signal: signal ?? new AbortController().signal,
		callId: 'call-1',
		arguments: {},
	}
}

// ---------------------------------------------------------------------------
// DSH JSON Schema subset checker (mirrors assertSupportedJsonSchema)
// ---------------------------------------------------------------------------

const DSH_CONSTRAINTS = new Set([
	'type',
	'oneOf',
	'properties',
	'required',
	'additionalProperties',
	'items',
	'enum',
	'const',
])
const DSH_ANNOTATIONS = new Set(['description', 'title', 'default', 'examples'])
const DSH_TYPES = new Set(['object', 'array', 'string', 'number', 'integer', 'boolean', 'null'])
const ONE_OF_SIBLINGS = ['properties', 'required', 'additionalProperties', 'items', 'enum', 'const']

/** Whether a value matches one DSH scalar schema type. */
function matchesScalar(type, value) {
	if (type === 'string') return typeof value === 'string'
	if (type === 'number') return typeof value === 'number' && Number.isFinite(value)
	if (type === 'integer') return Number.isSafeInteger(value)
	if (type === 'boolean') return typeof value === 'boolean'
	if (type === 'null') return value === null
	return false
}

/**
 * Assert that a schema belongs to the exact JSON Schema subset DSH enforces
 * (the subset checked by `assertSupportedJsonSchema`).
 */
export function assertDshSupportedSchema(schema, label = 'schema') {
	const violations = []
	const walk = (node, at) => {
		if (typeof node !== 'object' || node === null || Array.isArray(node)) {
			violations.push(`${at} must be a schema object`)
			return
		}
		for (const key of Object.keys(node)) {
			if (!DSH_CONSTRAINTS.has(key) && !DSH_ANNOTATIONS.has(key)) {
				violations.push(`${at}.${key} is not a supported keyword`)
			}
		}
		if (Object.hasOwn(node, 'description') && typeof node.description !== 'string') violations.push(`${at}.description must be a string`)
		if (Object.hasOwn(node, 'title') && typeof node.title !== 'string') violations.push(`${at}.title must be a string`)
		for (const key of ['default', 'examples']) {
			if (!Object.hasOwn(node, key)) continue
			try {
				JSON.stringify(node[key])
			} catch {
				violations.push(`${at}.${key} must be lossless JSON`)
			}
		}
		const hasType = Object.hasOwn(node, 'type')
		const hasOneOf = Object.hasOwn(node, 'oneOf')
		if (hasType && hasOneOf) {
			violations.push(`${at} cannot declare both type and oneOf`)
			return
		}
		if (!hasType && !hasOneOf) {
			for (const key of ONE_OF_SIBLINGS) {
				if (Object.hasOwn(node, key)) violations.push(`${at}.${key} requires type or oneOf`)
			}
			return
		}
		if (hasOneOf) {
			for (const key of ONE_OF_SIBLINGS) {
				if (Object.hasOwn(node, key)) violations.push(`${at}.${key} is not supported beside oneOf`)
			}
			if (!Array.isArray(node.oneOf) || node.oneOf.length < 2) violations.push(`${at}.oneOf must be an array of at least two schemas`)
			else node.oneOf.forEach((branch, index) => walk(branch, `${at}.oneOf[${index}]`))
			return
		}
		if (typeof node.type !== 'string' || !DSH_TYPES.has(node.type)) {
			violations.push(`${at}.type must be one single supported type string (got ${JSON.stringify(node.type)})`)
			return
		}
		const type = node.type
		const allowedKeyTypes = {
			properties: ['object'],
			required: ['object'],
			additionalProperties: ['object'],
			items: ['array'],
			enum: ['string', 'number', 'integer', 'boolean', 'null'],
			const: ['string', 'number', 'integer', 'boolean', 'null'],
		}
		for (const [key, types] of Object.entries(allowedKeyTypes)) {
			if (Object.hasOwn(node, key) && !types.includes(type)) violations.push(`${at}.${key} is not supported on type "${type}"`)
		}
		switch (type) {
			case 'object': {
				if (Object.hasOwn(node, 'properties')) {
					if (typeof node.properties !== 'object' || node.properties === null || Array.isArray(node.properties)) {
						violations.push(`${at}.properties must be an object of schemas`)
					} else {
						for (const [key, value] of Object.entries(node.properties)) walk(value, `${at}.properties.${key}`)
					}
				}
				if (Object.hasOwn(node, 'required')) {
					if (!Array.isArray(node.required) || node.required.some((entry) => typeof entry !== 'string')) {
						violations.push(`${at}.required must be an array of strings`)
					} else {
						const declared = typeof node.properties === 'object' && node.properties !== null ? node.properties : {}
						for (const key of node.required) {
							if (!Object.hasOwn(declared, key)) violations.push(`${at}.required names "${key}" which is not in properties`)
						}
					}
				}
				if (Object.hasOwn(node, 'additionalProperties') && typeof node.additionalProperties !== 'boolean') {
					violations.push(`${at}.additionalProperties must be a boolean`)
				}
				return
			}
			case 'array':
				if (Object.hasOwn(node, 'items')) walk(node.items, `${at}.items`)
				return
			default: {
				if (Object.hasOwn(node, 'enum')) {
					const allowed = node.enum
					if (!Array.isArray(allowed) || allowed.length === 0 || allowed.some((entry) => !matchesScalar(type, entry))) {
						violations.push(`${at}.enum must be a non-empty array of ${type} values`)
					}
				}
				if (Object.hasOwn(node, 'const') && !matchesScalar(type, node.const)) {
					violations.push(`${at}.const must be a ${type} value`)
				}
				return
			}
		}
	}
	walk(schema, label)
	assert.equal(violations.length, 0, `unsupported DSH schema (${label}): ${violations.join('; ')}`)
	return schema
}
