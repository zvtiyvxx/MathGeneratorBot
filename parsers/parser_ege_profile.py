import fitz
import os
import re
from tqdm import tqdm

PDF = "data/pdfBanks/egeProfile.pdf"
OUT = "data/parsedBanks/tasksEgeProfile"

ZOOM = 3

LEFT = 60
RIGHT = 60

TOP_PADDING = -30
BOTTOM_PADDING = 10

# Обрезка ответов 13-19: от № до точки (как в ОГЭ)
# Ответы в PDF с 309 страницы
ANSWER_PAGE_START = 308  # 0-based: страница 309
# Дефолты (если нет в ANSWER_PER_NUMBER)
ANSWER_LEFT = 58
ANSWER_DOT_MARGIN = 4
ANSWER_WIDTH = 150
ANSWER_TOP = -9
ANSWER_HEIGHT = 37
# Регулировки под каждый номер. Ключ — номер, значение — переопределения:
# left, fixed_width (если задано — не ищем точку), top, height, dot_margin, width
ANSWER_PER_NUMBER = {
    13: {"fixed_width": 450},
    14: {"left": 58, "fixed_width": 80, "top": -8, "height": 43},
    15: {"fixed_width": 450},
    16: {"left": 58, "fixed_width": 80, "top": -9, "height": 40},
    17: {"left": 58, "fixed_width": 80, "top": -6, "height": 42},
    18: {"left": 58, "fixed_width": 450, "top": -11, "height": 42},
    19: {},
}

TASK_RE = re.compile(r"(?:Задача|Аналог)\s+(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)
ANSWER_BLOCK_RE = re.compile(r"№\s*(\d+\.\d+(?:\.\d+)?)")


def ensure(p):
    os.makedirs(p, exist_ok=True)


def render(page, rect):

    mat = fitz.Matrix(ZOOM, ZOOM)
    pix = page.get_pixmap(matrix=mat, clip=rect)

    return pix


def safe_rect(page, left, top, right, bottom):

    left = max(0, left)
    top = max(0, top)

    right = min(page.rect.width, right)
    bottom = min(page.rect.height, bottom)

    if right - left < 5 or bottom - top < 5:
        return None

    return fitz.Rect(left, top, right, bottom)


def get_blocks(page):

    return page.get_text("blocks")


def find_tasks(blocks):

    tasks = []

    for b in blocks:

        m = TASK_RE.search(b[4])

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

    top = rect.y0 - TOP_PADDING
    bottom = ans.y1 + BOTTOM_PADDING

    left = LEFT
    right = page.rect.width - RIGHT

    return safe_rect(page, left, top, right, bottom)


def get_task_folder(task):

    n = int(task.split(".")[0])

    return os.path.join(
        OUT,
        str(n),
        task
    )


def save_task(task, pix):

    folder = get_task_folder(task)

    ensure(folder)

    try:
        pix.save(os.path.join(folder, "task.png"))
    except:
        pass


# ---------- ПОИСК ТЕКСТОВЫХ ОТВЕТОВ (1-12) ----------

ANSWER_TEXT_RE = re.compile(r"№\s*(\d+\.\d+(?:\.\d+)?)\s+(.+)")


def parse_answers(doc):

    answers = {}

    for page in doc:

        text = page.get_text()

        for m in ANSWER_TEXT_RE.finditer(text):

            task = m.group(1)
            answer = m.group(2).strip()

            n = int(task.split(".")[0])

            if n <= 12:

                answer = (
                    answer
                    .replace("−", "-")
                    .replace("–", "-")
                )

                answers[task] = answer

    return answers


def save_answer_text(task, answer):

    folder = get_task_folder(task)

    ensure(folder)

    with open(os.path.join(folder, "answer.txt"), "w", encoding="utf-8") as f:
        f.write(answer)


# ---------- ОТВЕТЫ КАРТИНКОЙ (13-19): от № до точки ----------

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


# Символы в слове — пропускаем (номер, форматирование). Ответы: цифры, √, −, ; и т.д.
_ANSWER_SKIP_CHARS = ":"

def _find_dot_right(page, blocks, rect, line_tolerance=4):
    """Правый край точки '.' в конце ответа. Только ASCII точка, без лишних символов."""
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
        if not word or len(word) < 2:
            continue
        if word[-1] != ".":
            continue
        if re.match(r"^[\d\.]+$", word):
            continue
        if any(c in word for c in _ANSWER_SKIP_CHARS):
            continue
        if re.search(r"[а-яА-ЯёЁ]", word):
            continue
        if best_x1 is None or x1 < best_x1:
            best_x1 = x1
    return best_x1


def find_answer_images(doc):
    """Ответы картинкой для 13-19: обрезка от № до точки. Ответы с 309 стр."""
    answers = {}
    for page_i in range(ANSWER_PAGE_START, len(doc)):
        page = doc.load_page(page_i)
        blocks = page.get_text("blocks")
        for b in blocks:
            m = ANSWER_BLOCK_RE.search(b[4])
            if not m:
                continue
            task = m.group(1)
            n = int(task.split(".")[0])
            if n < 13 or n > 19:
                continue
            rect = fitz.Rect(b[:4])
            opts = ANSWER_PER_NUMBER.get(n, {})
            left = max(0, rect.x0 + opts.get("left", ANSWER_LEFT))
            top = max(0, rect.y0 + opts.get("top", ANSWER_TOP))
            height = opts.get("height", ANSWER_HEIGHT)
            bottom = min(page.rect.height, top + height)
            if "fixed_width" in opts:
                right = min(page.rect.width, left + opts["fixed_width"])
            else:
                dot_margin = opts.get("dot_margin", ANSWER_DOT_MARGIN)
                width_fb = opts.get("width", ANSWER_WIDTH)
                dot_x1 = _find_dot_right(page, blocks, rect)
                right = (dot_x1 + dot_margin) if dot_x1 is not None else (left + width_fb)
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


# ---------- MAIN ----------

def main():

    ensure(OUT)

    doc = fitz.open(PDF)

    print("Scanning answers...")

    text_answers = parse_answers(doc)
    image_answers = find_answer_images(doc)

    task_count = 0

    for page_i in tqdm(range(len(doc))):

        page = doc.load_page(page_i)

        blocks = get_blocks(page)

        tasks = find_tasks(blocks)

        for task, rect in tasks:

            n = int(task.split(".")[0])

            if n > 19:
                continue

            clip = crop_task(page, blocks, rect)

            if not clip:
                continue

            pix = render(page, clip)

            save_task(task, pix)

            if 1 <= n <= 12 and task in text_answers:
                save_answer_text(task, text_answers[task])

            if 13 <= n <= 19 and task in image_answers:
                save_answer_image(task, image_answers[task])

            task_count += 1

    print(f"DONE: {task_count} tasks")


if __name__ == "__main__":
    main()