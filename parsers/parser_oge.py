import fitz
import os
import re
from tqdm import tqdm

PDF = "data/pdfBanks/oge.pdf"
OUT = "data/parsedBanks/tasksOge"

ZOOM = 3

LEFT = 60
RIGHT = 60

TOP_PADDING = -30
BOTTOM_PADDING = 10

TASK_RE = re.compile(r"(?:Задача|Аналог)\s+(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)
CONTEXT_RE = re.compile(
    r"Текст к задачам\s+(1\.\d+(?:\.\d+)?)\s*[-–]\s*(5\.\d+(?:\.\d+)?)",
    re.IGNORECASE
)

# теперь ловит любые ответы (включая множества и интервалы)
ANSWER_RE = re.compile(
    r"№\s*(\d+\.\d+(?:\.\d+)?)\s+(.+)"
)
ANSWER_BLOCK_RE = re.compile(r"№\s*(\d+\.\d+(?:\.\d+)?)")
# Обрезка ответов 20-25: все от rect.x0,y0 (левый верх №)
# Правая граница: ищем точку "." в конце ответа, берём её x1 + ANSWER_DOT_MARGIN
ANSWER_LEFT = 58            # отступ слева от rect.x0
ANSWER_DOT_MARGIN = 10      # отступ справа от точки в конце ответа
ANSWER_WIDTH = 250          # fallback, если точку не нашли
ANSWER_TOP = -9             # отступ верха от rect.y0
ANSWER_HEIGHT = 37          # высота вырезки

current_group = None


def ensure(p):
    os.makedirs(p, exist_ok=True)


def render(page, rect):
    mat = fitz.Matrix(ZOOM, ZOOM)
    pix = page.get_pixmap(matrix=mat, clip=rect)
    return pix


def get_blocks(page):
    return page.get_text("blocks")


def find_tasks(blocks):

    tasks = []

    for b in blocks:

        text = b[4]

        m = TASK_RE.search(text)

        if m:

            task = m.group(1)

            rect = fitz.Rect(b[:4])

            tasks.append((task, rect))

    tasks.sort(key=lambda x: x[1].y0)

    return tasks


def find_answer_block(blocks, y):

    for b in blocks:

        if "Ответ:" in b[4] and b[1] > y:
            return fitz.Rect(b[:4])

    return None


def crop_task(page, blocks, rect):

    ans = find_answer_block(blocks, rect.y0)

    if not ans:
        return None

    # Точная нижняя граница: строка "Ответ:" + 5.
    answer_line_bottom = ans.y1
    for b in blocks:
        txt = b[4] if len(b) > 4 else ""
        if "Ответ:" in txt and b[1] > rect.y0:
            answer_line_bottom = b[3]
            break

    top = rect.y0 - TOP_PADDING
    bottom = answer_line_bottom + 5

    left = LEFT
    right = page.rect.width - RIGHT

    return fitz.Rect(left, top, right, bottom)


# специальная обрезка для задач 11.x:
# от чуть ниже "Задача 11.x" до чуть ниже строки
# "В таблице под каждой ..."
def crop_task_11(page, blocks, rect, tasks, index):

    # по запросу: стартуем немного ниже заголовка задачи
    top = rect.y1 + 10

    marker_bottom = None
    for b in blocks:
        txt = (b[4] if len(b) > 4 else "").strip()
        if not txt:
            continue
        if b[1] <= rect.y0:
            continue
        if "В таблице под каждой" in txt:
            marker_bottom = b[3] + 8
            break

    if marker_bottom is not None:
        bottom = marker_bottom
    elif index + 1 < len(tasks):
        next_rect = tasks[index + 1][1]
        bottom = max(top + 40, next_rect.y0 - 20)
    else:
        bottom = max(top + 40, page.rect.height - 20)

    left = LEFT
    right = page.rect.width - RIGHT

    return fitz.Rect(left, top, right, bottom)


# специальная обрезка для задачи 24 (нет "Ответ:"): до след. задания или Оглавления
def crop_task_24(page, blocks, rect, tasks, index):
    top = rect.y0 - TOP_PADDING
    left = LEFT
    right = page.rect.width - RIGHT

    if index + 1 < len(tasks):
        next_rect = tasks[index + 1][1]
        bottom = next_rect.y0 - 30
    else:
        footer_top = None
        for b in blocks:
            txt = (b[4] if len(b) > 4 else "").strip()
            if not txt:
                continue
            r = fitz.Rect(b[:4])
            is_footer = (
                re.search(r"Оглавл", txt, re.IGNORECASE)
                or re.search(r"Справоч", txt, re.IGNORECASE)
                or "<<" in txt
                or ">>" in txt
            )
            if is_footer:
                footer_top = r.y0 if footer_top is None else min(footer_top, r.y0)
        bottom = footer_top - 6 if footer_top is not None else rect.y0 + 400
        bottom = max(bottom, top + 100)

    return fitz.Rect(left, top, right, bottom)


def detect_context(text):

    m = CONTEXT_RE.search(text)

    if not m:
        return None

    return f"{m.group(1)}-{m.group(2)}"


def crop_context(page, blocks, group, tasks):
    # Первая задача группы: используем полный ключ, например 1.7.3
    first_key = group.split("-")[0]
    first_task_y = None
    for task, rect in tasks:
        if task.startswith(first_key):
            first_task_y = rect.y0
            break
    # Fallback: иногда 1.Y уходит на следующую страницу.
    if first_task_y is None:
        parts = first_key.split(".")
        suffix = parts[1] if len(parts) >= 2 else ""
        same_suffix = [r.y0 for t, r in tasks if suffix and t.startswith(f"1.{suffix}")]
        if same_suffix:
            first_task_y = min(same_suffix)

    # Якорь заголовка "Текст к задачам ...": берем ближайший такой блок
    # перед первой задачей на странице (устойчиво к форматированию группы).
    header_rect = None
    for b in blocks:
        txt = (b[4] if len(b) > 4 else "").strip()
        if not txt:
            continue
        if "Текст к задачам" not in txt:
            continue
        r = fitz.Rect(b[:4])
        if first_task_y is not None and r.y1 >= first_task_y:
            continue
        if header_rect is None or r.y0 > header_rect.y0:
            header_rect = r

    # Верх футера страницы (навигация/оглавление), чтобы не захватывать его в контекст.
    footer_top = None
    for b in blocks:
        txt = (b[4] if len(b) > 4 else "").strip()
        if not txt:
            continue
        r = fitz.Rect(b[:4])
        is_footer = (
            re.search(r"Оглавл", txt, re.IGNORECASE)
            or re.search(r"Справоч", txt, re.IGNORECASE)
            or "<<" in txt
            or ">>" in txt
        )
        if is_footer:
            footer_top = r.y0 if footer_top is None else min(footer_top, r.y0)

    # По запросу: контекст начинается чуть ниже "Текст к задачам".
    if header_rect is not None:
        top = max(0, header_rect.y1 + 11)
    else:
        # fallback: от первого осмысленного блока страницы
        top = None
        for b in blocks:
            txt = (b[4] or "").strip()
            if not txt:
                continue
            y0 = b[1]
            if first_task_y is not None and y0 >= first_task_y:
                continue
            if "Текст к задачам" in txt:
                continue
            if re.search(r"Оглавн", txt, re.IGNORECASE):
                continue
            if re.match(r"^\s*(?:Задача|Аналог)\b", txt, re.IGNORECASE):
                continue
            if first_task_y is None or y0 < first_task_y:
                top = max(0, y0)
                break
        if top is None:
            return None

    # По запросу: снизу еще +3 к центру (сильнее подрезать).
    if first_task_y is not None:
        bottom = max(top + 40, first_task_y - 26)
    else:
        # По запросу: если следующей задачи на странице нет, режем до блока
        # "Оглавление" (чуть выше него).
        if footer_top is not None:
            bottom = max(top + 40, footer_top - 6)
        else:
            bottom = max(top + 40, page.rect.height - 26)

    # Общая защита: всегда режем выше футера, если он найден.
    if footer_top is not None:
        bottom = min(bottom, footer_top - 6)
        bottom = max(bottom, top + 40)

    left = LEFT
    right = page.rect.width - RIGHT

    rect = fitz.Rect(left, top, right, bottom)

    return render(page, rect)


def parse_answers(doc):

    answers = {}

    for page in doc:

        text = page.get_text()

        for m in ANSWER_RE.finditer(text):

            answer = m.group(2).strip()

            answer = (
                answer
                .replace("−", "-")
                .replace("–", "-")
            )

            answers[m.group(1)] = answer

    return answers


def save_answer_text(answer, folder):

    with open(os.path.join(folder, "answer.txt"), "w", encoding="utf-8") as f:
        f.write(answer)


def save_answer_image(task, pix):
    if pix is None:
        return
    folder = get_task_folder(task)
    ensure(folder)
    try:
        pix.save(os.path.join(folder, "answer.png"))
        txt_path = os.path.join(folder, "answer.txt")
        if os.path.exists(txt_path):
            os.remove(txt_path)
    except Exception:
        pass


def _find_next_no_x(blocks, current_rect, line_tolerance=5):
    """Левый край следующего блока «№ X.Y» на той же строке."""
    for b in blocks:
        if not ANSWER_BLOCK_RE.search(b[4]):
            continue
        r = fitz.Rect(b[:4])
        if r.x0 <= current_rect.x0:
            continue
        if abs(r.y0 - current_rect.y0) > line_tolerance:
            continue
        return r.x0
    return None


def _find_dot_right(page, blocks, rect, line_tolerance=4):
    """Правый край точки '.' в конце НАШЕГО ответа (ближайшая точка, не следующего)."""
    words = page.get_text("words")
    next_x = _find_next_no_x(blocks, rect)
    best_x1 = None
    for w in words:
        x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
        if x0 <= rect.x1:
            continue
        if next_x is not None and x1 >= next_x:
            continue
        if abs((y0 + y1) / 2 - (rect.y0 + rect.y1) / 2) > line_tolerance:
            continue
        if not (word.endswith(".") or word == "."):
            continue
        if re.match(r"^[\d\.]+$", word):
            continue
        if best_x1 is None or x1 < best_x1:
            best_x1 = x1
    return best_x1


def find_answer_images(doc):
    """Ответы картинкой для 20-25: обрезка по номерку слева, справа — до точки + margin."""
    answers = {}
    for page_i in range(len(doc)):
        page = doc.load_page(page_i)
        blocks = page.get_text("blocks")
        for b in blocks:
            m = ANSWER_BLOCK_RE.search(b[4])
            if not m:
                continue
            task = m.group(1)
            n = int(task.split(".")[0])
            if n < 20 or n > 25:
                continue
            rect = fitz.Rect(b[:4])
            left = max(0, rect.x0 + ANSWER_LEFT)
            top = max(0, rect.y0 + ANSWER_TOP)
            bottom = min(page.rect.height, top + ANSWER_HEIGHT)
            dot_x1 = _find_dot_right(page, blocks, rect)
            right = (dot_x1 + ANSWER_DOT_MARGIN) if dot_x1 is not None else (left + ANSWER_WIDTH)
            right = min(page.rect.width, right)
            if right - left < 20 or bottom - top < 15:
                continue
            clip = fitz.Rect(left, top, right, bottom)
            try:
                pix = render(page, clip)
                answers[task] = pix
            except Exception:
                continue
    return answers


def save_task_text(task, text, folder):
    ensure(folder)
    with open(os.path.join(folder, "task.txt"), "w", encoding="utf-8") as f:
        f.write((text or "").strip())


def get_task_folder(task):

    parts = task.split(".")
    n = int(parts[0])

    if n <= 5:

        return os.path.join(
            OUT,
            "1-5",
            current_group,
            task
        )

    return os.path.join(
        OUT,
        str(n),
        task
    )


def save_task(task, pix, answers, folder=None):

    folder = folder or get_task_folder(task)

    ensure(folder)

    pix.save(os.path.join(folder, "task.png"))

    n = int(task.split(".")[0])
    if 20 <= n <= 25:
        return  # ответы 20-25 только в .png
    if task in answers:
        save_answer_text(answers[task], folder)


def save_task_24_as_image(page, blocks, task, rect, tasks, index, folder):
    """Задание 24: сохраняем картинкой (нижняя граница — до след. задания или Оглавления)."""
    clip = crop_task_24(page, blocks, rect, tasks, index)
    if clip is None:
        return False
    try:
        pix = render(page, clip)
        pix.save(os.path.join(folder, "task.png"))
        txt_path = os.path.join(folder, "task.txt")
        if os.path.exists(txt_path):
            os.remove(txt_path)
        return True
    except Exception:
        return False


def main():

    global current_group

    ensure(OUT)

    doc = fitz.open(PDF)

    answers = parse_answers(doc)
    answer_images = find_answer_images(doc)

    task_count = 0

    for page_i in tqdm(range(len(doc))):

        page = doc.load_page(page_i)

        text = page.get_text()

        blocks = get_blocks(page)
        tasks = find_tasks(blocks)

        group = detect_context(text)

        if group:

            current_group = group

            folder = os.path.join(
                OUT,
                "1-5",
                group
            )

            ensure(folder)

            pix = crop_context(page, blocks, group, tasks)

            if pix:
                pix.save(os.path.join(folder, "context.png"))

        for i, (task, rect) in enumerate(tasks):

            n = int(task.split(".")[0])

            if n > 25:
                continue

            folder = get_task_folder(task)
            ensure(folder)

            if n == 24:
                if save_task_24_as_image(page, blocks, task, rect, tasks, i, folder):
                    task_count += 1
                    if task in answer_images:
                        save_answer_image(task, answer_images[task])
                    else:
                        save_answer_text("[ответа нет]", folder)
                continue

            if n == 11:
                clip = crop_task_11(page, blocks, rect, tasks, i)
            else:
                clip = crop_task(page, blocks, rect)

            if not clip:
                continue

            pix = render(page, clip)

            save_task(task, pix, answers, folder)

            if 20 <= n <= 25:
                if task in answer_images:
                    save_answer_image(task, answer_images[task])
            elif task in answers:
                save_answer_text(answers[task], folder)

            task_count += 1

    print(f"DONE: {task_count} tasks")


if __name__ == "__main__":
    main()