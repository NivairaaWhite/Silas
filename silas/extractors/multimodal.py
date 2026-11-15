"""OCR, video keyframes, audio transcripts — optional heavy backends."""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_HAS_EASYOCR = None
_HAS_TESSERACT = None
_HAS_FFMPEG = None
_HAS_WHISPER = None


def _probe(name: str) -> bool:
    global _HAS_EASYOCR, _HAS_TESSERACT, _HAS_FFMPEG, _HAS_WHISPER
    if name == "easyocr":
        if _HAS_EASYOCR is None:
            try:
                import easyocr  # noqa
                _HAS_EASYOCR = True
            except Exception:
                _HAS_EASYOCR = False
        return _HAS_EASYOCR
    if name == "tesseract":
        if _HAS_TESSERACT is None:
            try:
                import pytesseract  # noqa
                from PIL import Image  # noqa
                _HAS_TESSERACT = True
            except Exception:
                _HAS_TESSERACT = False
        return _HAS_TESSERACT
    if name == "ffmpeg":
        if _HAS_FFMPEG is None:
            _HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
        return _HAS_FFMPEG
    if name == "whisper":
        if _HAS_WHISPER is None:
            try:
                import whisper  # noqa
                _HAS_WHISPER = True
            except Exception:
                _HAS_WHISPER = False
        return _HAS_WHISPER
    return False


def capabilities() -> dict:
    return {
        "ocr_easyocr": _probe("easyocr"),
        "ocr_tesseract": _probe("tesseract"),
        "ffmpeg": _probe("ffmpeg"),
        "whisper": _probe("whisper"),
    }


@dataclass
class MultimodalResult:
    kind: str
    ocr_text: Optional[str] = None
    description: Optional[str] = None
    transcript: Optional[str] = None
    keyframes: List[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    backend: str = "none"
    confidence: float = 0.0


def _ocr(data: bytes) -> tuple[Optional[str], str, float]:
    if _probe("easyocr"):
        try:
            import easyocr
            import numpy as np
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGB")
            reader = easyocr.Reader(["en"], gpu=False, verbose=False)
            results = reader.readtext(np.array(img))
            lines = [r[1] for r in results if r[1].strip()]
            confs = [float(r[2]) for r in results if r[1].strip()]
            text = "\n".join(lines).strip() or None
            conf = sum(confs) / len(confs) if confs else 0.0
            return text, "easyocr", conf
        except Exception as exc:
            logger.debug("easyocr: %s", exc)
    if _probe("tesseract"):
        try:
            import pytesseract
            from PIL import Image
            text = pytesseract.image_to_string(Image.open(io.BytesIO(data))).strip() or None
            return text, "tesseract", 0.6 if text else 0.0
        except Exception as exc:
            logger.debug("tesseract: %s", exc)
    return None, "none", 0.0


def extract_image(data: bytes, content_type: str = "image/jpeg") -> MultimodalResult:
    ocr, backend, conf = _ocr(data)
    return MultimodalResult(
        kind="image",
        ocr_text=ocr,
        backend=backend,
        confidence=conf,
        metadata={"bytes": len(data), "content_type": content_type},
    )


def extract_video(data: bytes, content_type: str = "video/mp4", max_frames: int = 6) -> MultimodalResult:
    result = MultimodalResult(kind="video", metadata={"bytes": len(data), "content_type": content_type})
    if not _probe("ffmpeg"):
        return result
    with tempfile.TemporaryDirectory(prefix="omni_vid_") as tmp:
        tmp_path = Path(tmp)
        vid = tmp_path / "input.bin"
        vid.write_bytes(data)
        # keyframes
        out = tmp_path / "frames"
        out.mkdir()
        pattern = str(out / "frame_%03d.jpg")
        cmd = [
            "ffmpeg", "-y", "-i", str(vid),
            "-vf", f"fps=1/{max(1, 30 // max_frames)}",
            "-frames:v", str(max_frames), "-q:v", "3", pattern,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=False)
            for i, fp in enumerate(sorted(out.glob("frame_*.jpg"))):
                ocr, backend, conf = _ocr(fp.read_bytes())
                result.keyframes.append({"index": i, "ocr_text": ocr, "backend": backend, "confidence": conf})
        except Exception as exc:
            logger.debug("ffmpeg frames: %s", exc)
        if _probe("whisper"):
            try:
                import whisper
                model = whisper.load_model("base")
                result.transcript = (model.transcribe(str(vid)).get("text") or "").strip() or None
            except Exception as exc:
                logger.debug("whisper: %s", exc)
        result.backend = "ffmpeg" + ("+whisper" if result.transcript else "")
        if result.keyframes or result.transcript:
            result.confidence = 0.7
        ocr_parts = [k["ocr_text"] for k in result.keyframes if k.get("ocr_text")]
        if ocr_parts:
            result.ocr_text = "\n---\n".join(ocr_parts)
    return result


def extract_audio(data: bytes, content_type: str = "audio/mpeg") -> MultimodalResult:
    result = MultimodalResult(kind="audio", metadata={"bytes": len(data), "content_type": content_type})
    if not _probe("whisper"):
        return result
    with tempfile.TemporaryDirectory(prefix="omni_aud_") as tmp:
        path = Path(tmp) / "audio.bin"
        path.write_bytes(data)
        try:
            import whisper
            model = whisper.load_model("base")
            result.transcript = (model.transcribe(str(path)).get("text") or "").strip() or None
            result.backend = "whisper"
            result.confidence = 0.8 if result.transcript else 0.0
        except Exception as exc:
            logger.debug("whisper audio: %s", exc)
    return result


def enrich_assets(assets: List[dict]) -> List[dict]:
    """Attach OCR / transcript enrichment to downloaded media assets."""
    enriched = []
    for asset in assets:
        path = asset.get("path")
        if not path or not Path(path).exists():
            enriched.append(asset)
            continue
        data = Path(path).read_bytes()
        kind = asset.get("asset_type", "image")
        try:
            if kind == "image":
                mm = extract_image(data, asset.get("content_type", "image/jpeg"))
            elif kind == "video":
                mm = extract_video(data, asset.get("content_type", "video/mp4"))
            elif kind == "audio":
                mm = extract_audio(data, asset.get("content_type", "audio/mpeg"))
            else:
                enriched.append(asset)
                continue
            a = dict(asset)
            a["enrichment"] = {
                "ocr_text": mm.ocr_text,
                "transcript": mm.transcript,
                "keyframes": mm.keyframes,
                "backend": mm.backend,
                "confidence": mm.confidence,
            }
            enriched.append(a)
        except Exception as exc:
            logger.warning("Enrichment failed for %s: %s", path, exc)
            enriched.append(asset)
    return enriched
