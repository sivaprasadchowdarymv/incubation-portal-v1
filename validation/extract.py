"""Safe text extraction for PDF / DOCX / PPTX (+ TXT/MD for the knowledge base). Never executes content."""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = {".pdf": b"%PDF", ".docx": b"PK\x03\x04", ".pptx": b"PK\x03\x04"}
ZIP_MARKER = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml"}


@dataclass
class Precheck:
    ok: bool
    code: str = "OK"
    message: str = ""


@dataclass
class Extracted:
    text: str = ""
    units: list[dict] = field(default_factory=list)  # {"section","page","text"}
    error: str | None = None


def precheck(filename: str, data: bytes, policy: dict) -> Precheck:
    """Checks that run BEFORE a formal submission exists (do not consume an attempt)."""
    sub = policy["submission"]
    ext = Path(filename).suffix.lower()
    if ext not in sub["allowed_extensions"]:
        return Precheck(False, "INVALID_EXTENSION", f"File type '{ext or 'none'}' not allowed. Allowed: {', '.join(sub['allowed_extensions'])}")
    if not data:
        return Precheck(False, "EMPTY_FILE", "The uploaded file is empty (0 bytes).")
    if len(data) > sub["max_file_mb"] * 1024 * 1024:
        return Precheck(False, "FILE_TOO_LARGE", f"File exceeds {sub['max_file_mb']} MB limit.")
    magic = MAGIC.get(ext)
    if magic and not data.startswith(magic):
        return Precheck(False, "MIME_MISMATCH", f"File content does not match the '{ext}' format.")
    if ext in ZIP_MARKER:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if ZIP_MARKER[ext] not in names:
                    return Precheck(False, "MIME_MISMATCH", f"Archive is not a valid {ext} document.")
                total = sum(i.file_size for i in zf.infolist())
                if total > sub["max_uncompressed_mb"] * 1024 * 1024:
                    return Precheck(False, "ARCHIVE_TOO_LARGE", "Document expands beyond the allowed size.")
        except zipfile.BadZipFile:
            pass  # corrupted container: becomes a formal submission and fails FILE_READABLE
    return Precheck(True)


def _md_units(text: str) -> list[dict]:
    units, section, buf = [], None, []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            if buf and "".join(buf).strip():
                units.append({"section": section, "page": None, "text": "\n".join(buf).strip()})
            section, buf = line.lstrip("# ").strip(), [line.lstrip("# ").strip()]
        else:
            buf.append(line)
    if buf and "".join(buf).strip():
        units.append({"section": section, "page": None, "text": "\n".join(buf).strip()})
    return units


def extract(filename: str, data: bytes) -> Extracted:
    ext = Path(filename).suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                return Extracted(error="PDF is encrypted/password-protected.")
            units = [{"section": None, "page": i + 1, "text": (p.extract_text() or "").strip()} for i, p in enumerate(reader.pages)]
        elif ext == ".docx":
            import docx

            d = docx.Document(io.BytesIO(data))
            units, section, buf = [], None, []
            for p in d.paragraphs:
                style = (p.style.name or "") if p.style is not None else ""
                if style.lower().startswith(("heading", "title")) and p.text.strip():
                    if buf:
                        units.append({"section": section, "page": None, "text": "\n".join(buf)})
                    section, buf = p.text.strip(), [p.text.strip()]
                elif p.text.strip():
                    buf.append(p.text.strip())
            for t in d.tables:
                for row in t.rows:
                    buf.append(" | ".join(c.text.strip() for c in row.cells))
            if buf:
                units.append({"section": section, "page": None, "text": "\n".join(buf)})
        elif ext == ".pptx":
            from pptx import Presentation

            prs = Presentation(io.BytesIO(data))
            units = []
            for i, slide in enumerate(prs.slides, 1):
                parts, title = [], None
                if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                    title = slide.shapes.title.text_frame.text.strip() or None
                for shp in slide.shapes:
                    if shp.has_text_frame and shp.text_frame.text.strip():
                        parts.append(shp.text_frame.text.strip())
                    if getattr(shp, "has_table", False) and shp.has_table:
                        for row in shp.table.rows:
                            parts.append(" | ".join(c.text.strip() for c in row.cells))
                units.append({"section": title, "page": i, "text": "\n".join(parts)})
        elif ext in (".md", ".txt"):
            units = _md_units(data.decode("utf-8", errors="replace"))
        else:
            return Extracted(error=f"Unsupported file type '{ext}'.")
    except Exception as exc:  # corrupted / unreadable
        return Extracted(error=f"Could not read file ({type(exc).__name__}).")
    text = "\n".join(u["text"] for u in units if u["text"]).strip()
    return Extracted(text=text, units=[u for u in units if u["text"]])
