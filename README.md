# Silas v1.5.0

**Universal scraper for any website, any data, any format.**

Merges a production novel/chapter engine with multimodal extraction (text · image OCR · video keyframes · audio transcripts · PDFs). Built for real sites that use Cloudflare, pagination, and JS-rendered content.

## What's new in 1.5.0 (bugfix + hardening)

Critical correctness and safety fixes based on real-world testing:

- **Strict HTTP status handling** — only 2xx responses are accepted as chapter content. 404/500 error pages are no longer saved as "✓ Saved chapter".
- **Per-output resume state** — `scraper_state.json` now lives inside the output directory. Fresh output folders no longer skip every chapter.
- **`--content-selector` is respected** — user-supplied selectors take priority over trafilatura on the HTTP path.
- **Safer `clean_text`** — noise phrases are removed only as whole lines / whole-word matches; story text containing "Advertisement" or "VIP chapter" is preserved.
- **EPUB XML safety** — angle brackets and ampersands in chapter text are escaped; files no longer fail XML parsing.
- **Accurate natural sort** — `chapter-12-part-10` now sorts before chapter 13 (sub-part is a discrete key component).
- **Titles** — no more "Chapter 1.0"; integer chapters display as "Chapter 1".
- **False security-challenge detection eliminated** — mere occurrence of the words "captcha" / "cloudflare" in story text no longer blocks for 5 minutes. Detection requires title markers or real challenge DOM elements.
- **robots.txt** — 403 on robots.txt is treated as "no policy" (allow) instead of "disallow everything".
- **API hardening** — defaults to `127.0.0.1`, rejects private/metadata URLs (SSRF guard), constrains `output_dir`, and tightens CORS.
- **`--no-prefer-http` actually works** — the flag is now honoured by the HTTP client.
- **LLM provider `anthropic`** — no longer silently routes to OpenAI.
- **Version banner** matches the package version.

Retained from 1.4:

## What's new in 1.3.1

- **API host fix** — `silas-api` no longer advertises `http://0.0.0.0` as the client URL (browsers reject it). Server still binds `0.0.0.0` by default for LAN access; startup prints usable URLs (`http://127.0.0.1:8765`, localhost, LAN IP). Host/port configurable via `--host` / `--port` or `SILAS_API_HOST` / `SILAS_API_PORT`.

## What’s new in 1.3

- **Adaptive heuristics engine** — auto-detects site architecture (Madara / WordPress, RoyalRoad, Webnovel-style, generic novel) and applies tuned selectors + pagination strategies. No more manual CSS for most common novel CMSes.
- **Optional LLM assist** — when confidence is low, a lightweight call (OpenAI-compatible or Ollama) can propose TOC / content selectors from the DOM skeleton.
- **Dense link scoring** — falls back to clustering & scoring chapter-like links when profiles don’t match strongly.
- **FastAPI backend** — `silas-api` exposes REST + WebSocket progress for building a React / Vue / any SPA frontend. Streamlit UI remains for quick interactive use.
- **Stronger stealth** — extra navigator / WebGL / canvas hardening.
- Cleaner profile merging (user selectors always win).

## Architecture evolution path

| Layer | Current | Recommended next |
|-------|---------|-------------------|
| UI | Streamlit (`silas-ui`) | React / Vue / Svelte SPA talking to FastAPI |
| API | FastAPI (`silas-api`) — jobs, WebSocket progress | Add Redis job queue for multi-worker |
| Core | Playwright + adaptive profiles + optional LLM | Keep; add more site profiles as needed |
| Stealth | UA rotation, init scripts, Client Hints | Optional: residential proxy pool, Camoufox |

This release already provides the decoupled backend so a modern frontend can be attached without rewriting the extraction engine.

## What it can extract

| Mode | What you get |
|------|----------------|
| `page` | Clean text + markdown + title/author/date |
| `novel` | Deep paginated TOC → sequential chapters → master file + resume |
| `media` | Discover & download images / video / audio + OCR / transcripts |
| `document` | PDF → text + tables (requests + Playwright fallback) |
| `all` / `auto` | Best-effort combination |

## Install

```bash
pip install -e .
playwright install chromium

# Optional OCR / Whisper
pip install -r requirements-multimodal.txt
# system: tesseract-ocr, ffmpeg

# Optional LLM assist
pip install -e ".[llm]"
```

Commands after install:

- `silas` — CLI
- `silas-ui` — Web UI (Streamlit)
- `silas-api` — FastAPI server (bind 0.0.0.0:8765 → open http://127.0.0.1:8765)

## Quick start

```bash
# Single page
silas -u "https://quotes.toscrape.com/" --mode page

# Novel (limit for testing) — adaptive profiles handle most Madara / RR / webnovel sites
silas -u "https://www.royalroad.com/fiction/21220/mother-of-learning" --mode novel --max-chapters 3

# List chapters only
silas -u "…" --mode novel --list-only

# Custom selectors when auto-detection still fails
silas -u "…" --mode novel --toc-selector "a.chapter-item" --content-selector "#chapter-content"

# Optional LLM help for hard sites
silas -u "…" --mode novel --llm-provider ollama
# or OPENAI_API_KEY=… silas … --llm-provider openai

# Media + images
silas -u "http://books.toscrape.com/" --mode media --download-images

# PDF (handles some 403s via browser)
silas -u "https://pdfobject.com/pdf/sample.pdf" --mode document

# Proxy + stealth
silas -u "…" --proxy http://127.0.0.1:7890 --stealth

# Web UI
silas-ui

# FastAPI (for React/Vue frontends)
silas-api
# Binds 0.0.0.0:8765 — open http://127.0.0.1:8765 (browsers cannot use 0.0.0.0)
# Optional: silas-api --host 127.0.0.1 --port 8765
# then POST /extract  or  POST /extract/sync
# WebSocket: /ws/{job_id}
# Interactive docs: http://127.0.0.1:8765/docs
```

## Adaptive site profiles

Silas scores the page (URL + HTML signals) against built-in profiles:

- **madara** — WordPress Madara theme (very common web-novel / manga sites)
- **royalroad** — royalroad.com
- **webnovel_style** — Webnovel / Qidian-like platforms
- **wordpress** — generic WP blog / novel layouts
- **generic_novel** — broad chapter-link heuristics + density scoring

User-supplied `--toc-selector` / `--content-selector` always take priority. When profile confidence is low and `--llm-provider` is set, a small LLM call proposes selectors from a cleaned DOM skeleton.

## FastAPI surface

```
GET  /health
POST /extract          → { job_id, status, … }   (async)
GET  /jobs/{job_id}
GET  /jobs
WS   /ws/{job_id}      → live progress + final result
POST /extract/sync     → blocking result (dev / simple clients)
```

CORS is open by default for local SPA development. For production, lock origins and put a proper job queue (Redis / RQ / Celery) behind the API.

## Design principles

- **Semi-automated** — opens a real browser; pauses on Cloudflare / CAPTCHA so you can solve it once. Profile is reused.
- **Storage-aware** — hard caps on chapters, images, video, audio, and total MB.
- **Resume** — skips already-downloaded chapters; state file survives restarts.
- **Fallback** — if Playwright fails, falls back to requests + BeautifulSoup.
- **Optional heavy deps** — OCR / Whisper / OpenAI are probed at runtime; missing packages simply disable that path.
- **Stealth options** — rotate UA, anti-automation patches, proxy, realistic Client Hints headers.

## Output layout

```
output/
  chapters/chapter_0001.txt
  novel_full.txt
  page.txt / page.md
  document.txt / original.pdf
  media/images/…  media/videos/…  media/audio/…
  result.json / summary.json
scraper_state.json
browser_profile/
```

## Notes on anti-bot

No tool can reliably bypass every anti-bot system. Silas:

- Surfaces Cloudflare / Turnstile / captcha challenges and waits for a human (or fails cleanly under `--auto`).
- Uses realistic browser fingerprints, UA rotation, and optional proxies.
- Reuses a persistent profile so cookies / solved challenges stick.

For harder targets consider residential proxies and solving services; those are outside the core scope.

## License

Use at your own risk. Respect each site’s terms of service and robots.txt (`--ignore-robots` only silences the check — use sparingly). This tool is intended for hobby experimentation on sites you own or have permission to access.
