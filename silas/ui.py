"""Silas Web UI — Streamlit interface for the universal extractor."""
from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st

from silas.config import Settings
from silas.engine import run
from silas import __version__


st.set_page_config(
    page_title="Silas · Universal Extractor",
    page_icon="🕸️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .stApp { max-width: 1200px; }
    div[data-testid="stMetric"] { background: #1e1e2e; border-radius: 8px; padding: 8px; }
</style>
""", unsafe_allow_html=True)

st.title("🕸️ Silas — Universal Extractor")
st.caption(f"v{__version__} · adaptive heuristics · site profiles (Madara/WordPress/RR) · FastAPI backend available")

with st.sidebar:
    st.header("Settings")
    mode = st.selectbox("Mode", ["auto", "page", "novel", "media", "document", "all"], index=0)
    output_dir = st.text_input("Output directory", value="output")
    headless = st.checkbox("Headless browser", value=False)
    auto = st.checkbox("Auto (no CAPTCHA pause)", value=False)
    fallback_only = st.checkbox("Fallback only (no Playwright)", value=False)

    st.subheader("Stealth")
    stealth = st.checkbox("Stealth patches", value=True)
    rotate_ua = st.checkbox("Rotate User-Agent", value=True)
    proxy = st.text_input("Proxy (optional)", placeholder="http://127.0.0.1:7890")
    ignore_robots = st.checkbox("Ignore robots.txt", value=False)

    st.subheader("Limits")
    max_chapters = st.number_input("Max chapters", min_value=0, value=0, help="0 = unlimited")
    max_mb = st.number_input("Max total MB", min_value=0.0, value=0.0, help="0 = unlimited")
    max_images = st.number_input("Max images", min_value=1, value=50)
    max_videos = st.number_input("Max videos", min_value=0, value=5)

    st.subheader("Media")
    download_images = st.checkbox("Download images", value=False)
    download_video = st.checkbox("Download video", value=False)
    download_audio = st.checkbox("Download audio", value=False)
    enrich = st.checkbox("OCR / Whisper enrichment", value=True)

    st.subheader("Advanced selectors")
    toc_selector = st.text_input("TOC selector (CSS)", placeholder="a.chapter-link")
    content_selector = st.text_input("Content selector (CSS)", placeholder="#chapter-content")

    list_only = st.checkbox("List chapters only", value=False)

url = st.text_input("Target URL", placeholder="https://example.com/article or novel TOC page")

col1, col2 = st.columns([1, 3])
with col1:
    run_btn = st.button("▶ Run extraction", type="primary", use_container_width=True)
with col2:
    st.write("")

if run_btn:
    if not url or not url.startswith("http"):
        st.error("Please enter a full http(s) URL.")
    else:
        settings = Settings(
            output_dir=Path(output_dir),
            headless=headless,
            auto=auto,
            fallback_only=fallback_only,
            stealth=stealth,
            rotate_ua=rotate_ua,
            proxy=proxy or None,
            ignore_robots=ignore_robots,
            max_chapters=int(max_chapters) or None,
            max_total_mb=float(max_mb) or None,
            max_images=int(max_images),
            max_videos=int(max_videos),
            download_images=download_images,
            download_video=download_video,
            download_audio=download_audio,
            enrich_media=enrich,
            toc_selector=toc_selector or None,
            content_selector=content_selector or None,
            list_only=list_only,
        )
        with st.spinner("Extracting… (browser window may open for CAPTCHA)"):
            try:
                result = run(settings, url, mode=mode)
            except Exception as e:
                st.exception(e)
                result = {"error": str(e)}

        st.subheader("Result")
        if result.get("error"):
            st.error(result["error"])
        else:
            st.success("Done")

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            if "chapters_found" in result:
                st.metric("Chapters found", result["chapters_found"])
            elif "text" in result and result["text"].get("text"):
                st.metric("Text chars", f"{len(result['text']['text']):,}")
        with c2:
            if "chapters_saved" in result:
                st.metric("Chapters saved", result["chapters_saved"])
            if "document" in result:
                st.metric("Doc pages", result["document"].get("page_count", 0))
        with c3:
            if "downloaded_media" in result:
                st.metric("Media files", len(result["downloaded_media"]))
        with c4:
            if "size_mb" in result:
                st.metric("Size MB", result["size_mb"])

        if "text" in result and result["text"].get("text"):
            with st.expander("Extracted text preview", expanded=True):
                st.text(result["text"]["text"][:4000])
        if "document" in result and result["document"].get("text_preview"):
            with st.expander("Document preview", expanded=True):
                st.text(result["document"]["text_preview"])
        if result.get("chapter_urls"):
            with st.expander(f"Chapter URLs ({len(result['chapter_urls'])})"):
                for i, u in enumerate(result["chapter_urls"][:100], 1):
                    st.write(f"{i}. {u}")

        with st.expander("Full JSON result"):
            st.json(result)

        out_path = Path(output_dir).resolve()
        st.info(f"Files written under: `{out_path}`")

st.markdown("---")
st.markdown(
    "Use responsibly and in accordance with each site’s terms of service. "
    "Silas surfaces CAPTCHAs for human solving and does not claim to bypass all protections."
)
