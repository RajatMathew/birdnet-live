import { useCallback, useEffect, useRef, useState } from 'react'
import useBirdnet from './useBirdnet.js'

const fmtTime = (ms) =>
  new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })

const conf = (c) => `${Math.round(c * 100)}%`

export default function App() {
  const [detections, setDetections] = useState([])
  const [meta, setMeta] = useState({}) // scientific -> {image, description, credit}
  const [latest, setLatest] = useState(null)
  const [scores, setScores] = useState(null) // live top-k from the engine
  const seenRef = useRef(new Set())

  // Load saved history once, then prepend anything new pushed over the socket.
  useEffect(() => {
    fetch('/api/detections?limit=300')
      .then((r) => r.json())
      .then((d) => {
        const rows = d.detections || []
        setDetections((prev) => {
          const seen = new Set(rows.map((x) => x.db_id || x.id))
          return [...rows, ...prev.filter((x) => !seen.has(x.db_id || x.id))]
        })
        for (const x of rows) seenRef.current.add(x.db_id || x.id)
        // prime meta for anything already enriched
        for (const x of rows) {
          if (x.image || x.description) {
            setMeta((m) => ({
              ...m,
              [x.scientific]: { image: x.image, description: x.description },
            }))
          }
        }
      })
      .catch(() => {})
  }, [])

  const onDetection = useCallback((d) => {
    const key = d.db_id || d.id
    setLatest(d)
    setDetections((prev) =>
      prev.some((x) => (x.db_id || x.id) === key)
        ? prev
        : [{ ...d, db_id: key }, ...prev].slice(0, 500),
    )
    seenRef.current.add(key)
  }, [])

  const onMeta = useCallback((m) => {
    setMeta((prev) => ({
      ...prev,
      [m.scientific]: { image: m.image, description: m.description, credit: m.credit },
    }))
  }, [])

  const onScores = useCallback((s) => setScores(s), [])

  const { state, detail, level, model, devices, deviceId, source, selectDevice, setConfidence, setBirdBand, start, stop, getAnalyser } =
    useBirdnet({
      onDetection,
      onMeta,
      onScores,
    })

  const live = state === 'live'
  const busy = state === 'connecting'
  const ready = model.state === 'ready'

  const clearAll = async () => {
    if (!confirm('Clear all saved detections from this session?')) return
    await fetch('/api/detections/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    })
    setDetections([])
    setLatest(null)
    seenRef.current = new Set()
  }

  const latestMeta = latest ? meta[latest.scientific] : null
  const uniqueSpecies = new Set(detections.map((d) => d.scientific)).size

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="mark" aria-hidden>
            ◈
          </span>
          <div>
            <h1>BirdNET Live</h1>
            <p className="sub">Cornell Lab · V3.0 · listening through your Mac microphone</p>
          </div>
        </div>

        <div className="controls">
          <StatusPill state={state} model={model} detail={detail} />
          <span className="src-chip">{source === 'tab' ? 'tab audio' : 'mic'}</span>
          <div className="thresh" title="Minimum confidence to save a bird">
            <span>min conf</span>
            <input
              type="range"
              min="20"
              max="90"
              step="5"
              value={Math.round((model.confidence ?? 0.45) * 100)}
              onChange={(e) => setConfidence(Number(e.target.value) / 100)}
            />
            <b>{Math.round((model.confidence ?? 0.45) * 100)}%</b>
          </div>
          <button
            className={`band-toggle ${model.bird_band !== false ? 'on' : ''}`}
            onClick={() => setBirdBand(model.bird_band === false)}
            title="High-pass sub-900Hz rumble and lift the bird band"
          >
            bird-band boost
          </button>
          {devices.length > 0 && (
            <select
              className="devsel"
              value={deviceId || ''}
              onChange={(e) => selectDevice(e.target.value)}
              title="Microphone input"
            >
              {devices.map((d, i) => (
                <option key={d.deviceId || i} value={d.deviceId}>
                  {d.label || `Microphone ${i + 1}`}
                </option>
              ))}
            </select>
          )}
          <button
            className={`btn ${live ? 'btn-stop' : 'btn-go'}`}
            onClick={live ? stop : () => start()}
            disabled={busy}
          >
            {busy ? '…' : live ? 'Stop listening' : 'Start listening'}
          </button>
          {!live && (
            <button className="btn btn-tab" onClick={() => start('tab')} disabled={busy}>
              Share tab audio
            </button>
          )}
        </div>
      </header>

      {live && (
        <>
          <div className="meter" aria-hidden>
            <div className="meter-fill" style={{ width: `${Math.min(100, level * 260)}%` }} />
          </div>
          <LivePanel
            scores={scores}
            getAnalyser={getAnalyser}
            threshold={model.confidence ?? 0.45}
          />
        </>
      )}

      <section className="stage">
        {live && latest ? (
          <article className="now-playing">
            <div className="np-image">
              {latestMeta?.image ? (
                <img src={latestMeta.image} alt={latest.common} />
              ) : (
                <div className="np-placeholder">🐦</div>
              )}
            </div>
            <div className="np-text">
              <span className="np-label">Just heard</span>
              <h2>{latest.common}</h2>
              <em>{latest.scientific}</em>
              <div className="np-meta">
                <span className="chip chip-conf">{conf(latest.confidence)} confident</span>
                <span className="chip">{fmtTime(latest.ts)}</span>
              </div>
              {latestMeta?.description && (
                <p className="np-desc">{latestMeta.description.slice(0, 260)}…</p>
              )}
              {latestMeta?.credit && <p className="np-credit">{latestMeta.credit}</p>}
            </div>
          </article>
        ) : (
          <div className="idle">
            <div className="idle-glyph">👂</div>
            <h2>{busy ? 'Waking the model…' : live ? 'Listening — waiting for a bird' : 'Press start to listen'}</h2>
            <p>
              {ready
                ? 'Every identification is saved and stays on this page.'
                : model.state === 'loading'
                  ? 'First run downloads the 174 MB model — one time only.'
                  : detail || 'Microphone audio never leaves your machine except as the analysis stream.'}
            </p>
          </div>
        )}
      </section>

      <section className="log">
        <div className="log-head">
          <h3>
            Saved detections <span className="count">{detections.length}</span>
          </h3>
          <div className="log-stats">
            <span className="stat">
              <b>{uniqueSpecies}</b> {uniqueSpecies === 1 ? 'species' : 'species'} heard
            </span>
            {detections.length > 0 && (
              <button className="btn-ghost" onClick={clearAll}>
                Clear
              </button>
            )}
          </div>
        </div>

        {detections.length === 0 ? (
          <div className="empty">
            <span className="empty-glyph">🌿</span>
            <p>Nothing yet — every bird found stays here as a photo card.</p>
          </div>
        ) : (
          <div className="grid">
            {detections.map((d, i) => {
              const m = meta[d.scientific]
              const key = d.db_id || d.id
              const pct = Math.round(d.confidence * 100)
              return (
                <article
                  className="card"
                  key={key}
                  style={{ animationDelay: `${Math.min(i, 14) * 45}ms` }}
                >
                  <div className="card-img">
                    {m?.image ? (
                      <img src={m.image} alt={d.common} loading="lazy" />
                    ) : (
                      <span className="card-ph">🐦</span>
                    )}
                    <span className={`card-conf ${pct >= 60 ? 'hi' : pct >= 35 ? 'mid' : 'lo'}`}>
                      {pct}%
                    </span>
                  </div>
                  <div className="card-body">
                    <strong>{d.common}</strong>
                    <em>{d.scientific}</em>
                    <div className="card-foot">
                      <div className="mini-bar">
                        <div
                          className={`mini-fill ${pct >= 60 ? 'hi' : pct >= 35 ? 'mid' : 'lo'}`}
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <time>{fmtTime(d.ts)}</time>
                    </div>
                  </div>
                </article>
              )
            })}
          </div>
        )}
      </section>

      <footer className="foot">
        Model: BirdNET V3.0 (ONNX, fp32→fp16) · Species photos: Macaulay Library / iNaturalist via
        BirdNET+ Taxonomy API
      </footer>
    </div>
  )
}

/**
 * LivePanel — realtime spectrum visualizer + model confidence meter.
 * Confidence is always shown, even when the model is unsure or silent.
 */
function LivePanel({ scores, getAnalyser, threshold }) {
  const canvasRef = useRef(null)
  const rafRef = useRef(0)

  useEffect(() => {
    const cvs = canvasRef.current
    if (!cvs) return undefined
    const ctx2d = cvs.getContext('2d')
    let buf = null

    const draw = () => {
      rafRef.current = requestAnimationFrame(draw)
      const dpr = window.devicePixelRatio || 1
      const w = cvs.clientWidth
      const h = cvs.clientHeight
      if (!w || !h) return
      if (cvs.width !== Math.round(w * dpr)) {
        cvs.width = Math.round(w * dpr)
        cvs.height = Math.round(h * dpr)
      }
      ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx2d.clearRect(0, 0, w, h)

      const an = getAnalyser?.()
      if (!an) return
      if (!buf || buf.length !== an.frequencyBinCount) {
        buf = new Uint8Array(an.frequencyBinCount)
      }
      an.getByteFrequencyData(buf)

      const bars = 56
      const gap = 3
      const bw = (w - gap * (bars - 1)) / bars
      const usable = Math.floor(buf.length * 0.72) // drop near-DC junk
      for (let i = 0; i < bars; i++) {
        // log-ish bin grouping: more resolution at low frequencies
        const t0 = i / bars
        const t1 = (i + 1) / bars
        const i0 = Math.floor(Math.pow(t0, 1.7) * usable)
        const i1 = Math.max(i0 + 1, Math.floor(Math.pow(t1, 1.7) * usable))
        let peak = 0
        for (let j = i0; j < i1 && j < buf.length; j++) peak = Math.max(peak, buf[j])
        const v = peak / 255
        const bh = Math.max(2, v * h)
        const x = i * (bw + gap)
        const y = h - bh
        const alpha = 0.35 + 0.65 * v
        ctx2d.fillStyle = `rgba(126, 224, 163, ${alpha})`
        if (v > 0.72) ctx2d.fillStyle = `rgba(216, 243, 167, ${alpha})`
        ctx2d.beginPath()
        ctx2d.roundRect(x, y, bw, bh, 2)
        ctx2d.fill()
      }
    }
    draw()
    return () => cancelAnimationFrame(rafRef.current)
  }, [getAnalyser])

  const all = scores?.silent ? [] : scores?.top || []
  const top = all
    .filter((c) => c.is_bird !== false)
    .sort((a, b) => b.confidence - a.confidence)
  const best = top[0] || null
  const pct = best ? Math.round(best.confidence * 100) : 0
  const unsure = !best || best.confidence < threshold

  const R = 46
  const CIRC = 2 * Math.PI * R

  const db = scores?.peak ? Math.round(20 * Math.log10(Math.min(1, scores.peak))) : null
  const quiet = db !== null && db < -30

  return (
    <section className="live-panel">
      <div className="viz-wrap">
        <span className="viz-label">
          Spectrum · 0–8 kHz{' '}
          <span className={quiet ? 'quality bad' : 'quality'}>
            · input {db === null ? '--' : db} dB{quiet ? ' — too quiet' : ''}
          </span>
        </span>
        <canvas ref={canvasRef} className="viz" />
      </div>

      <div className="conf-wrap">
        <span className="viz-label">Model confidence</span>
        <div className="gauge">
          <svg viewBox="0 0 120 120" aria-hidden>
            <circle className="gauge-track" cx="60" cy="60" r={R} />
            <circle
              className={`gauge-fill ${unsure ? 'conf-low' : 'conf-high'}`}
              cx="60"
              cy="60"
              r={R}
              strokeDasharray={CIRC}
              strokeDashoffset={
                CIRC - (CIRC * (scores?.silent ? 0 : best?.confidence || 0)) / 1
              }
            />
          </svg>
          <div className={`gauge-num ${unsure ? 'conf-low' : 'conf-high'}`}>
            {scores?.silent ? '--' : pct}
            <span className="gauge-unit">%</span>
          </div>
        </div>
        <div className="conf-name">
          {scores?.silent ? 'no signal' : best ? best.common : 'analysing…'}
        </div>
        <div className="conf-bar">
          <div
            className={`conf-bar-fill ${unsure ? 'conf-low' : 'conf-high'}`}
            style={{ width: `${scores?.silent ? 0 : pct}%` }}
          />
          <div className="conf-threshold" title={`reporting threshold ${Math.round(threshold * 100)}%`} style={{ left: `${Math.round(threshold * 100)}%` }} />
        </div>
      </div>

      <div className="cands">
        <span className="viz-label">Top birds</span>
        {top.length === 0 ? (
          <p className="cand-empty">no bird in the noise floor</p>
        ) : (
          top.slice(0, 3).map((c) => (
            <div
              className={`cand ${c.confidence >= threshold ? 'cand-hot' : ''}`}
              key={c.scientific + c.confidence}
            >
              <div className="cand-row">
                <span className="cand-name">{c.common}</span>
                <span className="cand-pct">{Math.round(c.confidence * 100)}%</span>
              </div>
              <div className="cand-bar">
                <div className="cand-fill" style={{ width: `${c.confidence * 100}%` }} />
              </div>
            </div>
          ))
        )}
      </div>
    </section>
  )
}

function StatusPill({ state, model, detail }) {
  let cls = 'pill'
  let text = 'offline'
  if (state === 'connecting') {
    cls += ' pill-warn'
    text = 'connecting'
  } else if (state === 'live') {
    if (model.state === 'loading') {
      cls += ' pill-warn'
      text = 'model loading'
    } else if (model.state === 'ready') {
      cls += ' pill-ok'
      text = 'live'
    } else if (model.state === 'error') {
      cls += ' pill-bad'
      text = 'model error'
    } else {
      cls += ' pill-warn'
      text = 'waiting for model'
    }
  } else if (state === 'error') {
    cls += ' pill-bad'
    text = detail || 'error'
  }
  return <span className={cls}><i className="pill-dot" aria-hidden />{text}</span>
}
