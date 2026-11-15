"""Lightweight robots.txt check (polite by default)."""
from __future__ import annotations

import logging
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)


def allowed_by_robots(url: str, user_agent: str = "*") -> bool:
    """Return True if robots.txt allows fetching `url` for `user_agent`.

    On any network / parse failure we allow the request (fail-open) so a
    temporarily unreachable robots.txt does not block the whole job.
    A 403 on robots.txt itself is treated as "no robots policy" (allow)
    rather than the overly-strict "disallow everything" that some parsers use.
    """
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        # Use a short timeout via urllib; RobotFileParser itself has no timeout
        # so we pre-fetch with requests/curl if available, else fall back.
        try:
            import urllib.request
            req = urllib.request.Request(
                robots_url,
                headers={"User-Agent": user_agent or "*"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 403:
                    logger.debug("robots.txt returned 403 for %s — treating as allow", robots_url)
                    return True
                content = resp.read().decode("utf-8", errors="replace")
            rp.parse(content.splitlines())
        except Exception as fetch_err:
            # Network failure or non-HTTP scheme → allow
            logger.debug("robots.txt fetch failed for %s: %s — allowing", robots_url, fetch_err)
            return True
        return rp.can_fetch(user_agent or "*", url)
    except Exception as exc:
        logger.debug("robots.txt check failed for %s: %s — allowing by default", url, exc)
        return True
