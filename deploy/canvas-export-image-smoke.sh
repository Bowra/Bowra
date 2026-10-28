#!/bin/sh
set -eu

command -v pdftoppm >/dev/null
command -v soffice >/dev/null
fc-list | grep -qi "Noto Sans CJK"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
mkdir "$workdir/output"

/app/.venv/bin/python - "$workdir/input.xlsx" <<'PY'
from pathlib import Path
import sys

from openpyxl import Workbook

path = Path(sys.argv[1])
workbook = Workbook()
workbook.active["A1"] = 1
workbook.save(path)
PY

soffice \
  --headless \
  "-env:UserInstallation=file://$workdir/lo-profile" \
  --convert-to xlsx \
  --outdir "$workdir/output" \
  "$workdir/input.xlsx" >/dev/null
test -s "$workdir/output/input.xlsx"
/app/.venv/bin/python - "$workdir/output/input.xlsx" <<'PY'
from pathlib import Path
import sys

from openpyxl import load_workbook

path = Path(sys.argv[1])
workbook = load_workbook(path, read_only=True, data_only=True)
try:
    if workbook.active["A1"].value != 1:
        raise SystemExit("LibreOffice XLSX round-trip changed the fixture")
finally:
    workbook.close()
PY

# fc-list only proves fontconfig can see a CJK font; it says nothing about
# whether reportlab's TTFont embedder can actually use it. Debian's
# fonts-noto-cjk ships PostScript/CFF-outline OpenType fonts, which TTFont
# rejects outright — the renderer then silently falls back to the
# non-embedded STSong-Light CID font and every PDF with CJK text is later
# rejected by the closed-file `pdf_font_not_embedded` check. Call the real
# production font-registration function and verify it both picks something
# other than the fallback and actually embeds in a rendered PDF.
/app/.venv/bin/python - "$workdir/cjk.pdf" <<'PY'
import io
import sys
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate

from bowhead_core.export_pdf import _register_pdf_fonts

normal_font, _bold_font = _register_pdf_fonts()
if normal_font == "STSong-Light":
    raise SystemExit(
        "PDF CJK font registration fell back to the non-embedded STSong-Light "
        "font; no embeddable TrueType CJK font was found on this image"
    )

buffer = io.BytesIO()
style = ParagraphStyle("smoke", fontName=normal_font, fontSize=14)
SimpleDocTemplate(buffer, pagesize=A4).build([Paragraph("中文字体嵌入校验", style)])

reader = PdfReader(io.BytesIO(buffer.getvalue()))
fonts = (reader.pages[0].get("/Resources") or {}).get("/Font") or {}
embedded = False
for font_ref in fonts.values():
    font = font_ref.get_object()
    descriptor_ref = font.get("/FontDescriptor")
    if descriptor_ref is None:
        continue
    descriptor = descriptor_ref.get_object()
    if any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
        embedded = True
        break
if not embedded:
    raise SystemExit(f"PDF font {normal_font!r} registered but did not embed in the rendered PDF")

Path(sys.argv[1]).write_bytes(buffer.getvalue())
PY
test -s "$workdir/cjk.pdf"

cd /app/export-visual-runtime
node --input-type=module - "$workdir/browser.png" <<'JS'
import process from "node:process";
import { chromium } from "playwright";

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 64, height: 64 } });
  await page.setContent("<main style='width:64px;height:64px;background:#173d32'></main>");
  await page.screenshot({ path: process.argv[2] });
} finally {
  await browser.close();
}
JS
test -s "$workdir/browser.png"

pdftoppm -v >/dev/null 2>&1
