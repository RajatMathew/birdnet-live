/**
 * useBirdnet — mic capture + WebSocket stream to the FastAPI backend.
 *
 * AudioWorklet pulls Float32 mono frames from the mic, batches them, and the
 * main thread ships them as binary WebSocket frames. Backend replies with
 * {type:'status'|'ack'|'detection'|'meta'} JSON.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

const CHUNK_SAMPLES = 8192 // ~0.17 s at 48 kHz

// Virtual/loopback cables carry digital silence unless something routes into them.
const BAD_DEVICE = /blackhole|aggregate|loopback|soundflower|virt|insert/i

function pickDefaultDevice(devs) {
  return (
    devs.find((d) => d.label && !BAD_DEVICE.test(d.label)) ||
    devs.find((d) => d.label) ||
    devs[0] ||
    null
  )
}

const WORKLET_SRC = `
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.buf = new Float32Array(${CHUNK_SAMPLES})
    this.n = 0
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (!ch) return true
    for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i]
      if (this.n === this.buf.length) {
        const out = this.buf.slice(0, this.n)
        this.port.postMessage(out, [out.buffer])
        this.n = 0
      }
    }
    return true
  }
}
registerProcessor('capture', CaptureProcessor)
`

export default function useBirdnet({ onDetection, onMeta, onStatus, onScores }) {
  const [state, setState] = useState('idle') // idle | connecting | live | error
  const [detail, setDetail] = useState('')
  const [level, setLevel] = useState(0)
  const [model, setModel] = useState({ state: 'unknown' })
  const [devices, setDevices] = useState([])
  const [deviceId, setDeviceId] = useState(null)
  const [source, setSource] = useState('mic') // mic | tab

  const wsRef = useRef(null)
  const ctxRef = useRef(null)
  const streamRef = useRef(null)
  const workletRef = useRef(null)
  const analyserRef = useRef(null)
  const deviceIdRef = useRef(null)
  const bufRef = useRef([])
  const bytesRef = useRef(0)
  const flushTimer = useRef(null)
  const cbRef = useRef({ onDetection, onMeta, onStatus, onScores })
  useEffect(() => {
    cbRef.current = { onDetection, onMeta, onStatus, onScores }
  }, [onDetection, onMeta, onStatus, onScores])

  const flush = useCallback(() => {
    const ws = wsRef.current
    const parts = bufRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN || parts.length === 0) return
    const total = bytesRef.current
    const out = new Float32Array(total)
    let off = 0
    for (const p of parts) {
      out.set(p, off)
      off += p.length
    }
    bufRef.current = []
    bytesRef.current = 0
    ws.send(out.buffer)
  }, [])

  const stop = useCallback(() => {
    if (flushTimer.current) {
      clearInterval(flushTimer.current)
      flushTimer.current = null
    }
    try {
      workletRef.current?.disconnect()
    } catch {}
    workletRef.current = null
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    if (ctxRef.current) {
      ctxRef.current.close().catch(() => {})
      ctxRef.current = null
    }
    analyserRef.current = null
    try {
      wsRef.current?.close()
    } catch {}
    wsRef.current = null
    setState('idle')
    setLevel(0)
    analyserRef.current = null
    cbRef.current.onScores?.({ type: 'scores', top: [], silent: true })
  }, [])

  const start = useCallback(async (mode = 'mic') => {
    setSource(mode === 'tab' ? 'tab' : 'mic')
    setState('connecting')
    setDetail(mode === 'tab' ? 'choose a tab with audio…' : 'requesting microphone…')
    try {
      let stream = null
      if (mode === 'tab') {
        // Chrome/Edge: share a tab with "Share tab audio" checked. Video track
        // is required to open the picker but we only use the audio track.
        stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true })
        stream.getVideoTracks().forEach((t) => t.stop())
        if (!stream.getAudioTracks().length) {
          throw new Error('no audio track — enable "Share tab audio" in the picker')
        }
      } else {
      // First attempt: default device. If it's a virtual cable, retry once on
      // a real microphone (labels only appear after permission is granted).
      for (let attempt = 0; attempt < 2; attempt++) {
        const audio = {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          channelCount: 1,
        }
        if (deviceIdRef.current) audio.deviceId = { exact: deviceIdRef.current }
        stream = await navigator.mediaDevices.getUserMedia({ audio, video: false })

        const list = (await navigator.mediaDevices.enumerateDevices()).filter(
          (d) => d.kind === 'audioinput',
        )
        setDevices(list)

        const current = stream.getAudioTracks()[0]?.getSettings()?.deviceId || null
        if (attempt === 0 && !deviceIdRef.current) {
          const pick = pickDefaultDevice(list)
          if (pick && pick.deviceId && pick.deviceId !== current) {
            stream.getTracks().forEach((t) => t.stop())
            deviceIdRef.current = pick.deviceId
            setDeviceId(pick.deviceId)
            continue
          }
          deviceIdRef.current = current
          setDeviceId(current)
        }
        break
      }
      } // mic mode
      streamRef.current = stream

      const ctx = new AudioContext()
      ctxRef.current = ctx
      const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }))
      await ctx.audioWorklet.addModule(url)
      URL.revokeObjectURL(url)

      const src = ctx.createMediaStreamSource(stream)
      const node = new AudioWorkletNode(ctx, 'capture')
      src.connect(node)

      // Analyser taps the raw mic for the visualizer (no audio output).
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 2048
      analyser.smoothingTimeConstant = 0.75
      src.connect(analyser)
      analyserRef.current = analyser

      // keep the graph alive without routing mic audio to the speakers
      const mute = ctx.createGain()
      mute.gain.value = 0
      node.connect(mute)
      mute.connect(ctx.destination)
      workletRef.current = node

      node.port.onmessage = (ev) => {
        const chunk = ev.data
        bufRef.current.push(chunk)
        bytesRef.current += chunk.length
        let peak = 0
        for (let i = 0; i < chunk.length; i += 16) {
          const a = Math.abs(chunk[i])
          if (a > peak) peak = a
        }
        setLevel((prev) => Math.max(peak, prev * 0.9))
        if (bytesRef.current >= CHUNK_SAMPLES * 6) flush()
      }

      setDetail('connecting to analyzer…')
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const ws = new WebSocket(`${proto}://${location.host}/ws`)
      ws.binaryType = 'arraybuffer'
      wsRef.current = ws

      ws.onopen = () => {
        ws.send(JSON.stringify({ type: 'config', sample_rate: ctx.sampleRate }))
        ws.send(JSON.stringify({ type: 'claim_audio' }))
        setState('live')
        setDetail('')
      }
      ws.onmessage = (ev) => {
        let msg
        try {
          msg = JSON.parse(ev.data)
        } catch {
          return
        }
        if (msg.type === 'status') {
          setModel((prev) => ({ ...prev, ...msg }))
          cbRef.current.onStatus?.(msg)
        } else if (msg.type === 'detection') {
          cbRef.current.onDetection?.(msg)
        } else if (msg.type === 'meta') {
          cbRef.current.onMeta?.(msg)
        } else if (msg.type === 'scores') {
          cbRef.current.onScores?.(msg)
        }
      }
      ws.onerror = () => {
        setDetail('analyzer connection failed')
        setState('error')
      }
      ws.onclose = () => {
        if (wsRef.current === ws) {
          wsRef.current = null
          setState((s) => (s === 'live' ? 'error' : s))
          setDetail((d) => d || 'connection closed')
        }
      }

      flushTimer.current = setInterval(flush, 100)
    } catch (err) {
      setState('error')
      setDetail(err?.name === 'NotAllowedError' ? 'microphone permission denied' : String(err?.message || err))
    }
  }, [flush])

  useEffect(() => () => stop(), [stop])

  const selectDevice = useCallback(
    async (id) => {
      deviceIdRef.current = id || null
      setDeviceId(id || null)
      if (ctxRef.current) {
        stop()
        await start()
      }
    },
    [stop, start],
  )

  const getAnalyser = useCallback(() => analyserRef.current, [])

  const setConfidence = useCallback((value) => {
    const ws = wsRef.current
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'threshold', confidence: value }))
    }
  }, [])

  const setBirdBand = useCallback((enabled) => {
    const ws = wsRef.current
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'bird_band', enabled }))
    }
  }, [])

  return {
    state,
    detail,
    level,
    model,
    devices,
    deviceId,
    source,
    selectDevice,
    setConfidence,
    setBirdBand,
    start,
    stop,
    getAnalyser,
  }
}
