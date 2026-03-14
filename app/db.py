import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class TaskRow:
    id: int
    exam_type: str
    task_number: str
    task_code: str
    source_rel_path: str
    answer_text: str
    task_text: Optional[str]
    task_image_path: Optional[str]
    context_text: Optional[str]
    context_image_path: Optional[str]


class BotDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def _ensure_schema(self) -> None:
        self.conn.executescript(
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

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER NOT NULL UNIQUE,
                username TEXT,
                first_name TEXT,
                current_exam TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                exam_type TEXT NOT NULL,
                mode TEXT NOT NULL,
                requested_task_number TEXT,
                requested_count INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS variant_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variant_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                user_answer TEXT,
                is_correct INTEGER,
                checked_at TEXT,
                FOREIGN KEY (variant_id) REFERENCES variants(id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_variant_position
                ON variant_items (variant_id, position);

            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                exam_type TEXT NOT NULL,
                task_number TEXT NOT NULL,
                task_id INTEGER NOT NULL,
                variant_item_id INTEGER NOT NULL,
                user_answer TEXT,
                is_correct INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY (variant_item_id) REFERENCES variant_items(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_attempts_user_exam_task
                ON attempts (user_id, exam_type, task_number);
            """
        )
        self.conn.commit()

    def upsert_user(self, tg_user_id: int, username: Optional[str], first_name: Optional[str]) -> int:
        self.conn.execute(
            """
            INSERT INTO users (tg_user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (tg_user_id, username, first_name),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM users WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()
        return int(row["id"])

    def get_user(self, tg_user_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM users WHERE tg_user_id = ?",
            (tg_user_id,),
        ).fetchone()

    def set_current_exam(self, tg_user_id: int, exam_type: str) -> None:
        self.conn.execute(
            """
            UPDATE users
            SET current_exam = ?, updated_at = CURRENT_TIMESTAMP
            WHERE tg_user_id = ?
            """,
            (exam_type, tg_user_id),
        )
        self.conn.commit()

    def list_task_numbers(self, exam_type: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT task_number
            FROM tasks
            WHERE exam_type = ?
            """,
            (exam_type,),
        ).fetchall()
        return [str(r["task_number"]) for r in rows]

    def get_random_tasks(self, exam_type: str, task_number: str, limit: int) -> list[TaskRow]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM tasks
            WHERE exam_type = ? AND task_number = ?
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (exam_type, task_number, limit),
        ).fetchall()
        return [self._to_task_row(r) for r in rows]

    def get_solved_task_ids(self, user_id: int, exam_type: str, task_number: Optional[str] = None) -> set[int]:
        if task_number is None:
            rows = self.conn.execute(
                """
                SELECT DISTINCT task_id
                FROM attempts
                WHERE user_id = ? AND exam_type = ?
                """,
                (user_id, exam_type),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT DISTINCT task_id
                FROM attempts
                WHERE user_id = ? AND exam_type = ? AND task_number = ?
                """,
                (user_id, exam_type, task_number),
            ).fetchall()
        return {int(r["task_id"]) for r in rows}

    def get_random_tasks_for_user(
        self,
        exam_type: str,
        task_number: str,
        limit: int,
        user_id: int,
    ) -> list[TaskRow]:
        fresh_rows = self.conn.execute(
            """
            SELECT t.*
            FROM tasks t
            WHERE t.exam_type = ?
              AND t.task_number = ?
              AND t.id NOT IN (
                SELECT DISTINCT a.task_id
                FROM attempts a
                WHERE a.user_id = ? AND a.exam_type = ?
              )
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (exam_type, task_number, user_id, exam_type, limit),
        ).fetchall()

        fresh = [self._to_task_row(r) for r in fresh_rows]
        if len(fresh) >= limit:
            return fresh

        need = limit - len(fresh)
        picked_ids = [t.id for t in fresh]
        if picked_ids:
            placeholders = ",".join(["?"] * len(picked_ids))
            rows = self.conn.execute(
                f"""
                SELECT t.*
                FROM tasks t
                WHERE t.exam_type = ?
                  AND t.task_number = ?
                  AND t.id NOT IN ({placeholders})
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (exam_type, task_number, *picked_ids, need),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT t.*
                FROM tasks t
                WHERE t.exam_type = ?
                  AND t.task_number = ?
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (exam_type, task_number, need),
            ).fetchall()
        return fresh + [self._to_task_row(r) for r in rows]

    def get_tasks_by_ids(self, task_ids: Iterable[int]) -> list[TaskRow]:
        task_ids = list(task_ids)
        if not task_ids:
            return []
        placeholders = ",".join(["?"] * len(task_ids))
        rows = self.conn.execute(
            f"SELECT * FROM tasks WHERE id IN ({placeholders})",
            task_ids,
        ).fetchall()
        by_id = {int(r["id"]): self._to_task_row(r) for r in rows}
        return [by_id[tid] for tid in task_ids if tid in by_id]

    def create_variant(
        self,
        user_id: int,
        exam_type: str,
        mode: str,
        requested_task_number: Optional[str],
        requested_count: Optional[int],
        tasks: list[TaskRow],
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO variants (user_id, exam_type, mode, requested_task_number, requested_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, exam_type, mode, requested_task_number, requested_count),
        )
        variant_id = int(cur.lastrowid)
        for idx, task in enumerate(tasks, start=1):
            self.conn.execute(
                """
                INSERT INTO variant_items (variant_id, position, task_id)
                VALUES (?, ?, ?)
                """,
                (variant_id, idx, task.id),
            )
        self.conn.commit()
        return variant_id

    def get_variant_tasks(self, variant_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT
                vi.id AS variant_item_id,
                vi.position,
                vi.user_answer,
                vi.is_correct,
                t.*
            FROM variant_items vi
            JOIN tasks t ON t.id = vi.task_id
            WHERE vi.variant_id = ?
            ORDER BY vi.position
            """,
            (variant_id,),
        ).fetchall()

    def update_answer(self, variant_item_id: int, user_answer: str, is_correct: bool) -> None:
        self.conn.execute(
            """
            UPDATE variant_items
            SET user_answer = ?, is_correct = ?, checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (user_answer, 1 if is_correct else 0, variant_item_id),
        )
        self.conn.commit()

    def insert_attempt(
        self,
        user_id: int,
        exam_type: str,
        task_number: str,
        task_id: int,
        variant_item_id: int,
        user_answer: str,
        is_correct: bool,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO attempts (
                user_id, exam_type, task_number, task_id, variant_item_id, user_answer, is_correct
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, exam_type, task_number, task_id, variant_item_id, user_answer, 1 if is_correct else 0),
        )
        self.conn.commit()

    def get_wrong_task_ids(self, user_id: int, exam_type: str, limit: int) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT task_id
            FROM attempts
            WHERE user_id = ? AND exam_type = ? AND is_correct = 0
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, exam_type, limit),
        ).fetchall()
        return [int(r["task_id"]) for r in rows]

    def get_stats(self, user_id: int, exam_type: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT
                task_number,
                COUNT(*) AS total_attempts,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) AS correct_attempts
            FROM attempts
            WHERE user_id = ? AND exam_type = ?
            GROUP BY task_number
            """,
            (user_id, exam_type),
        ).fetchall()

    def get_admin_stats(self) -> dict:
        """Статистика для админ-панели: решено всего, % верных, пользователи, активные за 30 дней."""
        total_attempts = self.conn.execute(
            "SELECT COUNT(*) AS n FROM attempts",
        ).fetchone()
        total_attempts = int(total_attempts["n"] or 0)

        correct_attempts = self.conn.execute(
            "SELECT SUM(is_correct) AS n FROM attempts",
        ).fetchone()
        correct_attempts = int(correct_attempts["n"] or 0)

        total_users = self.conn.execute(
            "SELECT COUNT(*) AS n FROM users",
        ).fetchone()
        total_users = int(total_users["n"] or 0)

        active_30d = self.conn.execute(
            """
            SELECT COUNT(DISTINCT u.id) AS n
            FROM users u
            WHERE EXISTS (
                SELECT 1 FROM attempts a
                WHERE a.user_id = u.id AND a.created_at >= datetime('now', '-30 days')
            ) OR EXISTS (
                SELECT 1 FROM variants v
                WHERE v.user_id = u.id AND v.created_at >= datetime('now', '-30 days')
            )
            """,
        ).fetchone()
        active_30d = int(active_30d["n"] or 0)

        correct_pct = (correct_attempts / total_attempts * 100) if total_attempts else 0.0
        return {
            "total_attempts": total_attempts,
            "correct_attempts": correct_attempts,
            "correct_percent": correct_pct,
            "total_users": total_users,
            "active_users_30d": active_30d,
        }

    @staticmethod
    def _to_task_row(row: sqlite3.Row) -> TaskRow:
        return TaskRow(
            id=int(row["id"]),
            exam_type=str(row["exam_type"]),
            task_number=str(row["task_number"]),
            task_code=str(row["task_code"]),
            source_rel_path=str(row["source_rel_path"]),
            answer_text=str(row["answer_text"] or ""),
            task_text=row["task_text"],
            task_image_path=row["task_image_path"],
            context_text=row["context_text"],
            context_image_path=row["context_image_path"],
        )
