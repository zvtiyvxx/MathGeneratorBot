from pathlib import Path
from typing import Optional
import re

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT_MARGIN = 40
RIGHT_MARGIN = 40
TOP_MARGIN = 40
BOTTOM_MARGIN = 40
FONT_NAME = "BotFont"
BODY_FONT_SIZE = 14
HEADING_FONT_SIZE = 16
TITLE_FONT_SIZE = 16
BODY_LINE_HEIGHT = 20
HEADING_LINE_HEIGHT = 24
MAX_IMAGE_HEIGHT = 260


class PdfContext:
    def __init__(self, pdf: canvas.Canvas):
        self.pdf = pdf
        self.y = PAGE_HEIGHT - TOP_MARGIN
        self.font_size = BODY_FONT_SIZE
        self.line_height = BODY_LINE_HEIGHT
        self.pdf.setFont(FONT_NAME, self.font_size)

    def set_font(self, size: int, line_height: int) -> None:
        self.font_size = size
        self.line_height = line_height
        self.pdf.setFont(FONT_NAME, self.font_size)

    def ensure_space(self, needed_height: float) -> None:
        if self.y - needed_height < BOTTOM_MARGIN:
            self.pdf.showPage()
            self.pdf.setFont(FONT_NAME, self.font_size)
            self.y = PAGE_HEIGHT - TOP_MARGIN


def _group_key(source_rel_path: str) -> str:
    parts = source_rel_path.split("/")
    if len(parts) <= 1:
        return source_rel_path
    return "/".join(parts[:-1])


def _show_context_for_task(tasks: list, index: int) -> bool:
    task = tasks[index]
    if not (task.exam_type == "oge" and task.task_number == "1-5"):
        return True
    if index == 0:
        return True
    prev = tasks[index - 1]
    if not (prev.exam_type == "oge" and prev.task_number == "1-5"):
        return True
    return _group_key(task.source_rel_path) != _group_key(prev.source_rel_path)


def _is_oge_1_5(task) -> bool:
    return task.exam_type == "oge" and task.task_number == "1-5"


def _register_font() -> None:
    if FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return

    candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/tahoma.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
    ]
    for font_path in candidates:
        if font_path.exists():
            pdfmetrics.registerFont(TTFont(FONT_NAME, str(font_path)))
            return

    raise RuntimeError("Cannot find a unicode TTF font for PDF generation.")


def _draw_line(ctx: PdfContext, text: str = "", size: int = BODY_FONT_SIZE, line_height: int = BODY_LINE_HEIGHT) -> None:
    ctx.set_font(size, line_height)
    ctx.ensure_space(line_height)
    ctx.pdf.drawString(LEFT_MARGIN, ctx.y, text if text else " ")
    ctx.y -= line_height


def _draw_wrapped(
    ctx: PdfContext,
    text: str,
    size: int = BODY_FONT_SIZE,
    line_height: int = BODY_LINE_HEIGHT,
) -> None:
    ctx.set_font(size, line_height)
    words = text.split()
    if not words:
        _draw_line(ctx, "", size=size, line_height=line_height)
        return

    current = []
    max_width = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN
    for word in words:
        probe = " ".join(current + [word])
        width = pdfmetrics.stringWidth(probe, FONT_NAME, size)
        if width <= max_width:
            current.append(word)
        else:
            _draw_line(ctx, " ".join(current), size=size, line_height=line_height)
            current = [word]
    if current:
        _draw_line(ctx, " ".join(current), size=size, line_height=line_height)


def _draw_image(ctx: PdfContext, image_path: Optional[Path]) -> None:
    if image_path is None or not image_path.exists():
        return

    with Image.open(image_path) as img:
        width, height = img.size

    max_width = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN
    scale = min(max_width / width, MAX_IMAGE_HEIGHT / height, 1.0)
    draw_width = width * scale
    draw_height = height * scale

    ctx.ensure_space(draw_height + BODY_LINE_HEIGHT)
    x = LEFT_MARGIN + (max_width - draw_width) / 2
    y = ctx.y - draw_height
    ctx.pdf.drawImage(str(image_path), x, y, width=draw_width, height=draw_height, preserveAspectRatio=True)
    ctx.y = y - BODY_LINE_HEIGHT


def _normalize_text_for_output(text: str) -> str:
    text = (text or "").replace("\r", "").replace("\u00ad", "")
    text = re.sub(r"([A-Za-zА-Яа-яЁё])-\s*\n\s*([A-Za-zА-Яа-яЁё])", r"\1\2", text)
    text = re.sub(r"([A-Za-zА-Яа-яЁё])-\s+([A-Za-zА-Яа-яЁё])", r"\1\2", text)
    text = re.sub(r"\n\s*(\d+\))", r"@@LIST@@\1", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"@@LIST@@", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def write_tasks_pdf(file_path: Path, title: str, tasks: list, project_root: Path, include_answers: bool) -> None:
    _register_font()
    file_path.parent.mkdir(parents=True, exist_ok=True)

    pdf = canvas.Canvas(str(file_path), pagesize=A4)
    pdf.setAuthor("MathGeneratorBot")
    pdf.setTitle(title)
    ctx = PdfContext(pdf)

    _draw_line(ctx, title, size=TITLE_FONT_SIZE, line_height=HEADING_LINE_HEIGHT)
    _draw_line(ctx, "")

    for idx, task in enumerate(tasks, start=1):
        task_index = idx - 1
        show_context = _show_context_for_task(tasks, task_index)
        if _is_oge_1_5(task) and show_context:
            _draw_line(ctx, "Контекст к заданиям 1-5:", size=HEADING_FONT_SIZE, line_height=HEADING_LINE_HEIGHT)
            _draw_line(ctx, "")
            if task.context_text:
                _draw_wrapped(ctx, _normalize_text_for_output(task.context_text))
                _draw_line(ctx, "")
            if task.context_image_path:
                _draw_image(ctx, project_root / task.context_image_path)
                _draw_line(ctx, "")

        _draw_line(ctx, f"Задание {idx}.", size=HEADING_FONT_SIZE, line_height=HEADING_LINE_HEIGHT)
        _draw_line(ctx, "")

        if (not _is_oge_1_5(task)) and show_context and task.context_text:
            _draw_wrapped(ctx, _normalize_text_for_output(task.context_text))
            _draw_line(ctx, "")
        if (not _is_oge_1_5(task)) and show_context and task.context_image_path:
            _draw_image(ctx, project_root / task.context_image_path)
            _draw_line(ctx, "")

        if task.task_text:
            _draw_wrapped(ctx, _normalize_text_for_output(task.task_text))
        if task.task_image_path:
            _draw_image(ctx, project_root / task.task_image_path)
        _draw_line(ctx, "")

    if include_answers:
        _draw_line(ctx, "Ответы", size=HEADING_FONT_SIZE, line_height=HEADING_LINE_HEIGHT)
        _draw_line(ctx, "")
        for idx, task in enumerate(tasks, start=1):
            _draw_line(ctx, f"{idx}. {task.answer_text}")

    pdf.save()


def write_answers_pdf(file_path: Path, title: str, tasks: list) -> None:
    _register_font()
    file_path.parent.mkdir(parents=True, exist_ok=True)

    pdf = canvas.Canvas(str(file_path), pagesize=A4)
    pdf.setAuthor("MathGeneratorBot")
    pdf.setTitle(title)
    ctx = PdfContext(pdf)

    _draw_line(ctx, title, size=TITLE_FONT_SIZE, line_height=HEADING_LINE_HEIGHT)
    _draw_line(ctx, "")

    for idx, task in enumerate(tasks, start=1):
        _draw_line(ctx, f"{idx}. {task.answer_text}")

    pdf.save()
