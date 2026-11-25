"""Compare BirdNET accuracy with bird-band boost on/off on a known recording."""

from __future__ import annotations

import asyncio
import json
import sys

import soundfile as sf
import websockets


async def phase(ws, audio, sr, start_s: float, enabled: bool) -> None:
    await ws.send(json.dumps({"type": "bird_band", "enabled": enabled}))
    await asyncio.sleep(0.4)
    marker = len(getattr(phase, "hits", []))
    chunk = 8192
    begin = int(start_s * sr)
    end = min(audio.size, begin + int(20 * sr))
    for i in range(begin, end, chunk):
        await ws.send(audio[i : i + chunk].tobytes())
        await asyncio.sleep(0)
    await asyncio.sleep(5)


async def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/soundscape.wav"
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    async with websockets.connect("ws://127.0.0.1:8000/ws", max_size=2**24) as ws:
        await ws.send(json.dumps({"type": "config", "sample_rate": sr}))
        await ws.send(json.dumps({"type": "claim_audio"}))

        results: dict[bool, list] = {True: [], False: []}
        current = [False]
        detected: dict[bool, list] = {True: [], False: []}

        async def reader():
            try:
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "scores" and not m.get("silent"):
                        birds = [c for c in m.get("top", []) if c.get("is_bird")]
                        if birds:
                            results[current[0]].append(birds[0])
                    elif m.get("type") == "detection":
                        detected[current[0]].append(m)
            except Exception:
                pass

        rt = asyncio.create_task(reader())
        await asyncio.sleep(0.5)

        for mode, offset in ((False, 0.0), (True, 25.0)):
            current[0] = mode
            await ws.send(json.dumps({"type": "bird_band", "enabled": mode}))
            await asyncio.sleep(0.5)
            begin = int(offset * sr)
            end = min(audio.size, begin + int(20 * sr))
            for i in range(begin, end, 8192):
                await ws.send(audio[i : i + 8192].tobytes())
                await asyncio.sleep(0)
            await asyncio.sleep(5)
            print(
                f"boost={mode}: top-bird={len(results[mode])} "
                f"detections={len(detected[mode])}"
            )
            for d in detected[mode][:8]:
                print(f"   {d['confidence']:.3f}  {d['common']}")
            for c in results[mode][:6]:
                print(f"   score {c['confidence']:.3f}  {c['common']}")

        rt.cancel()


if __name__ == "__main__":
    asyncio.run(main())
