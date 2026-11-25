# BirdNET Live

Realtime bird identification from your Mac's microphone. React frontend + Python
FastAPI backend running Cornell's **BirdNET V3.0** model locally.

Every bird found is saved to SQLite and **stays on screen** — nothing is hidden
or auto-dismissed.

## How it works

```
Mac mic ──AudioWorklet──▶ Float32 PCM ──WebSocket──▶ FastAPI
                                                          │
                                          ring buffer, last 3 s window
                                                          │
                                        BirdNET V3.0 (ONNX, fp16)  ~60 ms
                                                          │
                                    filter to birds only (Aves whitelist)
                                                          │
                          ┌───────────────────────────────┴──────────────────┐
                          ▼                                                  ▼
                 SQLite (persist)                                Cornell Taxonomy API
                          │                                       photo + description
                          └──────────────────▶ WebSocket ──▶ React (stays on screen)
```

- **Model**: `birdnet` PyPI package (official, Cornell/Chemnitz). V3.0 acoustic,
  ONNX backend, fp16 — 72 MB, no TensorFlow needed. 32 kHz input, 3 s segments,
  11,725 classes. Auto-downloads on first run into
  `~/Library/Application Support/birdnet/`.
- **Photos + descriptions**: [BirdNET+ Taxonomy API](https://birdnet.cornell.edu/taxonomy/docs)
  (`/api/species/{scientific_name}`), images proxied through the backend so the
  browser never talks to Cornell directly.
- **Bird-only filter**: the label file also contains frogs, mammals and 14 noise
  classes (`Music`, `Chainsaw`, `Aircraft`, …). At startup we fetch all 11,022
  `Aves` species once and cache them in `backend/data/aves.json`.

## Run it

```bash
# one command
./run.sh

# or manually — terminal 1, backend (port 8000)
python3 -m venv .venv            # .venv at project root
.venv/bin/pip install -r backend/requirements.txt
cd backend && ../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# terminal 2 — frontend (port 5173)
cd frontend && npm install && npm run dev
```

## UI

- **Live analysis strip** — 56-bar spectrum from an `AnalyserNode`, circular
  confidence ring gauge, and top-3 candidates. Confidence updates every 1.5 s
  hop and is shown even when no bird clears the adjustable reporting threshold.
- **Header slider** — change minimum confidence live (20–90%) without restarting;
  the ring threshold marker follows it.
- **Detection grid** — every saved bird is a photo card with confidence badge,
  Latin name and timestamp. Cards stay on screen until you press Clear.
- **Mic picker** — virtual cables (BlackHole etc.) are auto-skipped because they
  deliver digital silence; you can switch inputs live from the header dropdown.
- **One audio producer** — only the first connected tab feeds PCM; other tabs can
  watch but won't interleave audio and corrupt analysis windows.

Open <http://localhost:5173>, press **Start listening**, allow microphone access.

> First start downloads the model (~72 MB) and the bird species list — one time
> only. Status pill shows `model loading` until it's ready.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `WS` | `/ws` | PCM in (binary float32 mono), JSON out (`status` / `scores` / `detection` / `meta`) |
| `GET` | `/api/detections?limit=300` | saved detections, newest first |
| `POST` | `/api/detections/clear` | `{"confirm": true}` |
| `GET` | `/api/species/{scientific_name}` | common name, photo, description |
| `GET` | `/api/image/{scientific_name}?size=medium` | proxied species photo |
| `GET` | `/api/health` | engine state |

## Tuning

All in `backend/app/engine.py`:

```python
SEGMENT_S = 3.0     # analysis window (BirdNET's native size)
HOP_S = 1.5         # analyse every 1.5 s (50% overlap)
CONFIDENCE = 0.45   # minimum score to report (change live with the header slider)
COOLDOWN_S = 15.0   # per-species re-report cooldown, stops spam
```

Measured latency: **~60-80 ms** per analysis on Apple silicon, against a 1.5 s
hop — comfortably realtime.

## Tests

```bash
cd backend
../.venv/bin/python test_realtime.py     # model load + inference latency
../.venv/bin/python test_e2e.py /tmp/soundscape.wav   # full WebSocket round trip
```

`test_e2e.py` streams real birdsong over the WebSocket and asserts detections
reach the database with photos attached.

## Notes

- Audio is captured at the browser's native rate and resampled server-side
  (windowed-sinc FIR + interpolation) to the model's 32 kHz.
- Model: V3.0 acoustic — Apache 2.0. V2.4 models are CC BY-NC-SA 4.0.
  Educational/research use is non-commercial and free.
- Photos: Macaulay Library / iNaturalist, credited per species in the UI.
