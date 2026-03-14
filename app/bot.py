import asyncio
import html
import logging
import os
import random
import re
import time
import hashlib
from pathlib import Path
from typing import Optional, Union
from logging.handlers import TimedRotatingFileHandler

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from PIL import Image

try:
    from app.db import BotDB, TaskRow
    from app.pdf_export import write_answers_pdf, write_tasks_pdf
except ModuleNotFoundError:
    from db import BotDB, TaskRow
    from pdf_export import write_answers_pdf, write_tasks_pdf


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_DATA_DIR = PROJECT_ROOT / "data" / "bot"
DB_PATH = BOT_DATA_DIR / "bot.db"
GENERATED_DIR = BOT_DATA_DIR / "generated"
LOG_DIR = BOT_DATA_DIR / "logs"
MAX_PRACTICE_COUNT = 10
MAX_MISTAKE_COUNT = 30
CLEANUP_MAX_AGE_SECONDS = 2 * 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 12 * 60 * 60

BTN_VARIANT = "Вариант"
BTN_SOLVE = "Решить задание N"
BTN_MISTAKES = "Работа над ошибками"
BTN_STATS = "Статистика"
BTN_CHANGE_EXAM = "Сменить экзамен"
BTN_FINISH_EARLY = "Завершить досрочно"
BTN_REGEN = "Сгенерировать заново"
BTN_ANSWER_INPUT = "Внести ответы"
BTN_SKIP = "Пропустить"
BTN_BACK = "Назад"
BTN_INFO = "Информация"
BTN_ADMIN_STATS = "📊 Статистика"

EXAM_LABELS = {
    "ege_base": "ЕГЭ База",
    "ege_profile": "ЕГЭ Профиль",
    "oge": "ОГЭ",
}


class SolveFlow(StatesGroup):
    waiting_count = State()


class AnswerFlow(StatesGroup):
    waiting_answer = State()


db = BotDB(DB_PATH)
LAST_VARIANT_BY_USER: dict[int, int] = {}

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        TimedRotatingFileHandler(
            LOG_DIR / "bot.log",
            when="midnight",
            interval=1,
            backupCount=2,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("math_generator_bot")


def normalize_answer(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = cleaned.replace("−", "-").replace("–", "-").replace("—", "-")
    cleaned = cleaned.replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    while cleaned.endswith((".", ";", ":")):
        cleaned = cleaned[:-1]
    return cleaned


def is_answer_correct(
    user_answer: str,
    expected: str,
    exam_type: str,
    task_number: str,
) -> bool:
    """
    Проверка ответа. Для ЕГЭ База задание 19: ответ — одно из чисел через запятую.
    """
    u = normalize_answer(user_answer)
    e = str(expected or "").strip()
    if not e:
        return False

    # ЕГЭ База, задание 19: ожидается список через запятую, пользователь выбирает одно
    if exam_type == "ege_base" and task_number == "19":
        options = [normalize_answer(p) for p in re.split(r"[,;\s]+", e) if p.strip()]
        return u in options if options else u == normalize_answer(e)

    return u == normalize_answer(e)


def sort_task_numbers(task_numbers: list[str]) -> list[str]:
    def key_fn(raw: str) -> tuple[int, str]:
        match = re.search(r"\d+", raw)
        if match:
            return int(match.group(0)), raw
        return 10_000, raw

    return sorted(task_numbers, key=key_fn)


def task_number_sort_key(raw: str) -> tuple[int, str]:
    match = re.search(r"\d+", raw)
    if match:
        return int(match.group(0)), raw
    return 10_000, raw


def _admin_ids() -> frozenset[int]:
    raw = os.getenv("ADMIN_IDS", "").strip()
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                pass
    return frozenset(ids)


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADMIN_STATS)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def exam_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for exam_type, label in EXAM_LABELS.items():
        kb.button(text=label, callback_data=f"exam:{exam_type}")
    kb.adjust(1)
    return kb.as_markup()


def change_exam_button_text(exam_type: Optional[str]) -> str:
    if exam_type and exam_type in EXAM_LABELS:
        return f"{BTN_CHANGE_EXAM} [{EXAM_LABELS[exam_type]}]"
    return BTN_CHANGE_EXAM


def mode_keyboard(current_exam: Optional[str]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_VARIANT), KeyboardButton(text=BTN_SOLVE)],
            [KeyboardButton(text=BTN_MISTAKES), KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=change_exam_button_text(current_exam)), KeyboardButton(text=BTN_INFO)],
        ],
        resize_keyboard=True,
    )


def task_number_keyboard(task_numbers: list[str], current_exam: Optional[str]) -> ReplyKeyboardMarkup:
    numbers = sort_task_numbers(task_numbers)
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for number in numbers:
        row.append(KeyboardButton(text=f"Задание {number}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton(text=BTN_BACK), KeyboardButton(text=change_exam_button_text(current_exam))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def variant_action_keyboard(variant_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_ANSWER_INPUT, callback_data=f"answer_start:{variant_id}")
    kb.button(text=BTN_REGEN, callback_data=f"regen:{variant_id}")
    kb.adjust(2)
    return kb.as_markup()


def show_task_keyboard(variant_id: int, position: int, total: int, show_answer: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    prev_pos = position - 1 if position > 1 else 1
    next_pos = position + 1 if position < total else total
    kb.button(text="⬅️ Предыдущее", callback_data=f"show:{variant_id}:{prev_pos}:{int(show_answer)}")
    kb.button(text="Следующее ➡️", callback_data=f"show:{variant_id}:{next_pos}:{int(show_answer)}")
    toggle_text = "❌ Скрыть ответы" if show_answer else "✅ Показать ответы"
    kb.button(text=toggle_text, callback_data=f"toggle_answer:{variant_id}:{position}:{int(show_answer)}")
    kb.adjust(2, 1)
    return kb.as_markup()


def finish_answer_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_SKIP, callback_data="answer_skip")
    kb.button(text=BTN_FINISH_EARLY, callback_data="answer_finish")
    kb.adjust(2)
    return kb.as_markup()


def back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BACK)]],
        resize_keyboard=True,
    )


def ensure_user(message_or_callback: Message | CallbackQuery) -> tuple[int, Optional[str]]:
    user = message_or_callback.from_user
    assert user is not None
    user_id = db.upsert_user(user.id, user.username, user.first_name)
    row = db.get_user(user.id)
    current_exam = row["current_exam"] if row else None
    return user_id, current_exam


def current_exam_for_tg_user(tg_user_id: int) -> Optional[str]:
    row = db.get_user(tg_user_id)
    if not row:
        return None
    exam = row["current_exam"]
    return str(exam) if exam else None


def write_variant_files(tg_user_id: int, variant_id: int, tasks: list[TaskRow]) -> tuple[Path, Path, Path]:
    user_dir = GENERATED_DIR / f"user_{tg_user_id}"
    user_dir.mkdir(parents=True, exist_ok=True)
    tasks_file = user_dir / f"Вариант_{variant_id}_Задания.pdf"
    answers_file = user_dir / f"Вариант_{variant_id}_Ответы.pdf"
    full_file = user_dir / f"Вариант_{variant_id}_Задания_и_Ответы.pdf"

    write_tasks_pdf(
        tasks_file,
        f"Вариант {variant_id} - задания",
        tasks,
        PROJECT_ROOT,
        include_answers=False,
    )
    write_answers_pdf(answers_file, f"Вариант {variant_id} - ответы", tasks)
    write_tasks_pdf(
        full_file,
        f"Вариант {variant_id} - задания и ответы",
        tasks,
        PROJECT_ROOT,
        include_answers=True,
    )
    return tasks_file, answers_file, full_file


async def send_variant_bundle(message: Message, tg_user_id: int, variant_id: int) -> None:
    rows = db.get_variant_tasks(variant_id)
    tasks = [BotDB._to_task_row(row) for row in rows]
    if not tasks:
        await message.answer("Не получилось собрать вариант: нет заданий.")
        return
    LAST_VARIANT_BY_USER[tg_user_id] = variant_id
    logger.info("Send variant bundle user=%s variant=%s tasks=%s", tg_user_id, variant_id, len(tasks))

    tasks_file, answers_file, full_file = write_variant_files(tg_user_id, variant_id, tasks)
    await message.answer_document(FSInputFile(tasks_file), caption="PDF с набором заданий")
    await message.answer_document(FSInputFile(answers_file), caption="PDF с ответами")
    await message.answer_document(FSInputFile(full_file), caption="PDF с заданиями и ответами")
    cleanup_generated_files([tasks_file, answers_file, full_file])
    variant_rows = db.get_variant_tasks(variant_id)
    first_row = variant_rows[0]
    show_context = should_show_context_for_row(variant_rows, 1)
    clickable_text = render_clickable_task_text(
        first_row,
        position=1,
        total=len(tasks),
        show_answer=False,
        show_context=show_context,
    )
    await send_or_edit_clickable_message(
        message=message,
        text=clickable_text,
        keyboard=show_task_keyboard(variant_id, position=1, total=len(tasks), show_answer=False),
        row=first_row,
        show_context=show_context,
        edit=False,
    )
    await message.answer(
        "Что дальше?",
        reply_markup=variant_action_keyboard(variant_id),
    )
    await message.answer("Для возврата в меню нажми 'Назад'.", reply_markup=back_keyboard())


async def run_generation_animation(message: Message) -> Message:
    status = await message.answer("Вариант генерируется")
    for dots in (".", "..", "..."):
        await asyncio.sleep(0.25)
        await status.edit_text(f"Вариант генерируется{dots}")
    return status


def build_variant_tasks(exam_type: str, user_id: int) -> list[TaskRow]:
    numbers = sort_task_numbers(db.list_task_numbers(exam_type))
    result: list[TaskRow] = []
    used_oge_groups: set[str] = set()
    for task_number in numbers:
        if exam_type == "oge" and task_number == "1-5":
            group_tasks = pick_random_oge_1_5_group(user_id=user_id, used_groups=used_oge_groups)
            result.extend(group_tasks)
            continue
        picked = db.get_random_tasks_for_user(exam_type, task_number, 1, user_id)
        if picked:
            result.append(picked[0])
    return result


def percent_grade(correct: int, total: int) -> int:
    if total <= 0:
        return 2
    pct = (correct / total) * 100
    if pct >= 85:
        return 5
    if pct >= 65:
        return 4
    if pct >= 40:
        return 3
    return 2


def create_stats_html(exam_label: str, rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    table_rows = []
    for row in rows:
        percent = row["percent"]
        if percent >= 70:
            color = "#b7f7c2"
        elif percent >= 40:
            color = "#ffe9a8"
        else:
            color = "#ffb3b3"
        table_rows.append(
            "<tr>"
            f"<td>{html.escape(row['task_number'])}</td>"
            f"<td>{row['correct']}</td>"
            f"<td>{row['total']}</td>"
            f"<td style='background:{color};'>{percent:.1f}%</td>"
            "</tr>"
        )

    html_body = (
        "<html><head><meta charset='utf-8'><title>Статистика</title></head><body>"
        f"<h2>Статистика: {html.escape(exam_label)}</h2>"
        "<table border='1' cellspacing='0' cellpadding='6'>"
        "<tr><th>Задание</th><th>Верно</th><th>Всего</th><th>Процент</th></tr>"
        f"{''.join(table_rows)}"
        "</table></body></html>"
    )
    output_path.write_text(html_body, encoding="utf-8")


def get_tasks_for_number(exam_type: str, task_number: str) -> list[TaskRow]:
    rows = db.conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE exam_type = ? AND task_number = ?
        """,
        (exam_type, task_number),
    ).fetchall()
    return [BotDB._to_task_row(r) for r in rows]


def build_oge_1_5_group_tasks(group: str) -> list[TaskRow]:
    rows = db.conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE exam_type = 'oge'
          AND task_number = '1-5'
          AND source_rel_path LIKE ?
        ORDER BY task_code
        """,
        (f"1-5/{group}/%",),
    ).fetchall()
    return [BotDB._to_task_row(r) for r in rows]


def pick_random_oge_1_5_group(user_id: int, used_groups: Optional[set[str]] = None) -> list[TaskRow]:
    rows = db.conn.execute(
        """
        SELECT DISTINCT substr(
            source_rel_path,
            instr(source_rel_path, '/') + 1,
            instr(substr(source_rel_path, instr(source_rel_path, '/') + 1), '/') - 1
        ) AS grp
        FROM tasks
        WHERE exam_type = 'oge' AND task_number = '1-5'
        """
    ).fetchall()
    groups = [str(r["grp"]) for r in rows if r["grp"]]
    if not groups:
        return []
    used_groups = used_groups or set()
    solved_ids = db.get_solved_task_ids(user_id, "oge", "1-5")

    fresh_groups: list[str] = []
    fallback_groups: list[str] = []
    for grp in groups:
        if grp in used_groups:
            continue
        grp_tasks = build_oge_1_5_group_tasks(grp)
        if not grp_tasks:
            continue
        if all(t.id not in solved_ids for t in grp_tasks):
            fresh_groups.append(grp)
        fallback_groups.append(grp)

    pick_from = fresh_groups if fresh_groups else fallback_groups
    if not pick_from:
        pick_from = groups
    grp = random.choice(pick_from)
    used_groups.add(grp)
    return build_oge_1_5_group_tasks(grp)


def progress_bar(percent: float, width: int = 10) -> str:
    filled = int(round((percent / 100.0) * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def stats_icon(percent: float) -> str:
    if percent >= 70:
        return "🟢"
    if percent >= 40:
        return "🟡"
    return "🔴"


def group_key(source_rel_path: str) -> str:
    parts = source_rel_path.split("/")
    if len(parts) <= 1:
        return source_rel_path
    return "/".join(parts[:-1])


def normalize_text_for_output(text: str) -> str:
    text = (text or "").replace("\r", "").replace("\u00ad", "")
    # Переносы с дефисом: "вы-\nсоте" -> "высоте"
    text = re.sub(r"([A-Za-zА-Яа-яЁё])-\s*\n\s*([A-Za-zА-Яа-яЁё])", r"\1\2", text)
    # OCR-вариант: "вы- соте" -> "высоте"
    text = re.sub(r"([A-Za-zА-Яа-яЁё])-\s+([A-Za-zА-Яа-яЁё])", r"\1\2", text)
    # Сохраняем вертикальный список вида "1) ...", "2) ..." (для задач типа 8).
    text = re.sub(r"\n\s*(\d+\))", r"@@LIST@@\1", text)
    # Одинарные переносы превращаем в пробелы, двойные сохраняем как абзацы
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"@@LIST@@", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_oge_1_5_row(row) -> bool:
    return str(row["exam_type"]) == "oge" and str(row["task_number"]) == "1-5"


def should_show_context_for_row(rows: list, position: int) -> bool:
    row = rows[position - 1]
    if not is_oge_1_5_row(row):
        return True
    if position == 1:
        return True
    prev = rows[position - 2]
    if not is_oge_1_5_row(prev):
        return True
    return group_key(str(row["source_rel_path"])) != group_key(str(prev["source_rel_path"]))


def render_clickable_task_text(row, position: int, total: int, show_answer: bool, show_context: bool) -> str:
    parts = [f"<b>Задание {position}.</b> ({position}/{total})"]
    if show_context and row["context_text"]:
        parts.append(html.escape(normalize_text_for_output(str(row["context_text"]))))
    if row["task_text"]:
        parts.append(html.escape(normalize_text_for_output(str(row["task_text"]))))
    if show_answer:
        parts.append(f"<b>Ответ:</b> {html.escape(str(row['answer_text'] or ''))}")
    return "\n\n".join(parts)


async def require_exam(target: Message | CallbackQuery) -> Optional[str]:
    _, current_exam = ensure_user(target)
    if current_exam:
        return str(current_exam)
    text = "Сначала выбери экзамен:"
    if isinstance(target, Message):
        await target.answer(text, reply_markup=exam_menu_keyboard())
    else:
        await target.message.answer(text, reply_markup=exam_menu_keyboard())
    return None


def parse_task_number_button(text: str) -> Optional[str]:
    match = re.match(r"^Задание\s+(.+)$", text.strip())
    if not match:
        return None
    return match.group(1).strip()


async def handle_make_variant(message: Message) -> None:
    exam_type = await require_exam(message)
    if exam_type is None:
        return
    user_id, _ = ensure_user(message)
    status_msg = await run_generation_animation(message)
    tasks = build_variant_tasks(exam_type, user_id)
    if not tasks:
        await status_msg.edit_text("Не удалось собрать вариант.")
        await message.answer("Для этого экзамена в БД пока нет заданий.")
        return
    variant_id = db.create_variant(
        user_id=user_id,
        exam_type=exam_type,
        mode="variant",
        requested_task_number=None,
        requested_count=len(tasks),
        tasks=tasks,
    )
    db.set_current_exam(message.from_user.id, exam_type)
    logger.info("Variant generated user=%s exam=%s variant=%s", message.from_user.id, exam_type, variant_id)
    await status_msg.edit_text("Вариант готов.")
    await message.answer(f"Вариант собран: {len(tasks)} заданий.")
    await send_variant_bundle(message, message.from_user.id, variant_id)


async def handle_solve_select(message: Message) -> None:
    exam_type = await require_exam(message)
    if exam_type is None:
        return
    task_numbers = db.list_task_numbers(exam_type)
    if not task_numbers:
        await message.answer("В БД нет заданий для выбранного экзамена.")
    else:
        await message.answer(
            "Какое задание хочешь порешать?",
            reply_markup=task_number_keyboard(task_numbers, exam_type),
        )


async def handle_mistakes(message: Message) -> None:
    exam_type = await require_exam(message)
    if exam_type is None:
        return
    user_id, _ = ensure_user(message)
    status_msg = await run_generation_animation(message)
    wrong_task_ids = db.get_wrong_task_ids(user_id, exam_type, MAX_MISTAKE_COUNT)
    tasks = db.get_tasks_by_ids(wrong_task_ids)
    if not tasks:
        await status_msg.edit_text("Не удалось собрать вариант.")
        await message.answer("Пока нет ошибок для работы над ними.")
        return
    variant_id = db.create_variant(
        user_id=user_id,
        exam_type=exam_type,
        mode="mistakes",
        requested_task_number=None,
        requested_count=len(tasks),
        tasks=tasks,
    )
    db.set_current_exam(message.from_user.id, exam_type)
    logger.info("Mistakes variant generated user=%s exam=%s variant=%s", message.from_user.id, exam_type, variant_id)
    await status_msg.edit_text("Вариант готов.")
    await message.answer(f"Собрал работу над ошибками: {len(tasks)} заданий.")
    await send_variant_bundle(message, message.from_user.id, variant_id)


async def handle_stats(message: Message) -> None:
    exam_type = await require_exam(message)
    if exam_type is None:
        return
    user_id, _ = ensure_user(message)
    rows = db.get_stats(user_id, exam_type)
    if not rows:
        await message.answer("Пока нет статистики по этому режиму экзамена.")
        return

    prepared = []
    total_attempts = 0
    total_correct = 0
    for row in rows:
        total = int(row["total_attempts"])
        correct = int(row["correct_attempts"] or 0)
        percent = (correct / total) * 100 if total else 0.0
        prepared.append(
            {
                "task_number": str(row["task_number"]),
                "total": total,
                "correct": correct,
                "percent": percent,
            }
        )
        total_attempts += total
        total_correct += correct

    prepared = sorted(prepared, key=lambda x: task_number_sort_key(x["task_number"]))
    total_percent = (total_correct / total_attempts) * 100 if total_attempts else 0.0
    header = (
        f"<b>Статистика: {EXAM_LABELS[exam_type]}</b>\n"
        "В процентном соотношении правильно и неправильно решенных заданий.\n"
        f"<b>Итог: {total_correct}/{total_attempts} ({total_percent:.1f}%)</b>\n\n"
        "<b>По заданиям:</b>\n"
    )
    lines = []
    for row in prepared:
        lines.append(
            f"{stats_icon(row['percent'])} №{row['task_number']}: {progress_bar(row['percent'])} "
            f"{row['percent']:.1f}% ({row['correct']}/{row['total']})"
        )
    await message.answer(header + "\n".join(lines), parse_mode="HTML")


async def ask_next_answer(
    message: Message,
    state: FSMContext,
    *,
    user_id: Optional[int] = None,
) -> None:
    data = await state.get_data()
    rows = data.get("rows", [])
    idx = int(data.get("idx", 0))
    if idx >= len(rows):
        await finalize_answers(message, state, user_id=user_id)
        return
    row = rows[idx]
    await message.answer(
        f"Ответ для задания №{row['position']}:",
        reply_markup=finish_answer_keyboard(),
    )


async def finalize_answers(
    target: Union[Message, CallbackQuery],
    state: FSMContext,
    *,
    user_id: Optional[int] = None,
) -> None:
    """target: Message при ответе текстом, CallbackQuery при нажатии inline (оттуда from_user — пользователь)."""
    data = await state.get_data()
    answered = int(data.get("answered", 0))
    correct = int(data.get("correct", 0))
    grade = percent_grade(correct, answered)
    override_user_id = user_id
    user_id = user_id or target.from_user.id
    msg = target.message if isinstance(target, CallbackQuery) else target
    logger.info("Answer flow finished user=%s correct=%s answered=%s grade=%s", user_id, correct, answered, grade)
    await state.clear()
    if override_user_id is not None:
        db.upsert_user(override_user_id, None, None)
    else:
        ensure_user(target)
    current_exam = current_exam_for_tg_user(user_id)
    await msg.answer(
        f"Готово. Верно: {correct} из {answered}. Оценка: {grade}.",
        reply_markup=mode_keyboard(current_exam),
    )


async def send_task_from_variant(callback: CallbackQuery, variant_id: int, position: int, show_answer: bool) -> None:
    rows = db.get_variant_tasks(variant_id)
    by_position = {int(r["position"]): r for r in rows}
    row = by_position.get(position)
    if row is None:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    show_context = should_show_context_for_row(rows, position)
    text = render_clickable_task_text(
        row,
        position=position,
        total=len(rows),
        show_answer=show_answer,
        show_context=show_context,
    )
    await send_or_edit_clickable_message(
        message=callback.message,
        text=text,
        keyboard=show_task_keyboard(variant_id, position=position, total=len(rows), show_answer=show_answer),
        row=row,
        show_context=show_context,
        edit=True,
    )


def primary_image_for_clickable(row, show_context: bool) -> Optional[Path]:
    task_image_path = row["task_image_path"]
    context_image_path = row["context_image_path"]
    task_img = PROJECT_ROOT / str(task_image_path) if task_image_path else None
    ctx_img = PROJECT_ROOT / str(context_image_path) if context_image_path else None

    if (
        show_context
        and is_oge_1_5_row(row)
        and task_img
        and ctx_img
        and task_img.exists()
        and ctx_img.exists()
    ):
        return build_combined_image(ctx_img, task_img, row["source_rel_path"])

    if task_img and task_img.exists():
        return task_img
    if show_context and ctx_img and ctx_img.exists():
        return ctx_img
    return None


def build_combined_image(context_img: Path, task_img: Path, source_rel_path: str) -> Optional[Path]:
    cache_dir = GENERATED_DIR / "combined_images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{context_img}|{task_img}|{source_rel_path}".encode("utf-8")).hexdigest()
    out_path = cache_dir / f"{key}.png"
    if out_path.exists():
        return out_path

    try:
        with Image.open(context_img) as cimg, Image.open(task_img) as timg:
            cimg = cimg.convert("RGB")
            timg = timg.convert("RGB")
            width = max(cimg.width, timg.width)
            pad = 24
            total_height = cimg.height + timg.height + pad
            canvas = Image.new("RGB", (width, total_height), color=(255, 255, 255))
            cx = (width - cimg.width) // 2
            tx = (width - timg.width) // 2
            canvas.paste(cimg, (cx, 0))
            canvas.paste(timg, (tx, cimg.height + pad))
            canvas.save(out_path, format="PNG")
        return out_path
    except Exception:
        return task_img


async def send_or_edit_clickable_message(
    message: Message,
    text: str,
    keyboard: InlineKeyboardMarkup,
    row,
    show_context: bool,
    edit: bool,
) -> None:
    image_path = primary_image_for_clickable(row, show_context)
    if edit:
        try:
            if image_path:
                await message.edit_media(
                    InputMediaPhoto(media=FSInputFile(image_path), caption=text, parse_mode="HTML"),
                    reply_markup=keyboard,
                )
                cleanup_combined_cache_file(image_path)
            else:
                await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            return
        except TelegramBadRequest:
            try:
                await message.delete()
            except TelegramBadRequest:
                pass

    if image_path:
        await message.answer_photo(
            FSInputFile(image_path),
            caption=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        cleanup_combined_cache_file(image_path)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


def cleanup_generated_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            logger.warning("Failed to delete generated file: %s", path)


def cleanup_combined_cache_file(path: Optional[Path]) -> None:
    if path is None:
        return
    cache_dir = GENERATED_DIR / "combined_images"
    try:
        if path.exists() and cache_dir in path.parents:
            path.unlink()
    except Exception:
        logger.warning("Failed to delete combined cache file: %s", path)


def cleanup_old_combined_images(max_age_seconds: int = CLEANUP_MAX_AGE_SECONDS) -> None:
    cache_dir = GENERATED_DIR / "combined_images"
    if not cache_dir.exists():
        return
    now = time.time()
    for path in cache_dir.glob("*"):
        if not path.is_file():
            continue
        try:
            age = now - path.stat().st_mtime
            if age > max_age_seconds:
                path.unlink()
        except Exception:
            logger.warning("Failed to cleanup cache file: %s", path)


async def periodic_cleanup_task() -> None:
    while True:
        cleanup_old_combined_images()
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def regenerate_variant(message: Message, tg_user_id: int, variant_id: int) -> bool:
    status_msg = await run_generation_animation(message)
    rows = db.get_variant_tasks(variant_id)
    if not rows:
        await status_msg.edit_text("Не удалось собрать вариант.")
        await message.answer("Вариант не найден.")
        return False
    first = rows[0]
    user_db_id, _ = ensure_user(message)
    exam_type = str(first["exam_type"])
    mode = db.conn.execute(
        "SELECT mode, requested_task_number, requested_count FROM variants WHERE id = ?",
        (variant_id,),
    ).fetchone()
    if mode is None:
        await status_msg.edit_text("Не удалось собрать вариант.")
        await message.answer("Вариант не найден.")
        return False

    if mode["mode"] == "practice":
        requested_num = str(mode["requested_task_number"])
        requested_count = int(mode["requested_count"])
        if exam_type == "oge" and requested_num == "1-5":
            tasks = []
            used_groups: set[str] = set()
            for _ in range(requested_count):
                tasks.extend(pick_random_oge_1_5_group(user_id=user_db_id, used_groups=used_groups))
        else:
            tasks = db.get_random_tasks_for_user(exam_type, requested_num, requested_count, user_db_id)
    elif mode["mode"] == "mistakes":
        wrong_task_ids = db.get_wrong_task_ids(user_db_id, exam_type, MAX_MISTAKE_COUNT)
        tasks = db.get_tasks_by_ids(wrong_task_ids)
    else:
        tasks = build_variant_tasks(exam_type, user_db_id)

    if not tasks:
        await status_msg.edit_text("Не удалось собрать вариант.")
        await message.answer("Не получилось сгенерировать заново.")
        return False

    new_variant_id = db.create_variant(
        user_id=user_db_id,
        exam_type=exam_type,
        mode=str(mode["mode"]),
        requested_task_number=mode["requested_task_number"],
        requested_count=int(mode["requested_count"] or len(tasks)),
        tasks=tasks,
    )
    db.set_current_exam(tg_user_id, exam_type)
    logger.info("Regenerated variant tg_user=%s old=%s new=%s", tg_user_id, variant_id, new_variant_id)
    await status_msg.edit_text("Вариант готов.")
    await message.answer("Сгенерировал заново.")
    await send_variant_bundle(message, tg_user_id, new_variant_id)
    return True


dp = Dispatcher(storage=MemoryStorage())


@dp.errors()
async def on_error(event) -> bool:
    logger.exception("Unhandled bot error: %s", event.exception)
    return True


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    ensure_user(message)
    logger.info("Start command user=%s", message.from_user.id)
    await message.answer(
        "👋 Добро пожаловать в MathGeneratorBot!\n\n"
        "Бот помогает готовиться к ОГЭ и ЕГЭ по математике.\n\n"
        "🎯 Возможности:\n"
        "• Генерация вариантов по реальной базе заданий\n"
        "• Режим «Решить задание N» с выбором количества\n"
        "• В .pdf и в самом боте\n"
        "• Со всеми формулами и рисунками\n"
        "• Проверка ответов, работа над ошибками и статистика\n\n"
        "📢 Наш канал: @dima_obisnit\n\n"
        "Выберите экзамен:",
        reply_markup=exam_menu_keyboard(),
    )


@dp.message(Command("admin"))
async def admin_handler(message: Message) -> None:
    if message.from_user.id not in _admin_ids():
        return
    logger.info("Admin panel opened user=%s", message.from_user.id)
    await message.answer(
        "🔐 Панель администратора",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == BTN_ADMIN_STATS)
async def admin_stats_handler(message: Message) -> None:
    if message.from_user.id not in _admin_ids():
        return
    stats = db.get_admin_stats()
    text = (
        "<b>📊 Статистика бота</b>\n\n"
        f"• Решено заданий всего: <b>{stats['total_attempts']}</b>\n"
        f"• Правильно решено: <b>{stats['correct_attempts']}</b> ({stats['correct_percent']:.1f}%)\n"
        f"• Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"• Активных за 30 дней: <b>{stats['active_users_30d']}</b>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.callback_query(F.data.startswith("exam:"))
async def exam_chosen(callback: CallbackQuery) -> None:
    exam_type = callback.data.split(":", maxsplit=1)[1]
    ensure_user(callback)
    db.set_current_exam(callback.from_user.id, exam_type)
    logger.info("Exam selected user=%s exam=%s", callback.from_user.id, exam_type)
    await callback.message.answer(
        f"Режим: {EXAM_LABELS[exam_type]}",
        reply_markup=mode_keyboard(exam_type),
    )
    await callback.answer()


@dp.callback_query(F.data == "mode:change_exam")
async def change_exam(callback: CallbackQuery) -> None:
    await callback.message.answer("Выбери экзамен:", reply_markup=exam_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "mode:back")
async def back_to_mode(callback: CallbackQuery) -> None:
    current_exam = current_exam_for_tg_user(callback.from_user.id)
    await callback.message.answer("Выбери действие:", reply_markup=mode_keyboard(current_exam))
    await callback.answer()


@dp.message(F.text == BTN_BACK)
async def back_to_mode_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    current_exam = current_exam_for_tg_user(message.from_user.id)
    await message.answer("Выбери действие:", reply_markup=mode_keyboard(current_exam))


@dp.message(F.text.regexp(r"^Сменить экзамен(?:\s+\[.*\])?$"))
async def change_exam_message(message: Message) -> None:
    await message.answer("Выбери экзамен:", reply_markup=exam_menu_keyboard())


@dp.message(F.text == BTN_INFO)
async def info_message(message: Message) -> None:
    current_exam = current_exam_for_tg_user(message.from_user.id)
    await message.answer(
        "ℹ️ Информация о боте\n\n"
        "MathGeneratorBot генерирует задания для:\n"
        "• ЕГЭ Профиль\n"
        "• ЕГЭ База\n"
        "• ОГЭ\n\n"
        "Как пользоваться:\n"
        "1. Выберите экзамен\n"
        "2. Нажмите «Вариант» или «Решить задание N»\n"
        "3. Решите задания и нажмите «Внести ответы»\n"
        "4. Проверьте результат\n"
        "5. Воспользуйтесь статистикой и работой над ошибками\n\n"
        "Что умеет бот:\n"
        "• Ведет статистику - показывает % правильно решенных задач\n"
        "• Формирует «Работу над ошибками»\n"
        "• Поддерживает задания с формулами и изображениями\n"
        "• Дает возможность решать множество вариаций любого задания\n\n"
        "📢 Наш канал: @dima_obisnit",
        reply_markup=mode_keyboard(current_exam),
    )


@dp.message(F.text == BTN_VARIANT)
async def make_variant_message(message: Message) -> None:
    await handle_make_variant(message)


@dp.callback_query(F.data == "mode:variant")
async def make_variant(callback: CallbackQuery) -> None:
    await handle_make_variant(callback.message)
    await callback.answer()


@dp.message(F.text == BTN_SOLVE)
async def solve_choose_number_message(message: Message) -> None:
    await handle_solve_select(message)


@dp.callback_query(F.data == "mode:solve")
async def solve_choose_number(callback: CallbackQuery) -> None:
    await handle_solve_select(callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("solve_task:"))
async def solve_choose_count(callback: CallbackQuery, state: FSMContext) -> None:
    exam_type = await require_exam(callback)
    if exam_type is None:
        await callback.answer()
        return
    task_number = callback.data.split(":", maxsplit=1)[1]
    await state.set_state(SolveFlow.waiting_count)
    await state.update_data(task_number=task_number, exam_type=exam_type)
    await callback.message.answer(
        f"Сколько заданий '{task_number}' сгенерировать? (1-{MAX_PRACTICE_COUNT})",
        reply_markup=back_keyboard(),
    )
    await callback.answer()


@dp.message(F.text.regexp(r"^Задание\s+.+$"))
async def solve_choose_count_message(message: Message, state: FSMContext) -> None:
    exam_type = await require_exam(message)
    if exam_type is None:
        return
    task_number = parse_task_number_button(message.text or "")
    if not task_number:
        return
    await state.set_state(SolveFlow.waiting_count)
    await state.update_data(task_number=task_number, exam_type=exam_type)
    await message.answer(
        f"Сколько заданий '{task_number}' сгенерировать? (1-{MAX_PRACTICE_COUNT})",
        reply_markup=back_keyboard(),
    )


@dp.message(SolveFlow.waiting_count)
async def solve_generate(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("Напиши число, например: 10")
        return
    count = int(message.text.strip())
    if count < 1 or count > MAX_PRACTICE_COUNT:
        await message.answer(f"Нужно число от 1 до {MAX_PRACTICE_COUNT}.")
        return

    data = await state.get_data()
    await state.clear()
    task_number = str(data["task_number"])
    exam_type = str(data["exam_type"])
    user_id, _ = ensure_user(message)

    status_msg = await run_generation_animation(message)
    if exam_type == "oge" and task_number == "1-5":
        tasks: list[TaskRow] = []
        used_groups: set[str] = set()
        for _ in range(count):
            tasks.extend(pick_random_oge_1_5_group(user_id=user_id, used_groups=used_groups))
    else:
        tasks = db.get_random_tasks_for_user(exam_type, task_number, count, user_id)
    if not tasks:
        await status_msg.edit_text("Не удалось собрать вариант.")
        await message.answer("Не нашел заданий по этому номеру.")
        return

    variant_id = db.create_variant(
        user_id=user_id,
        exam_type=exam_type,
        mode="practice",
        requested_task_number=task_number,
        requested_count=count,
        tasks=tasks,
    )
    db.set_current_exam(message.from_user.id, exam_type)
    await status_msg.edit_text("Вариант готов.")
    if exam_type == "oge" and task_number == "1-5":
        await message.answer(f"Собрано {count} набор(ов) по блоку 1-5 ({len(tasks)} заданий).")
    else:
        await message.answer(f"Собрано {len(tasks)} заданий по номеру {task_number}.")
    await send_variant_bundle(message, message.from_user.id, variant_id)


@dp.callback_query(F.data == "mode:mistakes")
async def mistakes_mode(callback: CallbackQuery) -> None:
    await handle_mistakes(callback.message)
    await callback.answer()


@dp.message(F.text == BTN_MISTAKES)
async def mistakes_mode_message(message: Message) -> None:
    await handle_mistakes(message)


@dp.callback_query(F.data == "mode:stats")
async def show_stats(callback: CallbackQuery) -> None:
    await handle_stats(callback.message)
    await callback.answer()


@dp.message(F.text == BTN_STATS)
async def show_stats_message(message: Message) -> None:
    await handle_stats(message)


@dp.callback_query(F.data.startswith("show:"))
async def show_task(callback: CallbackQuery) -> None:
    _, variant_id_raw, pos_raw, show_raw = callback.data.split(":", maxsplit=3)
    await send_task_from_variant(
        callback,
        int(variant_id_raw),
        int(pos_raw),
        bool(int(show_raw)),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("toggle_answer:"))
async def toggle_answer(callback: CallbackQuery) -> None:
    _, variant_id_raw, pos_raw, show_raw = callback.data.split(":", maxsplit=3)
    show_answer = not bool(int(show_raw))
    await send_task_from_variant(
        callback,
        int(variant_id_raw),
        int(pos_raw),
        show_answer,
    )
    await callback.answer()


@dp.message(F.text == BTN_REGEN)
async def regenerate_message(message: Message) -> None:
    variant_id = LAST_VARIANT_BY_USER.get(message.from_user.id)
    if not variant_id:
        current_exam = current_exam_for_tg_user(message.from_user.id)
        await message.answer("Сначала сгенерируй вариант.", reply_markup=mode_keyboard(current_exam))
        return
    await regenerate_variant(message, message.from_user.id, variant_id)


@dp.callback_query(F.data.startswith("regen:"))
async def regenerate(callback: CallbackQuery) -> None:
    variant_id = int(callback.data.split(":", maxsplit=1)[1])
    ok = await regenerate_variant(callback.message, callback.from_user.id, variant_id)
    if not ok:
        await callback.answer("Не удалось", show_alert=True)
        return
    await callback.answer()


@dp.message(F.text == BTN_ANSWER_INPUT)
async def answer_start_message(message: Message, state: FSMContext) -> None:
    variant_id = LAST_VARIANT_BY_USER.get(message.from_user.id)
    if not variant_id:
        current_exam = current_exam_for_tg_user(message.from_user.id)
        await message.answer("Сначала сгенерируй вариант.", reply_markup=mode_keyboard(current_exam))
        return
    rows = db.get_variant_tasks(variant_id)
    if not rows:
        current_exam = current_exam_for_tg_user(message.from_user.id)
        await message.answer("Вариант не найден.", reply_markup=mode_keyboard(current_exam))
        return

    await state.set_state(AnswerFlow.waiting_answer)
    await state.update_data(
        variant_id=variant_id,
        rows=[dict(r) for r in rows],
        idx=0,
        answered=0,
        correct=0,
    )
    logger.info("Answer flow started user=%s variant=%s", message.from_user.id, variant_id)
    await message.answer("Начинаем проверку ответов.")
    await message.answer("Чтобы вернуться в меню, нажми 'Назад'.", reply_markup=back_keyboard())
    await ask_next_answer(message, state)


@dp.callback_query(F.data.startswith("answer_start:"))
async def answer_start(callback: CallbackQuery, state: FSMContext) -> None:
    variant_id = int(callback.data.split(":", maxsplit=1)[1])
    rows = db.get_variant_tasks(variant_id)
    if not rows:
        await callback.answer("Вариант не найден", show_alert=True)
        return

    await state.set_state(AnswerFlow.waiting_answer)
    await state.update_data(
        variant_id=variant_id,
        rows=[dict(r) for r in rows],
        idx=0,
        answered=0,
        correct=0,
    )
    logger.info("Answer flow started user=%s variant=%s", callback.from_user.id, variant_id)
    await callback.message.answer("Начинаем проверку ответов.")
    await callback.message.answer("Чтобы вернуться в меню, нажми 'Назад'.", reply_markup=back_keyboard())
    await ask_next_answer(callback.message, state)
    await callback.answer()


@dp.callback_query(F.data == "answer_finish")
async def answer_finish(callback: CallbackQuery, state: FSMContext) -> None:
    await finalize_answers(callback, state)
    await callback.answer()


@dp.callback_query(F.data == "answer_skip")
async def answer_skip(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    rows = data.get("rows", [])
    idx = int(data.get("idx", 0))
    if idx >= len(rows):
        await finalize_answers(callback, state)
        await callback.answer()
        return

    row = rows[idx]
    # Пропуск = неверный ответ
    db.update_answer(int(row["variant_item_id"]), "", False)
    user_db_row = db.get_user(callback.from_user.id)
    if user_db_row:
        db.insert_attempt(
            user_id=int(user_db_row["id"]),
            exam_type=str(row["exam_type"]),
            task_number=str(row["task_number"]),
            task_id=int(row["id"]),
            variant_item_id=int(row["variant_item_id"]),
            user_answer="",
            is_correct=False,
        )

    answered = int(data.get("answered", 0)) + 1
    correct = int(data.get("correct", 0))
    await state.update_data(idx=idx + 1, answered=answered, correct=correct)
    await callback.message.answer("Пропущено (засчитано как неверно).")
    await ask_next_answer(callback.message, state, user_id=callback.from_user.id)
    await callback.answer()


@dp.message(F.text == BTN_FINISH_EARLY, AnswerFlow.waiting_answer)
async def answer_finish_message(message: Message, state: FSMContext) -> None:
    await finalize_answers(message, state)


@dp.message(AnswerFlow.waiting_answer)
async def process_answer(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пришли ответ текстом.")
        return

    data = await state.get_data()
    rows = data["rows"]
    idx = int(data["idx"])
    if idx >= len(rows):
        await finalize_answers(message, state)
        return

    row = rows[idx]
    user_answer = message.text.strip()
    expected = str(row["answer_text"] or "")
    is_correct = is_answer_correct(
        user_answer, expected, str(row["exam_type"]), str(row["task_number"])
    )
    logger.info(
        "Answer checked user=%s variant_item=%s task_number=%s correct=%s",
        message.from_user.id,
        row["variant_item_id"],
        row["task_number"],
        is_correct,
    )

    db.update_answer(int(row["variant_item_id"]), user_answer, is_correct)
    user_db_row = db.get_user(message.from_user.id)
    if user_db_row:
        db.insert_attempt(
            user_id=int(user_db_row["id"]),
            exam_type=str(row["exam_type"]),
            task_number=str(row["task_number"]),
            task_id=int(row["id"]),
            variant_item_id=int(row["variant_item_id"]),
            user_answer=user_answer,
            is_correct=is_correct,
        )

    answered = int(data.get("answered", 0)) + 1
    correct = int(data.get("correct", 0)) + (1 if is_correct else 0)
    idx += 1
    await state.update_data(idx=idx, answered=answered, correct=correct)

    verdict = "Верно" if is_correct else f"Неверно. Правильный ответ: {expected}"
    await message.answer(verdict)
    await ask_next_answer(message, state)


async def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Set BOT_TOKEN environment variable before launch.")
    bot = Bot(token=token)
    logger.info("Bot starting polling")
    cleanup_old_combined_images()
    cleanup_task = asyncio.create_task(periodic_cleanup_task())
    try:
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
