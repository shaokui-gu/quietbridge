/**
 * QuietBridge host bundle for DeepSeek Harness.
 *
 * One self-contained Host plugin with no DSH-internal imports:
 * - on `agent/created`, spawn one `server.py` MCP connector for that Agent with
 *   the trusted host session/agent identity in its environment;
 * - run a minimal newline-delimited JSON-RPC MCP client against it and
 *   register every `tools/list` tool on `agent.ctx.tools` under its raw name
 *   (agent-scoped, so parallel Agents stay isolated);
 * - forward session turn events and QuietBridge tool receipts to the Python
 *   lifecycle store through `lifecycle.py --config-dir ...` (best effort, never
 *   blocks or fails the host);
 * - dispose the connector and write `agent-disposed` when the Agent or the
 *   plugin goes away.
 *
 * Contract evidence: docs/architecture/dsh-host-contract.zh.md.
 * No DSH-internal or MCP SDK package import is allowed here:
 * profile-installed bundles cannot resolve those packages, so the MCP client
 * below is intentionally written from scratch on node built-ins only.
 *
 * @module @quietbridge/dsh
 */

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import path from 'node:path'
import process from 'node:process'

/** Cordis plugin name. */
export const name = 'quietbridge-dsh'

/** The only required service: tool registration on the calling Agent scope. */
export const inject = ['tools']

const LOG_PREFIX = '[quietbridge-dsh]'
const DEFAULT_PROTOCOL_VERSION = '2025-06-18'
const SUPPORTED_PROTOCOL_VERSIONS = Object.freeze(['2025-06-18', '2025-03-26', '2024-11-05'])
const CLIENT_INFO = Object.freeze({ name: 'quietbridge-dsh', version: '0.1.0' })

/** Refuse single JSON-RPC lines above this size instead of buffering forever. */
const MAX_RPC_LINE_CHARS = 8 * 1024 * 1024
/** Cap one rendered tool message. Base64 image payloads never reach this. */
const MAX_RENDER_CHARS = 256 * 1024
/** Cap one lifecycle stdin document; the Python store also has its own byte cap. */
const MAX_LIFECYCLE_BYTES = 256 * 1024
/** Bound the per-Agent lifecycle queue so a wedged helper cannot grow memory. */
const MAX_LIFECYCLE_QUEUE = 64
/** Bound callId correlation maps for tool receipts. */
const MAX_PENDING_TOOL_CALLS = 256
/** Bound the number of already-reported callIds kept for de-duplication. */
const MAX_REPORTED_CALL_IDS = 256
/** Cap structured JSON attached to lifecycle events. */
const MAX_STRUCTURED_CHARS = 64 * 1024
/** Connector close waits this long for SIGTERM before SIGKILL. */
const CLOSE_GRACE_MS = 2000
/** Default per-Agent startup budget for initialize + tools/list. */
const DEFAULT_STARTUP_TIMEOUT_MS = 5000
/** Default per-`tools/call` budget (server-side worker budget is 70s). */
const DEFAULT_REQUEST_TIMEOUT_MS = 70000
/** Default lifecycle child budget. */
const DEFAULT_LIFECYCLE_TIMEOUT_MS = 5000
/** Same capacity budget as the connector's server-side session limit. */
const DEFAULT_MAX_CONNECTORS = 3

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------

/** Append the plugin prefix to one log line. */
function withPrefix(message) {
	return `${LOG_PREFIX} ${message}`
}

/** Log a warning without ever letting logging throw into the host. */
function warn(logger, message) {
	try {
		logger?.warn?.(withPrefix(message))
	} catch {
		/* logging must not break plugin activation or tool calls */
	}
}

/** Log an error without ever letting logging throw into the host. */
function errorLog(logger, message) {
	try {
		logger?.error?.(withPrefix(message))
	} catch {
		/* ignore */
	}
}

/** Log at debug level when the logger supports it. */
function debugLog(logger, message) {
	try {
		logger?.debug?.(withPrefix(message))
	} catch {
		/* ignore */
	}
}

/** Whether a value is a plain JSON-style record. */
function isRecord(value) {
	return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Read a non-empty string field, or undefined. */
function optionalString(value) {
	return typeof value === 'string' && value.trim() !== '' ? value : undefined
}

/** Read a required non-empty string field. */
function requiredString(value, field) {
	const text = optionalString(value)
	if (text === undefined) throw new TypeError(`quietbridge-dsh: config.${field} must be a non-empty string`)
	return text
}

/** Read a positive integer field with a default. */
function positiveInteger(value, fallback, field) {
	if (value === undefined || value === null) return fallback
	if (!Number.isSafeInteger(value) || value <= 0) {
		throw new TypeError(`quietbridge-dsh: config.${field} must be a positive safe integer`)
	}
	return value
}

/**
 * Whether an interpreter command is a CPython executable.
 *
 * The connector is a Python program, so `-B` (never write .pyc) is passed for
 * `python`/`python3`/`python3.14`/`python3.exe`. Any other command is treated as
 * a direct executable for that script path, which keeps tests and custom
 * launchers (for example a wrapper) working without a special test-only config.
 */
export function isPythonCommand(command) {
	const base = path.basename(String(command)).toLowerCase()
	return /^python(\d+(?:\.\d+)*)?(?:\.exe)?$/.test(base)
}

/** Validate the MCP/DSH tool-name contract used for agent-scoped registration. */
export function isValidToolName(value) {
	return typeof value === 'string' && value.length > 0 && value.length <= 64 && /^[A-Za-z0-9_-]+$/.test(value)
}

/**
 * Resolve and validate the patch-row config.
 *
 * Manual validation is intentional: the bundle must not depend on schemastery
 * or any other DSH-internal package, and `apply` activation must fail loudly on
 * a half-configured row instead of silently degrading attribution.
 *
 * @param {unknown} raw - the loader `config` object.
 * @returns resolved, absolute-path config.
 */
export function resolveConfig(raw) {
	const input = raw === undefined || raw === null ? {} : raw
	if (!isRecord(input)) throw new TypeError('quietbridge-dsh: config must be an object')

	const connector = path.resolve(requiredString(input.connector, 'connector'))
	const configDir = path.resolve(requiredString(input.configDir, 'configDir'))
	// Electron launched from Finder may have a minimal PATH; prefer the system
	// interpreter when it exists and fall back to PATH lookup otherwise.
	const python = path.normalize(
		optionalString(input.python) ?? (existsSync('/usr/bin/python3') ? '/usr/bin/python3' : 'python3'),
	)
	const helper = optionalString(input.helper)
	const serverName = optionalString(input.serverName) ?? 'quietbridge'
	const requestTimeoutMs = positiveInteger(input.requestTimeoutMs, DEFAULT_REQUEST_TIMEOUT_MS, 'requestTimeoutMs')
	const startupTimeoutMs = positiveInteger(
		input.startupTimeoutMs,
		Math.min(requestTimeoutMs, DEFAULT_STARTUP_TIMEOUT_MS),
		'startupTimeoutMs',
	)
	const lifecycleTimeoutMs = positiveInteger(input.lifecycleTimeoutMs, DEFAULT_LIFECYCLE_TIMEOUT_MS, 'lifecycleTimeoutMs')
	// One collector per live Agent plus the helper connection shares the same
	// capacity budget as the server (3 sessions); extra Agents degrade to "no
	// QuietBridge tools" instead of spawning unbounded Python processes.
	const maxConnectors = positiveInteger(input.maxConnectors, DEFAULT_MAX_CONNECTORS, 'maxConnectors')
	if (maxConnectors > 32) throw new TypeError('quietbridge-dsh: config.maxConnectors must not exceed 32')
	const configuredLifecycle = optionalString(input.lifecycleScript)
	// Installed bridges keep adapters beside the connector. A source checkout
	// runs services/mcp/server.py with adapters two levels up, so accept that
	// layout too instead of silently dropping lifecycle bookkeeping.
	const bridgeRoot = path.dirname(connector)
	const installedLifecycle = path.join(bridgeRoot, 'adapters', 'deepseek_harness', 'lifecycle.py')
	const sourceLifecycle = path.join(bridgeRoot, '..', '..', 'adapters', 'deepseek_harness', 'lifecycle.py')
	const lifecycleScript = path.resolve(
		configuredLifecycle ??
			(existsSync(installedLifecycle) ? installedLifecycle :
				existsSync(sourceLifecycle) ? sourceLifecycle : installedLifecycle),
	)

	if (!/^[A-Za-z0-9_.:-]{1,128}$/.test(serverName)) {
		throw new TypeError('quietbridge-dsh: config.serverName must match [A-Za-z0-9_.:-]{1,128}')
	}

	return {
		python,
		connector,
		helper,
		configDir,
		serverName,
		requestTimeoutMs,
		startupTimeoutMs,
		lifecycleTimeoutMs,
		lifecycleScript,
		maxConnectors,
	}
}

// ---------------------------------------------------------------------------
// JSON Schema normalization for DSH's enforced subset
// ---------------------------------------------------------------------------

const DSH_CONSTRAINT_KEYS = new Set([
	'type',
	'oneOf',
	'properties',
	'required',
	'additionalProperties',
	'items',
	'enum',
	'const',
])
const DSH_ANNOTATION_KEYS = new Set(['description', 'title', 'default', 'examples'])
const DSH_TYPES = new Set(['object', 'array', 'string', 'number', 'integer', 'boolean', 'null'])
const DSH_SCALAR_TYPES = new Set(['string', 'number', 'integer', 'boolean', 'null'])
const ONE_OF_SIBLING_KEYS = ['properties', 'required', 'additionalProperties', 'items', 'enum', 'const']
const MAX_SCHEMA_DEPTH = 12

/** Recursively copy a JSON-safe value, dropping unsupported host objects. */
function cloneJsonSafe(value) {
	try {
		const text = JSON.stringify(value)
		if (typeof text !== 'string') return undefined
		return JSON.parse(text)
	} catch {
		return undefined
	}
}

/** Recursively bound a JSON value so lifecycle/stdin documents stay small. */
function boundJson(value, options = {}) {
	const {
		maxString = 8192,
		maxArray = 64,
		maxObjectKeys = 128,
		maxDepth = 6,
	} = options
	const walk = (current, depth) => {
		if (current === null || typeof current === 'boolean') return current
		if (typeof current === 'number') return Number.isFinite(current) ? current : null
		if (typeof current === 'string') {
			return current.length > maxString ? `${current.slice(0, maxString)}…[truncated ${current.length - maxString} chars]` : current
		}
		if (typeof current !== 'object') return undefined
		if (depth >= maxDepth) {
			return Array.isArray(current) ? '[array elided]' : '[object elided]'
		}
		if (Array.isArray(current)) {
			const items = current.slice(0, maxArray).map((item) => walk(item, depth + 1))
			return items
		}
		const out = {}
		let count = 0
		for (const [key, entry] of Object.entries(current)) {
			if (count >= maxObjectKeys) break
			const next = walk(entry, depth + 1)
			if (next !== undefined) {
				out[key] = next
				count += 1
			}
		}
		return out
	}
	const bounded = walk(value, 0)
	if (bounded === undefined) return undefined
	const text = JSON.stringify(bounded)
	if (typeof text === 'string' && text.length > MAX_STRUCTURED_CHARS) {
		return { truncated: true, preview: text.slice(0, MAX_STRUCTURED_CHARS) }
	}
	return bounded
}

/**
 * Normalize one tool-call argument document.
 *
 * DSH session events carry `tool/call.data.arguments` as the raw JSON string
 * while `tools/result.exec.arguments` is already parsed. Lifecycle receipts
 * must always see an object so the Python store can recognize begin/bind.
 */
function parseToolArguments(value) {
	if (isRecord(value)) return boundJson(value)
	if (typeof value === 'string' && value.length <= 65536) {
		try {
			const parsed = JSON.parse(value)
			if (isRecord(parsed)) return boundJson(parsed)
		} catch {
			/* not a JSON object: fall through to the empty record */
		}
	}
	return {}
}

/** Resolve one local JSON-pointer `$ref` (only `#...` pointers are supported). */
function resolveLocalRef(root, ref) {
	if (typeof ref !== 'string' || !ref.startsWith('#')) return undefined
	if (ref === '#') return root
	const tokens = ref
		.slice(1)
		.split('/')
		.slice(1)
		.map((token) => token.replace(/~1/g, '/').replace(/~0/g, '~'))
	let current = root
	for (const token of tokens) {
		if (!isRecord(current) && !Array.isArray(current)) return undefined
		if (!Object.hasOwn(current, token)) return undefined
		current = current[token]
	}
	return current
}

/** Merge object-schema branches for an `allOf` that DSH cannot represent. */
function mergeAllOfBranches(branches) {
	if (branches.length === 0) return {}
	if (branches.length === 1) return branches[0]
	const objectBranches = branches.filter((branch) => isRecord(branch) && branch.type === 'object')
	if (objectBranches.length !== branches.length) return {}
	const properties = {}
	const required = []
	let additionalProperties
	for (const branch of objectBranches) {
		if (isRecord(branch.properties)) Object.assign(properties, branch.properties)
		if (Array.isArray(branch.required)) {
			for (const key of branch.required) if (typeof key === 'string' && !required.includes(key)) required.push(key)
		}
		if (branch.additionalProperties === false) additionalProperties = false
	}
	const merged = { type: 'object', properties }
	if (required.length > 0) merged.required = required.filter((key) => Object.hasOwn(properties, key))
	if (additionalProperties !== undefined) merged.additionalProperties = additionalProperties
	return merged
}

/** Pick a single supported `type` string, or a `oneOf`, from a JSON Schema `type`. */
function normalizeTypeValue(value) {
	if (typeof value === 'string') return DSH_TYPES.has(value) ? { type: value } : undefined
	if (!Array.isArray(value)) return undefined
	const types = [...new Set(value.filter((entry) => typeof entry === 'string' && DSH_TYPES.has(entry)))]
	if (types.length === 0) return undefined
	if (types.length === 1) return { type: types[0] }
	const nullable = types.filter((entry) => entry === 'null')
	const concrete = types.filter((entry) => entry !== 'null')
	if (concrete.length === 1 && nullable.length === 1) return { type: concrete[0] }
	if (concrete.length === 0) return { type: 'null' }
	if (concrete.length === 1) return { type: concrete[0] }
	return { oneOf: concrete.map((entry) => ({ type: entry })) }
}

/** Whether a scalar schema keyword's declared values match one scalar type. */
function scalarMatches(type, value) {
	switch (type) {
		case 'string':
			return typeof value === 'string'
		case 'number':
			return typeof value === 'number' && Number.isFinite(value)
		case 'integer':
			return Number.isSafeInteger(value)
		case 'boolean':
			return typeof value === 'boolean'
		case 'null':
			return value === null
		default:
			return false
	}
}

/**
 * Normalize one JSON Schema node into DSH's enforced subset.
 *
 * MCP servers routinely publish `$schema`, `$defs`/`$ref`, `format`, numeric
 * bounds, `anyOf`, tuple `items`, and type arrays. DSH's
 * `assertSupportedJsonSchema` rejects every one of those, so this function
 * performs a lossy, presentation-only downgrade: local refs are inlined,
 * `anyOf` becomes `oneOf`, object `allOf` branches merge, tuple items become
 * `{}`, type arrays collapse, and unknown keywords are dropped. The connector
 * still receives the model's original arguments; this schema only drives the
 * model-facing tool declaration.
 */
function sanitizeSchemaNode(node, root, seenRefs, depth) {
	if (depth > MAX_SCHEMA_DEPTH) return {}
	if (node === true) return {}
	if (node === false) return {}
	if (!isRecord(node)) return {}

	if (typeof node.$ref === 'string') {
		const ref = node.$ref
		if (seenRefs.has(ref)) return {}
		const resolved = resolveLocalRef(root, ref)
		if (resolved === undefined) return {}
		seenRefs.add(ref)
		try {
			const inlined = sanitizeSchemaNode(resolved, root, seenRefs, depth + 1)
			if (isRecord(inlined) && typeof node.description === 'string' && inlined.description === undefined) {
				inlined.description = node.description
			}
			return inlined
		} finally {
			seenRefs.delete(ref)
		}
	}

	const out = {}
	for (const key of DSH_ANNOTATION_KEYS) {
		if (!Object.hasOwn(node, key)) continue
		if (key === 'description' || key === 'title') {
			if (typeof node[key] === 'string') out[key] = node[key]
			continue
		}
		const annotation = cloneJsonSafe(node[key])
		if (annotation !== undefined) out[key] = annotation
	}

	const siblingConstraints = {}
	for (const key of ['properties', 'required', 'additionalProperties', 'items', 'enum', 'const']) {
		if (Object.hasOwn(node, key)) siblingConstraints[key] = node[key]
	}

	// anyOf/allOf are outside the DSH subset; approximate them without ever
	// emitting the unsupported keyword.
	if (Array.isArray(node.anyOf)) {
		const branches = node.anyOf
			.map((branch) => sanitizeSchemaNode(branch, root, seenRefs, depth + 1))
			.filter((branch) => isRecord(branch))
		if (branches.length === 1) Object.assign(out, branches[0])
		else if (branches.length >= 2 && Object.keys(siblingConstraints).length === 0) out.oneOf = branches
		if (Object.keys(siblingConstraints).length > 0) {
			const sibling = sanitizeSchemaNode({ ...siblingConstraints, ...withoutKeys(node, ['anyOf']) }, root, seenRefs, depth + 1)
			return sibling
		}
		return out
	}
	if (Array.isArray(node.allOf)) {
		const branches = node.allOf
			.map((branch) => sanitizeSchemaNode(branch, root, seenRefs, depth + 1))
			.filter((branch) => isRecord(branch))
		const merged = mergeAllOfBranches(branches)
		if (isRecord(merged)) Object.assign(out, merged)
		if (Object.keys(siblingConstraints).length > 0) {
			const sibling = sanitizeSchemaNode({ ...siblingConstraints, ...withoutKeys(node, ['allOf']) }, root, seenRefs, depth + 1)
			if (isRecord(sibling)) Object.assign(out, sibling)
		}
		return out
	}

	if (Array.isArray(node.oneOf)) {
		const branches = node.oneOf
			.map((branch) => sanitizeSchemaNode(branch, root, seenRefs, depth + 1))
			.filter((branch) => isRecord(branch))
		if (Object.keys(siblingConstraints).length === 0 && !Object.hasOwn(node, 'type')) {
			if (branches.length >= 2) out.oneOf = branches
			else if (branches.length === 1) Object.assign(out, branches[0])
			return out
		}
		if (branches.length === 1 && Object.keys(siblingConstraints).length === 0) {
			Object.assign(out, branches[0])
			return out
		}
		// A oneOf mixed with sibling constraints cannot be represented; keep the
		// sibling keywords (the branches are dropped rather than mis-validated).
	}

	const typeSpec = normalizeTypeValue(node.type)
	const hasEncodedType = typeSpec !== undefined
	const siblingOnly = Object.keys(siblingConstraints).length > 0

	if (!hasEncodedType && !siblingOnly) {
		// Annotation-only schema: DSH accepts `{}` as unconstrained JSON.
		return out
	}

	if (hasEncodedType && Object.hasOwn(typeSpec, 'oneOf')) {
		out.oneOf = typeSpec.oneOf
		return out
	}

	let type = hasEncodedType ? typeSpec.type : undefined
	if (type === undefined) {
		if (Object.hasOwn(siblingConstraints, 'properties') || Object.hasOwn(siblingConstraints, 'required')) type = 'object'
		else if (Object.hasOwn(siblingConstraints, 'items')) type = 'array'
		else if (Object.hasOwn(siblingConstraints, 'enum') || Object.hasOwn(siblingConstraints, 'const')) type = 'string'
		else return out
	}
	out.type = type

	switch (type) {
		case 'object': {
			const properties = {}
			if (isRecord(node.properties)) {
				for (const [key, value] of Object.entries(node.properties)) {
					properties[key] = sanitizeSchemaNode(value, root, seenRefs, depth + 1)
				}
			}
			out.properties = properties
			if (Array.isArray(node.required)) {
				const required = [...new Set(node.required.filter((key) => typeof key === 'string' && Object.hasOwn(properties, key)))]
				if (required.length > 0) out.required = required
			}
			if (typeof node.additionalProperties === 'boolean') {
				out.additionalProperties = node.additionalProperties
			} else if (Object.hasOwn(node, 'additionalProperties')) {
				// DSH only accepts a boolean here; a schema-valued additionalProperties
				// means "extra keys are allowed with a type", so keep them allowed.
				out.additionalProperties = true
			}
			break
		}
		case 'array': {
			if (Array.isArray(node.items)) {
				// Tuple validation is not expressible; an empty schema accepts any item.
				out.items = {}
			} else if (Object.hasOwn(node, 'items')) {
				out.items = sanitizeSchemaNode(node.items, root, seenRefs, depth + 1)
			}
			break
		}
		default: {
			if (Array.isArray(node.enum)) {
				const allowed = node.enum.filter((entry) => scalarMatches(type, entry)).slice(0, 256)
				if (allowed.length > 0) out.enum = allowed
			}
			if (Object.hasOwn(node, 'const') && scalarMatches(type, node.const)) {
				if (!Array.isArray(out.enum) || out.enum.includes(node.const)) out.const = node.const
			}
			break
		}
	}
	return out
}

/** Copy a record without the named keys. */
function withoutKeys(record, keys) {
	const out = {}
	for (const [key, value] of Object.entries(record)) {
		if (!keys.includes(key)) out[key] = value
	}
	return out
}

/**
 * Downgrade one MCP `inputSchema` to the JSON Schema subset DSH enforces.
 *
 * @param {unknown} schema - the untrusted MCP input schema.
 * @returns {object} always a JSON-safe supported schema (`{}` if unusable).
 */
export function sanitizeToolInputSchema(schema) {
	if (!isRecord(schema)) return {}
	try {
		const clean = sanitizeSchemaNode(schema, schema, new Set(), 0)
		if (!isRecord(clean)) return {}
		return cloneJsonSafe(clean) ?? {}
	} catch {
		return {}
	}
}

// ---------------------------------------------------------------------------
// MCP content normalization
// ---------------------------------------------------------------------------

/** Describe an image block without ever copying base64 into text or a value. */
function describeImageBlock(block) {
	const mimeType = optionalString(block.mimeType) ?? 'image/unknown'
	const data = typeof block.data === 'string' ? block.data : ''
	const bytes = data.length > 0 ? Math.floor((data.length * 3) / 4) : 0
	const size = bytes > 0 ? `约 ${bytes} 字节` : '大小未知'
	return `[图片：${mimeType}，${size}；base64 数据未内联]`
}

/** Describe an embedded resource block without copying its payload. */
function describeResourceBlock(block) {
	const resource = isRecord(block.resource) ? block.resource : {}
	const uri = optionalString(resource.uri) ?? '未知 URI'
	const mimeType = optionalString(resource.mimeType)
	const text = typeof resource.text === 'string' ? resource.text : undefined
	const head = text !== undefined ? `，文本 ${text.length} 字符` : `，base64/blob ${typeof resource.blob === 'string' ? resource.blob.length : 0} 字符`
	return `[嵌入资源：${uri}${mimeType ? `（${mimeType}）` : ''}${head}；内容未内联]`
}

/** Convert one MCP content block into a bounded text block. */
function normalizeContentBlock(block) {
	if (!isRecord(block)) {
		const text = typeof block === 'string' ? block : safeStringify(block)
		return { type: 'text', text }
	}
	switch (block.type) {
		case 'text':
			return { type: 'text', text: typeof block.text === 'string' ? block.text : safeStringify(block.text) }
		case 'image':
			return { type: 'text', text: describeImageBlock(block) }
		case 'audio':
			return {
				type: 'text',
				text: `[音频：${optionalString(block.mimeType) ?? 'audio/unknown'}；音频数据未内联]`,
			}
		case 'resource_link':
			return {
				type: 'text',
				text: `[资源链接：${optionalString(block.name) ?? '未命名'}（${optionalString(block.uri) ?? '未知 URI'}）${
					optionalString(block.description) ? ` — ${block.description}` : ''
				}]`,
			}
		case 'resource':
			return { type: 'text', text: describeResourceBlock(block) }
		default:
			return {
				type: 'text',
				text: `[${optionalString(block.type) ?? '未知类型'} 内容：${safeStringify(boundJson(block))}]`,
			}
	}
}

/** Serialize a value for diagnostics without throwing. */
function safeStringify(value) {
	try {
		const text = JSON.stringify(value)
		return typeof text === 'string' ? text : String(value)
	} catch {
		return String(value)
	}
}

/**
 * Normalize an MCP content array into text-only blocks.
 *
 * Images, audio, and embedded resources become readable descriptors so a raw
 * base64 payload never enters the model context, the canonical value, or the
 * lifecycle store. This round intentionally does not emit image blocks.
 *
 * @param {unknown} content - MCP content blocks.
 * @returns {Array<{type:'text', text:string}>} normalized text blocks.
 */
export function normalizeMcpContent(content) {
	if (!Array.isArray(content)) return []
	const blocks = []
	let total = 0
	for (const entry of content.slice(0, 256)) {
		const normalized = normalizeContentBlock(entry)
		if (normalized === undefined) continue
		if (typeof normalized.text !== 'string') continue
		if (total + normalized.text.length > MAX_RENDER_CHARS) {
			blocks.push({ type: 'text', text: '[内容过长，后续内容已省略]' })
			break
		}
		blocks.push(normalized)
		total += normalized.text.length
	}
	return blocks
}

/** Join normalized blocks into one bounded text message. */
function blocksToText(blocks) {
	return blocks
		.map((block) => (block.type === 'text' && typeof block.text === 'string' ? block.text : safeStringify(block)))
		.join('\n')
}

/**
 * Build the canonical DSH tool value from one MCP `CallToolResult`.
 *
 * @param {unknown} rawResult - MCP CallToolResult.
 * @returns {{content: Array<{type:'text', text:string}>, structuredContent?: unknown}}
 */
export function buildToolValue(rawResult) {
	const result = isRecord(rawResult) ? rawResult : {}
	const value = { content: normalizeMcpContent(result.content) }
	if (Object.hasOwn(result, 'structuredContent')) {
		const structured = boundJson(result.structuredContent)
		if (structured !== undefined) value.structuredContent = structured
	}
	return value
}

/**
 * Render one canonical tool value for the model.
 *
 * Must stay synchronous: DSH calls `output.render` during result materialization
 * and rejects asynchronously produced content.
 *
 * @param {unknown} value - canonical value from `buildToolValue`.
 * @param {string} rawName - the MCP raw tool name, used in the empty fallback.
 * @returns {Array<{type:'text', text:string}>} DSH content blocks.
 */
export function renderToolResult(value, rawName) {
	const record = isRecord(value) ? value : {}
	const blocks = Array.isArray(record.content) ? record.content : []
	let text = blocksToText(blocks.filter(isRecord))
	if (text.trim() === '' && record.structuredContent !== undefined) {
		try {
			text = JSON.stringify(record.structuredContent, null, 2)
		} catch {
			text = safeStringify(record.structuredContent)
		}
	}
	if (typeof text !== 'string' || text.trim() === '') text = `${rawName}：无返回内容`
	if (text.length > MAX_RENDER_CHARS) text = `${text.slice(0, MAX_RENDER_CHARS)}\n[结果过长，已截断]`
	return [{ type: 'text', text }]
}

// ---------------------------------------------------------------------------
// Minimal MCP stdio client
// ---------------------------------------------------------------------------

/** Structured connector failure with a machine-readable `code`. */
export class ConnectorError extends Error {
	/**
	 * @param {string} code - stable failure code.
	 * @param {string} message - human-readable message.
	 * @param {unknown} [cause] - optional underlying error.
	 */
	constructor(code, message, cause) {
		super(message, cause === undefined ? undefined : { cause })
		this.name = 'ConnectorError'
		this.code = code
	}
}

/** Error for a connector that is closed, crashed, or never started. */
function connectorUnavailableError(label, detail) {
	return new ConnectorError(
		'CONNECTOR_UNAVAILABLE',
		`${label}: QuietBridge 连接器不可用（${detail}）。本次调用未执行；不要重放输入，请先核实既有结果。`,
	)
}

/**
 * One newline-delimited JSON-RPC 2.0 client over a child process's stdio.
 *
 * The client implements just the MCP surface this bundle needs: initialize,
 * notifications/initialized, tools/list (with cursor paging), tools/call, and
 * cancellation. It never writes to the session log, never retries a call, and
 * turns every transport failure into a `ConnectorError` so one crashed Agent
 * connector cannot affect another Agent.
 */
export class ConnectorClient {
	/**
	 * @param {object} options - spawn and timeout settings.
	 */
	constructor(options) {
		this.label = options.label ?? 'quietbridge'
		this.command = options.python
		this.args = isPythonCommand(options.python) ? ['-B', options.connector] : [options.connector]
		this.env = options.env
		this.cwd = options.cwd
		this.requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS
		this.logger = options.logger
		this.onToolsChanged = options.onToolsChanged ?? (() => {})
		this.onExit = options.onExit ?? (() => {})

		this.child = undefined
		this.status = 'new'
		this.nextId = 1
		this.pending = new Map()
		this.cancelledIds = new Set()
		this.buffer = ''
		this.exitError = undefined
		this.serverInfo = undefined
		this.protocolVersion = undefined
		this.exited = Promise.resolve()
		this.resolveExited = () => {}
		this.startError = undefined
	}

	/** Whether calls can be written to the child right now. */
	get usable() {
		if (this.child === undefined) return false
		if (this.status === 'closed' || this.status === 'failed') return false
		if (this.child.exitCode !== null && this.child.exitCode !== undefined) return false
		if (this.child.signalCode !== null && this.child.signalCode !== undefined) return false
		return this.child.stdin !== null && this.child.stdin !== undefined && !this.child.stdin.destroyed
	}

	/** Spawn the connector process and start reading its stdout. */
	start() {
		if (this.status !== 'new') throw new Error(`${this.label}: connector already started`)
		this.status = 'starting'
		let child
		try {
			child = spawn(this.command, this.args, {
				env: this.env,
				cwd: this.cwd,
				stdio: ['pipe', 'pipe', 'pipe'],
				windowsHide: true,
			})
		} catch (cause) {
			this.status = 'failed'
			this.startError = new ConnectorError('CONNECTOR_START_FAILED', `${this.label}: 连接器进程启动失败`, cause)
			throw this.startError
		}
		this.child = child
		this.exited = new Promise((resolve) => {
			this.resolveExited = resolve
		})
		child.once('error', (cause) => {
			const exit = this.handleExit('CONNECTOR_START_FAILED', cause)
			if (this.status !== 'closed') this.startError ??= exit
		})
		child.once('exit', (code, signal) => {
			this.handleExit(
				'CONNECTOR_EXITED',
				new Error(`连接器进程已退出（code=${code ?? 'null'}, signal=${signal ?? 'null'}）`),
			)
		})
		if (child.stdout) {
			child.stdout.setEncoding('utf8')
			child.stdout.on('data', (chunk) => this.handleStdout(chunk))
		}
		if (child.stderr) {
			child.stderr.setEncoding('utf8')
			child.stderr.on('data', (chunk) => this.handleStderr(chunk))
		}
		if (child.stdin) child.stdin.on('error', () => {})
		return this
	}

	/** Reject every in-flight request and record why the connector went away. */
	handleExit(code, cause) {
		if (this.status === 'closed') {
			this.resolveExited()
			return this.exitError
		}
		if (this.status !== 'failed') this.status = 'failed'
		const next = new ConnectorError(code, `${this.label}: ${cause instanceof Error ? cause.message : String(cause)}`, cause)
		this.exitError ??= next
		this.startError ??= next
		for (const [id, entry] of this.pending) {
			this.pending.delete(id)
			this.finishEntry(entry)
			entry.reject(next)
		}
		this.buffer = ''
		this.resolveExited()
		try {
			this.onExit(next)
		} catch {
			/* observer must not change exit handling */
		}
		return this.exitError
	}

	/** Buffer stdout and dispatch complete JSON-RPC lines. */
	handleStdout(chunk) {
		this.buffer += chunk
		if (this.buffer.length > MAX_RPC_LINE_CHARS) {
			warn(this.logger, `${this.label}: JSON-RPC stdout 超过 ${MAX_RPC_LINE_CHARS} 字符，关闭连接器`)
			this.close().catch(() => {})
			return
		}
		let newline = this.buffer.indexOf('\n')
		while (newline >= 0) {
			const line = this.buffer.slice(0, newline).replace(/\r$/, '')
			this.buffer = this.buffer.slice(newline + 1)
			if (line.trim() !== '') this.handleLine(line)
			if (this.buffer.length > MAX_RPC_LINE_CHARS) {
				warn(this.logger, `${this.label}: JSON-RPC stdout 缓冲溢出，关闭连接器`)
				this.close().catch(() => {})
				return
			}
			newline = this.buffer.indexOf('\n')
		}
	}

	/** Parse and route one JSON-RPC message. */
	handleLine(line) {
		let message
		try {
			message = JSON.parse(line)
		} catch {
			warn(this.logger, `${this.label}: 忽略无法解析的 JSON-RPC 行（${line.length} 字符）`)
			return
		}
		for (const entry of Array.isArray(message) ? message : [message]) this.handleMessage(entry)
	}

	/** Route one decoded message: notification, server request, or response. */
	handleMessage(message) {
		if (!isRecord(message)) return
		if (typeof message.method === 'string') {
			if (Object.hasOwn(message, 'id')) {
				// The client exposes no server-initiated requests; answer so a server
				// waiting on a reply cannot wedge its queue forever.
				this.tryWrite({
					jsonrpc: '2.0',
					id: message.id,
					error: { code: -32601, message: `Method not found: ${message.method}` },
				})
				return
			}
			if (message.method === 'notifications/tools/list_changed') {
				debugLog(this.logger, `${this.label}: 收到 tools/list_changed`)
				try {
					Promise.resolve(this.onToolsChanged()).catch((cause) =>
						warn(this.logger, `${this.label}: tools/list_changed 同步失败：${cause instanceof Error ? cause.message : String(cause)}`),
					)
				} catch (cause) {
					warn(this.logger, `${this.label}: tools/list_changed 处理失败：${cause instanceof Error ? cause.message : String(cause)}`)
				}
			}
			return
		}
		if (!Object.hasOwn(message, 'id')) return
		const id = message.id
		if (this.cancelledIds.has(id)) {
			this.cancelledIds.delete(id)
			return
		}
		const entry = this.pending.get(id)
		if (entry === undefined) return
		this.pending.delete(id)
		this.finishEntry(entry)
		if (isRecord(message.error)) {
			const rpcMessage = optionalString(message.error.message) ?? '未知 JSON-RPC 错误'
			entry.reject(new ConnectorError('CONNECTOR_RPC_ERROR', `${this.label}: ${rpcMessage}`))
			return
		}
		entry.resolve(message.result)
	}

	/** Capture a bounded amount of child stderr for diagnostics. */
	handleStderr(chunk) {
		const text = String(chunk).trim()
		if (text === '') return
		const line = text.length > 2000 ? `${text.slice(0, 2000)}…` : text
		debugLog(this.logger, `${this.label}: connector stderr: ${line}`)
	}

	/** Write one JSON-RPC object; return false and never throw. */
	tryWrite(message) {
		try {
			this.write(message)
			return true
		} catch {
			return false
		}
	}

	/** Write one JSON-RPC object, throwing a structured error when unusable. */
	write(message) {
		if (!this.usable) {
			throw this.exitError ?? connectorUnavailableError(this.label, '连接器进程未运行')
		}
		try {
			this.child.stdin.write(`${JSON.stringify(message)}\n`)
		} catch (cause) {
			throw new ConnectorError('CONNECTOR_EXITED', `${this.label}: 写入连接器失败`, cause)
		}
	}

	/** Send one notification and swallow transport failures. */
	notify(method, params) {
		return this.tryWrite({ jsonrpc: '2.0', method, ...(params === undefined ? {} : { params }) })
	}

	/**
	 * Send one request and resolve its result.
	 *
	 * Timeout and `signal` abort both send `notifications/cancelled` and record
	 * the id so a late response is discarded instead of resolving a call whose
	 * outcome the model already observed as cancelled.
	 *
	 * @param {string} method - JSON-RPC method.
	 * @param {object} [params] - method parameters.
	 * @param {{timeoutMs?: number, signal?: AbortSignal}} [options] - budget and cancellation.
	 * @returns {Promise<unknown>} the JSON-RPC result.
	 */
	request(method, params, options = {}) {
		if (!this.usable) {
			return Promise.reject(this.exitError ?? connectorUnavailableError(this.label, '连接器进程不可用'))
		}
		const id = this.nextId++
		const timeoutMs = options.timeoutMs ?? this.requestTimeoutMs
		const signal = options.signal
		return new Promise((resolve, reject) => {
			const entry = { id, method, resolve, reject, timer: undefined, signal, onAbort: undefined }
			entry.timer = setTimeout(() => {
				if (!this.settle(id, new ConnectorError('CONNECTOR_TIMEOUT', `${this.label}: ${method} 超时（${timeoutMs}ms），结果未知；不要重放输入`))) {
					return
				}
				this.cancel(id)
			}, timeoutMs)
			entry.timer.unref?.()
			this.pending.set(id, entry)
			if (signal !== undefined) {
				if (signal.aborted) {
					this.settle(id, new ConnectorError('CONNECTOR_CANCELLED', `${this.label}: 调用已取消`))
					return
				}
				entry.onAbort = () => {
					if (this.settle(id, new ConnectorError('CONNECTOR_CANCELLED', `${this.label}: 调用已取消`))) this.cancel(id)
				}
				signal.addEventListener('abort', entry.onAbort, { once: true })
			}
			try {
				this.write({ jsonrpc: '2.0', id, method, params: params ?? {} })
			} catch (cause) {
				this.settle(id, cause)
			}
		})
	}

	/** Resolve or reject one pending request exactly once. */
	settle(id, error, value) {
		const entry = this.pending.get(id)
		if (entry === undefined) return false
		this.pending.delete(id)
		this.finishEntry(entry)
		if (error !== undefined) entry.reject(error)
		else entry.resolve(value)
		return true
	}

	/** Clean up one pending request's timer and abort listener. */
	finishEntry(entry) {
		if (entry.timer !== undefined) clearTimeout(entry.timer)
		entry.timer = undefined
		if (entry.signal !== undefined && entry.onAbort !== undefined) {
			try {
				entry.signal.removeEventListener('abort', entry.onAbort)
			} catch {
				/* ignore */
			}
			entry.onAbort = undefined
		}
	}

	/** Send `notifications/cancelled` and remember the id for late-response discard. */
	cancel(id) {
		this.cancelledIds.add(id)
		if (this.cancelledIds.size > 256) {
			const oldest = this.cancelledIds.values().next().value
			if (oldest !== undefined) this.cancelledIds.delete(oldest)
		}
		this.notify('notifications/cancelled', { requestId: id })
	}

	/**
	 * Run the MCP initialize handshake.
	 *
	 * The QuietBridge server echoes the client's `protocolVersion`; we accept any
	 * echoed version and record it, falling back to the version we sent.
	 *
	 * @param {{timeoutMs?: number}} [options] - startup budget.
	 * @returns {Promise<{protocolVersion: string, serverInfo?: unknown}>} handshake result.
	 */
	async initialize(options = {}) {
		const timeoutMs = options.timeoutMs ?? this.requestTimeoutMs
		const result = await this.request(
			'initialize',
			{
				protocolVersion: DEFAULT_PROTOCOL_VERSION,
				capabilities: {},
				clientInfo: CLIENT_INFO,
			},
			{ timeoutMs },
		)
		const negotiated = isRecord(result) ? result.protocolVersion : undefined
		this.protocolVersion =
			typeof negotiated === 'string' && negotiated !== '' ? negotiated : DEFAULT_PROTOCOL_VERSION
		this.serverInfo = isRecord(result) ? result.serverInfo : undefined
		if (!SUPPORTED_PROTOCOL_VERSIONS.includes(this.protocolVersion)) {
			warn(this.logger, `${this.label}: 服务端协商到未验证的 MCP 协议版本 ${this.protocolVersion}`)
		}
		this.notify('notifications/initialized', {})
		return { protocolVersion: this.protocolVersion, serverInfo: this.serverInfo }
	}

	/**
	 * Fetch the complete tool list, following `nextCursor` pages defensively.
	 *
	 * @param {{timeoutMs?: number}} [options] - per-page budget.
	 * @returns {Promise<Array<object>>} raw MCP tool records.
	 */
	async listTools(options = {}) {
		const timeoutMs = options.timeoutMs ?? this.requestTimeoutMs
		const tools = []
		let cursor
		for (let page = 0; page < 20; page += 1) {
			const result = await this.request('tools/list', cursor === undefined ? {} : { cursor }, { timeoutMs })
			if (!isRecord(result) || !Array.isArray(result.tools)) {
				throw new ConnectorError('CONNECTOR_PROTOCOL_ERROR', `${this.label}: tools/list 返回格式无效`)
			}
			tools.push(...result.tools)
			cursor = typeof result.nextCursor === 'string' && result.nextCursor !== '' ? result.nextCursor : undefined
			if (cursor === undefined) return tools
		}
		throw new ConnectorError('CONNECTOR_PROTOCOL_ERROR', `${this.label}: tools/list 分页超过 20 页`)
	}

	/**
	 * Call one MCP tool by raw name.
	 *
	 * @param {string} rawName - the server's own tool name.
	 * @param {object} args - model arguments (passed through unchanged).
	 * @param {{signal?: AbortSignal, timeoutMs?: number}} [options] - cancellation and budget.
	 * @returns {Promise<unknown>} MCP CallToolResult.
	 */
	callTool(rawName, args, options = {}) {
		return this.request(
			'tools/call',
			{ name: rawName, arguments: isRecord(args) ? args : {} },
			{ signal: options.signal, timeoutMs: options.timeoutMs ?? this.requestTimeoutMs },
		)
	}

	/** Close the connector: reject everything pending, then reap the child. */
	async close() {
		this.status = 'closed'
		const exitError = new ConnectorError('CONNECTOR_CLOSED', `${this.label}: 连接器已关闭`)
		for (const [id, entry] of this.pending) {
			this.pending.delete(id)
			this.finishEntry(entry)
			entry.reject(exitError)
		}
		const child = this.child
		if (child === undefined) {
			this.resolveExited()
			return
		}
		try {
			if (child.stdin && !child.stdin.destroyed) child.stdin.end()
		} catch {
			/* ignore */
		}
		if (child.exitCode === null && child.signalCode === null) {
			try {
				child.kill('SIGTERM')
			} catch {
				/* ignore */
			}
			await Promise.race([this.exited, delay(CLOSE_GRACE_MS)])
			if (child.exitCode === null && child.signalCode === null) {
				try {
					child.kill('SIGKILL')
				} catch {
					/* ignore */
				}
				await Promise.race([this.exited, delay(CLOSE_GRACE_MS)])
			}
		}
	}
}

/** Sleep without keeping the event loop alive. */
function delay(ms) {
	return new Promise((resolve) => {
		const timer = setTimeout(resolve, ms)
		timer.unref?.()
	})
}

// ---------------------------------------------------------------------------
// Lifecycle bridge (plugin -> Python lifecycle.py)
// ---------------------------------------------------------------------------

/** Map a DSH `turn/end` reason to the lifecycle protocol reason. */
export function mapTurnReason(reason) {
	const kind = isRecord(reason) ? reason.kind : reason
	switch (kind) {
		case 'completed':
		case 'blocked':
		case 'max-tokens':
			return 'completed'
		case 'aborted':
			return 'cancelled'
		case 'interrupted':
			return 'interrupted'
		case 'error':
			return 'error'
		default:
			return 'completed'
	}
}

/** Join one session id and numeric turn into the lifecycle `turnId`. */
export function turnIdOf(sessionId, turn) {
	const value = typeof turn === 'number' && Number.isSafeInteger(turn) ? String(turn) : optionalString(turn)
	if (value === undefined) return ''
	return `${sessionId}:${value}`
}

/**
 * Best-effort bridge to `lifecycle.py` (one child per event).
 *
 * Ordering is preserved per Agent through a small promise chain, but nothing is
 * ever awaited by a DSH event listener. A slow, missing, or failing lifecycle
 * script can only lose lifecycle bookkeeping; it cannot block a turn or a tool
 * call, and it never causes a host continuation.
 */
export class LifecycleBridge {
	/**
	 * @param {object} options - script, interpreter, timeout, logger.
	 */
	constructor(options) {
		this.python = options.python
		this.script = options.script
		this.configDir = options.configDir
		this.timeoutMs = options.timeoutMs
		this.logger = options.logger
		this.missingWarned = false
		this.env = options.env
	}

	/** Whether the configured lifecycle script is present. */
	available() {
		try {
			return existsSync(this.script)
		} catch {
			return false
		}
	}

	/**
	 * Queue one lifecycle event for one Agent.
	 *
	 * @param {object} state - per-Agent state holding the queue.
	 * @param {object} payload - lifecycle event (without sessionId/agentId).
	 */
	send(state, payload) {
		if (this.available() === false) {
			if (!this.missingWarned) {
				this.missingWarned = true
				warn(this.logger, `lifecycle 脚本不存在，跳过生命周期事件：${this.script}`)
			}
			return
		}
		if (state.lifecycleBacklog >= MAX_LIFECYCLE_QUEUE) {
			if (!state.lifecycleDropped) {
				state.lifecycleDropped = true
				warn(this.logger, `${state.agentId}: lifecycle 队列已满，丢弃后续生命周期事件`)
			}
			return
		}
		state.lifecycleBacklog += 1
		state.lifecycle = state.lifecycle
			.then(() => this.invoke(state, { event: payload.event, sessionId: state.sessionId, agentId: state.agentId, ...payload }))
			.catch((cause) => {
				warn(this.logger, `${state.agentId}: lifecycle 事件 ${payload.event} 失败：${cause instanceof Error ? cause.message : String(cause)}`)
			})
			.finally(() => {
				state.lifecycleBacklog = Math.max(0, state.lifecycleBacklog - 1)
			})
	}

	/** Spawn one lifecycle child, feed it one JSON document, and bound its run. */
	invoke(state, document) {
		const text = JSON.stringify(document)
		if (typeof text !== 'string') return Promise.resolve()
		if (Buffer.byteLength(text, 'utf8') > MAX_LIFECYCLE_BYTES) {
			warn(this.logger, `${state.agentId}: lifecycle 事件 ${document.event} 超过 ${MAX_LIFECYCLE_BYTES} 字节，已丢弃`)
			return Promise.resolve()
		}
		return new Promise((resolve) => {
			let child
			let settled = false
			let stderr = ''
			const finish = (failure) => {
				if (settled) return
				settled = true
				clearTimeout(timer)
				if (failure !== undefined) warn(this.logger, `${state.agentId}: lifecycle ${failure}`)
				resolve()
			}
			const timer = setTimeout(() => {
				try {
					child?.kill('SIGKILL')
				} catch {
					/* ignore */
				}
				finish(`事件 ${document.event} 超时（${this.timeoutMs}ms）`)
			}, this.timeoutMs)
			timer.unref?.()
			try {
				const args = isPythonCommand(this.python)
					? ['-B', this.script, '--config-dir', this.configDir]
					: [this.script, '--config-dir', this.configDir]
				child = spawn(this.python, args, {
					env: this.env,
					cwd: path.dirname(this.script),
					stdio: ['pipe', 'ignore', 'pipe'],
					windowsHide: true,
				})
			} catch (cause) {
				finish(`spawn 失败：${cause instanceof Error ? cause.message : String(cause)}`)
				return
			}
			child.once('error', (cause) => finish(`spawn 失败：${cause instanceof Error ? cause.message : String(cause)}`))
			child.once('exit', (code) => {
				if (code !== 0 && code !== null && stderr.trim() !== '') {
					finish(`退出码 ${code}：${stderr.trim().slice(0, 500)}`)
					return
				}
				finish()
			})
			child.stderr?.setEncoding('utf8')
			child.stderr?.on('data', (chunk) => {
				if (stderr.length < 8192) stderr += String(chunk)
			})
			child.stdin?.on('error', () => {})
			try {
				child.stdin?.end(`${text}\n`)
			} catch {
				finish('stdin 写入失败')
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

/** Build the environment one per-Agent connector receives (trusted identity). */
function connectorEnvironment(settings, state) {
	const env = { ...process.env }
	// Never inherit a parent connector identity or an inherited worker role; this
	// Agent's identity wins and the connector must start as a router, not a worker.
	const inherited = new Set(['QUIETBRIDGE_HOST', 'QUIETBRIDGE_SESSION_KEY',
		'QUIETBRIDGE_ROUTING', 'QUIETBRIDGE_CONNECTOR_REEXEC'])
	for (const key of Object.keys(env)) {
		if (inherited.has(key) || key.startsWith('QUIETBRIDGE_HOST_')) delete env[key]
	}
	env.QUIETBRIDGE_HOST = 'deepseek-harness'
	env.QUIETBRIDGE_HOST_SESSION_ID = state.sessionId
	env.QUIETBRIDGE_HOST_AGENT_ID = state.agentId
	env.QUIETBRIDGE_CONFIG_DIR = settings.configDir
	if (settings.helper !== undefined) env.QUIETBRIDGE_HELPER_PATH = settings.helper
	env.QUIETBRIDGE_SERVER_NAME = settings.serverName
	return env
}

/** Build the environment one lifecycle child receives. */
function lifecycleEnvironment(settings, state) {
	const env = { ...process.env }
	env.QUIETBRIDGE_HOST = 'deepseek-harness'
	env.QUIETBRIDGE_HOST_SESSION_ID = state.sessionId
	env.QUIETBRIDGE_HOST_AGENT_ID = state.agentId
	env.QUIETBRIDGE_CONFIG_DIR = settings.configDir
	return env
}

/** Extract the Agent from an `agent/created` / `agent/disposed` payload. */
function agentFromPayload(payload) {
	if (!isRecord(payload)) return undefined
	const candidate = payload.agent
	if (!isRecord(candidate)) return undefined
	if (typeof candidate.id !== 'string' || candidate.id === '') return undefined
	const session = isRecord(candidate.session) ? candidate.session : undefined
	if (session === undefined || typeof session.id !== 'string' || session.id === '') return undefined
	if (!isRecord(candidate.ctx)) return undefined
	return candidate
}

/** Whether the session event is one this plugin understands. */
function isSessionEvent(event) {
	return isRecord(event) && typeof event.type === 'string'
}

/**
 * Register the QuietBridge host plugin.
 *
 * @param {object} ctx - Cordis plugin context.
 * @param {object} config - loader row config (see {@link resolveConfig}).
 */
export function apply(ctx, config) {
	const settings = resolveConfig(config)
	// The lifecycle CLI and the connector both write bookkeeping below
	// configDir. A fresh installation may not have created it yet, so create the
	// private directory once here instead of failing every lifecycle hint.
	try {
		mkdirSync(settings.configDir, { recursive: true, mode: 0o700 })
	} catch (cause) {
		warn(ctx.logger, `configDir 创建失败：${cause instanceof Error ? cause.message : String(cause)}`)
	}
	const logger = ctx.logger
	const states = new Map()
	const byAgentId = new Map()
	const bySessionId = new Map()
	/** States that currently hold a spawned connector process. */
	const connectorSlots = new Set()
	let disposed = false

	const lifecycle = new LifecycleBridge({
		python: settings.python,
		script: settings.lifecycleScript,
		configDir: settings.configDir,
		timeoutMs: settings.lifecycleTimeoutMs,
		logger,
		env: { ...process.env, QUIETBRIDGE_CONFIG_DIR: settings.configDir },
	})

	/** Track one Agent's state in the session-indexed maps. */
	const indexState = (state) => {
		let set = bySessionId.get(state.sessionId)
		if (set === undefined) {
			set = new Set()
			bySessionId.set(state.sessionId, set)
		}
		set.add(state)
	}

	/** Remove one Agent's state from the session-indexed maps. */
	const unindexState = (state) => {
		const set = bySessionId.get(state.sessionId)
		if (set === undefined) return
		set.delete(state)
		if (set.size === 0) bySessionId.delete(state.sessionId)
	}

	/** Send one lifecycle document through this Agent's ordered queue. */
	const sendLifecycle = (state, payload) => {
		try {
			lifecycle.send(state, payload)
		} catch (cause) {
			warn(logger, `${state.agentId}: lifecycle 入队失败：${cause instanceof Error ? cause.message : String(cause)}`)
		}
	}

	// -- Session event bridging (turn + tool receipts) -----------------------

	/** Remember one callId for tool/call -> tool/result correlation. */
	const rememberCall = (state, callId, entry) => {
		if (typeof callId !== 'string' || callId === '') return
		state.pendingCalls.set(callId, entry)
		if (state.pendingCalls.size > MAX_PENDING_TOOL_CALLS) {
			const oldest = state.pendingCalls.keys().next().value
			if (oldest !== undefined) state.pendingCalls.delete(oldest)
		}
	}

	/** Remember one reported callId so the two receipt paths cannot duplicate. */
	const markReported = (state, callId) => {
		if (typeof callId !== 'string' || callId === '') return false
		if (state.reportedCalls.has(callId)) return true
		state.reportedCalls.add(callId)
		if (state.reportedCalls.size > MAX_REPORTED_CALL_IDS) {
			const oldest = state.reportedCalls.values().next().value
			if (oldest !== undefined) state.reportedCalls.delete(oldest)
		}
		return false
	}

	/** Report one QuietBridge tool receipt through the lifecycle bridge. */
	const reportToolResult = (state, receipt, options = {}) => {
		const toolName = receipt.toolName
		if (!state.toolNames.has(toolName)) return
		const alreadyReported = markReported(state, receipt.callId)
		if (alreadyReported && options.force !== true) return
		const turnId = optionalString(receipt.turnId) ?? state.currentTurnId ?? state.lastTurnId ?? ''
		sendLifecycle(state, {
			event: 'tool-result',
			turnId,
			toolName,
			toolInput: parseToolArguments(receipt.arguments),
			toolResult: boundJson(receipt.toolResult),
		})
	}

	/** Handle one durable Session event for every Agent that owns the session. */
	const onSessionEvent = (session, event) => {
		try {
			if (disposed || !isSessionEvent(event)) return
			const sessionId = typeof session?.id === 'string' ? session.id : undefined
			if (sessionId === undefined) return
			const owners = bySessionId.get(sessionId)
			if (owners === undefined || owners.size === 0) return
			const seq = Number.isSafeInteger(event.seq) ? event.seq : undefined
			for (const state of [...owners]) {
				if (seq !== undefined) {
					if (state.seenSeqs.has(seq)) continue
					state.seenSeqs.add(seq)
					state.seenSeqQueue.push(seq)
					if (state.seenSeqQueue.length > 512) {
						const oldest = state.seenSeqQueue.shift()
						if (oldest !== undefined) state.seenSeqs.delete(oldest)
					}
				}
				handleSessionEvent(state, event)
			}
		} catch (cause) {
			warn(logger, `session/event 处理失败：${cause instanceof Error ? cause.message : String(cause)}`)
		}
	}

	/** Apply one Session event to one Agent state. */
	const handleSessionEvent = (state, event) => {
		const data = isRecord(event.data) ? event.data : {}
		switch (event.type) {
			case 'turn/start': {
				const turnId = turnIdOf(state.sessionId, data.turn)
				if (turnId === '') return
				state.currentTurnId = turnId
				state.lastTurnId = turnId
				sendLifecycle(state, { event: 'turn-start', turnId })
				return
			}
			case 'turn/end': {
				const reason = isRecord(data.reason) ? data.reason.kind : data.reason
				// A fork closes the source turn without a user-visible stop; do not
				// emit a turn-end that could tombstone the live fork's session.
				if (reason === 'forked') {
					state.currentTurnId = undefined
					return
				}
				const turnId = turnIdOf(state.sessionId, data.turn) || state.currentTurnId || state.lastTurnId || ''
				state.lastTurnId = turnId || state.lastTurnId
				state.currentTurnId = undefined
				sendLifecycle(state, { event: 'turn-end', turnId, reason: mapTurnReason(data.reason) })
				return
			}
			case 'tool/call': {
				const name = optionalString(data.name)
				if (name === undefined) return
				rememberCall(state, data.callId, {
					turnId: turnIdOf(state.sessionId, data.turn),
					name,
					arguments: parseToolArguments(data.arguments),
				})
				return
			}
			case 'tool/result': {
				const message = isRecord(data.message) ? data.message : {}
				const source = isRecord(message.source) ? message.source : {}
				const callId = source.callId
				const call = typeof callId === 'string' ? state.pendingCalls.get(callId) : undefined
				if (call === undefined) return
				// Keep the call entry until tools/result exposes the canonical value
				// (a task receipt lives in structuredContent); bounded eviction in
				// rememberCall removes callIds that never settle.
				call.resultReceived = true
				reportToolResult(state, {
					callId,
					turnId: turnIdOf(state.sessionId, data.turn) || call.turnId,
					toolName: call.name,
					arguments: call.arguments,
					toolResult: {
						isError: message.isError === true,
						content: normalizeMcpContent(message.content),
						...(isRecord(data.error) ? { error: boundJson(data.error) } : {}),
					},
				})
				return
			}
			default:
				return
		}
	}

	// -- tools/result supplement (no turn id available there) ----------------

	/** Observe final QuietBridge tool outcomes as a fallback receipt path. */
	const onToolResult = (exec, result) => {
		try {
			if (disposed || !isRecord(exec)) return
			const agent = exec.agent
			const state = agent === undefined ? undefined : states.get(agent)
			if (state === undefined) return
			const toolName = exec.name
			if (!state.toolNames.has(toolName)) return
			const callId = typeof exec.callId === 'string' ? exec.callId : undefined
			const call = callId === undefined ? undefined : state.pendingCalls.get(callId)
			// `tools/result` exposes parsed arguments; prefer them over the raw
			// string stored from the earlier session `tool/call` event.
			const coreArguments = isRecord(exec.arguments) || typeof exec.arguments === 'string'
				? parseToolArguments(exec.arguments)
				: parseToolArguments(call?.arguments)
			// The Python lifecycle store only needs the task receipt carried by
			// `structuredContent` for a begin/bind call; that value is only visible
			// here, so this observation is forwarded even after the session event
			// already reported a content-only receipt. For every other tool the
			// session receipt (which has the durable turn) is authoritative.
			const taskReceipt =
				toolName === 'quietbridge_task' &&
				isRecord(coreArguments) &&
				(coreArguments.action === 'begin' || coreArguments.action === 'bind')
			const toolResult =
				taskReceipt && isRecord(result) && result.value !== undefined
					? boundJson(result.value)
					: {
							isError: isRecord(result) && result.isError === true,
							content: normalizeMcpContent(isRecord(result) ? result.content : undefined),
						}
			reportToolResult(
				state,
				{
					callId: callId ?? '',
					turnId: call?.turnId ?? state.currentTurnId ?? state.lastTurnId ?? '',
					toolName,
					arguments: coreArguments,
					toolResult,
				},
				{ force: taskReceipt },
			)
			if (callId !== undefined) state.pendingCalls.delete(callId)
		} catch (cause) {
			warn(logger, `tools/result 处理失败：${cause instanceof Error ? cause.message : String(cause)}`)
		}
	}

	// -- Tool registration ---------------------------------------------------

	/** Build one DSH tool definition backed by the Agent connector. */
	const buildToolDefinition = (state, tool) => {
		const rawName = tool.name
		const description =
			typeof tool.description === 'string' && tool.description.trim() !== '' ? tool.description : rawName
		return {
			name: rawName,
			description,
			parameters: sanitizeToolInputSchema(tool.inputSchema),
			output: {
				// The canonical value carries content (and optional structuredContent).
				// `items: {}` is an annotation-only schema inside DSH's supported subset.
				schema: {
					type: 'object',
					properties: {
						content: { type: 'array', items: {} },
						structuredContent: {},
					},
					required: ['content'],
					additionalProperties: false,
				},
				render: (_args, value) => renderToolResult(value, rawName),
			},
			async execute(args, exec) {
				const client = state.client
				if (client === undefined || !client.usable) {
					const detail = client?.exitError?.message ?? '连接器未启动'
					throw new ConnectorError(
						'CONNECTOR_UNAVAILABLE',
						`${rawName}: QuietBridge 连接器不可用（${detail}）。本次调用未执行；不要重放输入，请先核实既有结果。`,
					)
				}
				let result
				try {
					result = await client.callTool(rawName, args, {
						signal: exec?.signal,
						timeoutMs: settings.requestTimeoutMs,
					})
				} catch (cause) {
					if (cause instanceof ConnectorError) {
						if (cause.code === 'CONNECTOR_CANCELLED') {
							throw new ConnectorError(cause.code, `${rawName}: ${cause.message}`)
						}
						throw new ConnectorError(
							cause.code,
							`${rawName}: 调用失败：${cause.message}。本次调用结果未知或未执行；不要重放输入，请先核实既有结果。`,
						)
					}
					throw cause
				}
				const record = isRecord(result) ? result : {}
				if (record.isError === true) {
					const blocks = normalizeMcpContent(record.content)
					const text = blocksToText(blocks).trim() || 'QuietBridge 工具返回错误'
					const failure = new Error(`${rawName}: ${text}`)
					failure.code = 'MCP_TOOL_ERROR'
					throw failure
				}
				return buildToolValue(record)
			},
		}
	}

	/** Fetch tools/list and atomically replace this Agent's registrations. */
	const syncTools = async (state) => {
		const client = state.client
		if (client === undefined) throw new ConnectorError('CONNECTOR_UNAVAILABLE', `${state.agentId}: 连接器未启动`)
		const tools = await client.listTools({ timeoutMs: settings.startupTimeoutMs })
		if (disposed || state.disposed) return
		const definitions = new Map()
		for (const tool of tools) {
			if (!isRecord(tool) || !isValidToolName(tool.name)) {
				warn(logger, `${state.agentId}: 跳过非法工具名 ${safeStringify(tool?.name)}`)
				continue
			}
			if (definitions.has(tool.name)) {
				throw new ConnectorError('CONNECTOR_PROTOCOL_ERROR', `${state.agentId}: tools/list 重复工具 ${tool.name}`)
			}
			definitions.set(tool.name, buildToolDefinition(state, tool))
		}
		// Registration is agent-scoped. A conflict (a foreign tool with the same
		// name registered on this Agent) rejects the whole generation so the model
		// never sees a partial QuietBridge tool set.
		for (const disposer of state.toolDisposers.values()) {
			try {
				disposer()
			} catch {
				/* ignore */
			}
		}
		state.toolDisposers.clear()
		state.toolNames.clear()
		const registered = new Map()
		try {
			for (const [toolName, definition] of definitions) {
				registered.set(toolName, state.agent.ctx.tools.register(definition))
			}
		} catch (cause) {
			for (const disposer of registered.values()) {
				try {
					disposer()
				} catch {
					/* ignore */
				}
			}
			throw cause
		}
		if (disposed || state.disposed) {
			for (const disposer of registered.values()) {
				try {
					disposer()
				} catch {
					/* ignore */
				}
			}
			return
		}
		state.toolDisposers = registered
		state.toolNames = new Set(registered.keys())
		state.pendingCalls.clear()
		state.reportedCalls.clear()
		debugLog(logger, `${state.agentId}: 已注册 ${state.toolNames.size} 个 QuietBridge 工具`)
	}

	// -- Agent lifecycle -----------------------------------------------------

	/** Register session listeners for one Agent (global fallback is installed once). */
	const subscribeAgentSession = (state) => {
		try {
			const disposer = state.agent.ctx.on?.('session/event', onSessionEvent)
			if (typeof disposer === 'function') state.agentSessionDisposer = disposer
		} catch (cause) {
			warn(logger, `${state.agentId}: 注册 session 事件监听失败：${cause instanceof Error ? cause.message : String(cause)}`)
		}
	}

	/** Start one Agent's connector and register its tools; never throws. */
	const startAgent = async (state) => {
		if (disposed) return
		if (connectorSlots.size >= settings.maxConnectors) {
			state.startFailed = true
			warn(
				logger,
				`${state.agentId}: 连接器数量已达上限 ${settings.maxConnectors}，该 Agent 不注册 QuietBridge 工具`,
			)
			return
		}
		const client = new ConnectorClient({
			python: settings.python,
			connector: settings.connector,
			env: connectorEnvironment(settings, state),
			cwd: path.dirname(settings.connector),
			requestTimeoutMs: settings.requestTimeoutMs,
			logger,
			label: state.agentId,
			onExit: () => {
				connectorSlots.delete(state)
			},
			onToolsChanged: async () => {
				if (disposed) return
				try {
					await syncTools(state)
				} catch (cause) {
					warn(
						logger,
						`${state.agentId}: 重新同步工具失败，保留上一代工具：${cause instanceof Error ? cause.message : String(cause)}`,
					)
				}
			},
		})
		state.client = client
		try {
			client.start()
			connectorSlots.add(state)
			await client.initialize({ timeoutMs: settings.startupTimeoutMs })
			if (disposed) {
				await client.close()
				return
			}
			await syncTools(state)
			state.ready = true
			sendLifecycle(state, { event: 'agent-created' })
			debugLog(logger, `${state.agentId}: 连接器就绪（protocol=${client.protocolVersion ?? 'unknown'}）`)
		} catch (cause) {
			state.startFailed = true
			connectorSlots.delete(state)
			warn(
				logger,
				`${state.agentId}: QuietBridge 连接器启动失败，该 Agent 不注册工具：${
					cause instanceof Error ? cause.message : String(cause)
				}`,
			)
			try {
				await client.close()
			} catch {
				/* ignore */
			}
			state.client = undefined
		}
	}

	/** Tear down one Agent: unregister tools, close the connector, write disposed. */
	const teardownAgent = async (state, options = {}) => {
		if (state.disposed) return
		state.disposed = true
		state.ready = false
		connectorSlots.delete(state)
		states.delete(state.agent)
		if (byAgentId.get(state.agentId) === state) byAgentId.delete(state.agentId)
		unindexState(state)
		for (const disposer of state.toolDisposers.values()) {
			try {
				disposer()
			} catch {
				/* ignore */
			}
		}
		state.toolDisposers.clear()
		state.toolNames.clear()
		if (typeof state.agentSessionDisposer === 'function') {
			try {
				state.agentSessionDisposer()
			} catch {
				/* ignore */
			}
			state.agentSessionDisposer = undefined
		}
		if (options.sendDisposed !== false) sendLifecycle(state, { event: 'agent-disposed' })
		const client = state.client
		state.client = undefined
		if (client !== undefined) {
			try {
				await client.close()
			} catch {
				/* ignore */
			}
		}
		// The Agent-scope effect is the second owner of this teardown; drop it
		// from the Agent fiber as well so a plugin unload with a still-live Agent
		// leaves no dangling registration behind. Never await that disposer from
		// inside its own cleanup (Cordis would return the in-flight disposal task
		// and deadlock).
		if (options.fromAgentEffect !== true && typeof state.agentEffectDisposer === 'function') {
			const disposeEffect = state.agentEffectDisposer
			state.agentEffectDisposer = undefined
			try {
				await disposeEffect()
			} catch {
				/* ignore */
			}
		}
		state.agentEffectDisposer = undefined
	}

	/** Register one Agent and start its connector, all failures contained. */
	const registerAgent = (agent) => {
		if (disposed) return
		if (states.has(agent)) return
		const state = {
			agent,
			agentId: agent.id,
			sessionId: agent.session.id,
			client: undefined,
			ready: false,
			startFailed: false,
			disposed: false,
			toolDisposers: new Map(),
			toolNames: new Set(),
			currentTurnId: undefined,
			lastTurnId: undefined,
			pendingCalls: new Map(),
			reportedCalls: new Set(),
			seenSeqs: new Set(),
			seenSeqQueue: [],
			lifecycle: Promise.resolve(),
			lifecycleBacklog: 0,
			lifecycleDropped: false,
			agentSessionDisposer: undefined,
			agentEffectDisposer: undefined,
		}
		states.set(agent, state)
		byAgentId.set(state.agentId, state)
		indexState(state)
		subscribeAgentSession(state)
		// Two owners for every registration: the Agent scope removes it on Agent
		// disposal, and this plugin effect removes it on plugin unload.
		try {
			const disposer = agent.ctx.effect?.(() => () => {
				// `fromAgentEffect` prevents awaiting this same effect's disposer
				// from inside its own cleanup (Cordis re-entrant dispose() returns
				// the in-flight task, so awaiting it here would deadlock Agent
				// disposal).
				teardownAgent(state, { fromAgentEffect: true }).catch(() => {})
			})
			if (typeof disposer === 'function') state.agentEffectDisposer = disposer
		} catch (cause) {
			warn(logger, `${state.agentId}: agent 作用域 effect 注册失败：${cause instanceof Error ? cause.message : String(cause)}`)
		}
		state.startPromise = startAgent(state).catch((cause) => {
			warn(logger, `${state.agentId}: 启动失败：${cause instanceof Error ? cause.message : String(cause)}`)
		})
		return state
	}

	/**
	 * Handle `agent/created` (payload `{agent, source, signal?}`).
	 *
	 * This is a serial creation listener: awaiting startup makes the tools visible
	 * before the Agent's first step, while `startAgent` bounds initialize/tools-list
	 * with `startupTimeoutMs` and contains every failure. The whole handler is
	 * wrapped because a throwing serial listener would fail Agent creation.
	 */
	const onAgentCreated = async (payload) => {
		try {
			const agent = agentFromPayload(payload)
			if (agent === undefined) {
				warn(logger, '忽略缺少 agent/session/ctx 的 agent/created 事件')
				return
			}
			const state = registerAgent(agent)
			if (state?.startPromise !== undefined) await state.startPromise
		} catch (cause) {
			// A throwing serial listener would fail Agent creation; contain it.
			warn(logger, `agent/created 处理失败：${cause instanceof Error ? cause.message : String(cause)}`)
		}
	}

	/** Handle `agent/disposed` (payload `{agent}`). */
	const onAgentDisposed = async (payload) => {
		try {
			const agent = agentFromPayload(payload)
			if (agent === undefined) return
			const state = states.get(agent)
			if (state === undefined) return
			// The disposal event is observe-only for the host; awaiting the bounded
			// connector close just makes the write-side effects deterministic.
			await teardownAgent(state).catch((cause) =>
				warn(logger, `${state.agentId}: 清理失败：${cause instanceof Error ? cause.message : String(cause)}`),
			)
		} catch (cause) {
			warn(logger, `agent/disposed 处理失败：${cause instanceof Error ? cause.message : String(cause)}`)
		}
	}

	// -- Wiring --------------------------------------------------------------

	ctx.on('agent/created', onAgentCreated)
	ctx.on('agent/disposed', onAgentDisposed)
	ctx.on('session/event', onSessionEvent)
	ctx.on('session/disposed', (session) => {
		try {
			const sessionId = typeof session?.id === 'string' ? session.id : undefined
			if (sessionId === undefined) return
			const owners = [...(bySessionId.get(sessionId) ?? [])]
			for (const state of owners) teardownAgent(state).catch(() => {})
		} catch (cause) {
			warn(logger, `session/disposed 处理失败：${cause instanceof Error ? cause.message : String(cause)}`)
		}
	})
	ctx.on('tools/result', onToolResult)

	// A profile reload may activate this plugin while Agents already exist; wire
	// them exactly as if they had just been announced.
	try {
		const agents = ctx.get?.('agents')
		const existing = typeof agents?.list === 'function' ? agents.list() : []
		for (const agent of Array.isArray(existing) ? existing : []) {
			try {
				const normalized = isRecord(agent) && isRecord(agent.session) && isRecord(agent.ctx) ? agent : undefined
				if (normalized !== undefined) registerAgent(normalized)
			} catch (cause) {
				warn(logger, `接入已有 Agent 失败：${cause instanceof Error ? cause.message : String(cause)}`)
			}
		}
	} catch (cause) {
		warn(logger, `枚举已有 Agent 失败：${cause instanceof Error ? cause.message : String(cause)}`)
	}

	ctx.effect(() => () => {
		if (disposed) return undefined
		disposed = true
		const pending = [...states.values()]
		states.clear()
		byAgentId.clear()
		bySessionId.clear()
		const tasks = pending.map((state) =>
			teardownAgent(state).catch(() => {
				/* teardown never rejects into the host */
			}),
		)
		return Promise.allSettled(tasks).then(() => undefined)
	}, 'quietbridge-dsh.cleanup')
}

/** Internals exposed for the repository's node --test suite. */
export const __internals = Object.freeze({
	ConnectorClient,
	ConnectorError,
	LifecycleBridge,
	buildToolValue,
	connectorEnvironment,
	isPythonCommand,
	isValidToolName,
	mapTurnReason,
	normalizeMcpContent,
	parseToolArguments,
	renderToolResult,
	resolveConfig,
	sanitizeToolInputSchema,
	turnIdOf,
})
