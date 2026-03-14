# -*- coding: utf-8 -*-
"""
Парсер банка ЕГЭ базы из PDF.

Форматы:
- 1, 5, 8: task.txt + answer.txt
- 2, 4, 6, 7: task.png + answer.txt
- 3: папки-группы по первой цифре (1, 2, ...), внутри context.png и подпапки x.y
"""

import os
import re
import shutil
from dataclasses import dataclass
from typing import Optional

import fitz
from PIL import Image

PDF_PATH = "data/pdfBanks/egeBaza.pdf"
OUT_DIR = "data/parsedBanks/tasksEgeBaza"

ZOOM = 2
MARGIN_LEFT = 10
MARGIN_RIGHT = 10
DEFAULT_TOP_PAD = -8
DEFAULT_BOTTOM_PAD = 10
TASK9_FIG_TOP_SHIFT = 0
TASK9_FIG_BOTTOM_SHIFT = 0
TASK9_COL_LEFT_SHIFT = 0
TASK9_COL_RIGHT_SHIFT = 0
TASK9_INNER_TRIM = 0
TASK9_ANCHOR_LEFT = 10
TASK9_ANCHOR_RIGHT = 130
TASK9_ANCHOR_TOP = 65
TASK9_ANCHOR_BOTTOM = 65
TASK9_ROW2_EXTRA_RIGHT = 50
TASK10_EXTRA_UP = -45
TASK10_BOTTOM_ADJ = -10

TASK_HEADERS = {
    1: "01. Текстовые задачи (простейшие)",
    2: "02. Размеры и единицы измерения",
    3: "03. Графики и диаграммы",
    4: "04. Преобразование выражений (формулы)",
    5: "05. Теория вероятностей",
    6: "06. Выбор оптимального варианта",
    7: "07. Анализ графиков и таблиц",
    8: "08. Анализ утверждений",
    9: "09. Площадь",
    10: "10. Прикладная планиметрия",
    11: "11. Прикладная стереометрия",
    12: "12. Планиметрия",
    13: "13. Стереометрия",
    14: "14. Действия с дробями",
    15: "15. Текстовые задачи (проценты)",
    16: "16. Вычисления и преобразования",
    17: "17. Уравнения",
    18: "18. Числа и неравенства",
    19: "19. Цифровая запись числа",
    20: "20. Текстовые задачи",
    21: "21. Задачи на смекалку",
}


@dataclass
class SectionRange:
    start: int
    end: int


@dataclass
class Marker:
    page_idx: int
    x0: float
    y0: float
    y1: float
    text: str
    bold: bool = False


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write((text or "").strip())


def norm_text(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"\u00ad", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def fix_hyphen_wraps(text: str) -> str:
    # Склейка переносов слов вида "школь-\nники"
    return re.sub(r"([A-Za-zА-Яа-яЁё])-\n([A-Za-zА-Яа-яЁё])", r"\1\2", text)


def clean_page_noise(text: str) -> str:
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            lines.append("")
            continue
        if "Е. А. Ширяева" in s or "Задачник ЕГЭбаз" in s:
            continue
        if re.match(r"^--\s*\d+\s*of\s*\d+\s*--$", s):
            continue
        lines.append(line)
    return norm_text("\n".join(lines))


def render_region(page: fitz.Page, rect: fitz.Rect) -> fitz.Pixmap:
    mat = fitz.Matrix(ZOOM, ZOOM)
    return page.get_pixmap(matrix=mat, clip=rect, alpha=False)


def safe_crop(page: fitz.Page, y0: float, y1: float, top_pad: int, bottom_pad: int) -> fitz.Rect:
    top = max(0, y0 + top_pad)
    bottom = min(page.rect.height, y1 + bottom_pad)
    if bottom <= top:
        bottom = min(page.rect.height, top + 40)
    return fitz.Rect(MARGIN_LEFT, top, page.rect.width - MARGIN_RIGHT, bottom)


def safe_crop_lr(
    page: fitz.Page,
    y0: float,
    y1: float,
    top_pad: int,
    bottom_pad: int,
    left: float,
    right: float,
) -> fitz.Rect:
    top = max(0, y0 + top_pad)
    bottom = min(page.rect.height, y1 + bottom_pad)
    if bottom <= top:
        bottom = min(page.rect.height, top + 40)
    l = max(0, left)
    r = min(page.rect.width, right)
    if r <= l:
        l, r = MARGIN_LEFT, page.rect.width - MARGIN_RIGHT
    return fitz.Rect(l, top, r, bottom)


def marker_pos(m: Marker) -> tuple[int, float]:
    return (m.page_idx, m.y0)


def split_numbered_text(section_text: str, pattern: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(pattern, section_text, re.M))
    items: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        num = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(section_text)
        body = section_text[start:end].strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        if len(body) < 8:
            continue
        items.append((num, body))
    return items


def page_texts(doc: fitz.Document) -> list[str]:
    return [doc[i].get_text() for i in range(len(doc))]


def find_answers_start(texts: list[str]) -> int:
    for i, t in enumerate(texts):
        if "ОТВЕТЫ" in t or "(ответы)" in t.lower():
            return i
    raise RuntimeError("Не найден блок ответов")


def find_header_page(texts: list[str], header: str, start: int, end: int) -> Optional[int]:
    header_line = re.compile(rf"(?m)^\s*{re.escape(header)}\s*$")
    for i in range(start, end + 1):
        t = texts[i]
        if header_line.search(t) and "Блок 1. ФИПИ" in t:
            return i
    return None


def build_section_ranges(texts: list[str], start_idx: int, end_idx: int) -> dict[int, SectionRange]:
    starts: dict[int, int] = {}
    for tid, header in TASK_HEADERS.items():
        p = find_header_page(texts, header, start_idx, end_idx)
        if p is not None:
            starts[tid] = p

    ranges: dict[int, SectionRange] = {}
    ordered = sorted(starts.items(), key=lambda x: x[1])
    for i, (tid, pstart) in enumerate(ordered):
        pend = ordered[i + 1][1] - 1 if i + 1 < len(ordered) else end_idx
        ranges[tid] = SectionRange(pstart, pend)
    return ranges


def section_text(texts: list[str], rng: SectionRange) -> str:
    parts = [clean_page_noise(texts[i]) for i in range(rng.start, rng.end + 1)]
    return norm_text("\n\n".join(parts))


def parse_simple_answers(sec_text: str) -> dict[str, str]:
    ans = {}
    for m in re.finditer(r"(\d+)\)\s*([^\n]+)", sec_text):
        ans[m.group(1)] = re.sub(r"\s+", " ", m.group(2)).strip()
    return ans


def parse_task3_answers(sec_text: str) -> dict[str, str]:
    ans = {}
    for m in re.finditer(r"(\d+\.\d+)\.?\s+([^\s\n]+)", sec_text):
        ans[m.group(1)] = m.group(2).strip()
    return ans


def parse_task8_answers(sec_text: str) -> dict[str, str]:
    ans = {}
    var_matches = list(re.finditer(r"Вариант\s+(\d+)", sec_text))
    for i, vm in enumerate(var_matches):
        var = vm.group(1)
        start = vm.end()
        end = var_matches[i + 1].start() if i + 1 < len(var_matches) else len(sec_text)
        block = sec_text[start:end]
        for m in re.finditer(r"(\d+)\)\s*([^\s\n]+)", block):
            q = m.group(1)
            ans[f"{var}.{q}"] = m.group(2).strip()
    return ans


def parse_task9_answers(sec_text: str) -> dict[str, str]:
    """
    Формат:
    Задание 1.
      1) 10 2) 14 ...
    ...
    Задание 8. 1
    """
    ans: dict[str, str] = {}
    heads = list(re.finditer(r"Задание\s+(\d+)\.\s*([^\n]*)", sec_text))
    for i, h in enumerate(heads):
        task_num = h.group(1)
        inline_tail = (h.group(2) or "").strip()
        start = h.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(sec_text)
        block = sec_text[start:end]

        found = False
        # Иногда часть ответов оказывается на той же строке, что и "Задание N."
        for m in re.finditer(r"(\d+)\)\s*([^\s\n]+)", inline_tail):
            ans[f"{task_num}.{m.group(1)}"] = m.group(2).strip()
            found = True
        for m in re.finditer(r"(\d+)\)\s*([^\s\n]+)", block):
            ans[f"{task_num}.{m.group(1)}"] = m.group(2).strip()
            found = True

        if not found:
            m_inline = re.match(r"([^\s\n]+)", inline_tail)
            if m_inline:
                ans[f"{task_num}.1"] = m_inline.group(1).strip()
    return ans


def parse_task18_answers(sec_text: str) -> dict[str, str]:
    """
    Формат для 18:
      1.1. 3142
      1.2. 4321
      ...
    """
    ans: dict[str, str] = {}
    for m in re.finditer(r"(?m)\b(\d+\.\d+)\.\s*([^\s\n]+)", sec_text):
        ans[m.group(1)] = m.group(2).strip()
    return ans


def build_answers(answer_ranges: dict[int, SectionRange], texts: list[str]) -> dict[int, dict[str, str]]:
    parsed: dict[int, dict[str, str]] = {}
    for tid, rng in answer_ranges.items():
        sec = section_text(texts, rng)
        if tid == 3:
            parsed[tid] = parse_task3_answers(sec)
        elif tid == 8:
            parsed[tid] = parse_task8_answers(sec)
        elif tid == 9:
            parsed[tid] = parse_task9_answers(sec)
        elif tid == 18:
            parsed[tid] = parse_task18_answers(sec)
        else:
            parsed[tid] = parse_simple_answers(sec)
    return parsed


def save_text_task(task_id: int, num: str, text: str, answers: dict[str, str]) -> None:
    folder = os.path.join(OUT_DIR, str(task_id), num)
    ensure_dir(folder)
    write_text(os.path.join(folder, "task.txt"), text)
    write_text(os.path.join(folder, "answer.txt"), answers.get(num, ""))


def parse_task1_text(task_text: str, answers: dict[str, str]) -> int:
    start = task_text.find("Блок 1. ФИПИ")
    body = task_text[start:] if start != -1 else task_text
    body = re.sub(r"(?m)^\s*[IVX]+\)\s+.*$", "", body)
    items = split_numbered_text(body, r"(?m)^\s*([1-9]\d{0,2})\.\s+")
    cnt = 0
    for num, txt in items:
        save_text_task(1, num, txt, answers)
        cnt += 1
    return cnt


def parse_task5_text(task_text: str, answers: dict[str, str]) -> int:
    start = task_text.find("Блок 1. ФИПИ")
    body = task_text[start:] if start != -1 else task_text
    body = re.sub(r"(?m)^\s*[IVX]+\)\s+.*$", "", body)
    items = split_numbered_text(body, r"(?m)^\s*([1-9]\d{0,3})\.\s+")
    cnt = 0
    for num, txt in items:
        save_text_task(5, num, txt, answers)
        cnt += 1
    return cnt


def parse_task8_text(task_text: str, answers: dict[str, str]) -> int:
    cnt = 0
    vmatches = list(re.finditer(r"Вариант\s+(\d+)", task_text))
    tmatches = list(re.finditer(r"Задание\s+([1-9]\d*(?:\.[1-9]\d*)?)\.\s+", task_text))
    if not tmatches:
        return 0

    def variant_for_pos(pos: int) -> str:
        v = "1"
        for vm in vmatches:
            if vm.start() <= pos:
                v = vm.group(1)
            else:
                break
        return v

    for i, tm in enumerate(tmatches):
        raw_num = tm.group(1)
        var = variant_for_pos(tm.start())
        start = tm.end()
        end = tmatches[i + 1].start() if i + 1 < len(tmatches) else len(task_text)
        txt = task_text[start:end].strip()
        txt = re.split(r"\n\s*08\.\s+Анализ утверждений|\n\s*Вариант\s+\d+", txt)[0].strip()
        txt = fix_hyphen_wraps(txt)

        if "." in raw_num:
            folder_num = raw_num
            answer_key = raw_num
        else:
            folder_num = f"{var}.{raw_num}"
            answer_key = f"{var}.{raw_num}"

        folder = os.path.join(OUT_DIR, "8", folder_num)
        ensure_dir(folder)
        write_text(os.path.join(folder, "task.txt"), txt)
        write_text(os.path.join(folder, "answer.txt"), answers.get(answer_key, ""))
        cnt += 1

    return cnt


def line_markers(
    doc: fitz.Document,
    rng: SectionRange,
    regex: str,
    left_max: Optional[float] = None,
    require_bold: bool = False,
) -> list[Marker]:
    marks: list[Marker] = []
    comp = re.compile(regex)
    for page_idx in range(rng.start, rng.end + 1):
        page = doc[page_idx]
        d = page.get_text("dict")
        lines_buf: list[Marker] = []
        for b in d.get("blocks", []):
            for ln in b.get("lines", []):
                spans = ln.get("spans", [])
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans).strip()
                if not text:
                    continue
                x0, y0, x1, y1 = ln.get("bbox", spans[0].get("bbox", (0, 0, 0, 0)))
                bold = any(("Bold" in s.get("font", "")) or (s.get("flags", 0) & 16) for s in spans)
                if left_max is not None and x0 > left_max:
                    continue
                if require_bold and not bold:
                    continue
                if comp.search(text):
                    lines_buf.append(Marker(page_idx, x0, y0, y1, text, bold))
        lines_buf.sort(key=lambda m: (m.y0, m.x0))
        marks.extend(lines_buf)
    return marks


def task3_context_markers(doc: fitz.Document, rng: SectionRange) -> list[Marker]:
    """
    Для задания 3 ищем начало контекста по последовательности слов:
    "На графике/рисунке/диаграмме".
    Это устойчиво к PDF, где первая строка разбита на отдельные слова.
    """
    markers: list[Marker] = []
    ctx_words = {"графике", "рисунке", "диаграмме"}

    for page_idx in range(rng.start, rng.end + 1):
        page = doc[page_idx]
        words = page.get_text("words") or []
        if not words:
            continue

        # words: (x0, y0, x1, y1, text, block_no, line_no, word_no)
        words.sort(key=lambda w: (float(w[1]), float(w[0])))
        for i in range(len(words) - 1):
            w1 = str(words[i][4]).strip()
            if w1 != "На":
                continue

            y0 = float(words[i][1])
            y1 = float(words[i][3])
            w2_raw = str(words[i + 1][4]).strip()
            w2 = re.sub(r"^[^A-Za-zА-Яа-яЁё]+|[^A-Za-zА-Яа-яЁё]+$", "", w2_raw).lower()
            y2 = float(words[i + 1][1])

            # Ожидаем, что это соседние слова в одной строке.
            if abs(y0 - y2) > 3:
                continue
            if w2 not in ctx_words:
                continue

            markers.append(Marker(page_idx, float(words[i][0]), y0, y1, f"На {w2}", False))

    markers.sort(key=lambda m: (m.page_idx, m.y0, m.x0))
    deduped: list[Marker] = []
    for m in markers:
        if deduped and deduped[-1].page_idx == m.page_idx and abs(deduped[-1].y0 - m.y0) <= 2:
            continue
        deduped.append(m)
    return deduped


def next_same_column_marker(marks: list[Marker], idx: int, x_thresh: float = 85) -> Optional[Marker]:
    cur = marks[idx]
    for j in range(idx + 1, len(marks)):
        m = marks[j]
        if m.page_idx < cur.page_idx:
            continue
        if m.page_idx == cur.page_idx and m.y0 <= cur.y0:
            continue
        if abs(m.x0 - cur.x0) <= x_thresh:
            return m
    return None


def next_lower_marker(marks: list[Marker], idx: int, min_dy: float = 40) -> Optional[Marker]:
    cur = marks[idx]
    for j in range(idx + 1, len(marks)):
        m = marks[j]
        if m.page_idx < cur.page_idx:
            continue
        if m.page_idx == cur.page_idx and m.y0 <= cur.y0 + min_dy:
            continue
        return m
    return None


def is_row_layout_marker(marks: list[Marker], idx: int) -> bool:
    cur = marks[idx]
    same_page = [m for m in marks if m.page_idx == cur.page_idx]
    # Если рядом по высоте есть минимум еще 2 старта с сильно отличающимся x,
    # считаем это "строчной" раскладкой (как 65-67).
    neighbors = [m for m in same_page if abs(m.y0 - cur.y0) <= 140]
    if len(neighbors) < 2:
        return False
    xs = sorted({round(m.x0, 1) for m in neighbors})
    return len(xs) >= 2 and (max(xs) - min(xs) >= 120)




def column_bounds_for_marker(
    page: fitz.Page,
    figs_on_page: list[Marker],
    cur: Marker,
    x_thresh: float = 70,
    left_shift: float = TASK9_COL_LEFT_SHIFT,
    right_shift: float = TASK9_COL_RIGHT_SHIFT,
) -> tuple[float, float]:
    """
    Динамически определяет левую/правую границу колонки для текущего рисунка.
    """
    xs = sorted(m.x0 for m in figs_on_page)
    if not xs:
        return (MARGIN_LEFT, page.rect.width - MARGIN_RIGHT)

    # Кластеризуем близкие x в опорные колонки
    clusters: list[list[float]] = []
    for x in xs:
        if not clusters or abs(x - clusters[-1][-1]) > x_thresh:
            clusters.append([x])
        else:
            clusters[-1].append(x)
    anchors = [sum(c) / len(c) for c in clusters]

    # Находим ближайшую колонку для текущего маркера
    idx = min(range(len(anchors)), key=lambda i: abs(anchors[i] - cur.x0))
    x = anchors[idx]
    prev_x = anchors[idx - 1] if idx > 0 else None
    next_x = anchors[idx + 1] if idx + 1 < len(anchors) else None

    if prev_x is None:
        left = MARGIN_LEFT
    else:
        left = (prev_x + x) / 2 - 10

    if next_x is None:
        right = page.rect.width - MARGIN_RIGHT
    else:
        right = (x + next_x) / 2 + 10

    left += left_shift
    right += right_shift
    return (left, right)


def extract_num_from_marker(text: str, regex: str) -> Optional[str]:
    m = re.search(regex, text)
    return m.group(1) if m else None


def earliest_end(a: Optional[tuple[int, float]], b: Optional[tuple[int, float]]) -> Optional[tuple[int, float]]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def find_answer_end(doc: fitz.Document, rng: SectionRange, start: Marker, next_mark: Optional[Marker]) -> Optional[tuple[int, float]]:
    end_page_limit = next_mark.page_idx if next_mark is not None else rng.end
    for p in range(start.page_idx, end_page_limit + 1):
        page = doc[p]
        d = page.get_text("dict")
        lines = []
        for b in d.get("blocks", []):
            for ln in b.get("lines", []):
                spans = ln.get("spans", [])
                if not spans:
                    continue
                txt = "".join(s.get("text", "") for s in spans).strip()
                if not txt:
                    continue
                x0, y0, x1, y1 = ln.get("bbox", spans[0].get("bbox", (0, 0, 0, 0)))
                lines.append((y0, y1, txt))
        lines.sort(key=lambda x: x[0])

        for y0, y1, txt in lines:
            if p == start.page_idx and y0 <= start.y0:
                continue
            if next_mark is not None and p == next_mark.page_idx and y0 >= next_mark.y0:
                return None
            if "Ответ:" in txt:
                return (p, y1)
    return None


def pix_to_image(pix: fitz.Pixmap) -> Image.Image:
    mode = "RGB" if pix.n < 4 else "RGBA"
    return Image.frombytes(mode, [pix.width, pix.height], pix.samples)


def save_stitched(pixmaps: list[fitz.Pixmap], out_path: str) -> None:
    images = [pix_to_image(p) for p in pixmaps]
    w = max(img.width for img in images)
    h = sum(img.height for img in images)
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    y = 0
    for img in images:
        canvas.paste(img.convert("RGB"), (0, y))
        y += img.height
    canvas.save(out_path)


def save_png_task(task_id: int, num: str, pixmaps: list[fitz.Pixmap], answers: dict[str, str]) -> None:
    folder = os.path.join(OUT_DIR, str(task_id), num)
    ensure_dir(folder)
    out = os.path.join(folder, "task.png")
    if len(pixmaps) == 1:
        pixmaps[0].save(out)
    else:
        save_stitched(pixmaps, out)
    write_text(os.path.join(folder, "answer.txt"), answers.get(num, ""))


def parse_png_by_starts(
    doc: fitz.Document,
    task_id: int,
    rng: SectionRange,
    answers: dict[str, str],
    start_regex: str,
    stop_at_answer_word: bool,
    top_pad: int = DEFAULT_TOP_PAD,
    bottom_pad: int = DEFAULT_BOTTOM_PAD,
    allow_multipage: bool = False,
    marker_left_max: Optional[float] = None,
    marker_require_bold: bool = False,
) -> int:
    marks = line_markers(
        doc,
        rng,
        start_regex,
        left_max=marker_left_max,
        require_bold=marker_require_bold,
    )
    count = 0

    for i, mk in enumerate(marks):
        num = extract_num_from_marker(mk.text, start_regex)
        if not num:
            continue

        next_mark = marks[i + 1] if i + 1 < len(marks) else None
        end_by_next: Optional[tuple[int, float]] = None
        if next_mark is not None:
            end_by_next = (next_mark.page_idx, next_mark.y0 - 3)

        end_by_answer = None
        if stop_at_answer_word:
            end_by_answer = find_answer_end(doc, rng, mk, next_mark)

        end_pos = earliest_end(end_by_next, end_by_answer)

        if end_pos is None:
            end_pos = (mk.page_idx, doc[mk.page_idx].rect.height)

        end_page, end_y = end_pos

        pixmaps: list[fitz.Pixmap] = []
        if not allow_multipage or end_page == mk.page_idx:
            page = doc[mk.page_idx]
            y1 = end_y if end_page == mk.page_idx else page.rect.height
            rect = safe_crop(page, mk.y0, y1, top_pad, bottom_pad)
            pixmaps.append(render_region(page, rect))
        else:
            # start page
            p0 = doc[mk.page_idx]
            rect0 = safe_crop(p0, mk.y0, p0.rect.height, top_pad, bottom_pad)
            pixmaps.append(render_region(p0, rect0))
            # middle pages
            for p in range(mk.page_idx + 1, end_page):
                pm = doc[p]
                rectm = safe_crop(pm, 0, pm.rect.height, 0, 0)
                pixmaps.append(render_region(pm, rectm))
            # end page
            pe = doc[end_page]
            recte = safe_crop(pe, 0, end_y, 0, bottom_pad)
            pixmaps.append(render_region(pe, recte))

        save_png_task(task_id, num, pixmaps, answers)
        count += 1

    return count


def parse_task3(doc: fitz.Document, rng: SectionRange, task3_answers: dict[str, str], task3_text: str) -> int:
    # Тексты задач
    text_items_dot = dict(split_numbered_text(task3_text, r"(?m)^\s*([1-9]\d*\.[1-9]\d*)\.\s+"))
    text_items_plain = dict(split_numbered_text(task3_text, r"(?m)^\s*((?:4[3-9]|50))\.\s+"))
    dot_matches = list(re.finditer(r"(?m)^\s*([1-9]\d*\.[1-9]\d*)\.\s+", task3_text))
    ctx_text_marks = list(re.finditer(r"(?im)^\s*(На рисунке|На графике|На диаграмме).*", task3_text))

    # Маркеры задач и контекста
    tasks = line_markers(doc, rng, r"^\s*([1-9]\d{0,2}(?:\.[1-9]\d*)?)\.")
    ctxs = task3_context_markers(doc, rng)
    if not ctxs:
        # fallback для старых/простых страниц
        ctxs = line_markers(doc, rng, r"^\s*(На рисунке|На графике|На диаграмме)")
    ctxs.sort(key=lambda x: (x.page_idx, x.y0))

    # Оставляем: x.y до 50 и плоские 43-50
    task_marks_dot: list[Marker] = []
    task_marks_plain: list[Marker] = []
    for m in tasks:
        mm = re.search(r"([1-9]\d{0,2}(?:\.[1-9]\d*)?)", m.text)
        if not mm:
            continue
        tnum = mm.group(1)
        if "." in tnum:
            major = int(tnum.split(".")[0])
            if major <= 50:
                task_marks_dot.append(Marker(m.page_idx, m.x0, m.y0, m.y1, tnum, m.bold))
        else:
            n = int(tnum)
            if 43 <= n <= 50:
                task_marks_plain.append(Marker(m.page_idx, m.x0, m.y0, m.y1, tnum, m.bold))

    if not task_marks_dot and not task_marks_plain:
        return 0

    # Группы по первой цифре (major)
    groups: dict[int, list[Marker]] = {}
    for m in task_marks_dot:
        major = int(m.text.split(".")[0])
        groups.setdefault(major, []).append(m)

    # Текстовые контексты групп: фрагмент от последнего "На рисунке/..." до first x.y
    # в окне текущей группы.
    group_first_pos: dict[int, int] = {}
    for dm in dot_matches:
        tnum = dm.group(1)
        major = int(tnum.split(".")[0])
        if major <= 50 and major not in group_first_pos:
            group_first_pos[major] = dm.start()
    sorted_majors = sorted(group_first_pos.keys())
    group_ctx_text: dict[int, str] = {}
    for i, major in enumerate(sorted_majors):
        start_pos = group_first_pos[major]
        prev_start = group_first_pos[sorted_majors[i - 1]] if i > 0 else 0
        chosen = None
        for cm in ctx_text_marks:
            p = cm.start()
            if prev_start < p < start_pos:
                chosen = cm
            if p >= start_pos:
                break
        if chosen is not None:
            ctx = task3_text[chosen.start():start_pos].strip()
            if ctx:
                group_ctx_text[major] = fix_hyphen_wraps(ctx)

    # Индекс следующей задачи внутри dot-блока (для границ context.png)
    task_marks_dot.sort(key=lambda x: (x.page_idx, x.y0))
    idx_map = {id(m): i for i, m in enumerate(task_marks_dot)}

    count = 0
    prev_group_last: Optional[Marker] = None
    for major, gmarks in sorted(groups.items()):
        gmarks.sort(key=lambda x: (x.page_idx, x.y0))
        first = gmarks[0]
        last = gmarks[-1]
        # Для контекста опираемся на маркер major.1 (если найден),
        # чтобы не смещаться на ложные x.y из текста формул.
        first_for_ctx = first
        m11 = [m for m in gmarks if m.text.startswith(f"{major}.1")]
        if m11:
            m11.sort(key=lambda x: (x.page_idx, x.y0))
            first_for_ctx = m11[0]

        group_dir = os.path.join(OUT_DIR, "3", str(major))
        ensure_dir(group_dir)

        # Контекст: последний "На рисунке/графике/диаграмме" перед первой задачей
        # в окне текущей группы (после предыдущей группы).
        chosen_ctx: Optional[Marker] = None
        lower_bound = marker_pos(prev_group_last) if prev_group_last is not None else None
        for c in ctxs:
            pos_c = marker_pos(c)
            if lower_bound is not None and pos_c <= lower_bound:
                continue
            if pos_c <= marker_pos(first_for_ctx):
                chosen_ctx = c
            else:
                break
        # Fallback: если в окне группы маркер не нашелся, берем последний до major.1.
        if chosen_ctx is None:
            for c in ctxs:
                if marker_pos(c) <= marker_pos(first_for_ctx):
                    chosen_ctx = c
                else:
                    break
        # Строгое правило: контекст только от "На рисунке/..." внутри окна группы
        # (после предыдущей группы и до первой подзадачи текущей).

        # Контекст в PNG (как в исходной структуре).
        if chosen_ctx is not None:
            cp = doc[chosen_ctx.page_idx]
            ctx_end_task = first_for_ctx
            # Исключение из промта: для 34.1-34.6 контекст до 34.2
            if major == 34 and len(gmarks) >= 2:
                ctx_end_task = gmarks[1]
            y_end = ctx_end_task.y0 - 4 if ctx_end_task.page_idx == chosen_ctx.page_idx else cp.rect.height
            crect = safe_crop(cp, chosen_ctx.y0, y_end, DEFAULT_TOP_PAD, max(0, DEFAULT_BOTTOM_PAD - 5))
            render_region(cp, crect).save(os.path.join(group_dir, "context.png"))

        # По запросу: для задания 3 сохраняем только context.png (без context.txt).

        for m in gmarks:
            tnum = m.text
            t_dir = os.path.join(group_dir, tnum)
            ensure_dir(t_dir)
            task_txt = text_items_dot.get(tnum, "")
            # Не захватываем контекст следующей группы в конце текущего задания.
            task_txt = re.split(r"(?im)\n\s*(?:На рисунке|На графике|На диаграмме)\b", task_txt)[0].strip()
            write_text(os.path.join(t_dir, "task.txt"), task_txt)
            write_text(os.path.join(t_dir, "answer.txt"), task3_answers.get(tnum, ""))
            count += 1

        prev_group_last = last

    # Исключение из промта: 43-50 отдельными папками без групп
    task_marks_plain.sort(key=lambda x: (x.page_idx, x.y0))
    for i, m in enumerate(task_marks_plain):
        tnum = m.text
        t_dir = os.path.join(OUT_DIR, "3", tnum)
        ensure_dir(t_dir)
        t_text = text_items_plain.get(tnum, "")
        t_text = re.split(r"(?im)\n\s*(?:На рисунке|На графике|На диаграмме)\b", t_text)[0].strip()
        if tnum == "50":
            mm = re.search(r"(?s)^(.*?Иванов\?)", t_text)
            if mm:
                t_text = mm.group(1)
        write_text(os.path.join(t_dir, "task.txt"), t_text)
        write_text(os.path.join(t_dir, "answer.txt"), task3_answers.get(tnum, ""))

        # Для 43-50 сохраняем условие и в PNG (как просил пользователь).
        nxt = task_marks_plain[i + 1] if i + 1 < len(task_marks_plain) else None
        page = doc[m.page_idx]
        y_end = page.rect.height
        if tnum == "50":
            d = page.get_text("dict")
            for b in d.get("blocks", []):
                for ln in b.get("lines", []):
                    spans = ln.get("spans", [])
                    if not spans:
                        continue
                    txt_line = "".join(s.get("text", "") for s in spans)
                    if "Иванов?" in txt_line:
                        bbox = ln.get("bbox", spans[0].get("bbox", (0, 0, 0, 0)))
                        y_end = min(y_end, bbox[3] + 4)
                        break
                else:
                    continue
                break
        if nxt is not None and nxt.page_idx == m.page_idx:
            y_end = min(y_end, nxt.y0 - 3)
        rect = safe_crop(page, m.y0, y_end, DEFAULT_TOP_PAD, max(0, DEFAULT_BOTTOM_PAD - 5))
        render_region(page, rect).save(os.path.join(t_dir, "task.png"))
        count += 1

    return count


def _in_interval(m: Marker, start: Marker, end: Optional[Marker], end_rng: SectionRange) -> bool:
    mp = marker_pos(m)
    sp = marker_pos(start)
    if end is not None:
        ep = marker_pos(end)
    else:
        ep = (end_rng.end, 10**9)
    return sp < mp < ep


def task9_group_margins(group_num: int) -> tuple[int, int, int, int]:
    """
    Возвращает (left, right, top, bottom) для вырезки от якоря n).
    База: L=10, R=130, T=65, B=65.
    """
    left = TASK9_ANCHOR_LEFT
    right = TASK9_ANCHOR_RIGHT
    top = TASK9_ANCHOR_TOP
    bottom = TASK9_ANCHOR_BOTTOM

    # 1 группа: сверху 20 вниз, снизу 10 вверх, слева 10 вправо, справа 10 влево
    if group_num == 1:
        top -= 20
        bottom -= 10
        left -= 10
        right -= 10

    # 2 группа: сверху 20 вниз, снизу 10 вверх, слева 10 вправо, справа 15 вправо
    if group_num == 2:
        top -= 20
        bottom -= 10
        left -= 10
        right += 15

    # 3 группа: на 10 меньше везде
    if group_num == 3:
        top -= 10
        bottom -= 10
        left -= 10
        right -= 10

    # 4 группа: слева на 15 меньше (и также как у 2-й группы по левому сдвигу)
    if group_num == 4:
        left -= 15

    # Уточнение: для 2 группы также слева -15 (поверх базового)
    if group_num == 2:
        left -= 5

    # 6 группа: как 1 группа
    if group_num == 6:
        top -= 20
        bottom -= 10
        left -= 10
        right -= 10

    # 7 группа: слева на 10 меньше
    if group_num == 7:
        left -= 10

    # не даем уйти в отрицательные/слишком узкие значения
    left = max(0, left)
    right = max(20, right)
    top = max(0, top)
    bottom = max(0, bottom)
    return left, right, top, bottom


def parse_task9(doc: fitz.Document, rng: SectionRange, answers: dict[str, str], task9_text: str) -> int:
    """
    Задание 9:
    - Задание N. (контекст)
    - ниже рисунки 1), 2), ...
    """
    count = 0
    assignment_marks = line_markers(doc, rng, r"^\s*Задание\s+([1-9]\d*)\.")
    fig_marks = line_markers(doc, rng, r"^\s*([1-9]\d*)\)")
    assignment_marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))
    fig_marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))

    # Контексты из текста секции
    context_items = dict(split_numbered_text(task9_text, r"(?m)^\s*Задание\s+([1-9]\d*)\.\s+"))

    for i, a in enumerate(assignment_marks):
        anum_match = re.search(r"Задание\s+([1-9]\d*)\.", a.text)
        if not anum_match:
            continue
        anum = anum_match.group(1)
        next_a = assignment_marks[i + 1] if i + 1 < len(assignment_marks) else None

        anum_int = int(anum)

        # Папка группы для задания N
        gdir = os.path.join(OUT_DIR, "9", anum)
        ensure_dir(gdir)

        # context.txt: от начала текста задания до первого "1)"
        raw_ctx = context_items.get(anum, "")
        ctx = re.split(r"(?m)^\s*1\)\s*", raw_ctx)[0].strip()
        write_text(os.path.join(gdir, "context.txt"), fix_hyphen_wraps(ctx))

        # С 8-го и дальше: одна общая PNG вырезка по заданию
        if anum_int >= 8:
            start_page = a.page_idx
            start_y = a.y0 - 30
            if next_a is not None:
                end_page = next_a.page_idx
                end_y = next_a.y0 - 20
            else:
                end_page = a.page_idx
                end_y = doc[a.page_idx].rect.height

            pixmaps: list[fitz.Pixmap] = []
            if end_page == start_page:
                p = doc[start_page]
                rect = safe_crop(p, start_y, end_y, 0, 0)
                pixmaps.append(render_region(p, rect))
            else:
                p0 = doc[start_page]
                rect0 = safe_crop(p0, start_y, p0.rect.height, 0, 0)
                pixmaps.append(render_region(p0, rect0))
                for pidx in range(start_page + 1, end_page):
                    pm = doc[pidx]
                    rectm = safe_crop(pm, 0, pm.rect.height, 0, 0)
                    pixmaps.append(render_region(pm, rectm))
                pe = doc[end_page]
                recte = safe_crop(pe, 0, end_y, 0, 0)
                pixmaps.append(render_region(pe, recte))

            out = os.path.join(gdir, "task.png")
            if len(pixmaps) == 1:
                pixmaps[0].save(out)
            else:
                save_stitched(pixmaps, out)
            write_text(os.path.join(gdir, "answer.txt"), answers.get(f"{anum}.1", ""))
            count += 1
            continue

        # До 7 включительно: рисунки-подзадачи внутри текущего Задание N
        # Более точная вырезка: границы по соседям в строке/колонке.
        figs = [m for m in fig_marks if _in_interval(m, a, next_a, rng)]
        figs.sort(key=lambda m: (m.page_idx, m.y0, m.x0))
        g_left, g_right, g_top, g_bottom = task9_group_margins(anum_int)
        for j, f in enumerate(figs):
            fnum_m = re.search(r"([1-9]\d*)\)", f.text)
            if not fnum_m:
                continue
            fnum = fnum_m.group(1)
            p = doc[f.page_idx]
            figs_on_page = [m for m in figs if m.page_idx == f.page_idx]

            # Горизонтальные границы по ближайшим соседям в строке.
            row_band = 28
            on_row = [m for m in figs_on_page if abs(m.y0 - f.y0) <= row_band]
            left_neighbor = None
            right_neighbor = None
            for m in on_row:
                if m.x0 < f.x0 and (left_neighbor is None or m.x0 > left_neighbor.x0):
                    left_neighbor = m
                if m.x0 > f.x0 and (right_neighbor is None or m.x0 < right_neighbor.x0):
                    right_neighbor = m

            left_bound = max(0, f.x0 - g_left)
            if left_neighbor is not None:
                # Смещаем разделитель ближе к соседу (почти в упор).
                split_left = (left_neighbor.x0 + f.x0) / 2 - (f.x0 - left_neighbor.x0) * 0.15
                left_bound = max(left_bound, split_left)

            right_bound = min(p.rect.width, f.x0 + g_right)
            if right_neighbor is not None:
                # Если справа есть сосед, берем границу почти у его якоря,
                # чтобы длинные линии текущего рисунка не обрезались раньше.
                split_right = right_neighbor.x0 - 4
                right_bound = min(p.rect.width, max(right_bound, split_right))
            else:
                # Для правого столбца оставляем дополнительный запас вправо.
                same_row_x = sorted({round(m.x0, 1) for m in on_row})
                if len(same_row_x) >= 2 and (max(same_row_x) - min(same_row_x) >= 120):
                    right_bound = min(p.rect.width, right_bound + TASK9_ROW2_EXTRA_RIGHT)

            # Вертикальные границы по соседям в той же колонке.
            col_band = 60
            prev_in_col = None
            next_in_col = None
            for m in figs_on_page:
                if abs(m.x0 - f.x0) > col_band:
                    continue
                if m.y0 < f.y0 and (prev_in_col is None or m.y0 > prev_in_col.y0):
                    prev_in_col = m
                if m.y0 > f.y0 and (next_in_col is None or m.y0 < next_in_col.y0):
                    next_in_col = m

            top_bound = max(0, f.y0 - g_top)
            if prev_in_col is not None:
                top_bound = max(top_bound, (prev_in_col.y0 + f.y0) / 2 + 2)

            bottom_bound = min(p.rect.height, f.y1 + g_bottom)
            if next_in_col is not None:
                bottom_bound = min(bottom_bound, (f.y0 + next_in_col.y0) / 2 - 2)

            if right_bound <= left_bound + 40:
                right_bound = min(p.rect.width, left_bound + 40)
            if bottom_bound <= top_bound + 40:
                bottom_bound = min(p.rect.height, top_bound + 40)

            # Корректируем бока по реальному контенту (текст + графика + raster image)
            # внутри уже рассчитанного окна, чтобы ничего не обрезать.
            bx0, by0, bx1, by1 = left_bound, top_bound, right_bound, bottom_bound
            c_left, c_right = None, None
            for w in p.get_text("words"):
                wx0, wy0, wx1, wy1 = float(w[0]), float(w[1]), float(w[2]), float(w[3])
                if wy1 <= by0 or wy0 >= by1:
                    continue
                if wx1 <= bx0 or wx0 >= bx1:
                    continue
                c_left = wx0 if c_left is None else min(c_left, wx0)
                c_right = wx1 if c_right is None else max(c_right, wx1)
            for d in p.get_drawings():
                r = d.get("rect")
                if not r:
                    continue
                if r.y1 <= by0 or r.y0 >= by1:
                    continue
                if r.x1 <= bx0 or r.x0 >= bx1:
                    continue
                rx0 = max(bx0, float(r.x0))
                rx1 = min(bx1, float(r.x1))
                if rx1 <= rx0:
                    continue
                c_left = rx0 if c_left is None else min(c_left, rx0)
                c_right = rx1 if c_right is None else max(c_right, rx1)
            # В ряде заданий 9 рисунок может быть растровым блоком, без words/drawings.
            dct = p.get_text("dict")
            for b in dct.get("blocks", []):
                if b.get("type") != 1:
                    continue
                bb = b.get("bbox")
                if not bb:
                    continue
                ix0, iy0, ix1, iy1 = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
                if iy1 <= by0 or iy0 >= by1:
                    continue
                if ix1 <= bx0 or ix0 >= bx1:
                    continue
                rx0 = max(bx0, ix0)
                rx1 = min(bx1, ix1)
                if rx1 <= rx0:
                    continue
                c_left = rx0 if c_left is None else min(c_left, rx0)
                c_right = rx1 if c_right is None else max(c_right, rx1)
            if c_left is not None and c_right is not None:
                content_w = c_right - c_left
                # Применяем только когда реально видим содержимое; затем расширяем
                # границы к содержимому, а не сжимаем их.
                if content_w >= 90:
                    left_bound = min(left_bound, max(0, c_left - 2))
                    right_bound = max(right_bound, min(p.rect.width, c_right + 2))
                    if right_bound <= left_bound + 25:
                        right_bound = min(p.rect.width, left_bound + 25)

            rect = fitz.Rect(
                left_bound,
                top_bound,
                right_bound,
                bottom_bound,
            )
            pixmaps: list[fitz.Pixmap] = [render_region(p, rect)]

            sdir = os.path.join(gdir, fnum)
            ensure_dir(sdir)
            out = os.path.join(sdir, "task.png")
            if len(pixmaps) == 1:
                pixmaps[0].save(out)
            else:
                save_stitched(pixmaps, out)
            write_text(os.path.join(sdir, "answer.txt"), answers.get(f"{anum}.{fnum}", ""))
            count += 1

    return count


def parse_task10(doc: fitz.Document, rng: SectionRange, answers: dict[str, str]) -> int:
    """
    Задание 10: png от N. до следующего N.
    Исключение:
    - 64: до строки с "Ответ дайте в" (чуть ниже)
    """
    count = 0
    # Для 10-го задания старты идут как "N.Условие" или "N. Условие"
    # Используем отдельный паттерн и не режем по x0, иначе теряются задачи.
    marks = line_markers(doc, rng, r"^\s*([1-9]\d{0,3})\.\s*[^\d\)]", require_bold=False)
    marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))

    for i, mk in enumerate(marks):
        if "Прикладная планиметрия" in mk.text:
            continue
        mm = re.search(r"^\s*([1-9]\d{0,3})\.", mk.text)
        if not mm:
            continue
        num = int(mm.group(1))
        if num in {88, 89, 93}:
            continue

        row_layout = is_row_layout_marker(marks, i)
        if row_layout:
            # По запросу: строчные задания пропускаем
            continue

        # Для много-колоночных страниц (как 65-67) ищем следующую задачу в той же колонке
        next_m = next_lower_marker(marks, i, min_dy=18)
        end_page = mk.page_idx
        end_y = doc[mk.page_idx].rect.height
        if next_m is not None and next_m.page_idx == mk.page_idx:
            end_page = mk.page_idx
            end_y = next_m.y0 - 3

        # Спец-правило для 64
        if num == 64:
            p = doc[mk.page_idx]
            d = p.get_text("dict")
            for b in d.get("blocks", []):
                for ln in b.get("lines", []):
                    spans = ln.get("spans", [])
                    if not spans:
                        continue
                    txt_line = "".join(s.get("text", "") for s in spans)
                    if "Ответ дайте в" in txt_line:
                        bbox = ln.get("bbox", spans[0].get("bbox", (0, 0, 0, 0)))
                        end_page = mk.page_idx
                        end_y = bbox[3] + 6
                        break
                else:
                    continue
                break

        # Для обычной вертикальной - по полной ширине страницы.
        page = doc[mk.page_idx]
        pixmaps: list[fitz.Pixmap] = []
        top_pad = DEFAULT_TOP_PAD + TASK10_EXTRA_UP
        rect = safe_crop(
            page,
            mk.y0,
            end_y,
            top_pad + 45,
            DEFAULT_BOTTOM_PAD + TASK10_BOTTOM_ADJ,
        )
        pixmaps.append(render_region(page, rect))

        folder = os.path.join(OUT_DIR, "10", str(num))
        ensure_dir(folder)
        out = os.path.join(folder, "task.png")
        if len(pixmaps) == 1:
            pixmaps[0].save(out)
        else:
            save_stitched(pixmaps, out)
        write_text(os.path.join(folder, "answer.txt"), answers.get(str(num), ""))
        count += 1

    return count


def main() -> None:
    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR)
    ensure_dir(OUT_DIR)

    doc = fitz.open(PDF_PATH)
    texts = page_texts(doc)

    answers_start = find_answers_start(texts)
    task_ranges = build_section_ranges(texts, 0, answers_start - 1)
    answer_ranges = build_section_ranges(texts, answers_start, len(texts) - 1)
    all_answers = build_answers(answer_ranges, texts)

    total = 0

    sec1 = section_text(texts, task_ranges[1])
    total += parse_task1_text(sec1, all_answers.get(1, {}))

    total += parse_png_by_starts(
        doc,
        2,
        task_ranges[2],
        all_answers.get(2, {}),
        r"^\s*Задание\s+([1-9]\d*)\.",
        True,
        bottom_pad=23,
    )

    sec3 = section_text(texts, task_ranges[3])
    total += parse_task3(doc, task_ranges[3], all_answers.get(3, {}), sec3)

    total += parse_png_by_starts(
        doc,
        4,
        task_ranges[4],
        all_answers.get(4, {}),
        r"^\s*([1-9]\d{0,3})\.",
        False,
        marker_left_max=65,
        marker_require_bold=True,
        bottom_pad=DEFAULT_BOTTOM_PAD - 5,
    )

    sec5 = section_text(texts, task_ranges[5])
    total += parse_task5_text(sec5, all_answers.get(5, {}))

    total += parse_png_by_starts(
        doc,
        6,
        task_ranges[6],
        all_answers.get(6, {}),
        r"^\s*([1-9]\d{0,3})\.",
        False,
        bottom_pad=-20,
    )

    # Для 7: сверху меньше, снизу после Ответ: больше, + кросс-страничный захват
    total += parse_png_by_starts(
        doc,
        7,
        task_ranges[7],
        all_answers.get(7, {}),
        r"^\s*Задание\s+([1-9]\d*)\.",
        True,
        top_pad=-3,
        bottom_pad=22,
        allow_multipage=True,
    )

    sec8 = section_text(texts, task_ranges[8])
    total += parse_task8_text(sec8, all_answers.get(8, {}))

    sec9 = section_text(texts, task_ranges[9])
    total += parse_task9(doc, task_ranges[9], all_answers.get(9, {}), sec9)

    total += parse_task10(doc, task_ranges[10], all_answers.get(10, {}))

    doc.close()
    print(f"DONE: {total} tasks saved")
    # Единая точка входа: после 1-10 сразу допарсиваем 11-21.
    try:
        import parser_ege_baze_11_21 as ext_11_21
        ext_11_21.main()
    except Exception as e:
        print(f"WARN 11-21 failed: {e}")


if __name__ == "__main__":
    main()
