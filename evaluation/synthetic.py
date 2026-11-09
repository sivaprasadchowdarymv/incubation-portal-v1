"""Synthetic (non-personal) test documents: DOCX, PPTX and a minimal PDF writer."""
from __future__ import annotations

import io
import random

SECTIONS = ["Problem Statement / Motivation", "Impact", "Methodology", "Deliverables / Outcomes"]
ADJ = "cheap scalable robust local modular adaptive affordable reliable portable efficient open simple durable smart remote shared".split()
NOUN = ("system platform network device model dashboard service pipeline method tool process kit unit module interface prototype "
        "community market partner region village school team record report schedule budget pilot survey").split()
VERB = "improve reduce monitor support enable deliver measure combine connect simplify track predict extend protect".split()


def body(seed: int, words: int, keywords: list[str]) -> str:
    rnd = random.Random(seed)
    out: list[str] = []
    while len(" ".join(out).split()) < words:
        kw = rnd.choice(keywords) if keywords else rnd.choice(NOUN)
        out.append(f"The {rnd.choice(ADJ)} {rnd.choice(NOUN)} will {rnd.choice(VERB)} the {kw} {rnd.choice(NOUN)} for each {rnd.choice(NOUN)} in the {rnd.choice(NOUN)}.")
    return " ".join(" ".join(out).split()[:words])


def abstract(seed: int, words_per_section: int = 50, keywords: list[str] | None = None, omit: tuple = ()) -> list[tuple[str, str]]:
    return [(s, body(seed * 10 + i, words_per_section, keywords or [])) for i, s in enumerate(SECTIONS) if s not in omit]


def make_docx(sections: list[tuple[str, str]]) -> bytes:
    import docx

    d = docx.Document()
    for h, b in sections:
        d.add_heading(h, level=1)
        if b:
            d.add_paragraph(b)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_pptx(sections: list[tuple[str, str]]) -> bytes:
    from pptx import Presentation

    prs = Presentation()
    for h, b in sections:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = h
        s.placeholders[1].text_frame.text = b
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(sections: list[tuple[str, str]], words_per_line: int = 12, lines_per_page: int = 55) -> bytes:
    lines: list[str] = []
    for h, b in sections:
        lines.append(h)
        w = b.split()
        lines += [" ".join(w[i : i + words_per_line]) for i in range(0, len(w), words_per_line)]
    pages = [lines[i : i + lines_per_page] for i in range(0, max(len(lines), 1), lines_per_page)] or [[]]
    objs: list[bytes] = []
    n_pages = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, pl in enumerate(pages):
        stream = ("BT /F1 10 Tf 14 TL 50 790 Td " + " ".join(f"({_esc(ln)}) Tj T*" for ln in pl) + " ET").encode("latin-1", "replace")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for n, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
