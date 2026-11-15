"""Site architecture detection and pre-configured scraper profiles.

Detects common CMS / novel themes (Madara, WordPress, RoyalRoad, Webnovel-style,
Chinese aggregator patterns, etc.) and returns tuned selectors + strategies so
auto mode succeeds without manual CSS selectors.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import copy
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass
class SiteProfile:
    name: str
    # CSS selectors (ordered preference)
    toc_selectors: List[str] = field(default_factory=list)
    chapter_link_selectors: List[str] = field(default_factory=list)
    content_selectors: List[str] = field(default_factory=list)
    # Extra href / text patterns that indicate chapters on this family of sites
    extra_href_patterns: List[str] = field(default_factory=list)
    # Texts that open the full TOC / catalog
    toc_button_texts: List[str] = field(default_factory=list)
    # Pagination strategies to try after the first TOC page
    pagination: List[str] = field(default_factory=lambda: ["path", "query", "load_more"])
    # Confidence 0-1 when this profile matched
    confidence: float = 0.0
    notes: str = ""


# ---------------------------------------------------------------------------
# Built-in profiles (order matters only for documentation; matching is scored)
# ---------------------------------------------------------------------------

MADARA = SiteProfile(
    name="madara",
    toc_selectors=[
        "div.listing-chapters_wrap",
        "ul.main.version-chap",
        "div.page-content-listing",
        "#manga-chapters-holder",
        ".c-page__content .listing-chapters_wrap",
    ],
    chapter_link_selectors=[
        "li.wp-manga-chapter a",
        "ul.main.version-chap li a",
        ".wp-manga-chapter a",
        "div.listing-chapters_wrap a",
        "a.chapter-link",
    ],
    content_selectors=[
        "div.reading-content",
        "div.text-left",
        ".reading-content .text-left",
        "#chapter-content",
        "div.entry-content",
        ".cha-words",
    ],
    extra_href_patterns=[
        r"/chapter[-_]?\d",
        r"/ch[-_]?\d",
        r"/manga/.+/",
        r"wp-manga",
    ],
    toc_button_texts=["chapters", "chapter list", "all chapters", "目录", "章节"],
    pagination=["query", "path", "load_more"],
    notes="WordPress Madara theme (very common on web-novel / manga sites)",
)

WORDPRESS_GENERIC = SiteProfile(
    name="wordpress",
    toc_selectors=[".entry-content", "article", "#content", ".post-content"],
    chapter_link_selectors=[
        "a[href*='chapter']",
        "a[href*='/ch-']",
        ".chapter-list a",
        ".toc a",
        "ul.chapters a",
    ],
    content_selectors=[
        ".entry-content",
        ".post-content",
        "article .content",
        "#content",
        ".wp-block-post-content",
    ],
    extra_href_patterns=[r"/chapter", r"/ch-\d", r"chapter-\d"],
    toc_button_texts=["table of contents", "chapters", "toc"],
    notes="Generic WordPress blog / novel layout",
)

ROYALROAD = SiteProfile(
    name="royalroad",
    toc_selectors=["#chapters", "table#chapters", ".chapter-container"],
    chapter_link_selectors=[
        "table#chapters tbody tr td:first-child a",
        "#chapters a",
        "tr.chapter-row a",
    ],
    content_selectors=[
        ".chapter-content",
        "div.chapter-inner",
        "#chapter-content",
        ".portlet-body",
    ],
    extra_href_patterns=[r"/chapter/", r"royalroad\.com/fiction/"],
    toc_button_texts=["chapters", "table of contents"],
    pagination=["query"],
    notes="royalroad.com",
)

WEBNOVEL_STYLE = SiteProfile(
    name="webnovel_style",
    toc_selectors=[
        ".j_catalog_list",
        "#catalog",
        ".volume-list",
        ".chapter-item",
        ".m-catalog",
    ],
    chapter_link_selectors=[
        "a.j_chapter",
        ".chapter-item a",
        ".catalog-list a",
        "a[data-cid]",
        ".cha-link",
    ],
    content_selectors=[
        ".cha-words",
        "#chapter-content",
        ".content-text",
        ".m-content",
        "div.cha-content",
    ],
    extra_href_patterns=[r"/chapter/", r"webnovel", r"/book/\d+/"],
    toc_button_texts=["catalog", "目录", "table of contents", "chapters"],
    notes="Webnovel / Qidian-style Chinese / international novel platforms",
)

GENERIC_NOVEL = SiteProfile(
    name="generic_novel",
    toc_selectors=[
        "#toc",
        ".toc",
        "#chapter-list",
        ".chapter-list",
        "nav.chapters",
        "ul.chapters",
        "div.chapters",
        "#list",
    ],
    chapter_link_selectors=[
        "a[href*='chapter']",
        "a[href*='/ch']",
        "a[href*='read']",
        ".chapter a",
        "li a",
    ],
    content_selectors=[
        "#chapter-content",
        ".chapter-content",
        "#content",
        ".content",
        "article",
        "main",
        ".post",
        "#noveltext",
        ".text-content",
    ],
    extra_href_patterns=[
        r"chapter",
        r"/ch[-_/]?\d",
        r"/c\d",
        r"第\s*\d+\s*章",
        r"/read/",
        r"/text/",
        r"/story/",
        r"episode",
        r"part[-_]?\d",
    ],
    toc_button_texts=[
        "catalog", "catalogue", "table of contents", "toc", "chapters",
        "目录", "章节", "目錄", "目次", "chapter list", "all chapters",
        "list of chapters", "index",
    ],
    notes="Fallback broad novel heuristics",
)


ALL_PROFILES: List[SiteProfile] = [
    MADARA,
    ROYALROAD,
    WEBNOVEL_STYLE,
    WORDPRESS_GENERIC,
    GENERIC_NOVEL,
]


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def detect_profile_from_url(url: str) -> Optional[SiteProfile]:
    """Fast URL / domain based detection (no network)."""
    d = _domain(url)
    path = urlparse(url).path.lower()
    if "royalroad.com" in d:
        p = ROYALROAD
        p.confidence = 0.95
        return p
    if any(x in d for x in ("webnovel.com", "qidian", "fanqie", "tomato")):
        p = WEBNOVEL_STYLE
        p.confidence = 0.9
        return p
    if "wp-manga" in path or "manga" in path and "chapter" in path:
        p = MADARA
        p.confidence = 0.7
        return p
    return None


def detect_profile_from_html(html: str, url: str = "") -> SiteProfile:
    """Score all profiles against page HTML / DOM signals and return the best match."""
    if not html:
        return GENERIC_NOVEL

    html_l = html.lower()
    scores: Dict[str, float] = {}

    # Madara signals
    madara_hits = sum(1 for s in (
        "wp-manga", "reading-content", "wp-manga-chapter", "c-blog-post",
        "madara", "manga-chapters-holder", "listing-chapters_wrap",
    ) if s in html_l)
    scores["madara"] = min(1.0, madara_hits * 0.22)

    # RoyalRoad
    if "royalroad" in html_l or "chapter-inner" in html_l or 'id="chapters"' in html_l:
        scores["royalroad"] = 0.95

    # Webnovel style
    wn_hits = sum(1 for s in ("j_catalog", "cha-words", "cha-content", "data-cid", "m-catalog") if s in html_l)
    scores["webnovel_style"] = min(1.0, wn_hits * 0.3)

    # WordPress
    wp_hits = sum(1 for s in ("wp-content", "wp-block", "entry-content", "post-content") if s in html_l)
    scores["wordpress"] = min(0.85, wp_hits * 0.25)

    # Generic novel signals (always present as baseline)
    gen_hits = sum(1 for s in ("chapter", "toc", "目录", "章节", "chapter-list", "chapter-content") if s in html_l)
    scores["generic_novel"] = min(0.6, 0.15 + gen_hits * 0.08)

    # URL boost
    url_prof = detect_profile_from_url(url)
    if url_prof:
        scores[url_prof.name] = max(scores.get(url_prof.name, 0), url_prof.confidence)

    best_name = max(scores, key=scores.get) if scores else "generic_novel"
    best_score = scores.get(best_name, 0.0)

    for p in ALL_PROFILES:
        if p.name == best_name:
            # Return a copy so callers can mutate freely
            out = copy.deepcopy(p)
            out.confidence = best_score
            logger.info("Site profile detected: %s (%.2f)", out.name, out.confidence)
            return out

    out = copy.deepcopy(GENERIC_NOVEL)
    out.confidence = 0.3
    return out


def merge_selectors(profile: SiteProfile, user_toc: Optional[str], user_content: Optional[str]) -> Tuple[List[str], List[str]]:
    """Produce final ordered selector lists (user overrides first)."""
    toc = []
    if user_toc:
        toc.append(user_toc)
    toc.extend(profile.chapter_link_selectors or profile.toc_selectors)
    # Dedup preserve order
    seen = set()
    toc = [x for x in toc if not (x in seen or seen.add(x))]

    content = []
    if user_content:
        content.append(user_content)
    content.extend(profile.content_selectors)
    seen = set()
    content = [x for x in content if not (x in seen or seen.add(x))]
    return toc, content
