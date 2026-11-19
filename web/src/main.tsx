import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { z } from 'zod'
import './style.css'

const profileSchema = z.enum(['balanced', 'robust'])
const modeSchema = z.enum(['adaptive', 'balanced', 'robust'])
const transportSchema = z.enum(['simulation', 'acoustic'])
const messageSchema = z.object({ name: z.string(), size: z.number(), data: z.string(), text: z.string().nullable() })
const stateSchema = z.object({
  status: z.enum(['idle', 'sending', 'listening', 'failed', 'complete']),
  error: z.string(), profile: profileSchema.nullable(), completed: z.number(), total: z.number(),
  quality: z.number().nullable(), goodput: z.number().nullable(), wave: z.array(z.number()),
  spectra: z.array(z.array(z.number())),
  events: z.array(z.object({ id: z.number(), kind: z.string(), message: z.string(), at: z.number() })),
  inbox: z.array(messageSchema),
  counts: z.object({ tx: z.number(), rx: z.number(), ack: z.number(), retry: z.number(), timeout: z.number(), crc: z.number() }),
})
const devicesSchema = z.object({
  devices: z.array(z.object({ id: z.number(), name: z.string(), input: z.boolean(), output: z.boolean() })),
  error: z.string(),
})
type LinkState = z.infer<typeof stateSchema>
type ReceivedMessage = z.infer<typeof messageSchema>

async function get<T>(path: string, schema: z.ZodType<T>, signal: AbortSignal) {
  const response = await fetch(path, { signal })
  if (!response.ok) throw new Error('Local service is unavailable')
  const body: unknown = await response.json()
  return schema.parse(body)
}

async function post(path: string, body: object) {
  const response = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  if (!response.ok) {
    const payload: unknown = await response.json()
    const parsed = z.object({ detail: z.string() }).safeParse(payload)
    throw new Error(parsed.success ? parsed.data.detail : 'The request could not be completed')
  }
}

function Signal({ wave, spectra }: Pick<LinkState, 'wave' | 'spectra'>) {
  const waveform = useRef<HTMLCanvasElement>(null)
  const spectrogram = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const draw = () => {
      for (const canvas of [waveform.current, spectrogram.current]) {
        if (!canvas) continue
        const scale = window.devicePixelRatio || 1
        const w = canvas.clientWidth, h = canvas.clientHeight
        if (!w || !h) continue
        canvas.width = w * scale
        canvas.height = h * scale
        const ctx = canvas.getContext('2d')
        if (!ctx) continue
        ctx.scale(scale, scale)
        ctx.fillStyle = '#0d1521'
        ctx.fillRect(0, 0, w, h)
        if (canvas === waveform.current && wave.length > 1) {
          ctx.beginPath()
          wave.forEach((value, index) => {
            const x = index * w / (wave.length - 1)
            const y = h / 2 - Math.max(-1, Math.min(1, value * 2)) * h * .46
            if (index === 0) ctx.moveTo(x, y)
            else ctx.lineTo(x, y)
          })
          ctx.strokeStyle = '#62eed1'
          ctx.lineWidth = 1.6
          ctx.stroke()
        }
        if (canvas === spectrogram.current) {
          const col = w / 72
          spectra.forEach((bins, index) => bins.forEach((value, bin) => {
            const row = h / bins.length
            ctx.fillStyle = `hsl(${230 - value * 72} 70% ${8 + value * 58}%)`
            ctx.fillRect(w - (spectra.length - index) * col, h - (bin + 1) * row, col + .5, row + .5)
          }))
        }
        ctx.strokeStyle = '#95abc71d'
        ctx.beginPath()
        for (let i = 1; i < 10; i++) {
          ctx.moveTo(w * i / 10, 0)
          ctx.lineTo(w * i / 10, h)
        }
        for (let i = 1; i < 4; i++) {
          ctx.moveTo(0, h * i / 4)
          ctx.lineTo(w, h * i / 4)
        }
        ctx.stroke()
      }
    }
    draw()
    window.addEventListener('resize', draw)
    return () => window.removeEventListener('resize', draw)
  }, [wave, spectra])

  return <section className="panel monitors">
    <div className="panel-head"><h2>Signal monitor</h2><span className="technical">48 kHz · mono</span></div>
    <div className="monitor-label"><span>Waveform</span><span>20 ms window</span></div>
    <canvas ref={waveform} aria-label="Audio waveform over 20 milliseconds" role="img" />
    <div className="monitor-label"><span>Spectrogram</span><span>0–4.8 kHz</span></div>
    <canvas ref={spectrogram} aria-label="Recent audio frequency energy from 0 to 4.8 kilohertz" role="img" />
    <div className="axis"><span>Earlier</span><span>Now</span></div>
  </section>
}

function download(message: ReceivedMessage) {
  const bytes = Uint8Array.from(atob(message.data), char => char.charCodeAt(0))
  const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = message.name
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function App() {
  const [state, setState] = useState<LinkState>()
  const [deviceList, setDeviceList] = useState<z.infer<typeof devicesSchema>>({ devices: [], error: '' })
  const [transport, setTransport] = useState<z.infer<typeof transportSchema>>('simulation')
  const [mode, setMode] = useState<z.infer<typeof modeSchema>>('adaptive')
  const [role, setRole] = useState<'send' | 'receive'>('send')
  const [text, setText] = useState('Hello from SonicBridge!')
  const [file, setFile] = useState<File>()
  const [noise, setNoise] = useState(.03)
  const [drop, setDrop] = useState(false)
  const [input, setInput] = useState('')
  const [output, setOutput] = useState('')
  const [connectionError, setConnectionError] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const busy = state?.status === 'sending' || state?.status === 'listening'
  const bytes = file?.size ?? new TextEncoder().encode(text).length

  useEffect(() => {
    const controller = new AbortController()
    let timer = 0
    const poll = async () => {
      try {
        setState(await get('/api/state', stateSchema, controller.signal))
        setConnectionError('')
      } catch {
        if (!controller.signal.aborted) setConnectionError('Local service disconnected')
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(poll, 300)
      }
    }
    void poll()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [])

  useEffect(() => {
    if (transport !== 'acoustic') return
    const controller = new AbortController()
    get('/api/devices', devicesSchema, controller.signal).then(setDeviceList).catch(() => {
      if (!controller.signal.aborted) setDeviceList({ devices: [], error: 'Audio devices could not be queried' })
    })
    return () => controller.abort()
  }, [transport])

  async function action(path: string, body: object) {
    setError('')
    setPending(true)
    try {
      await post(path, body)
      setState(await get('/api/state', stateSchema, new AbortController().signal))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Operation failed')
    } finally {
      setPending(false)
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    const devices = { input_device: input ? Number(input) : null, output_device: output ? Number(output) : null }
    if (role === 'receive') {
      await action('/api/listen', devices)
      return
    }
    try {
      const payload = file ? new Uint8Array(await file.arrayBuffer()) : new TextEncoder().encode(text)
      const data = btoa(Array.from(payload, byte => String.fromCharCode(byte)).join(''))
      await action('/api/send', { ...devices, data, name: file?.name ?? '', transport, mode, noise, drop_ack: drop ? 2 : 0 })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'File could not be read')
    }
  }

  const status = connectionError ? 'Disconnected' : state?.status ?? 'Connecting'
  const metrics = [
    state?.status === 'listening'
      ? ['Packets received', state.counts.rx, '']
      : ['Packets acknowledged', state?.completed ?? 0, state?.total ? ` / ${state.total}` : ''],
    ['Retries', state?.counts.retry ?? 0, ''],
    ['CRC failures', state?.counts.crc ?? 0, ''],
    ['Payload throughput', state?.goodput?.toFixed(1) ?? '—', state?.goodput == null ? '' : ' bit/s'],
  ]
  return <>
    <header className="top"><div className="top-inner">
      <div className="brand"><span className="mark" aria-hidden="true"><i /><i /><i /><i /><i /></span>SonicBridge</div>
      <span role="status" className={'status ' + (status === 'failed' || connectionError ? 'bad' : '')}><i />{status}</span>
    </div></header>
    <main>
      <div className="intro">
        <div><h1>Acoustic data link</h1><p>Send text and small files through sound.</p></div>
        <label className="transport">Channel
          <select value={transport} disabled={busy || pending} onChange={e => {
            const next = transportSchema.parse(e.target.value)
            setTransport(next)
            if (next === 'simulation') setRole('send')
          }}><option value="simulation">Simulation</option><option value="acoustic">Speaker + microphone</option></select>
        </label>
      </div>
      <section className="metrics" aria-label="Transfer metrics">
        {metrics.map(([label, value, unit]) => <div className="metric" key={label}><span>{label}</span><strong>{value}<small>{unit}</small></strong></div>)}
      </section>
      <div className="layout">
        <section className="panel controls">
          <div className="panel-head"><h2>Transfer</h2>{state?.profile && <span className="profile">{state.profile}</span>}</div>
          {transport === 'acoustic' && <div className="segmented" aria-label="Transfer direction">
            <button disabled={busy || pending} type="button" aria-pressed={role === 'send'} onClick={() => setRole('send')}>Send</button>
            <button disabled={busy || pending} type="button" aria-pressed={role === 'receive'} onClick={() => setRole('receive')}>Receive</button>
          </div>}
          <form onSubmit={submit}>
            <fieldset disabled={busy || pending}>
              {role === 'send' && <>
                <label>Transmission profile<select value={mode} onChange={e => setMode(modeSchema.parse(e.target.value))}>
                  <option value="adaptive">Adaptive</option><option value="balanced">Fixed Balanced</option><option value="robust">Fixed Robust</option>
                </select></label>
                <label>Message<textarea rows={4} value={text} disabled={!!file} onChange={e => setText(e.target.value)} /></label>
                <div className="file-row">
                  <label className="file-control">{file?.name ?? 'Attach small file'}<input type="file" onChange={e => {
                    setFile(e.target.files?.[0]); e.target.value = ''
                  }} /></label>
                  {file && <button className="quiet" type="button" onClick={() => setFile(undefined)}>Remove</button>}
                  <span className={bytes > 2048 ? 'bad-text' : ''}>{bytes} / 2048 B</span>
                </div>
              </>}
              {transport === 'simulation' ? <div className="options">
                <label>Noise amplitude <output>{noise.toFixed(2)}</output><input type="range" min="0" max=".75" step=".01" value={noise} onChange={e => setNoise(Number(e.target.value))} /></label>
                <label className="check"><input type="checkbox" checked={drop} onChange={e => setDrop(e.target.checked)} /> Drop the first two ACKs</label>
              </div> : <>
                <label>Microphone<select value={input} onChange={e => setInput(e.target.value)}>
                  <option value="">System default</option>{deviceList.devices.filter(d => d.input).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select></label>
                <label>Speaker<select value={output} onChange={e => setOutput(e.target.value)}>
                  <option value="">System default</option>{deviceList.devices.filter(d => d.output).map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
                </select></label>
                {deviceList.error && <p className="bad-text">{deviceList.error}</p>}
                <p className="hint">{role === 'receive' ? 'Start listening before the other device sends.' : 'Start the receiver on the other device before sending.'}</p>
              </>}
            </fieldset>
            {busy ? <button type="button" className="secondary" disabled={pending} onClick={() => void action('/api/stop', {})}>Stop operation</button> :
              <button className="primary" disabled={pending || !state || !!connectionError || (role === 'send' && (bytes < 1 || bytes > 2048))}>
                {role === 'receive' ? 'Start listening' : transport === 'simulation' ? 'Run simulation' : 'Transmit through sound'} <span aria-hidden="true">→</span>
              </button>}
          </form>
          {(error || state?.error || connectionError) && <p role="alert" className="bad-text">{error || state?.error || connectionError}</p>}
          {state?.quality != null && <p className="hint">Last ACK reported {state.quality} dB tone separation.</p>}
        </section>
        <Signal wave={state?.wave ?? []} spectra={state?.spectra ?? []} />
        <section className="panel inbox">
          <div className="panel-head"><h2>Received</h2><span className="technical">{state?.inbox.length ?? 0} messages</span></div>
          {state?.inbox.length ? [...state.inbox].reverse().map((message, i) =>
            <div className="received" key={i}><div><strong>{message.name || 'Text message'}</strong><span>{message.size} B</span></div>
              {message.text !== null ? <p>{message.text}</p> : <button className="quiet" onClick={() => download(message)}>Download file</button>}
            </div>) : <p className="empty">Decoded messages and files appear here.</p>}
        </section>
        <section className="panel activity">
          <div className="panel-head"><h2>Packet log</h2><span className="technical">Latest first</span></div>
          {state?.events.length ? <ul aria-label="Packet events">{[...state.events].reverse().map(event =>
            <li key={event.id}><time>{new Date(event.at * 1000).toLocaleTimeString()}</time><span className={'event ' + event.kind}>{event.kind}</span><span>{event.message}</span></li>)}</ul> :
            <p className="empty">Packet activity appears when the link runs.</p>}
        </section>
      </div>
    </main>
  </>
}

const root = document.getElementById('root')
if (!root) throw new Error('Missing dashboard root')
createRoot(root).render(<App />)
