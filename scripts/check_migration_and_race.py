"""Ad-hoc checks (run manually): legacy-DB migration and pause/save race."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from exam_service.clock import VirtualClock  # noqa: E402
from exam_service.main import create_app  # noqa: E402
from exam_service.security import hash_password  # noqa: E402

ADMIN = ("admin", "admin-pass-123")
C1 = ("candidate", "candidate-pass-123")

# Schema exactly as it was before timed sessions existed.
LEGACY_SQL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, role TEXT NOT NULL, display_name TEXT NOT NULL
);
CREATE TABLE questions (id INTEGER PRIMARY KEY, current_version INTEGER NOT NULL);
CREATE TABLE question_versions (
    id INTEGER PRIMARY KEY, question_id INTEGER NOT NULL, version INTEGER NOT NULL,
    qtype TEXT NOT NULL, stem TEXT NOT NULL, options_json TEXT NOT NULL,
    correct_json TEXT NOT NULL, max_score INTEGER NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (question_id, version)
);
CREATE TABLE exams (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
    total_score INTEGER NOT NULL, question_pins_json TEXT NOT NULL,
    question_order_json TEXT NOT NULL
);
CREATE TABLE exam_questions (
    id INTEGER PRIMARY KEY, exam_id INTEGER NOT NULL, question_id INTEGER NOT NULL,
    version INTEGER NOT NULL, position INTEGER NOT NULL, qtype TEXT NOT NULL,
    stem TEXT NOT NULL, options_json TEXT NOT NULL, option_order_json TEXT NOT NULL,
    max_score INTEGER NOT NULL, UNIQUE (exam_id, question_id)
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY, exam_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
    status TEXT NOT NULL, answers_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 0, submitted_at TEXT, created_at TEXT NOT NULL,
    UNIQUE (exam_id, candidate_id)
);
CREATE TABLE answer_operations (
    id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL, op_id TEXT NOT NULL,
    kind TEXT NOT NULL, revision INTEGER NOT NULL, answers_json TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE (attempt_id, op_id)
);
CREATE TABLE grading_schemes (
    id INTEGER PRIMARY KEY, exam_id INTEGER NOT NULL, scheme_version INTEGER NOT NULL,
    answer_key_json TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (exam_id, scheme_version)
);
CREATE TABLE result_versions (
    id INTEGER PRIMARY KEY, exam_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
    result_version INTEGER NOT NULL, status TEXT NOT NULL, total_score INTEGER NOT NULL,
    scheme_id INTEGER NOT NULL, created_at TEXT NOT NULL, published_at TEXT,
    UNIQUE (exam_id, candidate_id, result_version)
);
CREATE TABLE result_items (
    id INTEGER PRIMARY KEY, result_version_id INTEGER NOT NULL, question_id INTEGER NOT NULL,
    auto_score INTEGER NOT NULL, manual_score INTEGER, manual_reason TEXT,
    effective_score INTEGER NOT NULL
);
CREATE TABLE manual_score_actions (
    id INTEGER PRIMARY KEY, exam_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL, score INTEGER, reason TEXT, grader_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE grading_runs (
    id INTEGER PRIMARY KEY, exam_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
    scheme_id INTEGER NOT NULL, trigger TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def check_migration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "legacy.db"
        # Build an old-style database by hand (no new tables/columns).
        raw = sqlite3.connect(db_path)
        raw.executescript(LEGACY_SQL)
        for username, password, role, display in [
            ("admin", "admin-pass-123", "admin", "管理员"),
            ("grader", "grader-pass-123", "grader", "阅卷员"),
            ("candidate", "candidate-pass-123", "candidate", "考生"),
        ]:
            raw.execute(
                "INSERT INTO users (username, password_hash, role, display_name)"
                " VALUES (?, ?, ?, ?)",
                (username, hash_password(password), role, display),
            )
        raw.commit()
        raw.close()

        clock = VirtualClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
        app = create_app(db_path, clock=clock)  # migration runs here
        with TestClient(app) as client:
            # Classic flow still works on the migrated database.
            q = client.post(
                "/admin/questions",
                auth=ADMIN,
                json={
                    "qtype": "single",
                    "stem": "s",
                    "options": [
                        {"option_id": "A", "text": "a"},
                        {"option_id": "B", "text": "b"},
                    ],
                    "correct_option_ids": ["A"],
                    "max_score": 5,
                },
            ).json()
            exam_id = client.post(
                "/admin/exams",
                auth=ADMIN,
                json={
                    "title": "legacy",
                    "questions": [{"question_id": q["question_id"], "version": 1}],
                },
            ).json()["exam_id"]
            # New timed feature can be configured on the migrated DB too
            # (timed config must precede any paper open).
            r = client.post(
                f"/admin/exams/{exam_id}/timed-config",
                auth=ADMIN,
                json={
                    "sessions": [
                        {
                            "name": "场",
                            "duration_seconds": 60,
                            "candidate_usernames": ["candidate"],
                        }
                    ]
                },
            )
            assert r.status_code == 201, r.text
            paper = client.get(f"/exams/{exam_id}/paper", auth=C1).json()
            assert paper["timing"]["timed_mode"] is True
            assert paper["timing"]["remaining_seconds"] == 60.0

            # Classic untimed flow on the migrated DB stays unchanged.
            classic_id = client.post(
                "/admin/exams",
                auth=ADMIN,
                json={
                    "title": "classic",
                    "questions": [{"question_id": q["question_id"], "version": 1}],
                },
            ).json()["exam_id"]
            classic_paper = client.get(f"/exams/{classic_id}/paper", auth=C1).json()
            assert classic_paper["timing"] == {"timed_mode": False}
        print("migration check: OK")


def check_pause_save_race() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clock = VirtualClock(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))
        app = create_app(Path(tmp) / "race.db", clock=clock)
        with TestClient(app) as client:
            q = client.post(
                "/admin/questions",
                auth=ADMIN,
                json={
                    "qtype": "single",
                    "stem": "s",
                    "options": [
                        {"option_id": "A", "text": "a"},
                        {"option_id": "B", "text": "b"},
                    ],
                    "correct_option_ids": ["A"],
                    "max_score": 5,
                },
            ).json()
            exam_id = client.post(
                "/admin/exams",
                auth=ADMIN,
                json={
                    "title": "race",
                    "questions": [{"question_id": q["question_id"], "version": 1}],
                },
            ).json()["exam_id"]
            client.post(
                f"/admin/exams/{exam_id}/timed-config",
                auth=ADMIN,
                json={
                    "sessions": [
                        {
                            "name": "场",
                            "duration_seconds": 600,
                            "candidate_usernames": ["candidate"],
                        }
                    ]
                },
            )
            session_id = client.get(f"/admin/exams/{exam_id}", auth=ADMIN).json()[
                "sessions"
            ][0]["session_id"]
            client.get(f"/exams/{exam_id}/paper", auth=C1)

            barrier = threading.Barrier(2)
            outcomes = []

            def save():
                barrier.wait()
                r = client.post(
                    f"/exams/{exam_id}/answers",
                    auth=C1,
                    json={
                        "op_id": "racing-save",
                        "base_revision": 0,
                        "answers": {str(q["question_id"]): ["A"]},
                    },
                )
                outcomes.append(("save", r.status_code))

            def pause():
                barrier.wait()
                r = client.post(
                    f"/admin/exams/{exam_id}/sessions/{session_id}/pause",
                    auth=ADMIN,
                    json={"op_id": "racing-pause"},
                )
                outcomes.append(("pause", r.status_code))

            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(save)
                f2 = pool.submit(pause)
                f1.result(), f2.result()

            assert ("pause", 200) in outcomes, outcomes
            save_code = next(code for name, code in outcomes if name == "save")
            assert save_code in (200, 409), outcomes
            paper = client.get(f"/exams/{exam_id}/paper", auth=C1).json()
            assert paper["timing"]["paused"] is True
            print(f"race check: OK (save got {save_code}, pause got 200)")


if __name__ == "__main__":
    check_migration()
    check_pause_save_race()
