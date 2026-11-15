#!/usr/bin/env node
/**
 * Fake `lifecycle.py` used only by the bundle's `node --test` suite.
 *
 * It records each stdin JSON document to
 * `<configDir>/fake-lifecycle.jsonl` and exits 0. Modes come from
 * `<configDir>/fake-lifecycle-mode`:
 *   normal - record and exit
 *   hang   - record, then stay alive so the plugin's timeout path is exercised
 *   fail   - record, write stderr, exit 2
 */
import { appendFileSync, readFileSync } from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const configIndex = process.argv.indexOf('--config-dir')
const configDir = configIndex >= 0 ? process.argv[configIndex + 1] : process.env.QUIETBRIDGE_CONFIG_DIR || '.'

let mode = 'normal'
try {
	mode = readFileSync(path.join(configDir, 'fake-lifecycle-mode'), 'utf8').trim() || 'normal'
} catch {
	/* default normal */
}

const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const raw = Buffer.concat(chunks).toString('utf8').trim()
let document
try {
	document = raw === '' ? null : JSON.parse(raw)
} catch {
	document = { unparsable: raw.slice(0, 200) }
}

if (mode !== 'fail') {
	try {
		appendFileSync(path.join(configDir, 'fake-lifecycle.jsonl'), `${JSON.stringify(document)}\n`)
	} catch {
		/* ignore */
	}
}

if (mode === 'hang') {
	setTimeout(() => process.exit(0), 60000).unref()
	// Keep the process alive (a ref'd interval) so only a kill ends it.
	setInterval(() => {}, 1000)
} else if (mode === 'fail') {
	process.stderr.write('fake lifecycle failure\n')
	process.exit(2)
} else {
	process.exit(0)
}
