import argparse
import sqlite3
from pathlib import Path
from typing import Iterator, Optional


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

EXAM_SOURCES = {
    "ege_base": "tasksEgeBaza",
    "ege_profile": "tasksEgeProfile",
    "oge": "tasksOge",
}


def read_text(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text if text else None


def to_posix_rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def pick_image(directory: Path, for_context: bool) -> Optional[Path]:
    if not directory.exists():
        return None

    files = sorted([p for p in directory.iterdir() if p.is_file()], key=lambda p: p.name.lower())
    image_files = [p for p in files if p.suffix.lower() in IMAGE_EXTENSIONS]
    if not image_files:
        return None

    preferred_names = ("context",) if for_context else ("task", "question", "problem")
    for name in preferred_names:
        for image_path in image_files:
            if image_path.stem.lower() == name:
                return image_path

    if for_context:
        for image_path in image_files:
            if image_path.stem.lower().startswith("context"):
                return image_path
        return None

    for image_path in image_files:
        if not image_path.stem.lower().startswith("context"):
            return image_path
    return image_files[0]


def find_context(current_dir: Path, exam_root: Path) -> tuple[Optional[str], Optional[Path]]:
    probe = current_dir
    while True:
        context_text = read_text(probe / "context.txt")
        context_image = pick_image(probe, for_context=True)
        if context_text is not None or context_image is not None:
            return context_text, context_image
        if probe == exam_root:
            return None, None
        probe = probe.parent


def iter_exam_tasks(exam_type: str, exam_root: Path, project_root: Path) -> Iterator[dict]:
    for current_dir, _, files in exam_root.walk():
        if "answer.txt" not in files:
            continue

        current_path = Path(current_dir)
        answer_text = read_text(current_path / "answer.txt")
        if answer_text is None:
            continue

        task_text = read_text(current_path / "task.txt")
        task_image = pick_image(current_path, for_context=False)
        context_text, context_image = find_context(current_path, exam_root)

        relative_dir = to_posix_rel(current_path, exam_root)
        parts = relative_dir.split("/") if relative_dir else []
        task_number = parts[0] if parts else "unknown"
        task_code = parts[-1] if parts else "unknown"

        if task_text is None and task_image is None:
            # Иногда парсер мог сохранить только answer.txt.
            # Такие записи пропускаем, чтобы не заносить пустые задания.
            continue

        yield {
            "exam_type": exam_type,
            "task_number": task_number,
            "task_code": task_code,
            "source_rel_path": to_posix_rel(current_path, exam_root),
            "answer_text": answer_text,
            "task_text": task_text,
            "task_image_path": to_posix_rel(task_image, project_root) if task_image else None,
            "context_text": context_text,
            "context_image_path": to_posix_rel(context_image, project_root) if context_image else None,
        }


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_type TEXT NOT NULL,
            task_number TEXT NOT NULL,
            task_code TEXT NOT NULL,
            source_rel_path TEXT NOT NULL,
            answer_text TEXT,
            task_text TEXT,
            task_image_path TEXT,
            context_text TEXT,
            context_image_path TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(exam_type, source_rel_path)
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_exam_task
            ON tasks (exam_type, task_number);
        """
    )


def recreate_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS tasks")
    ensure_schema(conn)


def upsert_task(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO tasks (
            exam_type, task_number, task_code, source_rel_path,
            answer_text, task_text, task_image_path, context_text, context_image_path
        )
        VALUES (
            :exam_type, :task_number, :task_code, :source_rel_path,
            :answer_text, :task_text, :task_image_path, :context_text, :context_image_path
        )
        ON CONFLICT(exam_type, source_rel_path) DO UPDATE SET
            task_number = excluded.task_number,
            task_code = excluded.task_code,
            answer_text = excluded.answer_text,
            task_text = excluded.task_text,
            task_image_path = excluded.task_image_path,
            context_text = excluded.context_text,
            context_image_path = excluded.context_image_path
        """
    , row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import parsed OGE/EGE tasks from folders into SQLite."
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root path (default: current directory).",
    )
    parser.add_argument(
        "--parsed-root",
        default="data/parsedBanks",
        help="Path to parsed tasks root directory.",
    )
    parser.add_argument(
        "--db-path",
        default="data/bot/bot.db",
        help="SQLite database path.",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop and recreate tasks table before import.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    parsed_root = (project_root / args.parsed_root).resolve()
    db_path = (project_root / args.db_path).resolve()

    if not parsed_root.exists():
        raise FileNotFoundError(f"Parsed root not found: {parsed_root}")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    imported = 0
    by_exam: dict[str, int] = {}

    with sqlite3.connect(db_path) as conn:
        if args.recreate:
            recreate_table(conn)
        else:
            ensure_schema(conn)

        for exam_type, source_dir_name in EXAM_SOURCES.items():
            exam_root = parsed_root / source_dir_name
            if not exam_root.exists():
                print(f"[WARN] Skip missing source: {exam_root}")
                continue

            exam_count = 0
            for row in iter_exam_tasks(exam_type, exam_root, project_root):
                upsert_task(conn, row)
                imported += 1
                exam_count += 1

            by_exam[exam_type] = exam_count

        conn.commit()

    print(f"[OK] Import finished: {imported} rows.")
    for exam_type, count in by_exam.items():
        print(f"  - {exam_type}: {count}")
    print(f"[OK] DB file: {db_path}")


if __name__ == "__main__":
    main()
