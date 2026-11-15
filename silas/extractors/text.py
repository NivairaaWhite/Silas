"""Text & structured content extraction from HTML."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from silas.utils import clean_text

# Preferred content selectors (ordered) — kept broad; site profiles add more specific ones first
CONTENT_SELECTORS = [
    "div.reading-content",
    "div.text-left",
    ".cha-words",
    "div.chr-c",
    "div.txt",
    "#chapter-content",
    ".chapter-content",
    ".reading-content",
    "div#content",
    "article.chapter",
    "article",
    ".entry-content",
    "div.content",
    "#novel-content",
    ".text-left",
    "div[itemprop='articleBody']",
    "#content",
    ".post-content",
    ".story-content",
    ".chapter-inner",
    "main",
]


@dataclass
class TextResult:
    text: Optional[str] = None
    markdown: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None
    date: Optional[str] = None
    selector: Optional[str] = None
    backend: str = "none"


def extract_with_trafilatura(html: str, url: Optional[str] = None) -> TextResult:
    try:
        import trafilatura
        plain = trafilatura.extract(html, url=url, output_format="txt", include_comments=False)
        md = trafilatura.extract(html, url=url, output_format="markdown", include_comments=False)
        meta = trafilatura.extract_metadata(html, default_url=url)
        return TextResult(
            text=plain,
            markdown=md,
            title=getattr(meta, "title", None) if meta else None,
            author=getattr(meta, "author", None) if meta else None,
            date=getattr(meta, "date", None) if meta else None,
            backend="trafilatura",
        )
    except Exception:
        return TextResult(backend="trafilatura-failed")


def extract_with_selectors(page_or_html, is_playwright: bool = True, content_selector: Optional[str] = None) -> TextResult:
    """Heuristic selector-based extraction (works for novel sites & generic pages).

    When content_selector is supplied it is tried first and accepted at a lower
    length threshold so user overrides actually win.
    """
    selectors = ([content_selector] if content_selector else []) + CONTENT_SELECTORS
    min_len = 80 if content_selector else 200  # lower bar when user forced a selector

    if is_playwright:
        for selector in selectors:
            if not selector:
                continue
            try:
                loc = page_or_html.locator(selector)
                if loc.count() == 0:
                    continue
                best = ""
                for i in range(min(loc.count(), 8)):
                    try:
                        t = loc.nth(i).inner_text(timeout=3000)
                        if len(t) > len(best):
                            best = t
                    except Exception:
                        pass
                if len(best.strip()) > min_len:
                    return TextResult(text=clean_text(best), selector=selector, backend="selector")
            except Exception:
                continue
        try:
            body = page_or_html.locator("body").inner_text(timeout=4000)
            if len(body) > 500:
                return TextResult(text=clean_text(body), selector="body", backend="body")
        except Exception:
            pass
        return TextResult(backend="selector-failed")
    else:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(page_or_html, "html.parser")
            for selector in selectors:
                if not selector:
                    continue
                nodes = soup.select(selector)
                if not nodes:
                    continue
                best = max((n.get_text(separator="\n", strip=True) for n in nodes), key=len, default="")
                if len(best) > min_len:
                    return TextResult(text=clean_text(best), selector=selector, backend="bs4-selector")
            for tag in ("article", "main"):
                node = soup.select_one(tag)
                if node:
                    t = node.get_text(separator="\n", strip=True)
                    if len(t) > 200:
                        return TextResult(text=clean_text(t), selector=tag, backend="bs4-tag")
            body = soup.body.get_text(separator="\n", strip=True) if soup.body else ""
            if len(body) > 500:
                return TextResult(text=clean_text(body), selector="body", backend="bs4-body")
        except Exception:
            pass
        return TextResult(backend="bs4-failed")


def extract_text_best(
    html: Optional[str] = None,
    page=None,
    url: Optional[str] = None,
    content_selector: Optional[str] = None,
) -> TextResult:
    """Best-effort extraction.

    Priority when a user content_selector is supplied:
      1. That selector (Playwright or BS4)
      2. Trafilatura only if selector yielded nothing useful
      3. Generic fallbacks

    Without a user selector the previous order (selectors → trafilatura → BS4) is kept.
    """
    # --- User-forced selector path ---
    if content_selector:
        if page is not None:
            r = extract_with_selectors(page, is_playwright=True, content_selector=content_selector)
            if r.text and len(r.text) > 80:
                return r
        src_html = html
        if src_html is None and page is not None:
            try:
                src_html = page.content()
            except Exception:
                src_html = None
        if src_html:
            r = extract_with_selectors(src_html, is_playwright=False, content_selector=content_selector)
            if r.text and len(r.text) > 80:
                return r
        # Fall through to trafilatura only if forced selector produced nothing

    # 1. Live Playwright selectors (best for novels)
    if page is not None and not content_selector:
        r = extract_with_selectors(page, is_playwright=True, content_selector=None)
        if r.text and len(r.text) > 250:
            return r

    # 2. Trafilatura on HTML
    src_html = html
    if src_html is None and page is not None:
        try:
            src_html = page.content()
        except Exception:
            src_html = None
    if src_html:
        r = extract_with_trafilatura(src_html, url=url)
        if r.text and len(r.text) > 200:
            # If user supplied a selector and trafilatura won, still prefer the
            # selector result if it exists (already tried above). Here we only
            # return trafilatura when no selector was forced or it failed.
            if not content_selector:
                return r
        # 3. BS4 fallback
        r2 = extract_with_selectors(src_html, is_playwright=False, content_selector=content_selector)
        if r2.text and len(r2.text) > 200:
            return r2
        if r.text:
            return r
        return r2

    return TextResult(backend="none")
