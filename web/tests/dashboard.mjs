import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout } from 'node:timers/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { chromium } from 'playwright'

const project = fileURLToPath(new URL('../../', import.meta.url))
const directory = await mkdtemp(join(tmpdir(), 'sonicbridge-check-'))
const port = process.env.SONICBRIDGE_TEST_PORT ?? '8731'
const address = `http://127.0.0.1:${port}`
let server
let browser

async function initialize() {
  server = spawn(process.env.SONICBRIDGE_PYTHON ?? 'python',
    ['-m', 'uvicorn', 'sonicbridge.app:app', '--port', port],
    { cwd: project, env: { ...process.env, SONICBRIDGE_STATE_DIR: directory }, stdio: 'inherit' })
  for (let attempt = 0; attempt < 60 && server.exitCode === null; attempt++) {
    try {
      if ((await fetch(address + '/api/state')).ok) return
    } catch {
      // Wait for the Python process to bind the socket.
    }
    await setTimeout(100)
  }
  throw new Error('SonicBridge did not become ready')
}

async function stopServer() {
  if (server && server.exitCode === null && server.signalCode === null) {
    const exited = once(server, 'exit')
    server.kill('SIGTERM')
    await exited
  }
}

try {
  await initialize()
  browser = await chromium.launch({ executablePath: process.env.SONICBRIDGE_CHROMIUM })
  const page = await browser.newPage()
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(address)
  await page.getByRole('button', { name: 'Test link' }).click()
  await page.getByText('Peer replied', { exact: true }).waitFor()
  await page.getByRole('button', { name: /Run loss.*recovery demo/ }).click()
  await page.getByText('The demo transfer is paused', { exact: true }).waitFor()
  await page.getByText('Robust enabled · two consecutive delivery failures').waitFor()

  await stopServer()
  await initialize()
  await page.reload()
  await page.getByRole('button', { name: 'Resume transfer' }).click()
  await page.getByText('Peer already has 1/4 packets').waitFor()
  await page.getByText('All packets acknowledged').waitFor()
  await page.getByText(/This message traveled through simulated sound/).waitFor()

  await page.getByLabel('Transmission profile').selectOption('fast')
  await page.getByLabel('Shared passphrase').fill('an example shared passphrase')
  const buffer = Buffer.from(Array.from({length: 140}, (_, i) => i))
  await page.locator('input[type=file]').setInputFiles({name: 'demo.bin', mimeType: 'application/octet-stream', buffer})
  await page.getByRole('button', { name: 'Run simulation' }).click()
  const file = page.locator('.received', {hasText: 'demo.bin'})
  await file.getByText(/Authenticated/).waitFor()
  await file.getByRole('button', {name: 'Download file'}).waitFor()
  const download = page.waitForEvent('download')
  await file.getByRole('button', {name: 'Download file'}).click()
  assert.deepEqual(await readFile(await (await download).path()), buffer)

  await page.getByRole('button', {name: 'Compare profiles'}).click()
  const result = page.locator('.results .result')
  await result.nth(3).waitFor()
  assert.equal(await result.count(), 4)
  const exportPromise = page.waitForEvent('download')
  await page.getByRole('button', {name: 'Export CSV'}).click()
  const csv = await readFile(await (await exportPromise).path(), 'utf8')
  assert.match(csv, /interference,fast,/)
  assert.equal(csv.split('\n').length, 5)
  await page.setViewportSize({width: 375, height: 812})
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))

  await page.getByRole('button', {name: 'Projector view'}).click()
  await page.getByRole('button', {name: 'Simulate a postcard'}).click()
  await page.getByRole('heading', {name: 'Pacific Coast'}).waitFor()
  await page.getByText('Card P01 · 3 payload bytes', {exact: true}).waitFor()
  await page.getByText('Simulated channel', {exact: true}).waitFor()
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
  await page.getByText('1.2 kHz', {exact: true}).waitFor()
  await page.getByText('2.2 kHz', {exact: true}).waitFor()
  await page.getByRole('button', {name: 'Freeze last signal'}).click()
  await page.getByText('Frozen · Simulated channel', {exact: true}).waitFor()
  const spectrum = page.getByRole('img', {name: /Audio frequency energy/})
  const frozenPixels = await spectrum.evaluate(canvas => canvas.toDataURL())
  await page.getByRole('button', {name: 'Simulate a postcard'}).click()
  await page.getByRole('button', {name: 'Stop operation'}).waitFor()
  await page.getByRole('heading', {name: 'Pacific Coast'}).waitFor()
  assert.equal(await spectrum.evaluate(canvas => canvas.toDataURL()), frozenPixels)
  await page.getByRole('button', {name: 'Return to live signal'}).click()
  await page.getByText('Updates during transfer', {exact: true}).waitFor()

  const phoneDownload = page.waitForEvent('download')
  await page.getByRole('link', {name: 'Download phone sender'}).click()
  const phonePath = join(directory, 'phone.html')
  await (await phoneDownload).saveAs(phonePath)
  const phone = await browser.newPage({viewport: {width: 390, height: 844}})
  phone.on('pageerror', error => errors.push(error.message))
  await phone.goto(pathToFileURL(phonePath).href)
  await phone.getByLabel('Postcard').selectOption('P02')
  await phone.getByRole('button', {name: 'Play signal'}).click()
  await phone.getByText(/Signal played/).waitFor()
  await phone.getByLabel('Postcard').selectOption('P03')
  await phone.getByLabel('Postcard').selectOption('P02')
  await phone.getByText('11 plays available for this card.').waitFor()

  await page.getByRole('button', {name: 'Dashboard', exact: true}).click()
  await page.getByRole('button', {name: 'Decode sample WAV'}).click()
  await page.getByText('Hello from a saved WAV.', {exact: true}).waitFor()
  await page.getByText(/Recorded WAV.*1 verified packet.*0 CRC failures/).waitFor()
  assert.equal(await page.locator('.inbox').getByText('Hello from a saved WAV.', {exact: true}).count(), 0)
  const wavDownload = page.waitForEvent('download')
  await page.getByRole('link', {name: 'Download sample WAV'}).click()
  const wav = await readFile(await (await wavDownload).path())
  assert.equal(wav.toString('ascii', 0, 4), 'RIFF')
  assert.equal(wav.toString('ascii', 8, 12), 'WAVE')

  assert.deepEqual(errors, [])
  console.log('Dashboard passed probe, loss recovery, restart resume, encrypted Fast file, comparison, CSV, mobile, postcards, signal freeze, and WAV decode.')
} finally {
  await browser?.close()
  await stopServer()
  await rm(directory, { recursive: true, force: true })
}
