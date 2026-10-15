"""Document builders — Word, Excel, PowerPoint and PDF deliverables.

Every builder takes a plain declarative spec (the shape the model is told to
send) and writes a real file, returning ``{"ok", "path", "detail"}``. The
optional third-party packages are probed first: a missing one produces a
sentence naming the package and the pip command, never a traceback and never a
half-written file.

Styling is deliberately restrained and consistent (one accent colour, one
heading hierarchy) so generated documents look deliberate rather than default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "ACCENT",
    "available",
    "create_docx",
    "create_pptx",
    "create_xlsx",
    "pdf_extract",
    "pdf_from_text",
    "pdf_merge",
    "workspace_dir",
]

#: One accent colour for every deliverable (Jarvis cyan, print-safe).
ACCENT = "0F7C93"

_PACKAGE_HINTS = {
    "docx": ("python-docx", "pip install python-docx"),
    "openpyxl": ("openpyxl", "pip install openpyxl"),
    "pptx": ("python-pptx", "pip install python-pptx"),
    "pypdf": ("pypdf", "pip install pypdf"),
    "reportlab": ("reportlab", "pip install reportlab"),
}


def _missing(module: str) -> str:
    package, command = _PACKAGE_HINTS.get(module, (module, f"pip install {module}"))
    return (f"Building that document needs the optional '{package}' package, "
            f"which is not installed — run '{command}' and ask me again. "
            f"Nothing was written.")


def available() -> dict[str, bool]:
    """Which document engines can be used right now."""
    import importlib.util
    return {module: importlib.util.find_spec(module) is not None
            for module in _PACKAGE_HINTS}


def workspace_dir() -> Path:
    """Where generated deliverables live unless the caller names a path."""
    try:
        from config.settings import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = Path(__file__).resolve().parent.parent / "data"
    target = base / "documents"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _resolve(path: str | None, default_name: str) -> Path:
    if path:
        target = Path(str(path)).expanduser()
        if not target.is_absolute():
            target = workspace_dir() / target
    else:
        target = workspace_dir() / default_name
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _spec(value: Any) -> dict:
    """Accept a dict, or a JSON string (what the tool layer passes through)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _ok(path: Path, detail: str, **extra: Any) -> dict:
    return {"ok": True, "path": str(path), "detail": detail, **extra}


def _fail(detail: str, reason: str = "build_failed") -> dict:
    return {"ok": False, "path": "", "detail": detail, "reason": reason}


# ── Word ──────────────────────────────────────────────────────────────────────

def create_docx(content: Any, path: str | None = None, *,
                author: str = "J.A.R.V.I.S.") -> dict:
    """A formatted .docx report/resume: headings, body text, bullets, tables."""
    try:
        from docx import Document  # type: ignore
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt, RGBColor
    except Exception:
        return _fail(_missing("docx"), "missing_dependency")

    spec = _spec(content)
    if not spec:
        return _fail("I need some content for the document, sir.", "no_content")

    title = str(spec.get("title") or "Report").strip()
    target = _resolve(path, f"{_slug(title)}.docx")
    document = Document()
    try:
        normal = document.styles["Normal"]
        normal.font.name = "Calibri"
        normal.font.size = Pt(11)
        heading = document.add_heading(title, level=0)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if spec.get("subtitle"):
            sub = document.add_paragraph(str(spec["subtitle"]))
            sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
            sub.runs[0].italic = True
            sub.runs[0].font.color.rgb = RGBColor(0x55, 0x66, 0x70)

        for section in spec.get("sections") or []:
            if not isinstance(section, dict):
                continue
            if section.get("heading"):
                document.add_heading(str(section["heading"]), level=1)
            body = str(section.get("body") or "").strip()
            if body:
                for paragraph in body.split("\n\n"):
                    document.add_paragraph(paragraph.strip())
            bullets = section.get("bullets") or []
            if isinstance(bullets, str):
                bullets = [line.strip(" -*") for line in bullets.splitlines() if line.strip()]
            for bullet in bullets:
                document.add_paragraph(str(bullet), style="List Bullet")
            table = section.get("table")
            if isinstance(table, dict) and table.get("rows"):
                headers = [str(h) for h in (table.get("headers") or [])]
                rows = table.get("rows") or []
                columns = len(headers) or max((len(r) for r in rows if isinstance(r, list)),
                                              default=1)
                grid = document.add_table(rows=0, cols=columns)
                grid.style = "Light Grid Accent 1"
                if headers:
                    cells = grid.add_row().cells
                    for index, text in enumerate(headers[:columns]):
                        cells[index].text = text
                for row in rows:
                    if not isinstance(row, (list, tuple)):
                        continue
                    cells = grid.add_row().cells
                    for index, value in enumerate(list(row)[:columns]):
                        cells[index].text = str(value)
            for image in section.get("images") or []:
                try:
                    document.add_picture(str(image))
                except Exception:
                    continue
        document.save(str(target))
    except Exception as exc:
        return _fail(f"Writing the Word document failed: {exc}")
    return _ok(target, f"Word document written to {target.name}.")


def _slug(text: str, limit: int = 40) -> str:
    clean = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in str(text or ""))
    clean = "_".join(clean.split())[:limit].strip("_")
    return clean or "document"


# ── Excel ─────────────────────────────────────────────────────────────────────

def create_xlsx(content: Any, path: str | None = None) -> dict:
    """A multi-sheet .xlsx workbook with headers, number formats and charts."""
    try:
        from openpyxl import Workbook  # type: ignore
        from openpyxl.chart import BarChart, LineChart, PieChart, Reference
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except Exception:
        return _fail(_missing("openpyxl"), "missing_dependency")

    spec = _spec(content)
    sheets = spec.get("sheets") if isinstance(spec, dict) else None
    if not sheets:
        return _fail("I need at least one sheet of content for the workbook, sir.",
                     "no_content")

    title = str(spec.get("title") or "Workbook").strip()
    target = _resolve(path, f"{_slug(title)}.xlsx")
    workbook = Workbook()
    workbook.remove(workbook.active)
    header_fill = PatternFill("solid", fgColor="DDEEF2")
    header_font = Font(bold=True, color="123038")
    chart_types = {"bar": BarChart, "line": LineChart, "pie": PieChart}

    try:
        for sheet_spec in sheets:
            if not isinstance(sheet_spec, dict):
                continue
            name = str(sheet_spec.get("name") or "Sheet")[:31]
            sheet = workbook.create_sheet(title=name)
            headers = [str(h) for h in (sheet_spec.get("headers") or [])]
            for column, text in enumerate(headers, start=1):
                cell = sheet.cell(row=1, column=column, value=text)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")
            row_index = 2
            for row in sheet_spec.get("rows") or []:
                if isinstance(row, dict):
                    values = [row.get(h) for h in headers] if headers else list(row.values())
                elif isinstance(row, (list, tuple)):
                    values = list(row)
                else:
                    values = [row]
                for column, value in enumerate(values, start=1):
                    if isinstance(value, str) and value.startswith("="):
                        sheet.cell(row=row_index, column=column, value=value)
                    else:
                        try:
                            numeric = float(value)  # type: ignore[arg-type]
                            sheet.cell(row=row_index, column=column, value=numeric)
                            continue
                        except Exception:
                            pass
                        sheet.cell(row=row_index, column=column,
                                   value="" if value is None else str(value))
                row_index += 1
            for column, text in enumerate(headers, start=1):
                width = max(12, min(32, len(text) + 6))
                sheet.column_dimensions[get_column_letter(column)].width = width
            chart_spec = sheet_spec.get("chart")
            if isinstance(chart_spec, dict) and row_index > 2:
                kind = str(chart_spec.get("type") or "bar").lower()
                chart = chart_types.get(kind, BarChart)()
                chart.title = str(chart_spec.get("title") or f"{name} chart")
                data = Reference(sheet, min_col=2 if len(headers) > 1 else 1,
                                 min_row=1, max_row=row_index - 1,
                                 max_col=max(2, len(headers)))
                chart.add_data(data, titles_from_data=True)
                sheet.add_chart(chart, str(chart_spec.get("anchor") or "H2"))
        workbook.save(str(target))
    except Exception as exc:
        return _fail(f"Writing the workbook failed: {exc}")
    return _ok(target, f"Workbook written to {target.name} with "
                       f"{len(workbook.sheetnames)} sheet(s).")


# ── PowerPoint ────────────────────────────────────────────────────────────────

def create_pptx(content: Any, path: str | None = None) -> dict:
    """A branded .pptx deck: title slide, section slides, bullets, speaker notes."""
    try:
        from pptx import Presentation  # type: ignore
        from pptx.dml.color import RGBColor
        from pptx.util import Inches, Pt
    except Exception:
        return _fail(_missing("pptx"), "missing_dependency")

    spec = _spec(content)
    title = str(spec.get("title") or "Presentation").strip()
    slides = spec.get("slides")
    if not slides:
        return _fail("I need some slides for the deck, sir.", "no_content")

    target = _resolve(path, f"{_slug(title)}.pptx")
    deck = Presentation()
    deck.slide_width = Inches(13.333)
    deck.slide_height = Inches(7.5)
    accent = RGBColor.from_string(ACCENT)

    def _brand(slide) -> None:
        try:
            bar = slide.shapes.add_shape(1, Inches(0), Inches(0), deck.slide_width,
                                         Inches(0.18))
            bar.fill.solid()
            bar.fill.fore_color.rgb = accent
            bar.line.fill.background()
        except Exception:
            pass

    try:
        cover = deck.slides.add_slide(deck.slide_layouts[0])
        cover.shapes.title.text = title
        if len(cover.placeholders) > 1:
            cover.placeholders[1].text = str(spec.get("subtitle") or "")
        _brand(cover)

        for slide_spec in slides:
            if not isinstance(slide_spec, dict):
                continue
            slide = deck.slides.add_slide(deck.slide_layouts[1])
            slide.shapes.title.text = str(slide_spec.get("title") or "")
            bullets = slide_spec.get("bullets") or []
            if isinstance(bullets, str):
                bullets = [line.strip(" -*") for line in bullets.splitlines() if line.strip()]
            body = slide.placeholders[1].text_frame
            body.clear()
            for index, bullet in enumerate(bullets):
                paragraph = body.paragraphs[0] if index == 0 else body.add_paragraph()
                paragraph.text = str(bullet)
                paragraph.font.size = Pt(18)
            notes = str(slide_spec.get("notes") or "").strip()
            if notes:
                slide.notes_slide.notes_text_frame.text = notes
            if slide_spec.get("image"):
                try:
                    slide.shapes.add_picture(str(slide_spec["image"]),
                                             Inches(9.0), Inches(1.6),
                                             width=Inches(3.6))
                except Exception:
                    pass
            _brand(slide)
        deck.save(str(target))
    except Exception as exc:
        return _fail(f"Writing the presentation failed: {exc}")
    return _ok(target, f"Presentation written to {target.name} with "
                       f"{len(deck.slides.__iter__.__self__._sldIdLst)} slide(s)."
             if False else f"Presentation written to {target.name}.")


# ── PDF ───────────────────────────────────────────────────────────────────────

def pdf_from_text(content: Any, path: str | None = None) -> dict:
    """Render a titled text document to PDF (ReportLab, falling back to a
    manual minimal PDF writer when ReportLab is absent)."""
    spec = _spec(content)
    title = str(spec.get("title") or "Document").strip()
    body = str(spec.get("body") or "").strip()
    if not body:
        return _fail("I need some text for the PDF, sir.", "no_content")
    target = _resolve(path, f"{_slug(title)}.pdf")

    try:
        from reportlab.lib.pagesizes import A4  # type: ignore
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except Exception:
        return _pdf_minimal(target, title, body)

    try:
        styles = getSampleStyleSheet()
        story = [Paragraph(title, styles["Title"]), Spacer(1, 0.4 * cm)]
        for block in body.split("\n\n"):
            text = block.strip()
            if not text:
                continue
            style = styles["Heading2"] if len(text) < 70 and text.endswith(":") else styles["BodyText"]
            story.append(Paragraph(text.replace("\n", "<br/>"), style))
            story.append(Spacer(1, 0.25 * cm))
        SimpleDocTemplate(str(target), pagesize=A4, title=title).build(story)
    except Exception as exc:
        return _fail(f"Writing the PDF failed: {exc}")
    return _ok(target, f"PDF written to {target.name}.")


def _pdf_minimal(target: Path, title: str, body: str) -> dict:
    """A tiny single-font PDF writer: no dependency at all, always available."""
    try:
        lines: list[str] = [title, ""]
        for block in body.split("\n"):
            while len(block) > 95:
                lines.append(block[:95])
                block = block[95:]
            lines.append(block)
        content = ["BT /F1 11 Tf 50 780 Td 16 TL"]
        for line in lines[:60]:
            safe = (line.replace("\\", r"\\").replace("(", r"\(")
                    .replace(")", r"\)"))
            content.append(f"({safe}) Tj T*")
        content.append("ET")
        stream = "\n".join(content).encode("latin-1", "replace")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        out = bytearray(b"%PDF-1.4\n")
        offsets: list[int] = []
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        target.write_bytes(bytes(out))
    except Exception as exc:
        return _fail(f"Writing the PDF failed: {exc}")
    return _ok(target, f"PDF written to {target.name} (built-in renderer — "
                       f"install reportlab for full typography).")


def _pypdf():
    try:
        import pypdf  # type: ignore
        return pypdf
    except Exception:
        return None


def pdf_merge(paths: Any, path: str | None = None) -> dict:
    """Merge several PDFs into one."""
    sources = [str(p) for p in (paths or []) if str(p).strip()]
    if len(sources) < 2:
        return _fail("Merging needs at least two PDF paths, sir.", "no_sources")
    pypdf = _pypdf()
    if pypdf is None:
        return _fail(_missing("pypdf"), "missing_dependency")
    missing = [p for p in sources if not Path(p).exists()]
    if missing:
        return _fail("These files do not exist: " + ", ".join(missing), "not_found")
    target = _resolve(path, f"merged_{int(__import__('time').time())}.pdf")
    try:
        writer = pypdf.PdfWriter()
        for source in sources:
            writer.append(source)
        writer.write(str(target))
    except Exception as exc:
        return _fail(f"Merging failed: {exc}")
    return _ok(target, f"Merged {len(sources)} PDFs into {target.name}.")


def pdf_extract(path: str | None, pages: str = "", out_path: str | None = None) -> dict:
    """Extract pages ('1-3,5') into a new PDF and return the text as well."""
    pypdf = _pypdf()
    if pypdf is None:
        return _fail(_missing("pypdf"), "missing_dependency")
    if not path or not Path(str(path)).exists():
        return _fail("I need the path of an existing PDF, sir.", "not_found")
    source = Path(str(path))
    target = _resolve(out_path, f"{source.stem}_extract.pdf")
    try:
        reader = pypdf.PdfReader(str(source))
        total = len(reader.pages)
        wanted: list[int] = []
        if pages.strip():
            for chunk in pages.split(","):
                chunk = chunk.strip()
                if "-" in chunk:
                    start, _, end = chunk.partition("-")
                    try:
                        wanted.extend(range(max(1, int(start)), min(total, int(end)) + 1))
                    except Exception:
                        continue
                elif chunk.isdigit():
                    index = int(chunk)
                    if 1 <= index <= total:
                        wanted.append(index)
        if not wanted:
            return _fail(f"'{pages}' does not name any of the {total} page(s) in "
                         f"{source.name}.", "bad_pages")
        writer = pypdf.PdfWriter()
        for index in wanted:
            writer.add_page(reader.pages[index - 1])
        writer.write(str(target))
        text = "\n".join((reader.pages[index - 1].extract_text() or "")
                         for index in wanted[:10]).strip()
    except Exception as exc:
        return _fail(f"Extracting pages failed: {exc}")
    detail = f"Extracted page(s) {pages} into {target.name}."
    if text:
        detail += "\n" + text[:1500]
    return _ok(target, detail, pages=wanted)
