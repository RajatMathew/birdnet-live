"""Compare raw vs high-pass-boosted mic audio through BirdNET."""

from __future__ import annotations

import sys

import numpy as np
import soundfile as sf

from app.engine import resample


def main() -> None:
    import birdnet
    from scipy.signal import butter, sosfilt

    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mic_now.wav"
    x, sr = sf.read(path, dtype="float32")
    x = resample(x, sr, 32000)
    mid = len(x) // 2
    x = x[mid - 48000 : mid + 48000]

    sos = butter(4, 900, btype="highpass", fs=32000, output="sos")
    y = sosfilt(sos, x).astype(np.float32)
    peak = float(np.max(np.abs(y)))
    boost = min(60.0, 0.35 / max(peak, 1e-9))
    y = y * boost
    print(
        f"input peak {float(np.max(np.abs(x))):.5f} -> filtered {peak:.5f} "
        f"-> boosted {float(np.max(np.abs(y))):.3f} (x{boost:.0f})"
    )

    model = birdnet.load("acoustic", "3.0", "onnx", precision="fp16")
    for label, audio in (("RAW   ", x), ("FILTER", y)):
        r = model.predict_arrays(
            (audio, 32000), n_workers=1, top_k=5, default_confidence_threshold=0.0
        )
        sp = np.asarray(r.species_list)
        ids = np.asarray(r.species_ids).reshape(-1, np.asarray(r.species_ids).shape[-1])
        ps = np.asarray(r.species_probs).reshape(-1, np.asarray(r.species_probs).shape[-1])
        print(
            label,
            " | ".join(
                f"{sp[int(i)]}:{float(c):.3f}" for i, c in zip(ids[0], ps[0])
            ),
        )


if __name__ == "__main__":
    main()
