"""Deep paginated TOC harvest + sequential chapter extraction.

Adaptive: auto-detects site architecture (Madara, RoyalRoad, Webnovel-style,
WordPress, generic) via site_profiles, falls back to dense-link heuristics,
and accepts user CSS overrides. Optional lightweight LLM assist when configured.

v1.4 upgrades:
  - Semantic volume/chapter natural sorting (fixes scrambled multi-volume TOCs)
  - Tiered chapter fetch: curl_cffi → requests → Playwright
  - Exponential backoff + Dead-Letter Queue for failed chapters
  - Automatic DLQ retry pass at end of job
  - EPUB + JSONL export hooks
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from silas.browser.manager import wait_for_human, BrowserSession, HAS_PLAYWRIGHT, _is_target_closed
from silas.config import Settings
from silas.extractors.site_profiles import (
    GENERIC_NOVEL,
    SiteProfile,
    detect_profile_from_html,
    detect_profile_from_url,
    merge_selectors,
)
from silas.extractors.text import extract_text_best
from silas.http_client import fetch_html, fetch_with_retries, HAS_CURL_CFFI
from silas.utils import (
    clean_text,
    extract_chapter_number,
    natural_sort_key,
    parse_volume_chapter,
    human_delay,
    exponential_backoff,
    load_state,
    save_state,
    append_failed_queue,
    load_failed_queue,
    over_storage_limit,
    same_domain,
)

logger = logging.getLogger(__name__)

# Broad patterns (augmented by profile.extra_href_patterns at runtime)
BASE_CHAPTER_HREF_PATTERNS = [
    r"chapter",
    r"/ch[-_/]?\d",
    r"/c\d",
    r"chap[-_]?\d",
    r"第\s*\d+\s*章",
    r"/read/",
    r"/content/",
    r"/novel/",
    r"/book/",
    r"/text/",
    r"/story/",
    r"episode",
    r"part[-_]?\d",
    r"/p/\d+",
    r"chapters?/\d+",
]


def _looks_like_chapter(
    href: str,
    text: str = "",
    extra_patterns: Optional[List[str]] = None,
) -> bool:
    h = (href or "").lower()
    t = (text or "").lower()
    patterns = BASE_CHAPTER_HREF_PATTERNS + (extra_patterns or [])
    if any(re.search(p, h, re.I) for p in patterns):
        return True
    if re.search(r"第\s*\d+\s*章", t) or re.search(r"chapter\s*\d+", t, re.I):
        return True
    if re.search(r"^\s*ch\.?\s*\d+", t, re.I) or re.search(r"^\s*\d+\s*[\.\:\-]", t):
        return True
    # Numeric-only last path segment often used by Chinese sites
    path = urlparse(href).path.rstrip("/")
    if re.search(r"/\d+$", path) and len(path) > 5:
        return True
    return False


def _try_click_toc(page, button_texts: List[str]) -> bool:
    """Try to open a Catalog / 目录 tab if present."""
    for label in button_texts:
        try:
            loc = page.get_by_role("link", name=re.compile(label, re.I))
            if loc.count() > 0:
                loc.first.click(timeout=3000)
                human_delay(0.8, 1.5)
                return True
        except Exception:
            pass
        try:
            loc = page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I))
            if loc.count() > 0:
                loc.first.click(timeout=3000)
                human_delay(0.8, 1.5)
                return True
        except Exception:
            pass
        try:
            loc = page.get_by_role("button", name=re.compile(label, re.I))
            if loc.count() > 0:
                loc.first.click(timeout=3000)
                human_delay(0.8, 1.5)
                return True
        except Exception:
            pass
    return False


def _score_link_cluster(items: List[Dict[str, str]], base_url: str, extra_patterns: List[str]) -> List[str]:
    """Score candidate links by density of chapter-like patterns and return ordered URLs."""
    candidates: List[Tuple[float, str, str]] = []
    for item in items:
        href = item.get("href") or ""
        text = (item.get("text") or "").strip()
        if not href or href.startswith("javascript:") or href.endswith("#"):
            continue
        if not same_domain(href, base_url):
            continue
        score = 0.0
        if _looks_like_chapter(href, text, extra_patterns):
            score += 2.0
        if re.search(r"\d+", text):
            score += 0.8
        if len(text) > 3 and len(text) < 120:
            score += 0.4
        path = urlparse(href).path
        if re.search(r"/\d+/?$", path):
            score += 0.6
        if score >= 1.5:
            candidates.append((score, href, text))
    # Dedup preserve highest score
    seen: Set[str] = set()
    scored: List[Tuple[str, str]] = []
    for _, href, text in sorted(candidates, key=lambda x: -x[0]):
        if href not in seen:
            seen.add(href)
            scored.append((href, text))
    # Semantic natural sort by volume/chapter
    scored.sort(key=lambda pair: natural_sort_key(pair[0], pair[1]))
    return [h for h, _ in scored]


def _collect_links_playwright(page, selectors: List[str], limit: int = 3000) -> List[Dict[str, str]]:
    """Collect href+text from the first matching selector group, else all anchors."""
    for sel in selectors:
        if not sel:
            continue
        try:
            els = page.locator(sel).evaluate_all(
                "els => els.map(e => ({href: e.href || e.getAttribute('href'), "
                "text: (e.innerText||'').slice(0,120)}))"
            )
            if els and len(els) >= 3:
                return els[:limit]
        except Exception:
            continue
    try:
        return page.locator("a[href]").evaluate_all(
            "els => els.map(e => ({href: e.href, text: (e.innerText||'').slice(0,120)})).slice(0, %d)"
            % limit
        ) or []
    except Exception:
        return []


def _optional_llm_toc_hint(html: str, settings: Settings) -> Optional[Dict[str, str]]:
    """Lightweight LLM call (OpenAI-compatible or Ollama) to suggest selectors."""
    if not settings.llm_provider:
        return None
    try:
        snippet = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.I)
        snippet = re.sub(r"<style[\s\S]*?</style>", "", snippet, flags=re.I)
        snippet = re.sub(r"\s+", " ", snippet)[:6000]

        prompt = (
            "You are helping a web scraper. Given this HTML skeleton of a novel/table-of-contents page, "
            "reply with ONLY a JSON object containing:\n"
            '  "toc_selector": best CSS selector for chapter links (or null),\n'
            '  "content_selector": best CSS selector for chapter body text (or null),\n'
            '  "notes": short reason\n'
            "Do not invent selectors that are unlikely. Prefer specific class/id over generic tags.\n\n"
            f"HTML:\n{snippet}"
        )

        if settings.llm_provider in ("openai", "ollama", "anthropic"):
            import os
            import json as _json
            if settings.llm_provider == "ollama":
                import requests
                r = requests.post(
                    "http://localhost:11434/api/generate",
                    json={"model": os.getenv("SILAS_OLLAMA_MODEL", "llama3.2"), "prompt": prompt, "stream": False},
                    timeout=45,
                )
                text = r.json().get("response", "")
            elif settings.llm_provider == "anthropic":
                try:
                    import anthropic
                    client = anthropic.Anthropic()
                    resp = client.messages.create(
                        model=os.getenv("SILAS_LLM_MODEL", "claude-3-haiku-20240307"),
                        max_tokens=300,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    text = resp.content[0].text if resp.content else ""
                except ImportError:
                    logger.warning("anthropic package not installed; falling back to OpenAI-compatible client")
                    from openai import OpenAI
                    client = OpenAI(base_url=os.getenv("ANTHROPIC_BASE_URL"), api_key=os.getenv("ANTHROPIC_API_KEY"))
                    resp = client.chat.completions.create(
                        model=os.getenv("SILAS_LLM_MODEL", "claude-3-haiku-20240307"),
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.1,
                        max_tokens=300,
                    )
                    text = resp.choices[0].message.content or ""
            else:  # openai
                from openai import OpenAI
                client = OpenAI()
                resp = client.chat.completions.create(
                    model=os.getenv("SILAS_LLM_MODEL", "gpt-4o-mini"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=300,
                )
                text = resp.choices[0].message.content or ""
            m = re.search(r"\{[\s\S]*\}", text)
            if m:
                return _json.loads(m.group(0))
    except Exception as e:
        logger.debug("LLM TOC hint failed: %s", e)
    return None


def _optional_llm_toc_order(html: str, settings: Settings) -> Optional[List[Dict[str, Any]]]:
    """Ask LLM to reconstruct ordered TOC from raw HTML fragment.

    Returns list of {volume, chapter, title, url} or None.
    """
    if not settings.llm_provider:
        return None
    try:
        snippet = re.sub(r"<script[\s\S]*?</script>", "", html, flags=re.I)
        snippet = re.sub(r"<style[\s\S]*?</style>", "", snippet, flags=re.I)
        snippet = re.sub(r"\s+", " ", snippet)[:8000]

        prompt = (
            "Extract an ordered table of contents from this novel page HTML. "
            "Reply with ONLY a JSON array of objects, each with keys: "
            '"volume" (int, default 1), "chapter" (float), "title" (str), "url" (str absolute or relative). '
            "Preserve reading order. Skip non-chapter links.\n\n"
            f"HTML:\n{snippet}"
        )
        import os
        import json as _json
        if settings.llm_provider == "ollama":
            import requests
            r = requests.post(
                "http://localhost:11434/api/generate",
                json={"model": os.getenv("SILAS_OLLAMA_MODEL", "llama3.2"), "prompt": prompt, "stream": False},
                timeout=60,
            )
            text = r.json().get("response", "")
        else:
            from openai import OpenAI
            client = OpenAI()
            resp = client.chat.completions.create(
                model=os.getenv("SILAS_LLM_MODEL", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=2000,
            )
            text = resp.choices[0].message.content or ""
        m = re.search(r"\[[\s\S]*\]", text)
        if m:
            data = _json.loads(m.group(0))
            if isinstance(data, list) and data:
                return data
    except Exception as e:
        logger.debug("LLM TOC order failed: %s", e)
    return None


def harvest_toc_playwright(page, base_url: str, settings: Settings) -> List[str]:
    """Crawl paginated table-of-contents until no new chapter links appear.

    Adaptive pipeline with semantic natural sorting.
    """
    print("\n[*] Harvesting Table of Contents (Playwright, adaptive)…")
    # Store (url, title) so we can natural-sort later
    seen_map: Dict[str, str] = {}  # url → title
    page_num = 1
    base = base_url.rstrip("/")

    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
        if not wait_for_human(page, auto=settings.auto):
            return []
    except Exception as e:
        print(f"    Initial navigation warning: {e}")

    html = ""
    try:
        html = page.content()
    except Exception:
        pass

    profile = detect_profile_from_url(base_url) or detect_profile_from_html(html, base_url)
    if profile.confidence < 0.4:
        profile = detect_profile_from_html(html, base_url)
    print(f"    Detected profile: {profile.name} (confidence {profile.confidence:.2f})")

    llm_hint = None
    if profile.confidence < 0.65 and settings.llm_provider:
        print("    Low confidence — trying lightweight LLM selector hint…")
        llm_hint = _optional_llm_toc_hint(html, settings)
        if llm_hint:
            print(f"    LLM suggested: {llm_hint}")

    user_toc = settings.toc_selector or settings.chapter_selector
    user_content = settings.content_selector
    if llm_hint:
        user_toc = user_toc or llm_hint.get("toc_selector")
        user_content = user_content or llm_hint.get("content_selector")
        if user_content and not settings.content_selector:
            settings.content_selector = user_content

    toc_selectors, _ = merge_selectors(profile, user_toc, user_content)
    extra_patterns = list(profile.extra_href_patterns or [])
    button_texts = list(dict.fromkeys((profile.toc_button_texts or []) + GENERIC_NOVEL.toc_button_texts))

    clicked = _try_click_toc(page, button_texts)
    if clicked:
        print("    Clicked TOC / catalog control")
        human_delay(0.6, 1.2)
        try:
            html = page.content()
        except Exception:
            pass

    while page_num <= settings.max_toc_pages:
        if page_num > 1:
            toc_url = f"{base}/{page_num}"
            alt = f"{base}?page={page_num}" if "?" not in base else None
            alts = [toc_url]
            if alt:
                alts.append(alt)
            alts.append(f"{base}?page={page_num}&per-page=100")
            alts.append(f"{base}/page/{page_num}")

            navigated = False
            for candidate in alts:
                try:
                    resp = page.goto(candidate, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
                    if not wait_for_human(page, auto=settings.auto):
                        break
                    if resp and resp.status == 404:
                        continue
                    if page.url.rstrip("/") == base.rstrip("/") and page_num > 2:
                        print("    → Redirected to base, end of TOC.")
                        navigated = False
                        break
                    navigated = True
                    break
                except Exception:
                    continue
            if not navigated:
                print("    → No more TOC pages.")
                break
            print(f"    TOC page {page_num}: {page.url}")
        else:
            print(f"    TOC page {page_num}: {page.url}")

        raw = _collect_links_playwright(page, toc_selectors)
        new_count = 0
        if user_toc or (profile.name != "generic_novel" and profile.confidence >= 0.5):
            for item in raw:
                href = item.get("href") or ""
                text = item.get("text") or ""
                if not href or href in seen_map:
                    continue
                if href.startswith("javascript:") or href.endswith("#"):
                    continue
                if not same_domain(href, base_url):
                    continue
                if user_toc or _looks_like_chapter(href, text, extra_patterns):
                    seen_map[href] = text
                    new_count += 1
        else:
            scored = _score_link_cluster(raw, base_url, extra_patterns)
            for href in scored:
                if href not in seen_map:
                    # recover title from raw
                    title = next((i.get("text", "") for i in raw if i.get("href") == href), "")
                    seen_map[href] = title
                    new_count += 1

        print(f"    → +{new_count} new (total {len(seen_map)})")
        if new_count == 0 and page_num > 1:
            break
        if new_count == 0 and page_num == 1 and len(seen_map) > 5:
            break
        if page_num > 3 and new_count < 2 and len(seen_map) > 30:
            break

        page_num += 1
        human_delay(settings.toc_delay_min, settings.toc_delay_max)

    # Semantic natural sort: (volume, chapter, subpart)
    ordered = sorted(seen_map.keys(), key=lambda u: natural_sort_key(u, seen_map.get(u, "")))
    if not ordered and seen_map:
        ordered = list(seen_map.keys())
    if not ordered:
        print("    Tip: pass --toc-selector 'CSS' or open the Catalog/Chapter-list URL directly.")
        print("         Supported profiles: madara, royalroad, webnovel_style, wordpress, generic.")
    else:
        # Log first few sort keys for debugging scrambled TOCs
        sample = ordered[:3]
        keys = [parse_volume_chapter(u, seen_map.get(u, "")) for u in sample]
        print(f"    Natural-sort sample keys: {keys}")
    return ordered


def harvest_toc_requests(base_url: str, settings: Settings) -> List[str]:
    """Requests / curl_cffi TOC harvest (no JS, limited but fast)."""
    print("\n[*] Harvesting TOC (HTTP fallback)…")
    seen_map: Dict[str, str] = {}
    base = base_url.rstrip("/")

    profile = detect_profile_from_url(base_url) or GENERIC_NOVEL
    extra_patterns = list(profile.extra_href_patterns or [])

    for page_num in range(1, settings.max_toc_pages + 1):
        toc_url = f"{base}/{page_num}" if page_num > 1 else base
        print(f"    TOC page {page_num}: {toc_url}")
        try:
            html, backend, err = fetch_with_retries(toc_url, settings, max_attempts=3)
            if not html:
                print(f"    Error: {err}")
                break
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            links = []
            for sel in (profile.chapter_link_selectors or []) + ["a[href]"]:
                try:
                    found = soup.select(sel)
                    if found:
                        links = found
                        break
                except Exception:
                    continue
            new = 0
            for a in links:
                href = a.get("href")
                if not href:
                    continue
                if not href.startswith("http"):
                    href = urljoin(base_url, href)
                text = a.get_text(strip=True)[:80]
                if href in seen_map or not same_domain(href, base_url):
                    continue
                if _looks_like_chapter(href, text, extra_patterns):
                    seen_map[href] = text
                    new += 1
            print(f"    → +{new} (total {len(seen_map)}) via {backend}")
            if new == 0 and page_num > 1:
                break
            human_delay(1.0, 2.5)
        except Exception as e:
            print(f"    Error: {e}")
            break

    ordered = sorted(seen_map.keys(), key=lambda u: natural_sort_key(u, seen_map.get(u, "")))
    return ordered


def _extract_chapter_text(
    url: str,
    settings: Settings,
    page=None,
) -> Tuple[Optional[str], str]:
    """Tiered chapter extraction.

    Priority:
      1. curl_cffi / requests (fast, low RAM)
      2. Playwright only if HTTP yields empty / challenge
    Returns (text, backend).
    """
    # Tier 1+2: HTTP
    html, backend, err = fetch_with_retries(url, settings, max_attempts=3, timeout=30.0)
    if html:
        result = extract_text_best(html=html, url=url, content_selector=settings.content_selector)
        if result.text and len(result.text) > 80:
            return result.text, f"{backend}+{result.backend}"

    # Tier 3: Playwright (only if we have a live page)
    if page is not None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
            if not wait_for_human(page, auto=settings.auto):
                return None, "security_challenge"
            result = extract_text_best(page=page, url=url, content_selector=settings.content_selector)
            if result.text and len(result.text) > 80:
                return result.text, f"playwright+{result.backend}"
        except Exception as e:
            if _is_target_closed(e):
                raise  # let caller recreate context
            logger.debug("Playwright chapter extract failed: %s", e)

    return None, "failed"


def download_chapters(
    page,
    chapter_urls: List[str],
    settings: Settings,
    output_dir: Path,
    master_file: Path,
    session: Optional[BrowserSession] = None,
) -> int:
    """Sequential chapter download with resume, backoff, DLQ, and storage limits.

    Returns count of chapters successfully saved.
    """
    chapters_dir = output_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(settings.resolved_state_file())
    completed = set(state.get("completed", []))
    failed_queue_path = output_dir / "failed_queue.json"
    saved = 0
    chapter_meta: List[Dict[str, Any]] = []  # for EPUB / JSONL

    max_retries = getattr(settings, "max_retries", 3)

    for idx, url in enumerate(chapter_urls, start=1):
        if settings.max_chapters and idx > settings.max_chapters:
            print(f"[*] Reached --max-chapters={settings.max_chapters}")
            break
        if over_storage_limit(output_dir, settings.max_total_mb):
            print(f"[*] Reached storage limit --max-mb={settings.max_total_mb}")
            break

        target = chapters_dir / f"chapter_{idx:04d}.txt"
        if target.exists() or url in completed:
            print(f"[{idx}/{len(chapter_urls)}] Skip (exists): {url.split('/')[-1][:60]}")
            # Still record meta if file exists
            if target.exists():
                vol, chap, _ = parse_volume_chapter(url)
                chapter_meta.append({
                    "index": idx,
                    "url": url,
                    "volume": vol,
                    "chapter": chap,
                    "path": str(target),
                    "title": f"Chapter {idx}",
                })
            continue

        print(f"\n[{idx}/{len(chapter_urls)}] {url}")
        text = None
        backend = "none"
        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            try:
                # Prefer HTTP path; fall back to Playwright page if available
                text, backend = _extract_chapter_text(url, settings, page=page)
                if text:
                    break
            except Exception as e:
                last_error = e
                if _is_target_closed(e) and session is not None:
                    try:
                        _, page = session.recreate()
                    except Exception as re:
                        print(f"    Context recreate failed: {re}")
                if attempt < max_retries - 1:
                    delay = exponential_backoff(attempt, base_delay=2.0, max_delay=45.0)
                    print(f"    Retry {attempt + 1}/{max_retries} after {delay:.1f}s ({e})")
                    time.sleep(delay)

        if text:
            text = clean_text(text)
            target.write_text(text, encoding="utf-8")
            vol, chap, sub = parse_volume_chapter(url)
            # Avoid "Chapter 1.0" — use int when whole number
            chap_disp = int(chap) if chap == int(chap) else chap
            if vol > 1:
                title = f"Vol {vol} Ch {chap_disp}"
            else:
                title = f"Chapter {chap_disp}"
            if sub:
                title += f" Part {sub}"
            with open(master_file, "a", encoding="utf-8") as mf:
                mf.write(
                    f"\n\n{'=' * 72}\n"
                    f"CHAPTER {idx}  |  {title}\n"
                    f"Source: {url}\n"
                    f"{'=' * 72}\n\n{text}\n"
                )
            print(f"  ✓ Saved {target.name} ({len(text):,} chars) via {backend}")
            completed.add(url)
            state["completed"] = list(completed)
            state["last_url"] = url
            save_state(settings.resolved_state_file(), state)
            saved += 1
            chapter_meta.append({
                "index": idx,
                "url": url,
                "volume": vol,
                "chapter": chap,
                "path": str(target),
                "title": title,
                "chars": len(text),
            })
        else:
            err_msg = str(last_error) if last_error else "empty extraction"
            print(f"  ✗ Failed chapter {idx}: {err_msg}")
            append_failed_queue(failed_queue_path, url, err_msg, attempt=max_retries)
            state.setdefault("failed", [])
            if url not in state["failed"]:
                state["failed"].append(url)
            save_state(settings.resolved_state_file(), state)

        human_delay(settings.delay_min, settings.delay_max)

    # --- Automatic DLQ retry pass ---
    dlq = load_failed_queue(failed_queue_path)
    if dlq and not settings.list_only:
        print(f"\n[*] Dead-letter queue: {len(dlq)} failed chapters — single retry pass…")
        remaining = []
        for entry in dlq:
            url = entry.get("url")
            if not url or url in completed:
                continue
            # Find original index
            try:
                idx = chapter_urls.index(url) + 1
            except ValueError:
                idx = 0
            print(f"  DLQ retry: {url}")
            delay = exponential_backoff(2, base_delay=3.0, max_delay=60.0)
            time.sleep(delay)
            text, backend = _extract_chapter_text(url, settings, page=page)
            if text:
                text = clean_text(text)
                target = chapters_dir / f"chapter_{idx:04d}.txt" if idx else chapters_dir / f"chapter_dlq_{saved+1:04d}.txt"
                target.write_text(text, encoding="utf-8")
                with open(master_file, "a", encoding="utf-8") as mf:
                    mf.write(
                        f"\n\n{'=' * 72}\n"
                        f"CHAPTER {idx or '?'} (DLQ recovered)\n"
                        f"Source: {url}\n"
                        f"{'=' * 72}\n\n{text}\n"
                    )
                print(f"  ✓ DLQ recovered via {backend}")
                completed.add(url)
                saved += 1
            else:
                remaining.append(entry)
                print(f"  ✗ DLQ still failed")
        # Rewrite DLQ with only persistent failures
        failed_queue_path.write_text(
            __import__("json").dumps(remaining, indent=2), encoding="utf-8"
        )
        state["completed"] = list(completed)
        save_state(settings.resolved_state_file(), state)

    # Persist chapter metadata for export
    meta_path = output_dir / "chapters_meta.json"
    meta_path.write_text(
        __import__("json").dumps(chapter_meta, indent=2), encoding="utf-8"
    )

    return saved


def export_epub(
    output_dir: Path,
    title: str = "Novel",
    author: str = "Unknown",
    language: str = "en",
) -> Optional[Path]:
    """Build a valid EPUB from chapters/ + chapters_meta.json using ebooklib."""
    try:
        from ebooklib import epub
    except ImportError:
        print("[!] ebooklib not installed — skip EPUB. pip install ebooklib")
        return None

    meta_path = output_dir / "chapters_meta.json"
    chapters_dir = output_dir / "chapters"
    if not chapters_dir.exists():
        return None

    book = epub.EpubBook()
    book.set_identifier(f"silas-{title[:40]}")
    book.set_title(title)
    book.set_language(language)
    book.add_author(author)

    spine = ["nav"]
    toc = []
    chapter_files = sorted(chapters_dir.glob("chapter_*.txt"))

    meta_list = []
    if meta_path.exists():
        try:
            meta_list = __import__("json").loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    meta_by_path = {m.get("path"): m for m in meta_list}

    for i, path in enumerate(chapter_files):
        text = path.read_text(encoding="utf-8")
        m = meta_by_path.get(str(path), {})
        ch_title = m.get("title") or f"Chapter {i + 1}"
        # Escape angle brackets / ampersands so <Skill Acquired: Fireball> does not break XML
        from silas.utils import escape_xml
        safe_title = escape_xml(ch_title)
        body = "".join(
            f"<p>{escape_xml(line)}</p>" for line in text.split("\n\n") if line.strip()
        )
        c = epub.EpubHtml(title=ch_title, file_name=f"chap_{i+1:04d}.xhtml", lang=language)
        c.content = f"<h1>{safe_title}</h1>{body}"
        book.add_item(c)
        spine.append(c)
        toc.append(c)

    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    out_path = output_dir / f"{re.sub(r'[^a-zA-Z0-9_-]', '_', title)[:60]}.epub"
    epub.write_epub(str(out_path), book, {})
    print(f"[+] EPUB written: {out_path}")
    return out_path


def export_jsonl(
    output_dir: Path,
    novel_title: str = "Novel",
    source_url: str = "",
) -> Optional[Path]:
    """Export chapters as JSONL for LLM pretraining / fine-tuning."""
    import json as _json

    meta_path = output_dir / "chapters_meta.json"
    chapters_dir = output_dir / "chapters"
    if not chapters_dir.exists():
        return None

    meta_list = []
    if meta_path.exists():
        try:
            meta_list = _json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    meta_by_path = {m.get("path"): m for m in meta_list}

    out_path = output_dir / "chapters.jsonl"
    count = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for path in sorted(chapters_dir.glob("chapter_*.txt")):
            text = path.read_text(encoding="utf-8")
            m = meta_by_path.get(str(path), {})
            record = {
                "text": text,
                "meta": {
                    "novel": novel_title,
                    "volume": m.get("volume", 1),
                    "chapter": m.get("chapter", 0),
                    "title": m.get("title", path.stem),
                    "source": m.get("url") or source_url,
                    "index": m.get("index"),
                },
            }
            f.write(_json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    print(f"[+] JSONL written: {out_path} ({count} records)")
    return out_path
