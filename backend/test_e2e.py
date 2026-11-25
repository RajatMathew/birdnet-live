"""End-to-end test: stream a WAV over the WebSocket like the browser would."""

from __future__ import annotations

import asyncio
import json
import sys
import time


async def main(path: str, seconds: float = 12.0) -> None:
    import soundfile as sf
    import websockets

    data, sr = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    print(f"loaded {path}: {data.size / sr:.1f}s @ {sr} Hz")

    url = "ws://127.0.0.1:8000/ws"
    async with websockets.connect(url, max_size=2**24) as ws:
        await ws.send(json.dumps({"type": "config", "sample_rate": sr}))
        got: list[dict] = []
        t0 = time.time()
        sent = 0

        async def reader() -> None:
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") in ("detection", "meta", "status", "ack"):
                        got.append(msg)
                        if msg["type"] == "status":
                            print("  status:", {k: v for k, v in msg.items() if k != "history"})
                        elif msg["type"] == "detection":
                            print(f"  DETECT {msg['confidence']:.3f} {msg['common']} "
                                  f"({msg['scientific']}) db_id={msg.get('db_id')}")
                        elif msg["type"] == "meta":
                            print(f"  META   {msg['scientific']} image={bool(msg.get('image'))}")
            except Exception as exc:  # noqa: BLE001
                print("  reader ended:", exc)

        rt = asyncio.create_task(reader())

        # Stream 12 s of audio in ~0.17 s chunks, as the AudioWorklet does.
        chunk = 8192
        limit = int(seconds * sr)
        for i in range(0, min(limit, data.size), chunk):
            await ws.send(data[i:i + chunk].tobytes())
            sent += chunk
            await asyncio.sleep(0)  # don't throttle: server analyses on its own timer

        # Let the engine finish analysing the tail.
        await asyncio.sleep(6)
        await ws.close()
        rt.cancel()
        elapsed = time.time() - t0
        dets = [m for m in got if m.get("type") == "detection"]
        print(f"\nstreamed {sent / sr:.1f}s in {elapsed:.1f}s wall")
        print(f"detections: {len(dets)}")
        for m in dets:
            print(f"   {m['confidence']:.3f}  {m['common']}")

        # Verify persistence
        import httpx

        async with httpx.AsyncClient() as c:
            r = await c.get("http://127.0.0.1:8000/api/detections")
            saved = r.json()["detections"]
        print(f"saved in DB: {len(saved)}")
        for s in saved[:10]:
            print(f"   id={s['id']} {s['common']} conf={s['confidence']:.3f} "
                  f"img={'yes' if s['image'] else 'no'}")

        if not dets:
            print("FAIL: no detections")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/soundscape.wav"))
