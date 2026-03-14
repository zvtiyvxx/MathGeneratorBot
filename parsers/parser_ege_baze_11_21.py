# -*- coding: utf-8 -*-
"""
Допарсер заданий 11-21 для egeBaza_compressed.pdf.

Важно:
- Не трогает уже готовые 1-10.
- Пишет только в tasksEgeBaza/11 ... tasksEgeBaza/21.
"""

import os
import re
import shutil
from pathlib import Path

import fitz

import parser_ege_baze as base


PDF_PATH = "data/pdfBanks/egeBaza.pdf"
OUT_DIR = "data/parsedBanks/tasksEgeBaza"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write((text or "").strip())


def save_png(folder: str, pixmap: fitz.Pixmap, answers: dict[str, str], key: str) -> None:
    ensure_dir(folder)
    pixmap.save(os.path.join(folder, "task.png"))
    write_text(os.path.join(folder, "answer.txt"), answers.get(key, ""))


def save_text(folder: str, text: str, answers: dict[str, str], key: str) -> None:
    ensure_dir(folder)
    write_text(os.path.join(folder, "task.txt"), text)
    write_text(os.path.join(folder, "answer.txt"), answers.get(key, ""))


def parse_answers_for_11_21(answer_ranges: dict[int, base.SectionRange], texts: list[str]) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    for tid in range(11, 22):
        rng = answer_ranges.get(tid)
        if not rng:
            out[tid] = {}
            continue
        sec = base.section_text(texts, rng)
        if tid == 18:
            out[tid] = base.parse_task18_answers(sec)
        else:
            out[tid] = base.parse_simple_answers(sec)
    return out


def parse_answer_sequence(section_text: str) -> list[str]:
    seq: list[str] = []
    for m in re.finditer(r"(?m)(\d+)\)\s*([^\n]+)", section_text):
        ans = (m.group(2) or "").strip()
        seq.append(ans)
    return seq


def parse_png_numbered(
    doc: fitz.Document,
    rng: base.SectionRange,
    task_id: int,
    answers: dict[str, str],
    regex: str = r"^\s*([1-9]\d{0,3})\.",
    top_pad: int = -8,
    bottom_pad: int = 10,
    stop_at_answer: bool = False,
    limit_max_num: int | None = None,
    exclude_nums: set[int] | None = None,
) -> int:
    exclude_nums = exclude_nums or set()
    marks = base.line_markers(doc, rng, regex)
    marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))
    count = 0

    for i, mk in enumerate(marks):
        mm = re.search(regex, mk.text)
        if not mm:
            continue
        key = mm.group(1)
        try:
            n = int(float(key))
        except Exception:
            continue

        if limit_max_num is not None and n > limit_max_num:
            continue
        if n in exclude_nums:
            continue

        next_m = marks[i + 1] if i + 1 < len(marks) else None
        end_page = mk.page_idx
        end_y = doc[mk.page_idx].rect.height
        if next_m is not None and next_m.page_idx == mk.page_idx:
            end_y = next_m.y0 - 3

        if stop_at_answer:
            ans_end = base.find_answer_end(doc, rng, mk, next_m)
            if ans_end is not None and ans_end[0] == mk.page_idx:
                end_y = min(end_y, ans_end[1])

        page = doc[mk.page_idx]
        rect = base.safe_crop(page, mk.y0, end_y, top_pad, bottom_pad)
        pix = base.render_region(page, rect)

        folder = os.path.join(OUT_DIR, str(task_id), str(n))
        save_png(folder, pix, answers, str(n))
        count += 1

    return count


def parse_text_numbered(
    section_text: str,
    task_id: int,
    answers: dict[str, str],
    regex: str = r"(?m)^\s*([1-9]\d{0,3})\.\s+",
) -> int:
    body = re.sub(r"(?m)^\s*[IVX]+\)\s+.*$", "", section_text)
    items = base.split_numbered_text(body, regex)
    count = 0
    for num, txt in items:
        try:
            n = int(float(num))
        except Exception:
            continue
        folder = os.path.join(OUT_DIR, str(task_id), str(n))
        save_text(folder, base.fix_hyphen_wraps(txt), answers, str(n))
        count += 1
    return count


def in_any_range(n: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= n <= b for a, b in ranges)


def parse_task11_grouped(
    doc: fitz.Document,
    rng: base.SectionRange,
    answers: dict[str, str],
    section_text: str,
) -> int:
    """
    11 задание: часть диапазонов идет "1 рисунок на 2 задания".
    Сохраняем один и тот же task.png в обе папки пары (n и n+1).
    """
    pair_ranges = [
        (7, 14),
        (17, 28),
        (46, 69),
        (78, 81),
        (92, 95),
        (99, 103),
        (105, 106),
        (108, 122),
    ]
    exclude = {33, 34, 35}
    txt_map = text_map_for_numbered(section_text)

    marks = base.line_markers(doc, rng, r"^\s*([1-9]\d{0,3})\.")
    marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))

    parsed: list[tuple[int, base.Marker, int]] = []
    for i, mk in enumerate(marks):
        mm = re.search(r"^\s*([1-9]\d{0,3})\.", mk.text)
        if not mm:
            continue
        n = int(mm.group(1))
        if n in exclude:
            continue
        parsed.append((n, mk, i))

    by_num: dict[int, tuple[base.Marker, int]] = {}
    for n, mk, i in parsed:
        if n not in by_num:
            by_num[n] = (mk, i)

    all_nums = sorted(by_num.keys())
    done: set[int] = set()
    count = 0

    for n in all_nums:
        if n in done:
            continue
        mk, i = by_num[n]

        # Пара "n и n+1" внутри заданных диапазонов.
        paired = False
        if in_any_range(n, pair_ranges):
            start = next(a for a, b in pair_ranges if a <= n <= b)
            if (n - start) % 2 == 0 and (n + 1) in by_num and in_any_range(n + 1, pair_ranges):
                paired = True

        if paired:
            mk2, i2 = by_num[n + 1]
            next_i = i2 + 1
            next_m = marks[next_i] if next_i < len(marks) else None

            end_page = mk2.page_idx
            end_y = doc[end_page].rect.height
            if next_m is not None and next_m.page_idx == end_page:
                end_y = next_m.y0 - 3

            pixmaps: list[fitz.Pixmap] = []
            if end_page == mk.page_idx:
                page = doc[mk.page_idx]
                # Как в 12: для пары берем именно левый контекст-рисунок.
                pair_top = max(0, min(mk.y0, mk2.y0) - 6)
                pair_bottom = min(page.rect.height, end_y - 10)
                left = min(page.rect.width - base.MARGIN_RIGHT - 40, base.MARGIN_LEFT + 10)
                text_x = min(mk.x0, mk2.x0)
                right = max(left + 40, min(page.rect.width - base.MARGIN_RIGHT, text_x - 8))
                if pair_bottom <= pair_top + 20:
                    pair_bottom = min(page.rect.height, pair_top + 120)
                rect = fitz.Rect(left, pair_top, right, pair_bottom)
                pixmaps.append(base.render_region(page, rect))
            else:
                p0 = doc[mk.page_idx]
                rect0 = base.safe_crop(p0, mk.y0, p0.rect.height, -8, 10)
                pixmaps.append(base.render_region(p0, rect0))
                for pidx in range(mk.page_idx + 1, end_page):
                    pm = doc[pidx]
                    rectm = base.safe_crop(pm, 0, pm.rect.height, 0, 0)
                    pixmaps.append(base.render_region(pm, rectm))
                pe = doc[end_page]
                recte = base.safe_crop(pe, 0, end_y, 0, 10)
                pixmaps.append(base.render_region(pe, recte))

            # Оформление как в 12: общая папка пары и подпапки заданий.
            pair_dir = os.path.join(OUT_DIR, "11", f"{n}-{n+1}")
            ensure_dir(pair_dir)
            out = os.path.join(pair_dir, "task.png")
            if len(pixmaps) == 1:
                pixmaps[0].save(out)
            else:
                base.save_stitched(pixmaps, out)

            for k in [n, n + 1]:
                sub = os.path.join(pair_dir, str(k))
                ensure_dir(sub)
                write_text(os.path.join(sub, "task.txt"), txt_map.get(k, ""))
                write_text(os.path.join(sub, "answer.txt"), answers.get(str(k), ""))
                count += 1
                done.add(k)
            continue

        # Обычный одиночный режим.
        next_i = i + 1
        next_m = marks[next_i] if next_i < len(marks) else None
        end_page = mk.page_idx
        end_y = doc[end_page].rect.height
        if next_m is not None and next_m.page_idx == end_page:
            end_y = next_m.y0 - 3
        page = doc[mk.page_idx]
        # Для негрупповых задач 11: снизу на 4 выше.
        rect = base.safe_crop(page, mk.y0, end_y, -8, 6)
        pix = base.render_region(page, rect)
        folder = os.path.join(OUT_DIR, "11", str(n))
        save_png(folder, pix, answers, str(n))
        count += 1
        done.add(n)

    return count


def parse_14_like_expressions(doc: fitz.Document, rng: base.SectionRange, task_id: int, answers: dict[str, str]) -> int:
    # Для 14/16/17: выражения обычно "1) ... 2) ..."
    return parse_png_numbered(
        doc=doc,
        rng=rng,
        task_id=task_id,
        answers=answers,
        regex=r"^\s*([1-9]\d{0,3})\)",
        top_pad=-8,
        bottom_pad=8,
    )


def tight_rect_by_elements(
    page: fitz.Page,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    pad_left: float = 4,
    pad_right: float = 4,
    pad_top: float = 4,
    pad_bottom: float = 4,
    include_drawings: bool = True,
) -> fitz.Rect:
    l0 = max(base.MARGIN_LEFT, x0)
    r0 = min(page.rect.width - base.MARGIN_RIGHT, x1)
    if r0 <= l0:
        l0, r0 = base.MARGIN_LEFT, page.rect.width - base.MARGIN_RIGHT
    top = max(0, y0)
    bottom = min(page.rect.height, y1)
    if bottom <= top:
        bottom = min(page.rect.height, top + 40)

    left = l0
    right = r0
    t = top
    b = bottom
    found = False

    # 1) Текстовые элементы
    words = page.get_text("words")
    for w in words:
        wx0, wy0, wx1, wy1 = float(w[0]), float(w[1]), float(w[2]), float(w[3])
        if wy1 <= top or wy0 >= bottom:
            continue
        if wx1 <= l0 or wx0 >= r0:
            continue
        token = str(w[4]).strip() if len(w) > 4 else ""
        if not token:
            continue
        if not found:
            left, right, t, b = wx0, wx1, wy0, wy1
            found = True
        else:
            left = min(left, wx0)
            right = max(right, wx1)
            t = min(t, wy0)
            b = max(b, wy1)

    # 2) Графические элементы (границы таблиц/линии), если есть
    if include_drawings:
        for d in page.get_drawings():
            r = d.get("rect")
            if not r:
                continue
            if r.y1 <= top or r.y0 >= bottom:
                continue
            if r.x1 <= l0 or r.x0 >= r0:
                continue
            dx0 = max(l0, float(r.x0))
            dx1 = min(r0, float(r.x1))
            if dx1 <= dx0:
                continue
            if not found:
                left, right, t, b = dx0, dx1, r.y0, r.y1
                found = True
            else:
                left = min(left, dx0)
                right = max(right, dx1)
                t = min(t, r.y0)
                b = max(b, r.y1)

    if found:
        left = max(base.MARGIN_LEFT, left - pad_left)
        right = min(page.rect.width - base.MARGIN_RIGHT, right + pad_right)
        t = max(0, t - pad_top)
        b = min(page.rect.height, b + pad_bottom)
        if right <= left:
            left, right = base.MARGIN_LEFT, page.rect.width - base.MARGIN_RIGHT
        if b <= t:
            b = min(page.rect.height, t + 40)
        return fitz.Rect(left, t, right, b)

    return base.safe_crop_lr(page, top, bottom, -2, 2, l0, r0)


def parse_task14_precise(doc: fitz.Document, rng: base.SectionRange, answer_seq: list[str], task_id: int = 14) -> int:
    marker_re = r"^\s*([1-9]\d{0,3})\)"
    marks = base.line_markers(doc, rng, marker_re)
    marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))

    # Группируем по "Задание N.", чтобы получать ключи вида N.k (1.1, 1.2, 2.1...).
    assign_re = r"^\s*Задание\s+([1-9]\d{0,3})\."
    assignment_marks = base.line_markers(doc, rng, assign_re)
    assignment_marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))
    count = 0
    # Для 14 ответы в блоке могут повторять нумерацию 1),2),...
    # поэтому сопоставляем по порядку, а не по ключу номера.
    ans_vals = answer_seq or []

    def in_interval(m: base.Marker, start: base.Marker, end: base.Marker | None) -> bool:
        sp = (start.page_idx, start.y0)
        mp = (m.page_idx, m.y0)
        ep = (end.page_idx, end.y0) if end is not None else (rng.end, 10**9)
        return sp < mp < ep

    def marker_num(m: base.Marker) -> int | None:
        mm = re.search(marker_re, m.text)
        if not mm:
            return None
        tail = (m.text[mm.end():] if mm.end() < len(m.text) else "").lstrip()
        if tail and tail[0] in "·:;,)]":
            return None
        return int(mm.group(1))

    if assignment_marks:
        for i, a in enumerate(assignment_marks):
            am = re.search(assign_re, a.text)
            if not am:
                continue
            assign_num = int(am.group(1))
            next_a = assignment_marks[i + 1] if i + 1 < len(assignment_marks) else None
            local = [m for m in marks if in_interval(m, a, next_a)]
            # Устойчиво: внутри блока сортируем по номеру n), затем по позиции.
            local.sort(key=lambda m: (marker_num(m) if marker_num(m) is not None else 10**9, m.page_idx, m.y0, m.x0))

            for mk in local:
                sub_num = marker_num(mk)
                if sub_num is None:
                    continue
                page = doc[mk.page_idx]
                same_page = [m for m in local if m.page_idx == mk.page_idx]
                same_page.sort(key=lambda m: (m.y0, m.x0))
                TOP_PAD = -8
                BOTTOM_PAD = 27
                LEFT_PAD = 4
                RIGHT_PAD = 4

                top_bound = max(0, mk.y0 + TOP_PAD)
                bottom_bound = min(page.rect.height, mk.y1 + BOTTOM_PAD)

                row_band = 18
                on_row = [m for m in same_page if abs(m.y0 - mk.y0) <= row_band]
                right_neighbor = None
                for m in on_row:
                    if m.x0 > mk.x0 and (right_neighbor is None or m.x0 < right_neighbor.x0):
                        right_neighbor = m
                right_limit = (page.rect.width - base.MARGIN_RIGHT) if right_neighbor is None else (right_neighbor.x0 - 6)

                next_in_col = None
                col_thresh = 34
                for m in same_page:
                    if m.y0 <= mk.y0:
                        continue
                    if abs(m.x0 - mk.x0) <= col_thresh:
                        if next_in_col is None or m.y0 < next_in_col.y0:
                            next_in_col = m
                if next_in_col is not None:
                    bottom_bound = min(bottom_bound, next_in_col.y0 - 5)
                    bottom_bound = max(bottom_bound, mk.y1 + 8)

                left_bound = max(base.MARGIN_LEFT, mk.x0 - LEFT_PAD)

                words = page.get_text("words")
                expr_words = []
                for w in words:
                    wx0, wy0, wx1, wy1 = float(w[0]), float(w[1]), float(w[2]), float(w[3])
                    if wy1 <= top_bound or wy0 >= bottom_bound:
                        continue
                    if wx0 < mk.x0 - 2:
                        continue
                    if wx1 > right_limit:
                        continue
                    expr_words.append((wx0, wy0, wx1, wy1))

                if expr_words:
                    right_bound = min(page.rect.width - base.MARGIN_RIGHT, max(w[2] for w in expr_words) + RIGHT_PAD)
                else:
                    right_bound = min(page.rect.width - base.MARGIN_RIGHT, right_limit)
                if right_bound <= left_bound + 30:
                    right_bound = min(page.rect.width - base.MARGIN_RIGHT, left_bound + 80)

                rect = fitz.Rect(left_bound, top_bound, right_bound, bottom_bound)
                pix = base.render_region(page, rect)
                key = f"{assign_num}.{sub_num}"
                folder = os.path.join(OUT_DIR, str(task_id), key)
                ensure_dir(folder)
                pix.save(os.path.join(folder, "task.png"))
                write_text(os.path.join(folder, "answer.txt"), ans_vals[count] if count < len(ans_vals) else "")
                count += 1
    else:
        # fallback на старую линейную нумерацию, если не нашли "Задание N."
        for mk in marks:
            sub_num = marker_num(mk)
            if sub_num is None:
                continue
            n = count + 1
            page = doc[mk.page_idx]
            rect = base.safe_crop(page, mk.y0, page.rect.height, -8, 27)
            pix = base.render_region(page, rect)
            folder = os.path.join(OUT_DIR, str(task_id), str(n))
            ensure_dir(folder)
            pix.save(os.path.join(folder, "task.png"))
            write_text(os.path.join(folder, "answer.txt"), ans_vals[count] if count < len(ans_vals) else "")
            count += 1

    return count


def text_map_for_numbered(section_text: str) -> dict[int, str]:
    body = re.sub(r"(?m)^\s*[IVX]+\)\s+.*$", "", section_text)
    items = base.split_numbered_text(body, r"(?m)^\s*([1-9]\d{0,3})\.\s+")
    out: dict[int, str] = {}
    for num, txt in items:
        try:
            out[int(num)] = base.fix_hyphen_wraps(txt)
        except Exception:
            pass
    return out


def marker_map_for_numbered(doc: fitz.Document, rng: base.SectionRange) -> dict[int, base.Marker]:
    marks = base.line_markers(doc, rng, r"^\s*([1-9]\d{0,3})\.")
    marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))
    out: dict[int, base.Marker] = {}
    for m in marks:
        mm = re.search(r"^\s*([1-9]\d{0,3})\.", m.text)
        if not mm:
            continue
        n = int(mm.group(1))
        if n not in out:
            out[n] = m
    return out


def ordered_numbered_markers(doc: fitz.Document, rng: base.SectionRange) -> tuple[list[tuple[int, base.Marker]], dict[int, tuple[base.Marker, int]]]:
    marks = base.line_markers(doc, rng, r"^\s*([1-9]\d{0,3})\.")
    marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))
    ordered: list[tuple[int, base.Marker]] = []
    by_num: dict[int, tuple[base.Marker, int]] = {}
    for m in marks:
        mm = re.search(r"^\s*([1-9]\d{0,3})\.", m.text)
        if not mm:
            continue
        n = int(mm.group(1))
        idx = len(ordered)
        ordered.append((n, m))
        if n not in by_num:
            by_num[n] = (m, idx)
    return ordered, by_num


def save_single_png_from_markers(
    doc: fitz.Document,
    ordered: list[tuple[int, base.Marker]],
    by_num: dict[int, tuple[base.Marker, int]],
    task_id: int,
    n: int,
    answers: dict[str, str],
    top_pad: int = -8,
    bottom_pad: int = 6,
) -> int:
    item = by_num.get(n)
    if not item:
        return 0
    mk, idx = item
    next_m = ordered[idx + 1][1] if idx + 1 < len(ordered) else None
    end_y = doc[mk.page_idx].rect.height
    if next_m is not None and next_m.page_idx == mk.page_idx:
        end_y = next_m.y0 - 3
    page = doc[mk.page_idx]
    rect = base.safe_crop(page, mk.y0, end_y, top_pad, bottom_pad)
    pix = base.render_region(page, rect)
    folder = os.path.join(OUT_DIR, str(task_id), str(n))
    save_png(folder, pix, answers, str(n))
    return 1


def next_marker_same_page(markers: dict[int, base.Marker], n: int) -> base.Marker | None:
    cur = markers.get(n)
    if not cur:
        return None
    candidates = [m for k, m in markers.items() if k > n and m.page_idx == cur.page_idx and m.y0 > cur.y0]
    if not candidates:
        return None
    candidates.sort(key=lambda m: m.y0)
    return candidates[0]


def save_context_group(
    doc: fitz.Document,
    task_id: int,
    group_name: str,
    numbers: list[int],
    marker_map: dict[int, base.Marker],
    text_map: dict[int, str],
    answers: dict[str, str],
    with_context: bool,
) -> int:
    gdir = Path(OUT_DIR) / str(task_id) / group_name
    ensure_dir(str(gdir))
    count = 0

    if with_context:
        first = marker_map.get(numbers[0])
        if first:
            page = doc[first.page_idx]
            # Контекст слева от текста задач
            left = base.MARGIN_LEFT
            right = max(left + 40, min(page.rect.width - base.MARGIN_RIGHT, first.x0 - 2))
            top = max(0, first.y0 - 20)
            bottom = min(page.rect.height, top + 250)
            last = marker_map.get(numbers[-1])
            if last and last.page_idx == first.page_idx:
                nxt = next_marker_same_page(marker_map, numbers[-1])
                if nxt:
                    bottom = min(page.rect.height, nxt.y0 - 8)
                else:
                    bottom = min(page.rect.height, last.y0 + 180)

            if task_id == 12:
                # По промту для 12:
                # - контекст ограничен по верх/низом двух задач пары;
                # - левый край дополнительно правее на 10;
                # - правый край чуть левее текста.
                left = min(page.rect.width - base.MARGIN_RIGHT - 40, base.MARGIN_LEFT + 10)
                right = max(left + 40, min(page.rect.width - base.MARGIN_RIGHT, first.x0 - 8))
                top = max(0, first.y0 - 6)
                if last and last.page_idx == first.page_idx:
                    nxt = next_marker_same_page(marker_map, numbers[-1])
                    if nxt:
                        bottom = min(page.rect.height, nxt.y0 - 10)
                    else:
                        bottom = min(page.rect.height, last.y0 + 120)
                else:
                    bottom = min(page.rect.height, top + 220)
                bottom = max(top + 30, bottom)
            rect = fitz.Rect(left, top, right, bottom)
            pix = base.render_region(page, rect)
            pix.save(str(gdir / "context.png"))

    for n in numbers:
        sdir = gdir / str(n)
        ensure_dir(str(sdir))
        write_text(str(sdir / "task.txt"), text_map.get(n, ""))
        write_text(str(sdir / "answer.txt"), answers.get(str(n), ""))
        count += 1

    return count


def parse_task12_grouped(doc: fitz.Document, rng: base.SectionRange, answers: dict[str, str], section_text: str) -> int:
    # До 200 задач
    text_map = {k: v for k, v in text_map_for_numbered(section_text).items() if k <= 200}
    marker_map = marker_map_for_numbered(doc, rng)
    ordered, by_num = ordered_numbered_markers(doc, rng)
    count = 0

    no_image = set(range(99, 103)) | {145, 146} | set(range(151, 155)) | set(range(163, 175))
    one_img = set(range(71, 83))

    n = 1
    while n <= 200:
        if n not in text_map:
            n += 1
            continue
        if n in no_image:
            # Без контекста
            folder = Path(OUT_DIR) / "12" / str(n)
            ensure_dir(str(folder))
            write_text(str(folder / "task.txt"), text_map.get(n, ""))
            write_text(str(folder / "answer.txt"), answers.get(str(n), ""))
            count += 1
            n += 1
            continue
        if n in one_img:
            # Одиночные в 12 сохраняем как целое задание в PNG.
            count += save_single_png_from_markers(doc, ordered, by_num, 12, n, answers)
            n += 1
            continue

        # Обычный парный формат (2 задачи на 1 контекст)
        if (n + 1) in text_map and (n + 1) not in no_image and (n + 1) not in one_img:
            count += save_context_group(doc, 12, f"{n}-{n+1}", [n, n + 1], marker_map, text_map, answers, with_context=True)
            n += 2
        else:
            count += save_context_group(doc, 12, str(n), [n], marker_map, text_map, answers, with_context=True)
            n += 1

    return count


def parse_task13_grouped(doc: fitz.Document, rng: base.SectionRange, answers: dict[str, str], section_text: str) -> int:
    text_map = text_map_for_numbered(section_text)
    marker_map = marker_map_for_numbered(doc, rng)
    ordered, by_num = ordered_numbered_markers(doc, rng)
    count = 0

    skip = set(range(7, 11)) | {75, 76} | set(range(81, 87)) | set(range(97, 100))
    one_img = set(range(100, 113))

    n = 1
    nmax = max(text_map.keys()) if text_map else 0
    while n <= nmax:
        if n in skip or n not in text_map:
            n += 1
            continue
        if n in one_img:
            # Одиночные в 13 сохраняем как целое задание в PNG.
            count += save_single_png_from_markers(doc, ordered, by_num, 13, n, answers)
            n += 1
            continue

        if (n + 1) in text_map and (n + 1) not in skip and (n + 1) not in one_img:
            count += save_context_group(doc, 13, f"{n}-{n+1}", [n, n + 1], marker_map, text_map, answers, with_context=True)
            n += 2
        else:
            count += save_context_group(doc, 13, str(n), [n], marker_map, text_map, answers, with_context=True)
            n += 1

    return count


def parse_task18(doc: fitz.Document, rng: base.SectionRange, answers: dict[str, str]) -> int:
    # По промту: формат 1.1., png до "Ответ:"
    marks = base.line_markers(doc, rng, r"^\s*([1-9]\d*\.[1-9]\d*)\.")
    marks.sort(key=lambda m: (m.page_idx, m.y0, m.x0))
    count = 0
    for i, mk in enumerate(marks):
        mm = re.search(r"^\s*([1-9]\d*\.[1-9]\d*)\.", mk.text)
        if not mm:
            continue
        key = mm.group(1)
        next_m = marks[i + 1] if i + 1 < len(marks) else None
        end_page = mk.page_idx
        end_y = doc[mk.page_idx].rect.height
        if next_m is not None and next_m.page_idx == mk.page_idx:
            end_y = next_m.y0 - 3
        ans_end = base.find_answer_end(doc, rng, mk, next_m)
        if ans_end is not None and ans_end[0] == mk.page_idx:
            end_y = min(end_y, ans_end[1] + 6)

        page = doc[mk.page_idx]
        rect = base.safe_crop(page, mk.y0, end_y, -8, 35)
        pix = base.render_region(page, rect)

        folder = os.path.join(OUT_DIR, "18", key)
        save_png(folder, pix, answers, key)
        count += 1
    return count


def main() -> None:
    ensure_dir(OUT_DIR)
    for tid in range(11, 22):
        p = Path(OUT_DIR) / str(tid)
        if p.exists():
            shutil.rmtree(p)

    doc = fitz.open(PDF_PATH)
    texts = base.page_texts(doc)
    answers_start = base.find_answers_start(texts)

    task_ranges = base.build_section_ranges(texts, 0, answers_start - 1)
    answer_ranges = base.build_section_ranges(texts, answers_start, len(texts) - 1)
    all_answers = parse_answers_for_11_21(answer_ranges, texts)

    total = 0

    # 11: смешанный режим, часть диапазонов "1 рисунок на 2 задания"
    sec11 = base.section_text(texts, task_ranges[11])
    total += parse_task11_grouped(doc, task_ranges[11], all_answers[11], sec11)

    # 12: группировка + отдельный контекст + текст условий
    sec12 = base.section_text(texts, task_ranges[12])
    total += parse_task12_grouped(doc, task_ranges[12], all_answers[12], sec12)

    # 13: группировка + отдельный контекст + текст условий + исключения
    sec13 = base.section_text(texts, task_ranges[13])
    total += parse_task13_grouped(doc, task_ranges[13], all_answers[13], sec13)

    # 14: выражения 1),2),... с точным кропом по bbox элементов
    ans14_text = base.section_text(texts, answer_ranges[14])
    ans14_seq = parse_answer_sequence(ans14_text)
    total += parse_task14_precise(doc, task_ranges[14], ans14_seq)

    # 15: текст
    sec15 = base.section_text(texts, task_ranges[15])
    total += parse_text_numbered(sec15, 15, all_answers[15])

    # 16: как 14 (та же логика точной вырезки и последовательных ответов)
    ans16_text = base.section_text(texts, answer_ranges[16])
    ans16_seq = parse_answer_sequence(ans16_text)
    total += parse_task14_precise(doc, task_ranges[16], ans16_seq, task_id=16)

    # 17: как 14 (та же логика точной вырезки и последовательных ответов)
    ans17_text = base.section_text(texts, answer_ranges[17])
    ans17_seq = parse_answer_sequence(ans17_text)
    total += parse_task14_precise(doc, task_ranges[17], ans17_seq, task_id=17)

    # 18: формат 1.1 + Ответ:
    total += parse_task18(doc, task_ranges[18], all_answers[18])

    # 19: png
    total += parse_png_numbered(doc, task_ranges[19], 19, all_answers[19], bottom_pad=6)

    # 20: текст
    sec20 = base.section_text(texts, task_ranges[20])
    total += parse_text_numbered(sec20, 20, all_answers[20])

    # 21: текст, но 107-112 png
    sec21 = base.section_text(texts, task_ranges[21])
    total += parse_text_numbered(sec21, 21, all_answers[21])
    total += parse_png_numbered(
        doc, task_ranges[21], 21, all_answers[21],
        top_pad=-8, bottom_pad=10,
        limit_max_num=112,
        exclude_nums=set(range(1, 107)),
    )
    # Для 107-112 оставляем только png+answer
    for n in range(107, 113):
        p = Path(OUT_DIR) / "21" / str(n) / "task.txt"
        if p.exists():
            p.unlink()

    doc.close()
    print(f"DONE 11-21: {total}")


if __name__ == "__main__":
    main()

