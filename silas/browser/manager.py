"""Playwright browser lifecycle + Cloudflare / challenge detection + stealth.

Improvements in 1.4:
  - Isolated context factory that can be recreated after TargetClosedError
  - Clean recovery hooks so a single navigation failure does not kill the whole job
  - Stronger stealth init scripts
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Generator, Optional, Tuple

from silas.config import Settings
from silas.utils import human_delay, random_ua

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext, Playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    Page = object  # type: ignore
    BrowserContext = object  # type: ignore
    Playwright = object  # type: ignore


# Strong title-level indicators (any one is enough)
SECURITY_TITLE_INDICATORS = [
    "just a moment",
    "security verification",
    "verify you are human",
    "checking your browser",
    "attention required",
    "are you a robot",
    "please complete the security check",
    "access denied",
]

# Body indicators that are strong only when combined with challenge UI elements
SECURITY_BODY_PHRASES = [
    "cf-browser-verification",
    "enable javascript and cookies",
    "checking if the site connection is secure",
    "please wait while we verify",
    "ddos protection by cloudflare",
]


def is_security_page(page: Page) -> bool:
    """Return True only when the page looks like an active challenge interstitial.

    Mere mention of the words "captcha", "cloudflare" or "recaptcha" inside
    story text must not trigger a 5-minute wait.
    """
    try:
        title = (page.title() or "").lower()
        if any(x in title for x in SECURITY_TITLE_INDICATORS):
            return True

        # Look for common challenge DOM markers
        for sel in (
            "#challenge-form",
            "#cf-challenge-running",
            ".cf-browser-verification",
            "iframe[src*='captcha']",
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "div.g-recaptcha",
            "#turnstile-wrapper",
            "[data-sitekey]",
        ):
            try:
                if page.locator(sel).count() > 0:
                    return True
            except Exception:
                pass

        body = page.locator("body").inner_text(timeout=2500).lower()[:3000]
        # Only treat body phrases as challenge if the page is short (interstitial)
        if len(body) < 1500 and any(x in body for x in SECURITY_BODY_PHRASES):
            return True
        return False
    except Exception:
        return False


def wait_for_human(page: Page, timeout: int = 300, auto: bool = False) -> bool:
    """Pause so the operator can solve CAPTCHA / Cloudflare. Returns True if cleared."""
    if not is_security_page(page):
        return True
    if auto:
        print("[!] Security challenge detected and --auto is set; cannot proceed.")
        return False
    print("\n[!] SECURITY / CLOUDFLARE DETECTED")
    print("    → Solve the challenge in the browser window.")
    print(f"    → Waiting up to {timeout}s …")
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(2)
        try:
            if not is_security_page(page):
                print("[+] Verification cleared.")
                human_delay(1.5, 3.0)
                return True
        except Exception:
            # page may have navigated away
            return True
    print("[!] Timed out waiting for verification.")
    return False


def _is_target_closed(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return (
        "target page, context or browser has been closed" in msg
        or "targetclosederror" in msg
        or "browser has been closed" in msg
        or "context has been closed" in msg
    )


class BrowserSession:
    """Manages a recoverable Playwright persistent context.

    If a navigation raises TargetClosedError the session can recreate the
    context without aborting the entire job.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._pw: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._closed = False

    def start(self) -> Tuple[BrowserContext, Page]:
        if not HAS_PLAYWRIGHT:
            raise RuntimeError(
                "Playwright is not installed. pip install playwright && playwright install chromium"
            )
        self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
        ua = random_ua() if self.settings.rotate_ua else self.settings.user_agent

        launch_kwargs = {
            "user_data_dir": str(self.settings.profile_dir),
            "headless": self.settings.headless,
            "viewport": {
                "width": self.settings.viewport_width,
                "height": self.settings.viewport_height,
            },
            "user_agent": ua,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                # --no-sandbox only when explicitly requested (e.g. restricted containers)
                *(["--no-sandbox"] if __import__("os").environ.get("SILAS_NO_SANDBOX") == "1" else []),
                "--disable-infobars",
                "--window-position=0,0",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if self.settings.proxy:
            launch_kwargs["proxy"] = {"server": self.settings.proxy}

        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)

        if self.settings.stealth:
            try:
                self._context.add_init_script(STEALTH_JS)
            except Exception:
                pass

        headers = {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        if self.settings.extra_headers:
            headers.update(self.settings.extra_headers)
        try:
            self._context.set_extra_http_headers(headers)
        except Exception:
            pass

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._closed = False
        return self._context, self._page

    def recreate(self) -> Tuple[BrowserContext, Page]:
        """Discard the dead context and start a fresh one."""
        print("[*] Browser context closed unexpectedly — recreating…")
        self.close()
        return self.start()

    def ensure_alive(self) -> Tuple[BrowserContext, Page]:
        """Return a live context/page, recreating if necessary."""
        if self._closed or self._context is None or self._page is None:
            return self.start()
        try:
            # Lightweight health check
            _ = self._page.url
            return self._context, self._page
        except Exception as e:
            if _is_target_closed(e):
                return self.recreate()
            raise

    def safe_goto(self, url: str, **kwargs) -> Optional[Page]:
        """Navigate with automatic context recovery on TargetClosedError."""
        ctx, page = self.ensure_alive()
        timeout = kwargs.pop("timeout", self.settings.nav_timeout_ms)
        wait_until = kwargs.pop("wait_until", "domcontentloaded")
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout, **kwargs)
            return page
        except Exception as e:
            if _is_target_closed(e):
                ctx, page = self.recreate()
                try:
                    page.goto(url, wait_until=wait_until, timeout=timeout, **kwargs)
                    return page
                except Exception as e2:
                    print(f"    Navigation failed after recreate: {e2}")
                    return None
            print(f"    Navigation error: {e}")
            return None

    def close(self) -> None:
        self._closed = True
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._pw = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.close()


@contextmanager
def browser_context(settings: Settings) -> Generator[tuple[BrowserContext, Page], None, None]:
    """Backward-compatible context manager used by engine.py."""
    session = BrowserSession(settings)
    try:
        ctx, page = session.start()
        yield ctx, page
    finally:
        session.close()
