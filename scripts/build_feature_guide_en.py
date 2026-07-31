#!/usr/bin/env python3
"""Build the illustrated English TradeCraft user manual from Markdown."""

from __future__ import annotations

import html
import re
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak,
    Image,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "TradeCraft_User_Manual_en.md"
OUTPUT = ROOT / "TradeCraft_User_Manual_en.pdf"

INK = colors.HexColor("#111111")
MUTED = colors.HexColor("#626262")
LINE = colors.HexColor("#D7D7D7")
PANEL = colors.HexColor("#F5F5F3")
GREEN = colors.HexColor("#089981")
YELLOW = colors.HexColor("#FFF7D6")
WHITE = colors.white


def register_fonts() -> tuple[str, str]:
    candidates = [
        (
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
        ),
        (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        ),
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
    ]
    for regular, bold in candidates:
        if Path(regular).exists() and Path(bold).exists():
            kwargs = {"subfontIndex": 0} if regular.endswith(".ttc") else {}
            pdfmetrics.registerFont(TTFont("TCRegular", regular, **kwargs))
            kwargs = {"subfontIndex": 0} if bold.endswith(".ttc") else {}
            pdfmetrics.registerFont(TTFont("TCBold", bold, **kwargs))
            pdfmetrics.registerFontFamily(
                "TC",
                normal="TCRegular",
                bold="TCBold",
                italic="TCRegular",
                boldItalic="TCBold",
            )
            return "TCRegular", "TCBold"
    raise RuntimeError("A Unicode TrueType font is required to build the manual.")


REGULAR, BOLD = register_fonts()
BASE = getSampleStyleSheet()
STYLES = {
    "cover_title": ParagraphStyle(
        "CoverTitle",
        parent=BASE["Title"],
        fontName=BOLD,
        fontSize=31,
        leading=38,
        alignment=TA_CENTER,
        textColor=INK,
        spaceAfter=7,
    ),
    "cover_subtitle": ParagraphStyle(
        "CoverSubtitle",
        parent=BASE["Normal"],
        fontName=REGULAR,
        fontSize=12,
        leading=19,
        alignment=TA_CENTER,
        textColor=MUTED,
    ),
    "h1": ParagraphStyle(
        "H1",
        parent=BASE["Heading1"],
        fontName=BOLD,
        fontSize=22,
        leading=28,
        textColor=INK,
        spaceAfter=9,
    ),
    "h2": ParagraphStyle(
        "H2",
        parent=BASE["Heading2"],
        fontName=BOLD,
        fontSize=16,
        leading=21,
        textColor=INK,
        spaceBefore=4,
        spaceAfter=7,
        keepWithNext=True,
    ),
    "h3": ParagraphStyle(
        "H3",
        parent=BASE["Heading3"],
        fontName=BOLD,
        fontSize=12.5,
        leading=17,
        textColor=INK,
        spaceBefore=4,
        spaceAfter=5,
        keepWithNext=True,
    ),
    "body": ParagraphStyle(
        "Body",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=9.2,
        leading=14,
        textColor=INK,
        spaceAfter=5,
    ),
    "bullet": ParagraphStyle(
        "Bullet",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=9,
        leading=13.5,
        leftIndent=12,
        firstLineIndent=0,
        bulletIndent=1,
        textColor=INK,
        spaceAfter=3,
    ),
    "quote": ParagraphStyle(
        "Quote",
        parent=BASE["BodyText"],
        fontName=REGULAR,
        fontSize=9,
        leading=14,
        leftIndent=9,
        rightIndent=9,
        borderWidth=0.6,
        borderColor=colors.HexColor("#E3D49A"),
        borderPadding=8,
        backColor=YELLOW,
        textColor=INK,
        spaceBefore=3,
        spaceAfter=8,
    ),
    "caption": ParagraphStyle(
        "Caption",
        parent=BASE["Normal"],
        fontName=REGULAR,
        fontSize=7.8,
        leading=11,
        alignment=TA_CENTER,
        textColor=MUTED,
        spaceBefore=4,
        spaceAfter=8,
    ),
    "code": ParagraphStyle(
        "Code",
        parent=BASE["Code"],
        fontName="Courier",
        fontSize=7.4,
        leading=10.5,
        leftIndent=8,
        rightIndent=8,
        borderWidth=0.5,
        borderColor=LINE,
        borderPadding=7,
        backColor=PANEL,
        textColor=INK,
        spaceBefore=3,
        spaceAfter=7,
    ),
}


def inline_markup(value: str) -> str:
    value = html.escape(value.strip())
    value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<u>\1</u> (\2)", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"`([^`]+)`", r'<font name="TCBold">\1</font>', value)
    return value


def fitted_image(path: Path, max_width=177 * mm, max_height=151 * mm) -> Image:
    if not path.exists():
        raise FileNotFoundError(path)
    with PILImage.open(path) as source:
        width, height = source.size
    scale = min(max_width / width, max_height / height)
    return Image(str(path), width=width * scale, height=height * scale)


def image_panel(path: Path, caption: str):
    image = fitted_image(path)
    return [
        Table(
            [[image]],
            colWidths=[179 * mm],
            style=TableStyle(
                [
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("BACKGROUND", (0, 0), (-1, -1), WHITE),
                    ("BOX", (0, 0), (-1, -1), 0.7, LINE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            ),
        ),
        Paragraph(inline_markup(caption), STYLES["caption"]),
    ]


def page_decor(canvas, _doc):
    page = canvas.getPageNumber()
    width, height = A4
    canvas.saveState()
    if page > 1:
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(16 * mm, height - 13 * mm, width - 16 * mm, height - 13 * mm)
        canvas.setFont(REGULAR, 7.2)
        canvas.setFillColor(MUTED)
        canvas.drawString(16 * mm, height - 10 * mm, "TradeCraft Illustrated User Manual")
        canvas.drawRightString(width - 16 * mm, 9 * mm, f"{page:02d}")
    canvas.restoreState()


def cover_story() -> list:
    return [
        Spacer(1, 20 * mm),
        Image(str(ROOT / "static" / "site-icon.png"), width=31 * mm, height=31 * mm),
        Spacer(1, 8 * mm),
        Paragraph("TradeCraft", STYLES["cover_title"]),
        Paragraph("Illustrated User Manual", STYLES["cover_title"]),
        Spacer(1, 4 * mm),
        Paragraph("The Trade Review System for Serious Traders", STYLES["cover_subtitle"]),
        Paragraph("Know Yourself. Trade Better.", STYLES["cover_subtitle"]),
        Spacer(1, 13 * mm),
        Table(
            [["LOCAL-FIRST", "ENGLISH UI", "RANDOMIZED DEMO"]],
            colWidths=[59 * mm, 59 * mm, 59 * mm],
            rowHeights=[8 * mm],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#111111")),
                    ("BACKGROUND", (1, 0), (1, 0), GREEN),
                    ("BACKGROUND", (2, 0), (2, 0), colors.HexColor("#F2C94C")),
                    ("TEXTCOLOR", (0, 0), (1, 0), WHITE),
                    ("TEXTCOLOR", (2, 0), (2, 0), INK),
                    ("FONTNAME", (0, 0), (-1, -1), BOLD),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            ),
        ),
        Spacer(1, 12 * mm),
        Paragraph(
            "Every account value, trade, position, return, symbol, and audit conclusion shown in this manual comes from a randomized synthetic Demo workspace. No real personal data is included.",
            STYLES["quote"],
        ),
        Spacer(1, 17 * mm),
        Paragraph("v0.1.0  |  English edition  |  Apache-2.0", STYLES["cover_subtitle"]),
        PageBreak(),
    ]


def markdown_story() -> list:
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    story: list = []
    paragraph_lines: list[str] = []
    code_lines: list[str] = []
    in_code = False
    seen_first_section = False

    def flush_paragraph():
        nonlocal paragraph_lines
        if paragraph_lines:
            story.append(Paragraph(inline_markup(" ".join(paragraph_lines)), STYLES["body"]))
            paragraph_lines = []

    for index, raw in enumerate(lines):
        line = raw.rstrip()
        if line.startswith("```"):
            flush_paragraph()
            if in_code:
                story.append(Preformatted("\n".join(code_lines), STYLES["code"]))
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line:
            flush_paragraph()
            continue
        if line.startswith("# "):
            flush_paragraph()
            # The PDF cover already carries the document title.
            continue
        if re.fullmatch(r"Version .+", line):
            continue
        if line.startswith("## "):
            flush_paragraph()
            if seen_first_section:
                story.append(PageBreak())
            seen_first_section = True
            story.append(Paragraph(inline_markup(line[3:]), STYLES["h1"]))
            continue
        if line.startswith("### "):
            flush_paragraph()
            next_slice = "\n".join(lines[index + 1 : index + 7])
            if "![" in next_slice:
                story.append(CondPageBreak(190 * mm))
            story.append(Paragraph(inline_markup(line[4:]), STYLES["h2"]))
            continue
        image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line)
        if image_match:
            flush_paragraph()
            path = ROOT / image_match.group(2)
            story.extend(image_panel(path, image_match.group(1)))
            continue
        if line.startswith("> "):
            flush_paragraph()
            story.append(Paragraph(inline_markup(line[2:]), STYLES["quote"]))
            continue
        bullet_match = re.match(r"^-\s+(.+)$", line)
        if bullet_match:
            flush_paragraph()
            story.append(
                Paragraph(inline_markup(bullet_match.group(1)), STYLES["bullet"], bulletText="•")
            )
            continue
        ordered_match = re.match(r"^(\d+)\.\s+(.+)$", line)
        if ordered_match:
            flush_paragraph()
            story.append(
                Paragraph(
                    inline_markup(ordered_match.group(2)),
                    STYLES["bullet"],
                    bulletText=f"{ordered_match.group(1)}.",
                )
            )
            continue
        paragraph_lines.append(line)

    flush_paragraph()
    return story


def main():
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    story = cover_story() + markdown_story()
    doc = SimpleDocTemplate(
        str(OUTPUT),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=17 * mm,
        bottomMargin=15 * mm,
        title="TradeCraft Illustrated User Manual",
        author="TradeCraft contributors",
        subject="English user guide for the local-first TradeCraft review workflow",
    )
    doc.build(story, onFirstPage=page_decor, onLaterPages=page_decor)
    print(OUTPUT)


if __name__ == "__main__":
    main()
