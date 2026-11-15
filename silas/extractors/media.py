"""Discover + download images / video / audio with strict storage limits."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from silas.config import Settings
from silas.utils import same_domain

logger = logging.getLogger(__name__)

MEDIA_JS = (
    "els => els.map(e => ({url: e.currentSrc || e.src || e.href || e.getAttribute('data-src'), "
    "type: e.tagName.toLowerCase()})).filter(x => x.url).slice(0, 800)"
)


def discover_media_playwright(page) -> List[dict]:
    try:
        raw = page.locator("video, audio, source, img[src], img[data-src], a[href$='.mp4'], a[href$='.webm']").evaluate_all(MEDIA_JS)
        return [{"url": r["url"], "type": r.get("type", "media")} for r in raw if r.get("url")]
    except Exception as exc:
        logger.warning("Media discovery failed: %s", exc)
        return []


def discover_media_html(html: str, base_url: str) -> List[dict]:
    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        soup = BeautifulSoup(html, "html.parser")
        found = []
        for tag in soup.find_all(["img", "video", "audio", "source"]):
            src = tag.get("src") or tag.get("data-src") or tag.get("data-original")
            if src:
                found.append({"url": urljoin(base_url, src), "type": tag.name})
        return found[:800]
    except Exception:
        return []


def _classify(tag: str, content_type: str, url: str) -> Optional[str]:
    ct = (content_type or "").split(";")[0].strip().lower()
    tag = (tag or "").lower()
    lower = url.lower()
    if ct.startswith("image/") or tag == "img" or any(lower.endswith(e) for e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif")):
        return "image"
    if ct.startswith("video/") or tag == "video" or any(lower.endswith(e) for e in (".mp4", ".webm", ".mkv", ".mov", ".m3u8")):
        return "video"
    if ct.startswith("audio/") or tag == "audio" or any(lower.endswith(e) for e in (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac")):
        return "audio"
    return None


def download_media(
    media_refs: List[dict],
    settings: Settings,
    dest_dir: Path,
    base_url: str = "",
) -> List[dict]:
    """Download allowed media kinds under count + size caps. Returns asset dicts."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    allowed = set()
    if settings.download_images:
        allowed.add("image")
    if settings.download_video:
        allowed.add("video")
    if settings.download_audio:
        allowed.add("audio")
    if not allowed:
        return []

    limits = {
        "image": (settings.max_images, settings.max_image_bytes),
        "video": (settings.max_videos, settings.max_video_bytes),
        "audio": (settings.max_audios, settings.max_audio_bytes),
    }
    counters = {k: 0 for k in allowed}
    results: List[dict] = []

    proxies = settings.proxy if settings.proxy else None
    with httpx.Client(
        timeout=45.0,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
        proxy=proxies,
    ) as client:
        for item in media_refs:
            url = item.get("url")
            if not url or not url.startswith("http"):
                continue
            try:
                # HEAD first when possible to avoid loading huge files into memory
                try:
                    head = client.head(url)
                    cl = head.headers.get("content-length")
                    if cl and int(cl) > max(limits.get(k, (0, 0))[1] for k in ("image", "video", "audio")):
                        continue
                except Exception:
                    pass
                response = client.get(url)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                kind = _classify(item.get("type", ""), content_type, url)
                if kind is None or kind not in allowed:
                    continue
                max_count, max_bytes = limits[kind]
                if counters[kind] >= max_count:
                    continue
                # Stream into memory only after size is known / acceptable
                data = response.content
                if not data or len(data) > max_bytes:
                    continue

                checksum = hashlib.sha256(data).hexdigest()[:16]
                ext = {
                    "image": ".jpg",
                    "video": ".mp4",
                    "audio": ".mp3",
                }.get(kind, ".bin")
                if "png" in content_type:
                    ext = ".png"
                elif "webp" in content_type:
                    ext = ".webp"
                elif "gif" in content_type:
                    ext = ".gif"
                elif "webm" in content_type:
                    ext = ".webm"
                elif "wav" in content_type:
                    ext = ".wav"
                elif "ogg" in content_type:
                    ext = ".ogg"

                folder = dest_dir / (kind + "s")
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{checksum}{ext}"
                path.write_bytes(data)

                results.append({
                    "original_url": url,
                    "path": str(path),
                    "content_type": content_type or f"{kind}/*",
                    "size_bytes": len(data),
                    "asset_type": kind,
                    "checksum": checksum,
                })
                counters[kind] += 1
            except Exception as exc:
                logger.debug("Skip media %s: %s", url, exc)

    return results
