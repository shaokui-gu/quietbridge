/**
 * Manifest, patch-template, locale, icon, and import-boundary checks.
 *
 * These never install the bundle and never touch the real DSH profile.
 */
import assert from 'node:assert/strict'
import { existsSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const bundleDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

const readJson = (name) => JSON.parse(readFileSync(path.join(bundleDir, name), 'utf8'))
const readText = (name) => readFileSync(path.join(bundleDir, name), 'utf8')

test('package.json declares an installable Host bundle', () => {
	const manifest = readJson('package.json')
	assert.equal(manifest.name, '@quietbridge/dsh')
	assert.match(manifest.version, /^\d+\.\d+\.\d+$/)
	assert.equal(manifest.private, true)
	assert.equal(manifest.type, 'module')
	assert.equal(manifest.main, 'index.js')
	assert.equal(manifest.exports['.'], './index.js')
	assert.equal(manifest.exports['./locale/*.json'], './locale/*.json')
	assert.equal(manifest.exports['./icon'], './icon.svg')
	assert.equal(manifest.dsh.bundle.patch, './cordis.patch.yml')
	assert.ok(existsSync(path.join(bundleDir, manifest.main)))
	assert.ok(existsSync(path.join(bundleDir, manifest.dsh.bundle.patch)))
})

test('files lists runtime resources and excludes tests', () => {
	const manifest = readJson('package.json')
	assert.ok(Array.isArray(manifest.files))
	for (const entry of ['index.js', 'cordis.patch.yml', 'locale/*.json', 'icon.svg', 'README.md']) {
		assert.ok(manifest.files.includes(entry), `files must include ${entry}`)
	}
	for (const entry of manifest.files) {
		assert.ok(!/^test\//.test(entry) && !/\.test\.mjs$/.test(entry), `files must not ship test entry ${entry}`)
	}
	assert.ok(existsSync(path.join(bundleDir, 'index.js')))
	assert.ok(existsSync(path.join(bundleDir, 'README.md')))
	assert.ok(existsSync(path.join(bundleDir, 'locale', 'en.json')))
	assert.ok(existsSync(path.join(bundleDir, 'locale', 'zh.json')))
})

test('cordis.patch.yml uses the block insert shape the installer renders', () => {
	const text = readText('cordis.patch.yml')
	const lines = text.split('\n')
	assert.ok(lines.includes('- insert:'), 'top-level insert list missing')
	assert.ok(!/^\s*-\s*insert:\s*\[/m.test(text), 'insert must be block style, not flow style')
	assert.ok(lines.includes('    - id: quietbridge-dsh'), 'row id line must be its own line')
	assert.ok(lines.includes("      name: '@quietbridge/dsh'"), 'row name line missing')
	assert.ok(lines.includes('      config:'), 'row config line missing')
	for (const envName of [
		'QUIETBRIDGE_DSH_PYTHON',
		'QUIETBRIDGE_DSH_CONNECTOR',
		'QUIETBRIDGE_DSH_HELPER',
		'QUIETBRIDGE_DSH_CONFIG_DIR',
		'QUIETBRIDGE_DSH_SERVER_NAME',
	]) {
		const line = lines.find((candidate) => candidate.includes(envName))
		assert.ok(line, `patch missing ${envName}`)
		assert.ok(line.includes('!!js'), `${envName} must be read through !!js`)
	}
	assert.ok(!lines.includes('- id: quietbridge-dsh'), 'row id must be nested under insert')
})

test('locale resources carry real localized display metadata', () => {
	const en = readJson('locale/en.json')
	const zh = readJson('locale/zh.json')
	for (const [locale, data] of [
		['en', en],
		['zh', zh],
	]) {
		assert.equal(typeof data.meta?.title, 'string', `${locale} meta.title`)
		assert.equal(typeof data.meta?.description, 'string', `${locale} meta.description`)
		assert.ok(data.meta.title.trim().length > 0)
		assert.ok(data.meta.description.trim().length > 0)
	}
	assert.notEqual(en.meta.title, zh.meta.title)
	assert.match(en.meta.title, /^[A-Z]/, 'English title should be sentence case')
})

test('icon.svg is a valid, small, original SVG resource', () => {
	const file = path.join(bundleDir, 'icon.svg')
	const bytes = statSync(file).size
	assert.ok(bytes > 0)
	assert.ok(bytes < 256 * 1024)
	const text = readText('icon.svg')
	assert.ok(text.trimStart().startsWith('<svg'))
	assert.ok(text.includes('xmlns="http://www.w3.org/2000/svg"'))
	assert.ok(text.trimEnd().endsWith('</svg>'))
})

test('index.js has no DSH/MCP imports and passes node --check', async () => {
	const source = readText('index.js')
	assert.ok(!/@deepseek-ai\//.test(source), 'index.js must not import or reference @deepseek-ai/* packages')
	assert.ok(!/@modelcontextprotocol\//.test(source), 'index.js must not import @modelcontextprotocol/* packages')
	assert.ok(!/from\s+['"](?!node:)[^'"]+['"]/.test(source), 'index.js may only import node built-ins')
	const { spawnSync } = await import('node:child_process')
	const checked = spawnSync(process.execPath, ['--check', path.join(bundleDir, 'index.js')], { encoding: 'utf8' })
	assert.equal(checked.status, 0, checked.stderr)
})
