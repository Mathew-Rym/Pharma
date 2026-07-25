"""PDF output. fpdf2 + matplotlib, deliberately.

WeasyPrint gives prettier HTML-driven layout but needs system libraries (pango, cairo)
that will eat an hour on your first Railway deploy. fpdf2 is pure Python and deploys
first try. Optimise this later, not on build weekend.
"""
import io
import logging
from datetime import date

import matplotlib
matplotlib.use("Agg")             # must precede pyplot import; no display on a server
import matplotlib.pyplot as plt
import qrcode
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from utils import kes

log = logging.getLogger(__name__)

INK = (23, 37, 42)
ACCENT = (13, 124, 102)
MUTED = (110, 122, 128)
RULE = (222, 228, 230)


class Doc(FPDF):
    def __init__(self, pharmacy_name: str, doc_title: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.pharmacy_name = pharmacy_name
        self.doc_title = doc_title
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(15, 15, 15)

    def header(self):
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(*INK)
        self.cell(0, 8, self.pharmacy_name, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", "", 10)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"{self.doc_title}  ·  generated {date.today():%d %b %Y}  ·  Dishii",
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*RULE)
        self.line(15, self.get_y() + 2, 195, self.get_y() + 2)
        self.ln(6)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"Page {self.page_no()}", align="C")

    # ------------------------------------------------------------- building blocks
    def h2(self, text: str):
        self.ln(2)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*ACCENT)
        self.cell(0, 7, text.upper(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*INK)

    def kpis(self, pairs: list[tuple[str, str]]):
        """Row of big numbers across the page."""
        if not pairs:
            return
        w = 180 / len(pairs)
        y0 = self.get_y()
        for label, value in pairs:
            x = 15 + w * pairs.index((label, value))
            self.set_xy(x, y0)
            self.set_font("Helvetica", "B", 14)
            self.set_text_color(*INK)
            self.cell(w, 8, value, new_x=XPos.LEFT, new_y=YPos.NEXT)
            self.set_xy(x, y0 + 8)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(*MUTED)
            self.cell(w, 4, label.upper())
        self.set_y(y0 + 15)
        self.set_text_color(*INK)

    def table(self, headers: list[str], rows: list[list[str]], widths: list[int],
              aligns: list[str] | None = None):
        aligns = aligns or ["L"] * len(headers)
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(245, 247, 247)
        self.set_text_color(*MUTED)
        for h, w, a in zip(headers, widths, aligns):
            self.cell(w, 7, h.upper(), border=0, align=a, fill=True)
        self.ln()
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*INK)
        for r in rows:
            if self.get_y() > 262:
                self.add_page()
            for cell, w, a in zip(r, widths, aligns):
                txt = str(cell) if cell is not None else "-"
                if self.get_string_width(txt) > w - 2:
                    while txt and self.get_string_width(txt + "..") > w - 2:
                        txt = txt[:-1]
                    txt += ".."
                self.cell(w, 6, txt, border="B", align=a)
            self.ln()
        self.ln(3)

    def image_bytes(self, png: bytes, w: int = 180):
        self.image(io.BytesIO(png), w=w)
        self.ln(3)


# ------------------------------------------------------------------ charts
def bar_chart(labels: list[str], values: list[float], title: str,
              ylabel: str = "") -> bytes:
    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=150)
    ax.bar(labels, values, color="#0d7c66", width=0.6)
    ax.set_title(title, fontsize=11, loc="left", color="#17252a")
    ax.set_ylabel(ylabel, fontsize=8)
    ax.tick_params(axis="x", labelsize=8, rotation=30)
    ax.tick_params(axis="y", labelsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", alpha=0.25, linewidth=0.6)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def line_chart(labels: list[str], series: dict[str, list[float]], title: str) -> bytes:
    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=150)
    for name, vals in series.items():
        ax.plot(labels, vals, marker="o", markersize=4, linewidth=1.8, label=name)
    ax.set_title(title, fontsize=11, loc="left", color="#17252a")
    ax.tick_params(labelsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(alpha=0.25, linewidth=0.6)
    if len(series) > 1:
        ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def qr_png(data: str, box: int = 6) -> bytes:
    img = qrcode.make(data, box_size=box, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
