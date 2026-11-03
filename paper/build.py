"""Assemble paper/paper.html + generated figures/tables into paper/build/paper_full.html and render the PDF.

usage: python paper/build.py      (run paper/analysis.py first)
Markers in paper.html:
  <!--F:name|caption-->   figure paper/build/figs/name.png, auto-numbered
  <!--T:name|caption-->   table paper/build/tables/name.html, auto-numbered
  <!--CODE:path-->        full source listing of a repository file (appendix)
  {{F:name}} / {{T:name}} cross-reference to the number of a figure / table
"""
import html, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
P = ROOT / "paper"
B = P / "build"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

src = (P / "paper.html").read_text(encoding="utf-8")
num = {}
HAND_TABLES = 2  # Tables 1-2 are written by hand in paper.html; generated tables continue from 3


def number(kind, name):
    key = f"{kind}:{name}"
    if key not in num:
        num[key] = sum(1 for k in num if k.startswith(kind + ":")) + 1 + (HAND_TABLES if kind == "T" else 0)
    return num[key]


# first pass: assign numbers in document order so cross-references resolve
for m in re.finditer(r"<!--([FT]):([\w]+)\|", src):
    number(m[1], m[2])


def fig(m):
    name, cap = m[1], m[2]
    n = number("F", name)
    return (f'<figure id="F-{name}"><img src="figs/{name}.png">'
            f'<figcaption><b>Figure {n}.</b> {cap}</figcaption></figure>')


def tab(m):
    name, cap = m[1], m[2]
    n = number("T", name)
    body = (B / "tables" / f"{name}.html").read_text(encoding="utf-8")
    return f'<div class="tbl" id="T-{name}"><div class="tcap"><b>Table {n}.</b> {cap}</div>{body}</div>'


def code(m):
    path = m[1].strip()
    text = (ROOT / path).read_text(encoding="utf-8")
    lines = text.rstrip("\n").split("\n")
    body = "\n".join(f'<span class="ln">{i:>4}</span> {html.escape(l)}' for i, l in enumerate(lines, 1))
    return (f'<div class="listing"><div class="lhead">{html.escape(path)} — {len(lines)} lines</div>'
            f'<pre>{body}</pre></div>')


out = re.sub(r"\{\{([FT]):(\w+)\}\}", lambda m: str(num.get(f"{m[1]}:{m[2]}", "??")), src)  # before code listings
missing = re.findall(r"\?\?", out)
out = re.sub(r"<!--F:([\w]+)\|(.*?)-->", fig, out, flags=re.S)
out = re.sub(r"<!--T:([\w]+)\|(.*?)-->", tab, out, flags=re.S)
out = re.sub(r"<!--CODE:(.*?)-->", code, out)
(B / "paper_full.html").write_text(out, encoding="utf-8")
print("figures", sum(k.startswith("F:") for k in num), "tables", sum(k.startswith("T:") for k in num) + HAND_TABLES,
      "unresolved refs", len(missing))

pdf = P / "Pandey_2026_Deribit_Lead_Lag_Crypto_Options.pdf"
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=15000",
                f"--print-to-pdf={pdf}", (B / "paper_full.html").as_uri()], check=False,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
data = pdf.read_bytes()
print(pdf.name, f"{len(data) / 1e6:.1f} MB", "pages:", len(re.findall(rb"/Type\s*/Page[^s]", data)))
