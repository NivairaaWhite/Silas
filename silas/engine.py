"""Core orchestration: route by mode and run the appropriate pipeline."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from silas.browser.manager import browser_context, wait_for_human, HAS_PLAYWRIGHT, BrowserSession
from silas.browser.robots import allowed_by_robots
from silas.config import Settings
from silas.extractors.document import parse_document
from silas.extractors.media import discover_media_playwright, discover_media_html, download_media
from silas.extractors.multimodal import enrich_assets, capabilities as mm_caps
from silas.extractors.novel import (
    harvest_toc_playwright,
    harvest_toc_requests,
    download_chapters,
    export_epub,
    export_jsonl,
)
from silas.extractors.text import extract_text_best
from silas.utils import dir_size_mb, human_delay, over_storage_limit, random_ua

logger = logging.getLogger(__name__)

DOCUMENT_EXTS = (".pdf", ".docx", ".doc", ".pptx", ".xlsx")


def _looks_like_document(url: str) -> bool:
    return urlparse(url).path.lower().endswith(DOCUMENT_EXTS)


def run_page(settings: Settings, url: str) -> Dict[str, Any]:
    """Single-page extraction: text + optional media + multimodal enrichment."""
    out: Dict[str, Any] = {"url": url, "mode": "page"}
    output_dir = settings.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    if settings.fallback_only or not HAS_PLAYWRIGHT:
        return _run_page_requests(settings, url, out)

    with browser_context(settings) as (ctx, page):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
            if not wait_for_human(page, auto=settings.auto):
                out["error"] = "security_challenge"
                return out
            html = page.content()
            text_result = extract_text_best(html=html, page=page, url=url, content_selector=settings.content_selector)
            out["text"] = {
                "text": text_result.text,
                "markdown": text_result.markdown,
                "title": text_result.title,
                "author": text_result.author,
                "date": text_result.date,
                "backend": text_result.backend,
            }
            media_refs = discover_media_playwright(page)
            out["discovered_media"] = media_refs
            if media_refs and (settings.download_images or settings.download_video or settings.download_audio):
                assets = download_media(media_refs, settings, output_dir / "media", base_url=url)
                if settings.enrich_media and assets:
                    assets = enrich_assets(assets)
                out["downloaded_media"] = assets
            if text_result.text:
                (output_dir / "page.txt").write_text(text_result.text, encoding="utf-8")
            if text_result.markdown:
                (output_dir / "page.md").write_text(text_result.markdown, encoding="utf-8")
            (output_dir / "result.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        except Exception as e:
            out["error"] = str(e)
            logger.exception("page mode failed")
    return out


def _run_page_requests(settings: Settings, url: str, out: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import requests
        headers = {"User-Agent": settings.user_agent}
        headers.update(settings.extra_headers or {})
        proxies = {"http": settings.proxy, "https": settings.proxy} if settings.proxy else None
        r = requests.get(url, timeout=40, headers=headers, proxies=proxies)
        r.raise_for_status()
        html = r.text
        text_result = extract_text_best(html=html, url=url, content_selector=settings.content_selector)
        out["text"] = {
            "text": text_result.text,
            "markdown": text_result.markdown,
            "title": text_result.title,
            "backend": text_result.backend,
        }
        media_refs = discover_media_html(html, url)
        out["discovered_media"] = media_refs
        if media_refs and (settings.download_images or settings.download_video or settings.download_audio):
            assets = download_media(media_refs, settings, settings.output_dir / "media", base_url=url)
            if settings.enrich_media and assets:
                assets = enrich_assets(assets)
            out["downloaded_media"] = assets
        if text_result.text:
            (settings.output_dir / "page.txt").write_text(text_result.text, encoding="utf-8")
        (settings.output_dir / "result.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        out["error"] = str(e)
    return out


def run_novel(settings: Settings, url: str) -> Dict[str, Any]:
    """Full novel / multi-chapter scrape with deep TOC pagination.

    v1.4: HTTP-first chapter fetch, recoverable browser sessions, natural TOC sort,
    DLQ retry, optional EPUB/JSONL export.
    """
    out: Dict[str, Any] = {"url": url, "mode": "novel"}
    output_dir = settings.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    master = settings.master_file or (output_dir / "novel_full.txt")

    chapter_urls: List[str] = []

    # --- HTTP-only path ---
    if settings.fallback_only or not HAS_PLAYWRIGHT:
        chapter_urls = harvest_toc_requests(url, settings)
        out["chapters_found"] = len(chapter_urls)
        if settings.list_only:
            out["chapter_urls"] = chapter_urls
            print(f"\nFound {len(chapter_urls)} chapters")
            for i, u in enumerate(chapter_urls, 1):
                print(f"  {i:4d}. {u}")
            return out
        print("[*] Novel mode (HTTP-only) — Playwright recommended for JS-heavy sites.")
        saved = download_chapters(None, chapter_urls, settings, output_dir, master, session=None)
        out["chapters_saved"] = saved
        out["master_file"] = str(master)
        out["size_mb"] = round(dir_size_mb(output_dir), 2)
        _run_exports(settings, url, out)
        return out

    # --- Browser path with recoverable session ---
    session = BrowserSession(settings)
    try:
        ctx, page = session.start()
        chapter_urls = harvest_toc_playwright(page, url, settings)
        out["chapters_found"] = len(chapter_urls)

        if settings.list_only:
            out["chapter_urls"] = chapter_urls
            print(f"\nFound {len(chapter_urls)} chapters")
            for i, u in enumerate(chapter_urls, 1):
                print(f"  {i:4d}. {u}")
            return out

        if not chapter_urls:
            out["error"] = "no_chapters_found"
            print("Critical: no chapter links found.")
            print("  Tip: try --toc-selector 'a.chapter-link' or open the Catalog page URL.")
            return out

        print(f"\n{'=' * 72}")
        print(f"Total chapters: {len(chapter_urls)}")
        if settings.max_chapters:
            print(f"Limiting to first {settings.max_chapters}")
        print(f"{'=' * 72}\n")

        saved = download_chapters(page, chapter_urls, settings, output_dir, master, session=session)
        out["chapters_saved"] = saved
        out["master_file"] = str(master)
        out["size_mb"] = round(dir_size_mb(output_dir), 2)
        _run_exports(settings, url, out)
    except Exception as e:
        out["error"] = str(e)
        logger.exception("novel mode failed")
    finally:
        session.close()

    return out


def _run_exports(settings: Settings, url: str, out: Dict[str, Any]) -> None:
    """Optional EPUB / JSONL packaging."""
    title = settings.novel_title or "Novel"
    author = settings.novel_author or "Unknown"
    if settings.export_epub:
        path = export_epub(settings.output_dir, title=title, author=author)
        if path:
            out["epub"] = str(path)
    if settings.export_jsonl:
        path = export_jsonl(settings.output_dir, novel_title=title, source_url=url)
        if path:
            out["jsonl"] = str(path)


def run_document(settings: Settings, url: str) -> Dict[str, Any]:
    """Download and parse a PDF / Office document. Falls back to Playwright on 403."""
    out: Dict[str, Any] = {"url": url, "mode": "document"}
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    data: Optional[bytes] = None

    # 1. Try plain requests first
    try:
        import requests
        headers = {"User-Agent": settings.user_agent}
        headers.update(settings.extra_headers or {})
        proxies = {"http": settings.proxy, "https": settings.proxy} if settings.proxy else None
        r = requests.get(url, timeout=60, headers=headers, proxies=proxies)
        r.raise_for_status()
        data = r.content
    except Exception as e:
        out["requests_error"] = str(e)
        print(f"[!] requests failed ({e}); trying Playwright…")

    # 2. Playwright fallback (handles 403 / anti-bot)
    if data is None and HAS_PLAYWRIGHT and not settings.fallback_only:
        try:
            with browser_context(settings) as (ctx, page):
                resp = page.goto(url, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
                if not wait_for_human(page, auto=settings.auto):
                    out["error"] = "security_challenge"
                    return out
                # Prefer network response body if available
                if resp:
                    try:
                        data = resp.body()
                    except Exception:
                        data = None
                if not data:
                    # last resort: save page content (not ideal for binary PDF)
                    data = page.content().encode("utf-8", errors="replace")
        except Exception as e:
            out["error"] = str(e)
            return out

    if not data:
        out["error"] = out.get("requests_error") or "download_failed"
        return out

    if len(data) > settings.max_image_bytes * 6:
        out["error"] = "document_too_large"
        return out

    result = parse_document(data, filename=urlparse(url).path)
    out["document"] = {
        "backend": result.backend,
        "page_count": result.page_count,
        "text_preview": (result.text or "")[:500],
        "chars": len(result.text or ""),
    }
    if result.text:
        (settings.output_dir / "document.txt").write_text(result.text, encoding="utf-8")
    if result.markdown:
        (settings.output_dir / "document.md").write_text(result.markdown, encoding="utf-8")
    # also keep original bytes when it looks like a PDF
    if data[:4] == b"%PDF":
        (settings.output_dir / "original.pdf").write_bytes(data)
    (settings.output_dir / "result.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def run_media(settings: Settings, url: str) -> Dict[str, Any]:
    """Focus on media discovery + download + enrichment."""
    if not (settings.download_images or settings.download_video or settings.download_audio):
        settings.download_images = True
    return run_page(settings, url)


def run_all(settings: Settings, url: str) -> Dict[str, Any]:
    """Best-effort everything: document if applicable, else page + media."""
    if _looks_like_document(url):
        return run_document(settings, url)
    settings.download_images = True
    return run_page(settings, url)


def run(settings: Settings, url: str, mode: str = "auto") -> Dict[str, Any]:
    mode = (mode or "auto").lower()
    if settings.rotate_ua:
        settings.user_agent = random_ua()

    if not settings.ignore_robots and settings.respect_robots:
        if not allowed_by_robots(url, settings.user_agent):
            print("[!] robots.txt disallows this URL for the configured User-Agent.")
            print("    Aborting. Pass --ignore-robots to override (use only on sites you control or have permission for).")
            return {"url": url, "error": "robots_disallowed"}

    print("=" * 72)
    print("  SILAS  ·  universal extractor  v1.2")
    print("=" * 72)
    print(f"  URL    : {url}")
    print(f"  Mode   : {mode}")
    print(f"  Output : {settings.output_dir}")
    print(f"  Playwright : {HAS_PLAYWRIGHT}")
    print(f"  Multimodal : {mm_caps()}")
    print(f"  Stealth    : {settings.stealth}  |  UA rotate: {settings.rotate_ua}")
    if settings.proxy:
        print(f"  Proxy  : {settings.proxy}")
    if settings.max_chapters:
        print(f"  Max chapters : {settings.max_chapters}")
    if settings.max_total_mb:
        print(f"  Max size     : {settings.max_total_mb} MB")
    print("=" * 72)

    if mode == "auto":
        if _looks_like_document(url):
            mode = "document"
        else:
            mode = "page"

    if mode == "novel":
        return run_novel(settings, url)
    if mode == "document":
        return run_document(settings, url)
    if mode == "media":
        return run_media(settings, url)
    if mode == "all":
        return run_all(settings, url)
    return run_page(settings, url)
