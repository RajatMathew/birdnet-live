"""BirdNET Live — FastAPI backend.

WebSocket /ws  : browser streams mic PCM (float32 mono) as binary frames.
REST /api/*   : saved detections, species metadata, proxied species photos.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import numpy as np
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from . import store, taxonomy
from .engine import BirdEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("api")

engine = BirdEngine()

app = FastAPI(title="BirdNET Live")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SaveBody(BaseModel):
    scientific: str
    common: str
    confidence: float


@app.on_event("startup")
async def _startup() -> None:
    engine.on_detection(_on_detection)
    engine.start(asyncio.get_running_loop())

    # Fetch the Aves whitelist off the event loop (one-time, cached on disk).
    def _birds() -> set[str]:
        from .taxonomy import load_aves_sync

        return load_aves_sync()

    def _set(birds: set[str]) -> None:
        engine.allowed_species = birds or None

    try:
        birds = await asyncio.to_thread(_birds)
        _set(birds)
    except Exception as exc:  # noqa: BLE001
        log.warning("Aves whitelist unavailable, using label heuristic: %s", exc)


_pending_meta: dict[str, asyncio.Task] = {}


async def _on_detection(payload: dict[str, Any]) -> None:
    """Route engine events: persist detections, fan everything else out."""
    if payload.get("type") != "detection":
        broadcast(payload)
        return

    det_id = store.insert_detection(
        payload["scientific"], payload["common"], payload["confidence"]
    )
    payload = {**payload, "db_id": det_id, "image": None, "description": None}
    broadcast(payload)

    sci = payload["scientific"]
    if sci in _pending_meta:
        return

    async def enrich() -> None:
        _pending_meta.pop(sci, None)
        rec = await taxonomy.fetch_species(sci)
        if not rec:
            return
        image = taxonomy.image_url(sci)
        store.update_detection_meta(det_id, image, rec.get("description"))
        broadcast({
            "type": "meta",
            "scientific": sci,
            "image": image,
            "description": rec.get("description"),
            "credit": rec.get("image_credit"),
        })

    _pending_meta[sci] = asyncio.create_task(enrich())


# ----------------------------------------------------------------- websocket

clients: set[WebSocket] = set()
audio_owner: WebSocket | None = None   # only one tab may stream mic PCM


def broadcast(payload: dict[str, Any]) -> None:
    if not clients:
        return
    text = json.dumps(payload)
    dead: list[WebSocket] = []
    for ws in list(clients):
        try:
            asyncio.ensure_future(ws.send_text(text))
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


@app.websocket("/ws")
async def ws_stream(ws: WebSocket) -> None:
    global audio_owner
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({
        "type": "status",
        "state": engine.state,
        "detail": engine.error or "model loading" if engine.state == "loading" else None,
        "segment_s": 3.0,
        "confidence": engine.confidence,
        "bird_band": engine.bird_band,
        "history": store.list_detections(200),
    }))
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"]:
                if audio_owner is None:
                    audio_owner = ws
                if ws is audio_owner:
                    pcm = np.frombuffer(msg["bytes"], dtype=np.float32)
                    engine.feed(pcm)
            elif msg.get("text"):
                try:
                    ctrl = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                if ctrl.get("type") == "config":
                    engine.set_client_sample_rate(int(ctrl.get("sample_rate", 32000)))
                    await ws.send_text(json.dumps({"type": "ack", **ctrl}))
                elif ctrl.get("type") == "claim_audio":
                    audio_owner = ws
                    await ws.send_text(json.dumps({"type": "ack", "claimed": True}))
                elif ctrl.get("type") == "threshold":
                    value = engine.set_confidence(float(ctrl["confidence"]))
                    broadcast({"type": "status", "confidence": value})
                elif ctrl.get("type") == "bird_band":
                    value = engine.set_bird_band(bool(ctrl["enabled"]))
                    broadcast({"type": "status", "bird_band": value})
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        if ws is audio_owner:
            audio_owner = None


@app.on_event("shutdown")
async def _shutdown() -> None:
    engine.stop()


# ---------------------------------------------------------------------- REST


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "state": engine.state,
        "error": engine.error,
        "clients": len(clients),
        "confidence": engine.confidence,
        "audio_owner": audio_owner is not None,
        "stats": engine.stats,
        "allowed_species": len(engine.allowed_species) if engine.allowed_species else None,
        "last_rms": engine.rms,
    }


@app.get("/api/detections")
async def detections(limit: int = Query(300, ge=1, le=2000)) -> dict[str, Any]:
    return {"detections": store.list_detections(limit)}


class ClearBody(BaseModel):
    confirm: bool = False


@app.post("/api/detections/clear")
async def clear_detections(body: ClearBody) -> dict[str, Any]:
    if not body.confirm:
        return {"ok": False, "reason": "confirm=false"}
    store.clear_detections()
    return {"ok": True}


@app.get("/api/species/{scientific_name}")
async def species(scientific_name: str) -> dict[str, Any]:
    rec = await taxonomy.fetch_species(scientific_name)
    if rec is None:
        return {"found": False, "scientific": scientific_name}
    return {"found": True, **rec, "image": taxonomy.image_url(scientific_name)}


@app.get("/api/image/{scientific_name}")
async def image(scientific_name: str, size: str = Query("medium")) -> Response:
    data = await taxonomy.proxy_image(scientific_name, size)
    if not data:
        return Response(status_code=404, media_type="text/plain", content="no image")
    media = "image/webp" if data[:2] == b"RIFF" else "image/jpeg"
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/rms")
async def rms() -> dict[str, float]:
    return {"rms": engine.rms}
