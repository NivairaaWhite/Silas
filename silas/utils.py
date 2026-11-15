"""Shared helpers: delays, text cleaning, natural sort, state, size checks, stealth UAs."""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse


# Realistic rotating User-Agents (desktop + mobile mix)
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
]


def random_ua() -> str:
    return random.choice(USER_AGENTS)


def human_delay(min_s: float = 1.5, max_s: float = 4.5) -> None:
    time.sleep(random.uniform(min_s, max_s))


def exponential_backoff(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
) -> float:
    """Compute delay for attempt (0-indexed) with optional jitter.

    delay = min(max_delay, base_delay * 2^attempt) + uniform(0.5, 1.5)
    """
    delay = min(max_delay, base_delay * (2 ** attempt))
    if jitter:
        delay += random.uniform(0.5, 1.5)
    return delay


# Noise phrases that are typically standalone UI chrome, not story content.
# Matched as whole lines or whole phrases with word boundaries to avoid
# damaging legitimate story text that happens to contain the same words.
NOISE_PHRASES = [
    "Read more at NovelLive",
    "novellive.app",
    "noveltrust.com",
    "Please report any issues",
    "Support the author",
    "Cookie Policy",
    "Accept cookies",
    "Subscribe to continue",
    "Sign in to read",
    "VIP chapter",
    "付费章节",
    "登录后阅读",
]


def clean_text(text: str) -> str:
    """Clean extracted text without destroying legitimate story content.

    - Normalise newlines
    - Remove known UI noise phrases only when they appear as whole lines
      or as whole-word matches (not inside larger sentences)
    - Collapse excessive whitespace
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        # Drop lines that are exactly a noise phrase (or pure advertisement chrome)
        lower = stripped.lower()
        if any(phrase.lower() == lower for phrase in NOISE_PHRASES):
            continue
        if lower in ("advertisement", "ads", "sponsored", "promo"):
            continue
        lines.append(re.sub(r"[ \t]+", " ", stripped))

    cleaned = "\n\n".join(lines)

    # Do NOT strip noise phrases from inside sentences — that destroys story text.
    # Only whole-line matches (handled above) are removed.

    # Final whitespace tidy
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def extract_chapter_number(url: str) -> int:
    """Legacy single-integer chapter extractor (kept for backward compat)."""
    patterns = [
        r"chapter[-_/]?(\d+)",
        r"ch[-_/]?(\d+)",
        r"/c(\d+)",
        r"/(\d+)(?:\.html?)?$",
        r"第\s*(\d+)\s*章",
        r"chap[-_]?(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, url, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 999999


def parse_volume_chapter(url: str, title: str = "") -> Tuple[int, float, int]:
    """Semantic natural-order key: (volume, chapter, subpart).

    Matches common patterns:
      vol-3-chapter-12.html        → (3, 12.0, 0)
      chapter-12-part-2.html       → (1, 12.0, 2)   # part kept as separate component
      /v2/c15                      → (2, 15.0, 0)
      第3卷 第12章                  → (3, 12.0, 0)
      chapter-12.html (no volume)  → (1, 12.0, 0)
      chapter-12-part-10           → (1, 12.0, 10)  # sorts before chapter 13
    """
    combined = f"{url} {title}".lower()
    vol = 1
    chap = 0.0
    sub = 0

    # Volume patterns
    vol_patterns = [
        r"vol(?:ume)?[-_/\s]?(\d+)",
        r"/v(\d+)[/\-_]",
        r"第\s*(\d+)\s*卷",
        r"book[-_/\s]?(\d+)",
    ]
    for pat in vol_patterns:
        m = re.search(pat, combined, re.I)
        if m:
            vol = int(m.group(1))
            break

    # Chapter patterns (prefer more specific). Avoid matching the part number.
    chap_patterns = [
        r"chapter[-_/\s]?(\d+(?:\.\d+)?)",
        r"ch[-_/\s]?(\d+(?:\.\d+)?)",
        r"/c(\d+(?:\.\d+)?)",
        r"chap[-_]?(\d+(?:\.\d+)?)",
        r"第\s*(\d+(?:\.\d+)?)\s*章",
        r"episode[-_/\s]?(\d+(?:\.\d+)?)",
    ]
    for pat in chap_patterns:
        m = re.search(pat, combined, re.I)
        if m:
            try:
                chap = float(m.group(1))
            except ValueError:
                chap = float(int(m.group(1)))
            break

    # Sub-part / part-N — keep as discrete third component so
    # chapter-12-part-10 sorts before chapter-13, not as 13.0
    part_m = re.search(r"part[-_/\s]?(\d+)|-p(\d+)(?:\.|$|/)", combined, re.I)
    if part_m:
        sub = int(part_m.group(1) or part_m.group(2))

    if chap == 0.0:
        # Fallback: last numeric path segment (but not if it was a part)
        path = urlparse(url).path.rstrip("/")
        m = re.search(r"/(\d+)(?:\.html?)?$", path)
        if m and sub == 0:
            chap = float(m.group(1))
        else:
            chap = 999999.0

    return (vol, chap, sub)


def natural_sort_key(url: str, title: str = "") -> Tuple[int, float, int]:
    """Key function for sorted(..., key=natural_sort_key)."""
    return parse_volume_chapter(url, title)


def same_domain(url: str, base: str) -> bool:
    try:
        a = urlparse(url).netloc.lower().lstrip("www.")
        b = urlparse(base).netloc.lower().lstrip("www.")
        return a == b or a.endswith("." + b) or b.endswith("." + a)
    except Exception:
        return False


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"completed": [], "failed": [], "last_url": None}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def append_failed_queue(path: Path, url: str, error: str, attempt: int = 0) -> None:
    """Append a failed chapter to the dead-letter queue JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    queue: list = []
    if path.exists():
        try:
            queue = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            queue = []
    # Dedup by URL — keep latest error
    queue = [e for e in queue if e.get("url") != url]
    queue.append({"url": url, "error": str(error)[:500], "attempt": attempt, "ts": time.time()})
    path.write_text(json.dumps(queue, indent=2), encoding="utf-8")


def load_failed_queue(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def over_storage_limit(path: Path, max_mb: Optional[float]) -> bool:
    if max_mb is None:
        return False
    return dir_size_mb(path) >= max_mb


def escape_xml(text: str) -> str:
    """Escape characters that break XML/EPUB parsing."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
