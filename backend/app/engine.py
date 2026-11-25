"""Realtime BirdNET inference engine.

A single background thread owns the model and a ring buffer of PCM audio.
Every `HOP_S` seconds it takes the last `SEGMENT_S` seconds and runs one
inference, then publishes detections through a list of asyncio callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Callable

import numpy as np
from scipy.signal import butter, sosfilt

log = logging.getLogger("engine")

SEGMENT_S = 3.0          # BirdNET analysis window
HOP_S = 1.5              # how often we analyse (50% overlap)
CONFIDENCE = 0.45        # minimum score to report (adjustable live via /ws)
COOLDOWN_S = 15.0        # per-species re-report cooldown
MODEL_SR = 32000         # BirdNET V3.0 sample rate
HP_SOS = butter(4, 900, btype="highpass", fs=MODEL_SR, output="sos")


def enhance_bird_band(x: np.ndarray) -> np.ndarray:
    """Drop sub-900 Hz rumble (motors, voice) and lift what remains."""
    y = sosfilt(HP_SOS, x).astype(np.float32)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 1e-8:
        y = y * min(60.0, 0.35 / peak)
    return y


class Detection(dict):
    """Plain dict so it serialises straight to JSON."""


class BirdEngine:
    def __init__(self) -> None:
        self.model = None
        self.sample_rate = MODEL_SR
        self.state = "starting"          # starting | loading | ready | error
        self.error: str | None = None
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        self._audio: deque[np.ndarray] = deque()
        self._samples = 0                # samples currently buffered
        self._session = None
        self._session_lock = threading.Lock()
        self._last_seen: dict[str, float] = {}
        self._seq = 0
        self._busy = False
        self._pending: np.ndarray | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.rms = 0.0
        self.confidence = CONFIDENCE
        self.bird_band = True
        # When set, only these scientific names are reported (birds only).
        self.allowed_species: set[str] | None = None
        # Diagnostics — surfaced through /api/health so we can see where audio dies.
        self.stats: dict[str, Any] = {
            "fed_samples": 0,
            "fed_chunks": 0,
            "client_sr": MODEL_SR,
            "buffered": 0,
            "analysed": 0,
            "skipped_silence": 0,
            "skipped_no_window": 0,
            "errors": 0,
            "last_error": None,
            "last_confidence": 0.0,
        }

    # ---------------------------------------------------------------- wiring

    def on_detection(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self.listeners.append(cb)

    def _emit(self, payload: dict[str, Any]) -> None:
        if not self.listeners or self._loop is None:
            return
        for cb in list(self.listeners):
            # Listeners are async callables; schedule them on the event loop.
            self._loop.call_soon_threadsafe(self._invoke, cb, payload)

    @staticmethod
    def _invoke(cb: Callable[[dict[str, Any]], Any], payload: dict[str, Any]) -> None:
        import asyncio as _aio

        res = cb(payload)
        if isinstance(res, _aio.Future) or hasattr(res, "__await__"):
            _aio.ensure_future(res)

    # ------------------------------------------------------------- lifecycle

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._thread = threading.Thread(target=self._run, name="birdnet-engine", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.state = "loading"
            self._emit({"type": "status", "state": "loading",
                        "detail": "loading BirdNET V3.0 (first run downloads ~174 MB)"})
            import birdnet

            self.model = birdnet.load("acoustic", "3.0", "onnx", precision="fp16")
            self.sample_rate = int(self.model.get_sample_rate())
            self._segment_samples = int(self.model.get_segment_size_s() * self.sample_rate)
            self.state = "ready"
            log.info("model ready, sr=%s seg=%ss", self.sample_rate, self.model.get_segment_size_s())
            self._emit({"type": "status", "state": "ready",
                        "sample_rate": self.sample_rate,
                        "segment_s": self.model.get_segment_size_s(),
                        "hop_s": HOP_S, "confidence": self.confidence,
                        "bird_band": self.bird_band})
        except Exception as exc:  # noqa: BLE001
            self.state = "error"
            self.error = str(exc)
            log.exception("model load failed")
            self._emit({"type": "status", "state": "error", "detail": str(exc)})
            return

        # Analysis loop: never blocks audio intake.
        while True:
            try:
                self._analyse_once()
            except Exception:  # noqa: BLE001
                log.exception("analysis failed")
            time.sleep(HOP_S)

    # ---------------------------------------------------------------- input

    _client_sr: int = MODEL_SR

    def set_client_sample_rate(self, sr: int) -> None:
        if sr > 0:
            self._client_sr = int(sr)
            self.stats["client_sr"] = int(sr)

    def set_confidence(self, value: float) -> float:
        self.confidence = min(0.95, max(0.05, float(value)))
        return self.confidence

    def set_bird_band(self, enabled: bool) -> bool:
        self.bird_band = bool(enabled)
        return self.bird_band

    def feed(self, pcm: np.ndarray) -> None:
        """pcm: float32 mono at the client's sample rate."""
        if pcm.size == 0:
            return
        if pcm.dtype != np.float32:
            pcm = pcm.astype(np.float32)
        self.stats["fed_chunks"] += 1
        raw_peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
        self.stats["raw_peak"] = round(raw_peak, 5)
        self.stats["raw_rms"] = round(float(np.sqrt(np.mean(np.square(pcm)))), 5)
        if self._client_sr != self.sample_rate:
            pcm = resample(pcm, self._client_sr, self.sample_rate)
        self.stats["fed_samples"] += int(pcm.size)

        self.rms = float(np.sqrt(np.mean(np.square(pcm)))) if pcm.size else 0.0
        self.stats["last_rms"] = round(self.rms, 5)

        with self._lock:
            self._audio.append(pcm)
            self._samples += pcm.size
            # Keep 10 s worth; the analysis window only needs 3 s.
            limit = int((SEGMENT_S + 7.0) * self.sample_rate)
            while self._samples > limit and self._audio:
                oldest = self._audio.popleft()
                self._samples -= oldest.size

    # ------------------------------------------------------------ inference

    def _window(self) -> np.ndarray | None:
        need = self._segment_samples
        with self._lock:
            if self._samples < need or not self._audio:
                return None
            chunks: list[np.ndarray] = []
            left = need
            for c in reversed(self._audio):
                if left <= 0:
                    break
                chunks.append(c)
                left -= c.size
            if left > 0:
                return None
            win = np.concatenate(chunks[::-1])[-need:]
        return np.ascontiguousarray(win, dtype=np.float32)

    def _analyse_once(self) -> None:
        if self.state != "ready" or self._busy:
            return
        self.stats["analysed"] += 1
        win = self._window()
        if win is None:
            self.stats["skipped_no_window"] += 1
            return
        peak = float(np.max(np.abs(win)))
        self.stats["last_peak"] = round(peak, 5)
        if peak < 1e-4:   # digital silence
            self.stats["skipped_silence"] += 1
            self._emit({"type": "scores", "top": [], "peak": peak, "silent": True})
            return

        self._busy = True
        try:
            with self._session_lock:
                if self._session is None:
                    sess = self.model.predict_session(n_workers=1, top_k=5)
                    # predict_session() must be entered: it starts the worker
                    # processes and marks the pipeline initialized.
                    self._session = sess.__enter__()
                res = self._session.run_arrays((enhance_bird_band(win) if self.bird_band else win, self.sample_rate))
            top = self._publish(res, win)
            # Always broadcast live scores, even when nothing clears the bar.
            self._emit({"type": "scores", "top": top, "peak": peak, "silent": False})
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = str(exc)
            raise
        finally:
            self._busy = False

    def _publish(self, res: Any, win: np.ndarray) -> list[dict[str, Any]]:
        species = np.asarray(res.species_list)
        ids = np.asarray(res.species_ids)
        probs = np.asarray(res.species_probs)

        # shapes: (n_inputs, n_segments, top_k) — flatten to (rows, top_k)
        if ids.size == 0:
            return []
        ids = ids.reshape(-1, ids.shape[-1])
        probs = probs.reshape(-1, probs.shape[-1])
        row_ids, row_probs = ids[0], probs[0]

        now = time.monotonic()
        reported: list[dict[str, Any]] = []
        top: list[dict[str, Any]] = []
        best = 0.0
        for idx, conf in zip(row_ids, row_probs):
            conf = float(conf)
            best = max(best, conf)
            label = str(species[int(idx)])
            scientific, _, common = label.partition("_")
            common = common or scientific
            top.append({
                "scientific": scientific,
                "common": common,
                "confidence": round(conf, 4),
                "is_bird": (" " in scientific and (
                    self.allowed_species is None or scientific in self.allowed_species
                )),
            })
            if conf < self.confidence:
                continue
            # Non-species classes: "Music_Music", "Chainsaw_Chainsaw", ...
            if " " not in scientific:
                continue
            if self.allowed_species is not None and scientific not in self.allowed_species:
                self.stats["rejected_not_aves"] = self.stats.get("rejected_not_aves", 0) + 1
                self.stats["last_rejected"] = f"{scientific} ({conf:.2f})"
                continue
            last = self._last_seen.get(scientific, 0.0)
            if now - last < COOLDOWN_S:
                self.stats["cooldown_hits"] = self.stats.get("cooldown_hits", 0) + 1
                continue
            self._last_seen[scientific] = now
            self._seq += 1
            reported.append({
                "id": self._seq,
                "scientific": scientific,
                "common": common,
                "confidence": round(conf, 4),
                "ts": int(time.time() * 1000),
                "segment_s": SEGMENT_S,
            })

        self.stats["last_confidence"] = round(best, 4)
        for det in reported:
            self._emit({"type": "detection", **det})
        top.sort(key=lambda x: -x["confidence"])
        return top

    # ------------------------------------------------------------- shutdown

    def stop(self) -> None:
        with self._session_lock:
            if self._session is not None:
                try:
                    self._session.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    log.exception("session shutdown failed")
                self._session = None


def resample(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """Anti-aliased resample: windowed-sinc FIR lowpass, then linear interp."""
    if sr_from == sr_to or x.size == 0:
        return x
    # Cutoff at the lower Nyquist, with transition band.
    cutoff = 0.45 * min(sr_from, sr_to)
    half = 64
    n = np.arange(-half, half + 1)
    fc = cutoff / sr_from
    h = 2 * fc * np.sinc(2 * fc * n)
    # Blackman window for stopband attenuation.
    w = np.blackman(h.size)
    h = h * w
    h /= h.sum()

    y = np.convolve(x, h, mode="same")
    t_out = np.arange(0, y.size, sr_from / sr_to)
    t_out = t_out[t_out < y.size - 1]
    return np.interp(t_out, np.arange(y.size), y).astype(np.float32)
