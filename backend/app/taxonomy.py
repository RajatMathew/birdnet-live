"""BirdNET+ Taxonomy API client (Cornell Lab) — common name, photo, description.

Docs: https://birdnet.cornell.edu/taxonomy/docs
OpenAPI: https://birdnet.cornell.edu/taxonomy/openapi.json
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from . import store

log = logging.getLogger("taxonomy")

BASE = "https://birdnet.cornell.edu/taxonomy/api"

# In-memory cache: scientific name -> record (or None when unresolved).
_cache: dict[str, dict[str, Any] | None] = {}
# Negative cache so unknown names don't hammer the API.
_FAILED: set[str] = set()

_TIMEOUT = httpx.Timeout(10.0)


def image_url(scientific: str, size: str = "medium") -> str:
    """Proxy URL served by our own backend (avoids hotlink/CORS issues)."""
    from urllib.parse import quote

    return f"/api/image/{quote(scientific)}?size={size}"


async def fetch_species(scientific: str) -> dict[str, Any] | None:
    """Fetch + cache taxonomy record. Returns None when unknown."""
    if scientific in _cache:
        return _cache[scientific]
    if scientific in _FAILED:
        return None

    cached = store.get_species_meta(scientific)
    if cached is not None:
        rec = {
            "scientific": scientific,
            "common": cached.get("common"),
            "image": cached.get("image"),
            "description": cached.get("description"),
        }
        _cache[scientific] = rec
        return rec

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{BASE}/species/{scientific}", params={"fallback": "false"})
            if r.status_code == 404:
                _FAILED.add(scientific)
                _cache[scientific] = None
                return None
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001 — network failure must not break the stream
        log.warning("taxonomy lookup failed for %s: %s", scientific, exc)
        _cache[scientific] = None
        return None

    if not data.get("scientific_name") or data.get("found") is False:
        _FAILED.add(scientific)
        _cache[scientific] = None
        return None

    image = data.get("image") or {}
    description = (data.get("descriptions") or {}).get("en")
    rec = {
        "scientific": data["scientific_name"],
        "common": data.get("common_name"),
        "image": image.get("medium") or image.get("src"),
        "image_full": image.get("src"),
        "image_credit": f"{image.get('source', '')} — {image.get('author', '')} ({image.get('license', '')})".strip(" —"),
        "description": description,
    }
    _cache[scientific] = rec
    store.put_species_meta(rec["scientific"], rec["common"], rec["image"], rec["description"])
    return rec


async def proxy_image(scientific: str, size: str = "medium") -> bytes | None:
    """Download the species photo so the browser never talks to Cornell directly."""
    url = f"{BASE}/image/{scientific}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url, params={"size": size})
            if r.status_code != 200:
                return None
            return r.content
    except Exception as exc:  # noqa: BLE001
        log.warning("image proxy failed for %s: %s", scientific, exc)
        return None


# ----------------------------------------------------------- bird-only filter

AVES_CACHE = store.DB_PATH.parent / "aves.json"


def _load_aves_cache() -> set[str] | None:
    try:
        if AVES_CACHE.is_file():
            data = json.loads(AVES_CACHE.read_text())
            if isinstance(data, list) and data:
                return set(data)
    except Exception:  # noqa: BLE001
        pass
    return None


def _save_aves_cache(names: set[str]) -> None:
    try:
        AVES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        AVES_CACHE.write_text(json.dumps(sorted(names)))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not cache Aves list: %s", exc)


def load_aves_sync() -> set[str]:
    """Blocking fetch of every bird species name (used at startup, cached on disk).

    Resumes from the on-disk cache, retries per page, and only persists a cache
    that covers every page — a partial list would silently hide real species.
    """
    cached = _load_aves_cache()
    names: set[str] = set(cached or ())
    complete = False
    page = 1
    total: int | None = None
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
            while True:
                data = None
                for attempt in range(4):
                    try:
                        r = client.get(f"{BASE}/species", params={
                            "group": "Aves", "per_page": 500, "page": page,
                            "fields": "scientific_name",
                        })
                        r.raise_for_status()
                        data = r.json()
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 3:
                            raise
                        log.warning("Aves page %s failed (attempt %s): %s", page, attempt + 1, exc)
                        time.sleep(1.5 * (attempt + 1))
                results = (data or {}).get("results") or []
                total = int((data or {}).get("total") or 0)
                if not results:
                    break
                names.update(x["scientific_name"] for x in results if x.get("scientific_name"))
                if page * 500 >= total:
                    complete = True
                    break
                page += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("Aves list fetch stopped at page %s: %s", page, exc)

    if names:
        if complete:
            _save_aves_cache(names)
            log.info("loaded %d bird species (complete)", len(names))
        else:
            log.warning("Aves list incomplete: %s/%s species — keeping partial set",
                        len(names), total or "?")
    return names
