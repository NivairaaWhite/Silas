"""PDF / Office document parsing with graceful fallbacks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class DocumentResult:
    markdown: Optional[str] = None
    text: Optional[str] = None
    tables: List[Any] = field(default_factory=list)
    page_count: int = 0
    backend: str = "none"


def parse_document(data: bytes, filename: str = "") -> DocumentResult:
    lower = (filename or "").lower()

    # Prefer docling if installed (best layout)
    try:
        from docling.document_converter import DocumentConverter
        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix or ".pdf", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        converter = DocumentConverter()
        result = converter.convert(tmp_path)
        md = result.document.export_to_markdown()
        return DocumentResult(
            markdown=md, text=md,
            page_count=getattr(result.document, "page_count", 0) or 0,
            backend="docling",
        )
    except Exception:
        pass

    # pdfplumber for PDFs
    if lower.endswith(".pdf") or (len(data) > 4 and data[:4] == b"%PDF"):
        try:
            import pdfplumber
            import io
            pages_text = []
            tables = []
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text() or ""
                    pages_text.append(t)
                    for table in page.extract_tables() or []:
                        tables.append(table)
            text = "\n\n".join(pages_text)
            return DocumentResult(
                text=text, markdown=text, tables=tables,
                page_count=len(pages_text), backend="pdfplumber",
            )
        except Exception:
            pass

    # pypdf fallback
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        pages_text = [(p.extract_text() or "") for p in reader.pages]
        text = "\n\n".join(pages_text)
        return DocumentResult(
            text=text, markdown=text,
            page_count=len(pages_text), backend="pypdf",
        )
    except Exception:
        pass

    return DocumentResult(backend="none")
