"""Tiered HTTP client: curl_cffi (JA3/JA4 impersonation) → requests → Playwright.

Tier 1 is preferred for speed and anti-bot resilience without browser RAM cost.
Strict status-code handling prevents error pages from being treated as content.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from silas.config import Settings
from silas.utils import random_ua

logger = logging.getLogger(__name__)

HAS_CURL_CFFI = False
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    pass

import requests as std_requests


# Browser impersonation targets for curl_cffi (rotates lightly)
_IMPERSONATE_TARGETS = [
    "chrome131",
    "chrome124",
    "chrome120",
    "safari17_0",
    "edge101",
]


def _pick_impersonate() -> str:
    import random
    return random.choice(_IMPERSONATE_TARGETS)


# Status codes that indicate a hard block / rate-limit; never treat body as content.
_HARD_BLOCK = {403, 429, 503, 502, 504, 520, 521, 522, 523, 524}

# Soft error pages we still reject even if body is long.
_SOFT_ERROR = {404, 410, 451, 500, 501}


def _is_acceptable_status(code: int) -> bool:
    return 200 <= code < 300


def fetch_html(
    url: str,
    settings: Settings,
    *,
    timeout: float = 40.0,
    prefer_browser_fingerprint: bool = True,
) -> Tuple[Optional[str], str, Optional[Exception]]:
    """Fetch page HTML via best available tier.

    Returns (html, backend_name, error).
    backend_name is one of: curl_cffi | requests | none

    Only 2xx responses with a body are accepted. Non-2xx (including 404/500
    pages with long bodies) are rejected so error pages are never saved as chapters.
    """
    headers = {
        "User-Agent": random_ua() if settings.rotate_ua else settings.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if settings.extra_headers:
        headers.update(settings.extra_headers)

    proxies = None
    if settings.proxy:
        proxies = {"http": settings.proxy, "https": settings.proxy}

    # --- Tier 1: curl_cffi (TLS fingerprint impersonation) ---
    if HAS_CURL_CFFI and prefer_browser_fingerprint and getattr(settings, "prefer_http", True):
        try:
            impersonate = _pick_impersonate()
            r = curl_requests.get(
                url,
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                impersonate=impersonate,
                allow_redirects=True,
            )
            if _is_acceptable_status(r.status_code) and r.text and len(r.text) > 200:
                return r.text, f"curl_cffi/{impersonate}", None
            if r.status_code in _HARD_BLOCK:
                logger.debug("curl_cffi hard-block %s for %s", r.status_code, url)
                return None, f"curl_cffi/{impersonate}", Exception(f"HTTP {r.status_code}")
            if r.status_code in _SOFT_ERROR:
                logger.debug("curl_cffi soft-error %s for %s", r.status_code, url)
                return None, f"curl_cffi/{impersonate}", Exception(f"HTTP {r.status_code}")
            # Any other non-2xx is also rejected
            if not _is_acceptable_status(r.status_code):
                return None, f"curl_cffi/{impersonate}", Exception(f"HTTP {r.status_code}")
        except Exception as e:
            logger.debug("curl_cffi failed for %s: %s", url, e)

    # --- Tier 2: standard requests ---
    try:
        r = std_requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=True,
        )
        if _is_acceptable_status(r.status_code) and r.text and len(r.text) > 200:
            return r.text, "requests", None
        r.raise_for_status()  # will raise for non-2xx
        if r.text and len(r.text) > 200:
            return r.text, "requests", None
    except Exception as e:
        logger.debug("requests failed for %s: %s", url, e)
        return None, "none", e

    return None, "none", Exception("empty or non-2xx response")


def fetch_with_retries(
    url: str,
    settings: Settings,
    *,
    max_attempts: int = 4,
    timeout: float = 40.0,
) -> Tuple[Optional[str], str, Optional[Exception]]:
    """Fetch with exponential backoff + jitter on transient failures."""
    from silas.utils import exponential_backoff
    import time

    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        html, backend, err = fetch_html(url, settings, timeout=timeout)
        if html:
            return html, backend, None
        last_err = err
        # Do not retry hard blocks
        if err and any(code in str(err) for code in ("403", "429", "451")):
            break
        if attempt < max_attempts - 1:
            delay = exponential_backoff(attempt, base_delay=1.5, max_delay=45.0)
            logger.info("Retry %d/%d for %s after %.1fs", attempt + 1, max_attempts, url, delay)
            time.sleep(delay)
    return None, "none", last_err
