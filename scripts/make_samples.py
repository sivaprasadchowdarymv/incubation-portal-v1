"""Write synthetic sample abstracts to samples/ for manual UI testing (no personal data)."""
import _path  # noqa: F401
from config.settings import ROOT
from evaluation import synthetic as sy

out = ROOT / "samples"
out.mkdir(exist_ok=True)
kw = ["irrigation", "soil moisture", "water", "sensor", "farm"]
files = {
    "valid_AG01.docx": sy.make_docx(sy.abstract(11, 50, kw)),
    "valid_AG01_b.pdf": sy.make_pdf(sy.abstract(12, 50, kw)),
    "valid_AG01_c.pptx": sy.make_pptx(sy.abstract(13, 50, kw)),
    "missing_section.docx": sy.make_docx(sy.abstract(14, 60, kw, omit=("Methodology",))),
    "too_short.docx": sy.make_docx(sy.abstract(15, 20, kw)),
    "corrupt.docx": b"PK\x03\x04" + b"\x00" * 200,
    "wrong_type.txt": b"plain text is not an allowed submission format",
}
from workflow.setup import example_template_docx  # noqa: E402

(ROOT / "examples").mkdir(exist_ok=True)
(ROOT / "examples" / "abstract_template_example.docx").write_bytes(example_template_docx())
for n, d in files.items():
    (out / n).write_bytes(d)
print("Wrote:", ", ".join(files), "->", out)
