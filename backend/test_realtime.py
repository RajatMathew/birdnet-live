"""Latency + accuracy smoke test for the realtime path."""

from __future__ import annotations

import time

import numpy as np


def main() -> None:
    import birdnet

    t = time.time()
    model = birdnet.load("acoustic", "3.0", "onnx", precision="fp16")
    print(f"load {time.time() - t:.1f}s  sr={model.get_sample_rate()} "
          f"seg={model.get_segment_size_s()} classes={model.get_n_classes()}")

    sr = int(model.get_sample_rate())
    rng = np.random.default_rng(0)
    n = int(sr * 3.0)
    x = (0.05 * rng.standard_normal(n)).astype(np.float32)
    tt = np.arange(n) / sr
    for f0 in (1800, 2600, 3400):
        x += (0.2 * np.sin(2 * np.pi * (f0 + 500 * np.sin(2 * np.pi * 4 * tt)) * tt)).astype(np.float32)

    t = time.time()
    res = model.predict_arrays((x, sr), n_workers=1, top_k=5,
                               default_confidence_threshold=0.0)
    print(f"first (cold, spawns workers) {time.time() - t:.2f}s")
    _show(res)

    t = time.time()
    with model.predict_session(n_workers=1, top_k=5,
                               default_confidence_threshold=0.0) as session:
        for i in range(5):
            t0 = time.time()
            r = session.run_arrays((x, sr))
            print(f"  session call {i}: {time.time() - t0:.3f}s")
        _show(r)
    print(f"session total {time.time() - t:.2f}s")


def _show(res) -> None:
    species = np.asarray(res.species_list)
    ids = np.asarray(res.species_ids).reshape(-1, np.asarray(res.species_ids).shape[-1])
    probs = np.asarray(res.species_probs).reshape(-1, np.asarray(res.species_probs).shape[-1])
    print("  shapes:", species.shape, ids.shape, probs.shape)
    for idx, conf in zip(ids[0], probs[0]):
        print(f"    {float(conf):.4f}  {species[int(idx)]}")


if __name__ == "__main__":
    main()
