"""FastAPI backend for Silas — enables async jobs, progress streaming, and a clean
decoupled frontend (React / Vue / any SPA).

Run with:
  uvicorn silas.api:app --host 0.0.0.0 --port 8765 --reload
  # Open in browser: http://127.0.0.1:8765  (not http://0.0.0.0 — browsers reject that)

Or via the entry-point:
  silas-api
  silas-api --host 127.0.0.1 --port 8765
  SILAS_API_HOST=0.0.0.0 SILAS_API_PORT=8765 silas-api
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl

from silas import __version__
from silas.config import Settings
from silas.engine import run as engine_run

logger = logging.getLogger("silas.api")

import ipaddress
from urllib.parse import urlparse as _urlparse

_BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
    "169.254.169.254",  # cloud metadata
    "metadata.google.internal",
}

def _is_private_or_blocked(url: str) -> bool:
    """Reject requests that target loopback, link-local, or cloud metadata."""
    try:
        parsed = _urlparse(url)
        host = (parsed.hostname or "").lower()
        if host in _BLOCKED_HOSTS:
            return True
        # Resolve and check IP ranges
        import socket
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                return True
        return False
    except Exception:
        # On resolution failure, be conservative and allow (engine will fail later)
        return False

app = FastAPI(
    title="Silas API",
    description="Universal scraper — pages, novels, documents, media. Adaptive heuristics + stealth.",
    version=__version__,
)

# Localhost-only by default. For LAN/SPA development set SILAS_CORS_ORIGINS.
import os as _os
_cors_origins = _os.environ.get("SILAS_CORS_ORIGINS", "http://127.0.0.1:*,http://localhost:*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store (replace with Redis / DB for multi-worker production)
_jobs: Dict[str, Dict[str, Any]] = {}
_job_lock = threading.Lock()


class ExtractRequest(BaseModel):
    url: HttpUrl
    mode: str = Field(default="auto", pattern="^(auto|page|novel|media|document|all)$")
    output_dir: str = "output"
    headless: bool = False
    auto: bool = False
    fallback_only: bool = False
    stealth: bool = True
    rotate_ua: bool = True
    proxy: Optional[str] = None
    ignore_robots: bool = False
    max_chapters: Optional[int] = None
    max_total_mb: Optional[float] = None
    max_images: int = 50
    max_videos: int = 5
    download_images: bool = False
    download_video: bool = False
    download_audio: bool = False
    enrich_media: bool = True
    toc_selector: Optional[str] = None
    content_selector: Optional[str] = None
    list_only: bool = False
    llm_provider: Optional[str] = None  # openai | ollama | anthropic


class JobStatus(BaseModel):
    job_id: str
    status: str  # queued | running | done | error
    created_at: str
    finished_at: Optional[str] = None
    progress: List[str] = []
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


def _settings_from_req(req: ExtractRequest) -> Settings:
    return Settings(
        output_dir=Path(req.output_dir),
        headless=req.headless,
        auto=req.auto,
        fallback_only=req.fallback_only,
        stealth=req.stealth,
        rotate_ua=req.rotate_ua,
        proxy=req.proxy,
        ignore_robots=req.ignore_robots,
        max_chapters=req.max_chapters,
        max_total_mb=req.max_total_mb,
        max_images=req.max_images,
        max_videos=req.max_videos,
        download_images=req.download_images,
        download_video=req.download_video,
        download_audio=req.download_audio,
        enrich_media=req.enrich_media,
        toc_selector=req.toc_selector,
        content_selector=req.content_selector,
        list_only=req.list_only,
        llm_provider=req.llm_provider,
    )


def _run_job(job_id: str, req: ExtractRequest) -> None:
    with _job_lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["progress"].append(f"Started extraction for {req.url}")

    try:
        settings = _settings_from_req(req)
        # Capture print-style progress by temporarily redirecting stdout is heavy;
        # engine already prints; we just store the final result.
        result = engine_run(settings, str(req.url), mode=req.mode)
        with _job_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["result"] = result
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            _jobs[job_id]["progress"].append("Finished")
    except Exception as e:
        logger.exception("Job %s failed", job_id)
        with _job_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            _jobs[job_id]["progress"].append(f"Error: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}


@app.post("/extract", response_model=JobStatus)
def start_extract(req: ExtractRequest):
    """Enqueue an extraction job. Poll /jobs/{id} or connect to the WebSocket for progress."""
    if _is_private_or_blocked(str(req.url)):
        raise HTTPException(status_code=400, detail="URL targets a private or blocked address")
    # Constrain output_dir to a safe relative path under cwd
    out = Path(req.output_dir).resolve()
    cwd = Path.cwd().resolve()
    if not str(out).startswith(str(cwd)):
        raise HTTPException(status_code=400, detail="output_dir must be under the current working directory")
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _job_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "created_at": now,
            "finished_at": None,
            "progress": ["Queued"],
            "result": None,
            "error": None,
        }
    t = threading.Thread(target=_run_job, args=(job_id, req), daemon=True)
    t.start()
    return JobStatus(**_jobs[job_id])


@app.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str):
    with _job_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return JobStatus(**job)


@app.get("/jobs")
def list_jobs(limit: int = 20):
    with _job_lock:
        items = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)[:limit]
    return {"jobs": [JobStatus(**j) for j in items]}


@app.websocket("/ws/{job_id}")
async def job_ws(websocket: WebSocket, job_id: str):
    """Stream progress updates for a job until it finishes."""
    await websocket.accept()
    try:
        while True:
            with _job_lock:
                job = _jobs.get(job_id)
            if not job:
                await websocket.send_json({"error": "job not found"})
                break
            await websocket.send_json({
                "job_id": job_id,
                "status": job["status"],
                "progress": job["progress"][-20:],
                "result": job.get("result") if job["status"] == "done" else None,
                "error": job.get("error"),
            })
            if job["status"] in ("done", "error"):
                break
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass


@app.post("/extract/sync")
def extract_sync(req: ExtractRequest):
    """Blocking extraction (convenient for simple clients / testing). Prefer async /extract for long jobs."""
    if _is_private_or_blocked(str(req.url)):
        raise HTTPException(status_code=400, detail="URL targets a private or blocked address")
    out = Path(req.output_dir).resolve()
    cwd = Path.cwd().resolve()
    if not str(out).startswith(str(cwd)):
        raise HTTPException(status_code=400, detail="output_dir must be under the current working directory")
    try:
        settings = _settings_from_req(req)
        result = engine_run(settings, str(req.url), mode=req.mode)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def main() -> None:
    """Start the FastAPI server.

    Binding to 0.0.0.0 is correct (listen on all interfaces). Browsers and many
    HTTP clients cannot use http://0.0.0.0 as a destination — use 127.0.0.1 or
    localhost instead. Host/port are configurable via CLI flags or env vars.
    """
    import argparse
    import os
    import socket

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="silas-api",
        description="Silas FastAPI server (REST + WebSocket progress).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SILAS_API_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1 or SILAS_API_HOST). Use 0.0.0.0 only if you intentionally want LAN access.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SILAS_API_PORT", "8765")),
        help="Port (default: 8765 or SILAS_API_PORT).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (dev only).",
    )
    args = parser.parse_args()

    host = args.host
    port = args.port

    # Human-friendly URLs (0.0.0.0 is not a valid browser destination)
    if host in ("0.0.0.0", "::", "[::]"):
        display_urls = [
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ]
        try:
            # Best-effort LAN IP so remote clients know where to connect
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect(("8.8.8.8", 80))
            lan = s.getsockname()[0]
            s.close()
            if lan and not lan.startswith("127."):
                display_urls.append(f"http://{lan}:{port}")
        except Exception:
            pass
    else:
        display_urls = [f"http://{host}:{port}"]

    print(f"Silas API v{__version__}")
    print(f"  Binding:  {host}:{port}")
    print(f"  Open in browser / client:")
    for u in display_urls:
        print(f"    {u}")
    print(f"  Docs:     {display_urls[0]}/docs")
    print(f"  Health:   {display_urls[0]}/health")
    print()

    uvicorn.run(
        "silas.api:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
