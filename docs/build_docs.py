# -*- coding: utf-8 -*-
"""Сборка сопроводительной документации в PDF и DOCX из docs/DOCUMENTATION.md.

    docker run --rm -v "$PWD:/work" greenai-deck python3 docs/build_docs.py

Markdown → HTML (с оформлением) → LibreOffice Writer → PDF и DOCX.
Результат — docs/GreenAI_документация.pdf и .docx.
"""
import base64
import os
import re
import subprocess

import markdown

DOCS = os.path.dirname(os.path.abspath(__file__))
CSS = """
body { font-family: 'Montserrat', 'DejaVu Sans', sans-serif; font-size: 10pt; color: #1C1D22; }
h1, h2, h3 { font-family: 'Montserrat', 'DejaVu Sans', sans-serif; }
h1 { color: #310F53; font-size: 20pt; }
h2 { color: #520978; font-size: 14pt; margin-top: 18pt; }
h3 { color: #520978; font-size: 12pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; }
th { background: #520978; color: white; font-size: 9pt; padding: 3pt; }
td { border: 1px solid #D8CCE6; font-size: 9pt; padding: 3pt; }
code, pre { font-family: 'DejaVu Sans Mono', monospace; font-size: 8.5pt; background: #F4ECFA; }
blockquote { border-left: 3pt solid #FF0053; margin-left: 0; padding-left: 8pt; color: #333; }
img { max-width: 100%; }
"""


def _inline_img(m):
    """Рисунок встраивается в HTML: иначе DOCX хранит лишь ссылку на файл.
    Ширина задаётся явно — Writer не понимает max-width и растянет схему."""
    from PIL import Image
    path = os.path.join(DOCS, m.group(2))
    w, h = Image.open(path).size
    width = 560 if h > w else 640                  # px при 96 dpi, лист A4
    data = base64.b64encode(open(path, "rb").read()).decode()
    return (f'<img alt="{m.group(1)}" width="{width}" height="{round(width * h / w)}" '
            f'src="data:image/png;base64,{data}"')


def main():
    md = open(os.path.join(DOCS, "DOCUMENTATION.md"), encoding="utf-8").read()
    body = markdown.markdown(md, extensions=["tables", "fenced_code"])
    body = re.sub(r'<img alt="([^"]*)" src="([^"]+)"', _inline_img, body)
    html = (f'<html><head><meta charset="utf-8"><style>{CSS}</style></head>'
            f"<body>{body}</body></html>")
    src = os.path.join(DOCS, "GreenAI_документация.html")
    open(src, "w", encoding="utf-8").write(html)
    for fmt, filt in (("pdf", "pdf:writer_web_pdf_Export"), ("docx", 'docx:"MS Word 2007 XML"')):
        subprocess.run(f'soffice --headless --convert-to {filt} --outdir "{DOCS}" "{src}"',
                       shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.remove(src)
    print("готово:", [f for f in os.listdir(DOCS) if f.startswith("GreenAI_документация")])


if __name__ == "__main__":
    main()
