#!/usr/bin/env python3
"""Silas CLI — scrape any website, any data, any format."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from silas.config import Settings
from silas.engine import run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="silas",
        description="Universal scraper: pages, novels, documents, images, video, audio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  silas -u https://example.com/article
  silas -u https://novellive.app/book/foo --mode novel --max-chapters 50
  silas -u https://site.com/gallery --mode media --download-images --download-video
  silas -u https://example.com/doc.pdf --mode document
  silas -u https://... --list-only
  silas -u https://... --fallback-only --max-mb 20
  silas -u https://... --proxy http://127.0.0.1:7890 --stealth
  silas-ui          # launch web UI
        """,
    )
    p.add_argument("-u", "--url", required=True, help="Target URL")
    p.add_argument(
        "--mode",
        choices=["auto", "page", "novel", "media", "document", "all"],
        default="auto",
        help="Extraction strategy (default: auto)",
    )
    p.add_argument("-o", "--output", default="output", help="Output directory")
    p.add_argument("--master", default=None, help="Master concatenated text file (novel mode)")
    p.add_argument("--profile", default="browser_profile", help="Playwright persistent profile dir")
    p.add_argument("--state", default=None, help="Resume state file (default: <output>/scraper_state.json)")

    # Limits
    p.add_argument("--max-chapters", type=int, default=None)
    p.add_argument("--max-mb", type=float, default=None, help="Stop when output dir exceeds this many MB")
    p.add_argument("--max-toc-pages", type=int, default=120)
    p.add_argument("--max-images", type=int, default=50)
    p.add_argument("--max-videos", type=int, default=5)
    p.add_argument("--max-audios", type=int, default=10)

    # Media flags
    p.add_argument("--download-images", action="store_true")
    p.add_argument("--download-video", action="store_true")
    p.add_argument("--download-audio", action="store_true")
    p.add_argument("--no-enrich", action="store_true", help="Skip OCR / transcript enrichment")

    # Selectors
    p.add_argument("--toc-selector", default=None, help="CSS selector for TOC chapter links")
    p.add_argument("--content-selector", default=None, help="CSS selector for chapter body text")
    p.add_argument("--llm-provider", default=None, choices=["openai", "ollama", "anthropic"],
                   help="Optional LLM for adaptive selector discovery when auto-heuristics are weak")

    # Stealth / network
    p.add_argument("--proxy", default=None, help="Proxy URL e.g. http://127.0.0.1:7890 or socks5://...")
    p.add_argument("--no-stealth", action="store_true", help="Disable anti-detection patches")
    p.add_argument("--no-rotate-ua", action="store_true", help="Keep fixed User-Agent")
    p.add_argument("--ignore-robots", action="store_true", help="Skip robots.txt check")
    p.add_argument("--user-agent", default=None, help="Override User-Agent string")

    # Behaviour
    p.add_argument("--headless", action="store_true")
    p.add_argument("--fallback-only", action="store_true", help="requests only, no browser")
    p.add_argument("--list-only", action="store_true", help="Discover chapters/links only")
    p.add_argument("--auto", action="store_true", help="Never pause for CAPTCHA / failures")
    p.add_argument("--delay-min", type=float, default=1.5)
    p.add_argument("--delay-max", type=float, default=4.5)
    p.add_argument("--max-retries", type=int, default=3, help="Per-chapter retry budget with exponential backoff")
    p.add_argument("--export-epub", action="store_true", help="Generate EPUB after novel scrape")
    p.add_argument("--export-jsonl", action="store_true", help="Export chapters as JSONL for LLM training")
    p.add_argument("--novel-title", default=None, help="Title used in EPUB / JSONL metadata")
    p.add_argument("--novel-author", default=None, help="Author used in EPUB metadata")
    p.add_argument("--no-prefer-http", action="store_true", help="Force Playwright for chapter bodies instead of curl_cffi/requests")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.url.startswith("http"):
        print("ERROR: --url must be a full http(s) URL", file=sys.stderr)
        return 1

    settings = Settings(
        output_dir=Path(args.output),
        master_file=Path(args.master) if args.master else None,
        profile_dir=Path(args.profile),
        state_file=Path(args.state) if args.state else None,
        max_chapters=args.max_chapters,
        max_total_mb=args.max_mb,
        max_toc_pages=args.max_toc_pages,
        max_images=args.max_images,
        max_videos=args.max_videos,
        max_audios=args.max_audios,
        download_images=args.download_images,
        download_video=args.download_video,
        download_audio=args.download_audio,
        enrich_media=not args.no_enrich,
        headless=args.headless,
        fallback_only=args.fallback_only,
        list_only=args.list_only,
        auto=args.auto,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        proxy=args.proxy,
        stealth=not args.no_stealth,
        rotate_ua=not args.no_rotate_ua,
        ignore_robots=args.ignore_robots,
        toc_selector=args.toc_selector,
        content_selector=args.content_selector,
        llm_provider=args.llm_provider,
        max_retries=args.max_retries,
        export_epub=args.export_epub,
        export_jsonl=args.export_jsonl,
        novel_title=args.novel_title,
        novel_author=args.novel_author,
        prefer_http=not args.no_prefer_http,
    )
    if args.user_agent:
        settings.user_agent = args.user_agent
        settings.rotate_ua = False

    result = run(settings, args.url, mode=args.mode)

    # Pretty summary
    print("\n" + "=" * 72)
    print("  RESULT SUMMARY")
    print("=" * 72)
    if result.get("error"):
        print(f"  Error: {result['error']}")
    if "chapters_found" in result:
        print(f"  Chapters found : {result['chapters_found']}")
    if "chapters_saved" in result:
        print(f"  Chapters saved : {result['chapters_saved']}")
    if "text" in result and result["text"].get("text"):
        print(f"  Text length    : {len(result['text']['text']):,} chars")
        print(f"  Backend        : {result['text'].get('backend')}")
    if "document" in result:
        print(f"  Document backend: {result['document'].get('backend')}")
        print(f"  Pages / chars  : {result['document'].get('page_count')} / {result['document'].get('chars', 0)}")
    if "downloaded_media" in result:
        print(f"  Media files    : {len(result['downloaded_media'])}")
    if "size_mb" in result:
        print(f"  Output size    : {result['size_mb']} MB")
    print(f"  Output dir     : {settings.output_dir.resolve()}")
    print("=" * 72)

    summary_path = settings.output_dir / "summary.json"
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
