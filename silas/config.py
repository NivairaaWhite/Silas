"""Central configuration. Override via CLI flags, UI, or environment variables."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Settings:
    # Identity / stealth
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    contact: str = "operator@example.com"
    proxy: Optional[str] = None          # http://user:pass@host:port or socks5://...
    rotate_ua: bool = True               # pick a random realistic UA each run
    stealth: bool = True                 # extra anti-detection patches

    # Browser
    profile_dir: Path = Path("browser_profile")
    headless: bool = False
    viewport_width: int = 1366
    viewport_height: int = 768
    nav_timeout_ms: int = 60000

    # Rate limiting (human-like)
    delay_min: float = 1.5
    delay_max: float = 4.5
    toc_delay_min: float = 1.0
    toc_delay_max: float = 3.0

    # Storage limits (hard ceilings)
    max_chapters: Optional[int] = None
    max_pages: Optional[int] = None
    max_images: int = 50
    max_videos: int = 5
    max_audios: int = 10
    max_image_bytes: int = 20 * 1024 * 1024
    max_video_bytes: int = 120 * 1024 * 1024
    max_audio_bytes: int = 40 * 1024 * 1024
    max_total_mb: Optional[float] = None
    max_toc_pages: int = 120

    # Behaviour
    auto: bool = False          # never pause on CAPTCHA / failure
    enrich_media: bool = True   # OCR / captions / transcripts
    download_images: bool = False
    download_video: bool = False
    download_audio: bool = False
    fallback_only: bool = False # skip Playwright, use HTTP only
    list_only: bool = False
    respect_robots: bool = True
    ignore_robots: bool = False # override when True
    max_retries: int = 3        # per-chapter retry budget
    prefer_http: bool = True    # Tier-1 HTTP (curl_cffi) before Playwright for chapters

    # Selectors (advanced)
    toc_selector: Optional[str] = None
    chapter_selector: Optional[str] = None
    content_selector: Optional[str] = None

    # Output — state_file defaults relative to output_dir so resume is per-job
    output_dir: Path = Path("output")
    master_file: Optional[Path] = None
    state_file: Optional[Path] = None   # if None → output_dir / "scraper_state.json"
    export_epub: bool = False
    export_jsonl: bool = False
    novel_title: Optional[str] = None
    novel_author: Optional[str] = None

    # Optional LLM / VLM for captions & structured extraction
    llm_provider: Optional[str] = None  # openai | anthropic | ollama | None

    # Extra headers
    extra_headers: dict = field(default_factory=dict)

    def resolved_state_file(self) -> Path:
        """State lives inside the output directory so different jobs do not share resume data."""
        if self.state_file is not None:
            return Path(self.state_file)
        return Path(self.output_dir) / "scraper_state.json"
