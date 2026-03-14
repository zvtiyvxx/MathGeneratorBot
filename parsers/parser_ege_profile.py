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

# высота блока ответа (для задач 13-19)
ANSWER_HEIGHT = 30


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


# ---------- ОТВЕТЫ КАРТИНКОЙ (13-19) ----------

def find_answer_images(doc):

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

            if n < 13 or n > 19:
                continue

            rect = fitz.Rect(b[:4])

            left = rect.x0 - 5
            top = rect.y0 - 10

            right = page.rect.width - 10
            bottom = rect.y0 + ANSWER_HEIGHT

            clip = safe_rect(page, left, top, right, bottom)

            if clip is None:
                continue

            try:

                pix = render(page, clip)

                answers[task] = pix

            except:
                continue

    return answers


def save_answer_image(task, pix):

    if pix is None:
        return

    folder = get_task_folder(task)

    ensure(folder)

    try:
        pix.save(os.path.join(folder, "answer.png"))
    except:
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

            if n > 12:
                continue

            clip = crop_task(page, blocks, rect)

            if not clip:
                continue

            pix = render(page, clip)

            save_task(task, pix)

            if task in text_answers:
                save_answer_text(task, text_answers[task])

            if task in image_answers:
                save_answer_image(task, image_answers[task])

            task_count += 1

    print(f"DONE: {task_count} tasks")


if __name__ == "__main__":
    main()