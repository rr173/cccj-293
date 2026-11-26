"""Core domain logic.

Concurrency model: every public mutating function runs inside
``Database.write()``, i.e. one global ``BEGIN IMMEDIATE`` transaction at a
time. Read-modify-write checks (revision, idempotency keys, result-version
conflicts, suspension state, time-limit settlement) are therefore atomic.
Callers see strict serial ordering.

Time model: every time decision uses an injected :class:`Clock` (server
time). Production uses the real UTC wall clock; tests drive a virtual clock.
Time-limit auto-submission is *lazy*: nothing runs in the background, but
the first read or write that touches an expired attempt deterministically
settles it, so an attempt that expired while the server was down is still
submitted the moment anyone looks at it.
"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta
from typing import Any, Optional

from .clock import Clock, RealClock
from .db import Database
from .schemas import QuestionVersionIn

_REAL_CLOCK = RealClock()


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: str) -> Any:
    return json.loads(raw)


def _row_to_dict(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# =========================== Question bank ===========================


def create_question(
    db: Database, payload: QuestionVersionIn, clock: Clock = _REAL_CLOCK
) -> dict[str, Any]:
    with db.write() as conn:
        cur = conn.execute("INSERT INTO questions (current_version) VALUES (1)")
        question_id = cur.lastrowid
        _insert_question_version(conn, question_id, 1, payload, clock)
        return _get_question_out(conn, question_id, 1)


def edit_question(
    db: Database,
    question_id: int,
    payload: QuestionVersionIn,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        row = conn.execute(
            "SELECT current_version FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(404, f"question {question_id} not found")
        new_version = row["current_version"] + 1
        _insert_question_version(conn, question_id, new_version, payload, clock)
        conn.execute(
            "UPDATE questions SET current_version = ? WHERE id = ?",
            (new_version, question_id),
        )
        return _get_question_out(conn, question_id, new_version)


def _insert_question_version(
    conn, question_id: int, version: int, payload: QuestionVersionIn, clock: Clock
) -> None:
    option_ids = {o.option_id for o in payload.options}
    unknown = set(payload.correct_option_ids) - option_ids
    if unknown:
        raise ServiceError(
            422,
            f"correct_option_ids {sorted(unknown)} are not among the option ids",
        )
    if payload.qtype == "single" and len(payload.correct_option_ids) != 1:
        raise ServiceError(422, "single-choice question must have exactly one correct option")
    conn.execute(
        "INSERT INTO question_versions"
        " (question_id, version, qtype, stem, options_json, correct_json, max_score, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            question_id,
            version,
            payload.qtype,
            payload.stem,
            _dumps([o.model_dump() for o in payload.options]),
            _dumps(payload.correct_option_ids),
            payload.max_score,
            clock.now_iso(),
        ),
    )


def _get_question_out(conn, question_id: int, version: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT qv.*, q.current_version FROM question_versions qv"
        " JOIN questions q ON q.id = qv.question_id"
        " WHERE qv.question_id = ? AND qv.version = ?",
        (question_id, version),
    ).fetchone()
    return {
        "question_id": row["question_id"],
        "current_version": row["current_version"],
        "version": row["version"],
        "qtype": row["qtype"],
        "stem": row["stem"],
        "options": _loads(row["options_json"]),
        "correct_option_ids": _loads(row["correct_json"]),
        "max_score": row["max_score"],
    }


def list_questions(db: Database) -> list[dict[str, Any]]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT qv.*, q.current_version FROM question_versions qv"
            " JOIN questions q ON q.id = qv.question_id"
            " ORDER BY qv.question_id, qv.version"
        ).fetchall()
        return [
            {
                "question_id": r["question_id"],
                "current_version": r["current_version"],
                "version": r["version"],
                "qtype": r["qtype"],
                "stem": r["stem"],
                "options": _loads(r["options_json"]),
                "correct_option_ids": _loads(r["correct_json"]),
                "max_score": r["max_score"],
            }
            for r in rows
        ]


# =========================== Exam generation ===========================


def create_exam(
    db: Database,
    title: str,
    pins: list[dict[str, int]],
    shuffle: bool,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        pinned: list[dict[str, Any]] = []
        for pin in pins:
            qid, ver = pin["question_id"], pin["version"]
            row = conn.execute(
                "SELECT * FROM question_versions WHERE question_id = ? AND version = ?",
                (qid, ver),
            ).fetchone()
            if row is None:
                raise ServiceError(422, f"question {qid} version {ver} does not exist")
            pinned.append(_row_to_dict(row))

        if len({r["question_id"] for r in pinned}) != len(pinned):
            raise ServiceError(422, "the same question appears more than once")

        rng = random.Random()  # OS-seeded; ordering is then stored, never reshuffled
        question_order = list(range(len(pinned)))
        if shuffle:
            rng.shuffle(question_order)

        cur = conn.execute(
            "INSERT INTO exams (title, created_at, total_score, question_pins_json,"
            " question_order_json, timed_mode) VALUES (?, ?, ?, ?, ?, 0)",
            (
                title,
                clock.now_iso(),
                sum(r["max_score"] for r in pinned),
                _dumps([{"question_id": r["question_id"], "version": r["version"]} for r in pinned]),
                _dumps([pinned[i]["question_id"] for i in question_order]),
            ),
        )
        exam_id = cur.lastrowid

        # Snapshot: stem/options/max score are copied; the bank can change later.
        for position, idx in enumerate(question_order):
            r = pinned[idx]
            option_ids = [o["option_id"] for o in _loads(r["options_json"])]
            option_order = list(option_ids)
            if shuffle:
                rng.shuffle(option_order)
            conn.execute(
                "INSERT INTO exam_questions"
                " (exam_id, question_id, version, position, qtype, stem, options_json,"
                "  option_order_json, max_score)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    exam_id,
                    r["question_id"],
                    r["version"],
                    position,
                    r["qtype"],
                    r["stem"],
                    r["options_json"],
                    _dumps(option_order),
                    r["max_score"],
                ),
            )

        # Initial grading scheme = the answer key captured at issue time.
        answer_key = {
            str(r["question_id"]): _loads(r["correct_json"]) for r in pinned
        }
        _insert_scheme(conn, exam_id, 1, answer_key, "initial at exam creation", clock)
        return _exam_out(conn, exam_id)


def _insert_scheme(
    conn, exam_id: int, scheme_version: int, answer_key: dict, source: str, clock: Clock
) -> int:
    cur = conn.execute(
        "INSERT INTO grading_schemes"
        " (exam_id, scheme_version, answer_key_json, source, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (exam_id, scheme_version, _dumps(answer_key), source, clock.now_iso()),
    )
    return cur.lastrowid


def _exam_out(conn, exam_id: int) -> dict[str, Any]:
    exam = conn.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
    scheme = conn.execute(
        "SELECT * FROM grading_schemes WHERE exam_id = ?"
        " ORDER BY scheme_version DESC LIMIT 1",
        (exam_id,),
    ).fetchone()
    out = {
        "exam_id": exam["id"],
        "title": exam["title"],
        "total_score": exam["total_score"],
        "created_at": exam["created_at"],
        "question_pins": _loads(exam["question_pins_json"]),
        "active_scheme_version": scheme["scheme_version"],
        "active_answer_key": _loads(scheme["answer_key_json"]),
        "timed_mode": bool(exam["timed_mode"]),
    }
    if exam["timed_mode"]:
        sessions = conn.execute(
            "SELECT id, name, duration_seconds, paused, created_at, capacity"
            " FROM exam_sessions WHERE exam_id = ? ORDER BY id",
            (exam_id,),
        ).fetchall()
        out["sessions"] = [
            {
                "session_id": s["id"],
                "name": s["name"],
                "duration_seconds": s["duration_seconds"],
                "capacity": s["capacity"],
                "paused": bool(s["paused"]),
                "candidates": [
                    r["username"]
                    for r in conn.execute(
                        "SELECT u.username FROM exam_enrollments e"
                        " JOIN users u ON u.id = e.candidate_id"
                        " WHERE e.session_id = ? ORDER BY u.username",
                        (s["id"],),
                    )
                ],
                "created_at": s["created_at"],
            }
            for s in sessions
        ]
    return out


def get_exam(db: Database, exam_id: int) -> dict[str, Any]:
    with db.read() as conn:
        row = conn.execute("SELECT 1 FROM exams WHERE id = ?", (exam_id,)).fetchone()
        if row is None:
            raise ServiceError(404, f"exam {exam_id} not found")
        return _exam_out(conn, exam_id)


def _get_exam_questions(conn, exam_id: int) -> list:
    return conn.execute(
        "SELECT * FROM exam_questions WHERE exam_id = ? ORDER BY position",
        (exam_id,),
    ).fetchall()


# =========================== Timed sessions & roster ===========================


def configure_timed_exam(
    db: Database,
    exam_id: int,
    sessions_in: list[dict[str, Any]],
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    """Create timed sessions and the roster for a new exam.

    Each candidate belongs to exactly one session; only roster candidates may
    open the timed exam afterwards.
    """
    with db.write() as conn:
        exam = conn.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
        if exam is None:
            raise ServiceError(404, f"exam {exam_id} not found")
        if exam["timed_mode"]:
            raise ServiceError(409, f"exam {exam_id} is already configured as timed")
        existing = conn.execute(
            "SELECT 1 FROM attempts WHERE exam_id = ? LIMIT 1", (exam_id,)
        ).fetchone()
        if existing is not None:
            raise ServiceError(409, "exam already has attempts; timed config must precede any open")

        now_iso = clock.now_iso()
        created_session_ids: list[int] = []
        for s in sessions_in:
            cur = conn.execute(
                "INSERT INTO exam_sessions (exam_id, name, duration_seconds, paused, created_at)"
                " VALUES (?, ?, ?, 0, ?)",
                (exam_id, s["name"], s["duration_seconds"], now_iso),
            )
            created_session_ids.append(cur.lastrowid)

        for s, session_id in zip(sessions_in, created_session_ids):
            for username in s["candidate_usernames"]:
                user = conn.execute(
                    "SELECT id, role FROM users WHERE username = ?", (username,)
                ).fetchone()
                if user is None:
                    raise ServiceError(422, f"candidate user '{username}' not found")
                if user["role"] != "candidate":
                    raise ServiceError(422, f"user '{username}' is not a candidate")
                conn.execute(
                    "INSERT INTO exam_enrollments (exam_id, session_id, candidate_id, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (exam_id, session_id, user["id"], now_iso),
                )

        conn.execute(
            "UPDATE exams SET timed_mode = 1 WHERE id = ?", (exam_id,)
        )
        return _exam_out(conn, exam_id)


# =========================== Pause bookkeeping helpers ===========================


def _paused_micros(conn, clock: Clock, attempt, now: datetime) -> int:
    """Total stopped time for the attempt up to ``now``.

    Intervals from the session layer and the candidate layer are unioned via
    interval merging, so an overlap stops the clock only once.
    """
    rows = conn.execute(
        "SELECT pause_at, resume_at FROM attempt_pause_intervals WHERE attempt_id = ?",
        (attempt["id"],),
    ).fetchall()
    spans: list[tuple[int, int]] = []
    for r in rows:
        start = clock.micros(clock.parse(r["pause_at"]))
        end = (
            clock.micros(clock.parse(r["resume_at"]))
            if r["resume_at"] is not None
            else clock.micros(now)
        )
        spans.append((start, max(end, start)))
    if not spans:
        return 0
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def _deadline_micros(conn, clock: Clock, attempt, now: datetime) -> int:
    started = clock.parse(attempt["started_at"])
    base = clock.micros(started) + attempt["duration_seconds"] * 1_000_000
    return base + _paused_micros(conn, clock, attempt, now)


def _timing_view(conn, clock: Clock, attempt, now: datetime) -> dict[str, Any]:
    if not attempt["timed_mode"]:
        return {"timed_mode": False}
    deadline_us = _deadline_micros(conn, clock, attempt, now)
    now_us = clock.micros(now)
    session = conn.execute(
        "SELECT id, name, paused FROM exam_sessions WHERE id = ?",
        (attempt["session_id"],),
    ).fetchone()
    return {
        "timed_mode": True,
        "duration_seconds": attempt["duration_seconds"],
        "started_at": attempt["started_at"],
        "session_id": attempt["session_id"],
        "session_name": session["name"] if session is not None else None,
        "session_paused": bool(session["paused"]) if session is not None else False,
        "candidate_paused": bool(attempt["candidate_paused"]),
        "paused": bool(attempt["candidate_paused"])
        or (bool(session["paused"]) if session is not None else False),
        "now": clock.now_iso(),
        "remaining_seconds": max(0, (deadline_us - now_us) / 1_000_000),
        "deadline_at": clock.iso(clock.from_micros(deadline_us)),
        "auto_submitted_at": attempt["auto_submitted_at"],
    }


# =========================== Lazy time-limit settlement ===========================


def _scheme_effective_at(conn, exam_id: int, cutoff: datetime, clock: Clock):
    """Newest grading scheme strictly older than the cutoff instant.

    A key becoming effective exactly at the cutoff is intentionally excluded
    from the automatic grading and left for a later regrade.
    """
    cutoff_us = clock.micros(cutoff)
    rows = conn.execute(
        "SELECT * FROM grading_schemes WHERE exam_id = ? ORDER BY scheme_version DESC",
        (exam_id,),
    ).fetchall()
    for row in rows:
        if clock.micros(clock.parse(row["created_at"])) < cutoff_us:
            return row
    return rows[-1] if rows else None


def _settle_if_expired(conn, clock: Clock, attempt, now: datetime):
    """Auto-submit an in-progress timed attempt once its time is exhausted.

    Idempotent: a settled attempt is simply reloaded afterwards. Must run
    inside a write transaction.
    """
    if attempt["status"] == "submitted" or not attempt["timed_mode"]:
        return attempt
    if attempt["started_at"] is None:
        return attempt
    deadline_us = _deadline_micros(conn, clock, attempt, now)
    if clock.micros(now) < deadline_us:
        return attempt

    cutoff = clock.from_micros(deadline_us)
    cutoff_iso = clock.iso(cutoff)
    # Only answers already saved before the cutoff count. In practice the
    # write lock guarantees nothing newer exists inside this transaction, but
    # the audit timestamps make the rule explicit and durable.
    conn.execute(
        "UPDATE attempts SET status = 'submitted', submitted_at = ?,"
        " auto_submitted_at = ?, graded_at = ? WHERE id = ?",
        (cutoff_iso, cutoff_iso, cutoff_iso, attempt["id"]),
    )
    auto_op_id = f"auto-submit@{cutoff_iso}"
    conn.execute(
        "INSERT INTO answer_operations"
        " (attempt_id, op_id, kind, revision, answers_json, created_at)"
        " VALUES (?, ?, 'submit', ?, ?, ?)",
        (
            attempt["id"],
            auto_op_id,
            attempt["revision"],
            attempt["answers_json"],
            cutoff_iso,
        ),
    )
    scheme = _scheme_effective_at(conn, attempt["exam_id"], cutoff, clock)
    _auto_grade(
        conn,
        attempt["exam_id"],
        attempt["candidate_id"],
        trigger=f"auto at time limit ({cutoff_iso})",
        scheme_row=scheme,
        created_at=cutoff_iso,
    )
    return conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt["id"],)).fetchone()


def _settle_candidate(db: Database, exam_id: int, candidate_id: int, clock: Clock) -> None:
    """Settle the candidate's attempt if it expired (used by read endpoints)."""
    with db.write() as conn:
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()
        if attempt is not None:
            _settle_if_expired(conn, clock, attempt, clock.now())


# =========================== Attempt / paper ===========================


def _roster_row(conn, exam_id: int, candidate_id: int):
    return conn.execute(
        "SELECT e.*, s.name AS session_name, s.duration_seconds, s.paused AS session_paused"
        " FROM exam_enrollments e JOIN exam_sessions s ON s.id = e.session_id"
        " WHERE e.exam_id = ? AND e.candidate_id = ?",
        (exam_id, candidate_id),
    ).fetchone()


def _get_or_create_attempt(
    conn, exam_id: int, candidate_id: int, clock: Clock
):
    row = conn.execute(
        "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
        (exam_id, candidate_id),
    ).fetchone()
    if row is not None:
        return row, False
    exam = conn.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
    if exam is None:
        raise ServiceError(404, f"exam {exam_id} not found")

    now = clock.now()
    if exam["timed_mode"]:
        roster = _roster_row(conn, exam_id, candidate_id)
        if roster is None:
            raise ServiceError(403, "candidate is not on this exam's roster")
        # Neither suspension layer may be active when the clock starts.
        if roster["session_paused"]:
            raise ServiceError(409, "session is paused; the exam cannot be started yet")
        candidate_flag = _candidate_paused_from_events(conn, exam_id, candidate_id)
        if candidate_flag:
            raise ServiceError(409, "your attempt is paused; the exam cannot be started yet")
        cur = conn.execute(
            "INSERT INTO attempts (exam_id, candidate_id, status, answers_json, revision,"
            " created_at, timed_mode, duration_seconds, session_id, started_at,"
            " candidate_paused)"
            " VALUES (?, ?, 'in_progress', '{}', 0, ?, 1, ?, ?, ?, ?)",
            (
                exam_id,
                candidate_id,
                clock.now_iso(),
                roster["duration_seconds"],
                roster["session_id"],
                clock.now_iso(),  # first successful open starts the clock
                1 if candidate_flag else 0,
            ),
        )
        # A first successful start on a makeup exam finalizes the candidate's
        # reservation state in the same transaction (no half-finished moves).
        _makeup_on_exam_start(conn, exam_id, candidate_id, clock)
    else:
        cur = conn.execute(
            "INSERT INTO attempts (exam_id, candidate_id, status, answers_json, revision,"
            " created_at, timed_mode) VALUES (?, ?, 'in_progress', '{}', 0, ?, 0)",
            (exam_id, candidate_id, clock.now_iso()),
        )
    return conn.execute("SELECT * FROM attempts WHERE id = ?", (cur.lastrowid,)).fetchone(), True


def get_paper(
    db: Database, exam_id: int, candidate_id: int, clock: Clock = _REAL_CLOCK
) -> dict[str, Any]:
    """Candidate view: never contains the answer key."""
    with db.write() as conn:
        now = clock.now()
        attempt, _created = _get_or_create_attempt(conn, exam_id, candidate_id, clock)
        attempt = _settle_if_expired(conn, clock, attempt, now)
        questions = []
        for eq in _get_exam_questions(conn, exam_id):
            options = _loads(eq["options_json"])
            order = _loads(eq["option_order_json"])
            by_id = {o["option_id"]: o["text"] for o in options}
            questions.append(
                {
                    "question_id": eq["question_id"],
                    "position": eq["position"],
                    "qtype": eq["qtype"],
                    "stem": eq["stem"],
                    "max_score": eq["max_score"],
                    "options": [
                        {"option_id": oid, "text": by_id[oid]} for oid in order
                    ],
                }
            )
        return {
            "exam_id": exam_id,
            "attempt_id": attempt["id"],
            "status": attempt["status"],
            "revision": attempt["revision"],
            "submitted_at": attempt["submitted_at"],
            "questions": questions,
            "saved_answers": _loads(attempt["answers_json"]),
            "timing": _timing_view(conn, clock, attempt, now),
        }


# =========================== Save / submit ===========================


def _guard_writable_timed_attempt(conn, attempt) -> None:
    """A timed attempt accepts writes only while started, un-paused and open."""
    if not attempt["timed_mode"]:
        return
    if attempt["started_at"] is None:
        raise ServiceError(409, "the exam has not been started yet")
    if attempt["candidate_paused"]:
        raise ServiceError(409, "your attempt is paused; saving is suspended")
    session = conn.execute(
        "SELECT paused FROM exam_sessions WHERE id = ?", (attempt["session_id"],)
    ).fetchone()
    if session is not None and session["paused"]:
        raise ServiceError(409, "the session is paused; saving is suspended")


def save_answers(
    db: Database,
    exam_id: int,
    candidate_id: int,
    op_id: str,
    base_revision: int,
    answers: dict[int, list[str]],
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        attempt, _ = _get_or_create_attempt(conn, exam_id, candidate_id, clock)
        now = clock.now()
        attempt = _settle_if_expired(conn, clock, attempt, now)

        # Idempotent retry of the same operation id: replay the recorded result.
        existing = conn.execute(
            "SELECT * FROM answer_operations WHERE attempt_id = ? AND op_id = ?",
            (attempt["id"], op_id),
        ).fetchone()
        if existing is not None:
            return _attempt_state(
                conn, clock, attempt, idempotent=True, note="op_id already processed"
            )

        if attempt["status"] == "submitted":
            raise ServiceError(409, "attempt already submitted; answers are locked")
        _guard_writable_timed_attempt(conn, attempt)
        if base_revision != attempt["revision"]:
            raise ServiceError(
                409,
                f"stale base_revision {base_revision}: current revision is"
                f" {attempt['revision']}; refetch the paper and retry",
            )

        _validate_answers(conn, exam_id, answers)
        normalized = {str(qid): sorted(oids) for qid, oids in answers.items()}
        new_revision = attempt["revision"] + 1
        conn.execute(
            "UPDATE attempts SET answers_json = ?, revision = ? WHERE id = ?",
            (_dumps(normalized), new_revision, attempt["id"]),
        )
        conn.execute(
            "INSERT INTO answer_operations"
            " (attempt_id, op_id, kind, revision, answers_json, created_at)"
            " VALUES (?, ?, 'save', ?, ?, ?)",
            (attempt["id"], op_id, new_revision, _dumps(normalized), clock.now_iso()),
        )
        fresh = conn.execute(
            "SELECT * FROM attempts WHERE id = ?", (attempt["id"],)
        ).fetchone()
        return _attempt_state(conn, clock, fresh, idempotent=False)


def submit_attempt(
    db: Database,
    exam_id: int,
    candidate_id: int,
    op_id: str,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        attempt, _ = _get_or_create_attempt(conn, exam_id, candidate_id, clock)
        now = clock.now()
        attempt = _settle_if_expired(conn, clock, attempt, now)

        existing = conn.execute(
            "SELECT * FROM answer_operations WHERE attempt_id = ? AND op_id = ?",
            (attempt["id"], op_id),
        ).fetchone()
        if existing is not None:
            # A retry must never produce a second submission.
            fresh = conn.execute(
                "SELECT * FROM attempts WHERE id = ?", (attempt["id"],)
            ).fetchone()
            return _attempt_state(
                conn, clock, fresh, idempotent=True, note="submit op_id already processed"
            )

        if attempt["status"] == "submitted":
            raise ServiceError(409, "attempt already submitted")
        _guard_writable_timed_attempt(conn, attempt)

        submitted_at = clock.now_iso()
        conn.execute(
            "UPDATE attempts SET status = 'submitted', submitted_at = ?,"
            " graded_at = ? WHERE id = ?",
            (submitted_at, submitted_at, attempt["id"]),
        )
        conn.execute(
            "INSERT INTO answer_operations"
            " (attempt_id, op_id, kind, revision, answers_json, created_at)"
            " VALUES (?, ?, 'submit', ?, ?, ?)",
            (
                attempt["id"],
                op_id,
                attempt["revision"],
                attempt["answers_json"],
                submitted_at,
            ),
        )
        # Auto-score immediately against the then-active answer key.
        fresh = conn.execute(
            "SELECT * FROM attempts WHERE id = ?", (attempt["id"],)
        ).fetchone()
        result = _auto_grade(
            conn, exam_id, candidate_id, trigger="auto at submit", created_at=submitted_at
        )
        state = _attempt_state(conn, clock, fresh, idempotent=False)
        state["graded_result_version"] = result["result_version"]
        return state


def _validate_answers(conn, exam_id: int, answers: dict[int, list[str]]) -> None:
    eq_rows = _get_exam_questions(conn, exam_id)
    valid_questions = {r["question_id"]: r for r in eq_rows}
    for qid, oids in answers.items():
        if qid not in valid_questions:
            raise ServiceError(422, f"question {qid} is not on this exam")
        if len(oids) != len(set(oids)):
            raise ServiceError(422, f"duplicate option ids for question {qid}")
        valid_options = {
            o["option_id"] for o in _loads(valid_questions[qid]["options_json"])
        }
        unknown = set(oids) - valid_options
        if unknown:
            raise ServiceError(
                422, f"unknown option ids {sorted(unknown)} for question {qid}"
            )


def _attempt_state(conn, clock: Clock, attempt, *, idempotent: bool, note: str | None = None) -> dict[str, Any]:
    now = clock.now()
    return {
        "attempt_id": attempt["id"],
        "exam_id": attempt["exam_id"],
        "status": attempt["status"],
        "revision": attempt["revision"],
        "answers": _loads(attempt["answers_json"]),
        "submitted_at": attempt["submitted_at"],
        "idempotent_replay": idempotent,
        "note": note,
        "timing": _timing_view(conn, clock, attempt, now),
    }


# =========================== Proctor: pause / resume ===========================


def _candidate_paused_from_events(conn, exam_id: int, candidate_id: int) -> bool:
    """Candidate suspension flag as recorded in the append-only event log."""
    row = conn.execute(
        "SELECT action FROM proctor_events WHERE exam_id = ? AND scope = 'candidate'"
        " AND candidate_id = ? ORDER BY id DESC LIMIT 1",
        (exam_id, candidate_id),
    ).fetchone()
    return row is not None and row["action"] == "pause"


def _record_proctor_event(
    conn, exam_id: int, scope: str, action: str, op_id: str, reason: str | None,
    actor_id: int, clock: Clock, *, session_id: int | None = None,
    candidate_id: int | None = None,
) -> dict[str, Any]:
    cur = conn.execute(
        "INSERT INTO proctor_events"
        " (exam_id, scope, session_id, candidate_id, action, op_id, reason, actor_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            exam_id,
            scope,
            session_id,
            candidate_id,
            action,
            op_id,
            reason,
            actor_id,
            clock.now_iso(),
        ),
    )
    return _row_to_dict(
        conn.execute("SELECT * FROM proctor_events WHERE id = ?", (cur.lastrowid,)).fetchone()
    )


def _proctor_event_out(conn, event: dict[str, Any]) -> dict[str, Any]:
    out = {k: event[k] for k in ("id", "exam_id", "scope", "action", "op_id", "reason", "created_at")}
    out["session_id"] = event["session_id"]
    out["candidate_id"] = event["candidate_id"]
    return out


def _require_timed_exam(conn, exam_id: int):
    exam = conn.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
    if exam is None:
        raise ServiceError(404, f"exam {exam_id} not found")
    if not exam["timed_mode"]:
        raise ServiceError(422, f"exam {exam_id} is not configured with timed sessions")
    return exam


def pause_session(
    db: Database, exam_id: int, session_id: int, op_id: str, reason: str | None,
    actor_id: int, clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        _require_timed_exam(conn, exam_id)
        if replay := _replayed_proctor_event(conn, exam_id, op_id):
            return replay
        session = conn.execute(
            "SELECT * FROM exam_sessions WHERE id = ? AND exam_id = ?",
            (session_id, exam_id),
        ).fetchone()
        if session is None:
            raise ServiceError(404, f"session {session_id} not found on exam {exam_id}")
        if session["paused"]:
            raise ServiceError(409, f"session '{session['name']}' is already paused")

        event = _record_proctor_event(
            conn, exam_id, "session", "pause", op_id, reason, actor_id, clock,
            session_id=session_id,
        )
        conn.execute(
            "UPDATE exam_sessions SET paused = 1 WHERE id = ?", (session_id,)
        )
        now = clock.now()
        # Settle any attempt that already expired before this pause: a late
        # suspension must never extend a deadline or move the cutoff instant.
        attempts = conn.execute(
            "SELECT * FROM attempts WHERE session_id = ? AND status = 'in_progress'"
            " AND started_at IS NOT NULL",
            (session_id,),
        ).fetchall()
        for attempt in attempts:
            settled = _settle_if_expired(conn, clock, attempt, now)
            if settled["status"] == "in_progress":
                conn.execute(
                    "INSERT INTO attempt_pause_intervals"
                    " (attempt_id, source, pause_event_id, pause_at) VALUES (?, 'session', ?, ?)",
                    (settled["id"], event["id"], clock.now_iso()),
                )
        return _session_proctor_state(conn, clock, exam_id, session_id, event, now, idempotent=False)


def resume_session(
    db: Database, exam_id: int, session_id: int, op_id: str, reason: str | None,
    actor_id: int, clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        _require_timed_exam(conn, exam_id)
        if replay := _replayed_proctor_event(conn, exam_id, op_id):
            return replay
        session = conn.execute(
            "SELECT * FROM exam_sessions WHERE id = ? AND exam_id = ?",
            (session_id, exam_id),
        ).fetchone()
        if session is None:
            raise ServiceError(404, f"session {session_id} not found on exam {exam_id}")
        if not session["paused"]:
            raise ServiceError(409, f"session '{session['name']}' is not paused")

        event = _record_proctor_event(
            conn, exam_id, "session", "resume", op_id, reason, actor_id, clock,
            session_id=session_id,
        )
        conn.execute(
            "UPDATE exam_sessions SET paused = 0 WHERE id = ?", (session_id,)
        )
        now = clock.now()
        # Close only the session-layer open intervals; candidate-layer ones stay open.
        open_rows = conn.execute(
            "SELECT * FROM attempt_pause_intervals WHERE source = 'session'"
            " AND resume_at IS NULL AND attempt_id IN"
            " (SELECT id FROM attempts WHERE session_id = ?)",
            (session_id,),
        ).fetchall()
        for row in open_rows:
            conn.execute(
                "UPDATE attempt_pause_intervals SET resume_event_id = ?, resume_at = ?"
                " WHERE id = ?",
                (event["id"], clock.now_iso(), row["id"]),
            )
        return _session_proctor_state(conn, clock, exam_id, session_id, event, now, idempotent=False)


def pause_candidate(
    db: Database, exam_id: int, candidate_id: int, op_id: str, reason: str | None,
    actor_id: int, clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        _require_timed_exam(conn, exam_id)
        if replay := _replayed_proctor_event(conn, exam_id, op_id):
            return replay
        roster = _roster_row(conn, exam_id, candidate_id)
        if roster is None:
            raise ServiceError(404, "candidate is not enrolled in this exam")
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()
        if attempt is not None:
            # Settle first: an expired attempt is submitted, never paused, so a
            # late suspension cannot push the cutoff into the future.
            attempt = _settle_if_expired(conn, clock, attempt, clock.now())
        if attempt is not None and attempt["status"] == "submitted":
            raise ServiceError(409, "attempt already submitted; proctor actions no longer apply")
        if attempt is not None and attempt["candidate_paused"]:
            raise ServiceError(409, "candidate attempt is already paused")

        event = _record_proctor_event(
            conn, exam_id, "candidate", "pause", op_id, reason, actor_id, clock,
            candidate_id=candidate_id,
        )
        now = clock.now()
        if attempt is not None:
            conn.execute(
                "UPDATE attempts SET candidate_paused = 1 WHERE id = ?", (attempt["id"],)
            )
            if attempt["started_at"] is not None:
                conn.execute(
                    "INSERT INTO attempt_pause_intervals"
                    " (attempt_id, source, pause_event_id, pause_at)"
                    " VALUES (?, 'candidate', ?, ?)",
                    (attempt["id"], event["id"], clock.now_iso()),
                )
        return _candidate_proctor_state(
            conn, clock, exam_id, candidate_id, event, now, idempotent=False
        )


def resume_candidate(
    db: Database, exam_id: int, candidate_id: int, op_id: str, reason: str | None,
    actor_id: int, clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        _require_timed_exam(conn, exam_id)
        if replay := _replayed_proctor_event(conn, exam_id, op_id):
            return replay
        roster = _roster_row(conn, exam_id, candidate_id)
        if roster is None:
            raise ServiceError(404, "candidate is not enrolled in this exam")
        if not _candidate_paused_from_events(conn, exam_id, candidate_id):
            raise ServiceError(409, "candidate attempt is not paused")

        event = _record_proctor_event(
            conn, exam_id, "candidate", "resume", op_id, reason, actor_id, clock,
            candidate_id=candidate_id,
        )
        now = clock.now()
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()
        if attempt is not None:
            conn.execute(
                "UPDATE attempts SET candidate_paused = 0 WHERE id = ?", (attempt["id"],)
            )
            open_rows = conn.execute(
                "SELECT * FROM attempt_pause_intervals WHERE source = 'candidate'"
                " AND resume_at IS NULL AND attempt_id = ?",
                (attempt["id"],),
            ).fetchall()
            for row in open_rows:
                conn.execute(
                    "UPDATE attempt_pause_intervals SET resume_event_id = ?, resume_at = ?"
                    " WHERE id = ?",
                    (event["id"], clock.now_iso(), row["id"]),
                )
        return _candidate_proctor_state(
            conn, clock, exam_id, candidate_id, event, now, idempotent=False
        )


def _replayed_proctor_event(conn, exam_id: int, op_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM proctor_events WHERE exam_id = ? AND op_id = ?",
        (exam_id, op_id),
    ).fetchone()
    if row is None:
        return None
    event = _row_to_dict(row)
    return {
        "idempotent_replay": True,
        "note": "op_id already processed; replay changes nothing",
        "event": _proctor_event_out(conn, event),
    }


def _session_proctor_state(conn, clock, exam_id, session_id, event, now, *, idempotent):
    session = conn.execute(
        "SELECT * FROM exam_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    candidates = []
    for r in conn.execute(
        "SELECT u.id AS candidate_id, u.username FROM exam_enrollments e"
        " JOIN users u ON u.id = e.candidate_id WHERE e.session_id = ? ORDER BY u.username",
        (session_id,),
    ):
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, r["candidate_id"]),
        ).fetchone()
        entry = {
            "candidate_id": r["candidate_id"],
            "username": r["username"],
            "attempt_status": attempt["status"] if attempt is not None else "not_started",
        }
        if attempt is not None:
            entry["timing"] = _timing_view(conn, clock, attempt, now)
        candidates.append(entry)
    return {
        "idempotent_replay": idempotent,
        "event": _proctor_event_out(conn, event),
        "session": {
            "session_id": session["id"],
            "name": session["name"],
            "duration_seconds": session["duration_seconds"],
            "paused": bool(session["paused"]),
        },
        "candidates": candidates,
    }


def _candidate_proctor_state(conn, clock, exam_id, candidate_id, event, now, *, idempotent):
    roster = _roster_row(conn, exam_id, candidate_id)
    attempt = conn.execute(
        "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
        (exam_id, candidate_id),
    ).fetchone()
    out = {
        "idempotent_replay": idempotent,
        "event": _proctor_event_out(conn, event),
        "session": {
            "session_id": roster["session_id"],
            "name": roster["session_name"],
            "paused": bool(roster["session_paused"]),
        },
        "candidate_paused": _candidate_paused_from_events(conn, exam_id, candidate_id),
        "attempt_status": attempt["status"] if attempt is not None else "not_started",
    }
    if attempt is not None:
        out["timing"] = _timing_view(conn, clock, attempt, now)
    return out


def list_proctor_events(db: Database, exam_id: int) -> dict[str, Any]:
    with db.read() as conn:
        _require_timed_exam(conn, exam_id)
        rows = conn.execute(
            "SELECT * FROM proctor_events WHERE exam_id = ? ORDER BY id", (exam_id,)
        ).fetchall()
        return {"exam_id": exam_id, "events": [_proctor_event_out(conn, _row_to_dict(r)) for r in rows]}


# =========================== Grading ===========================


def _active_scheme(conn, exam_id: int):
    return conn.execute(
        "SELECT * FROM grading_schemes WHERE exam_id = ?"
        " ORDER BY scheme_version DESC LIMIT 1",
        (exam_id,),
    ).fetchone()


def _latest_manual(conn, exam_id: int, candidate_id: int, question_id: int):
    return conn.execute(
        "SELECT * FROM manual_score_actions"
        " WHERE exam_id = ? AND candidate_id = ? AND question_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (exam_id, candidate_id, question_id),
    ).fetchone()


def _effective_appeal_ruling(
    conn, exam_id: int, candidate_id: int, question_id: int
):
    """Permanent changed-score ruling for this exact answer sheet/question."""
    return conn.execute(
        "SELECT r.* FROM appeal_rulings r"
        " JOIN score_appeals a ON a.final_ruling_id = r.id"
        " WHERE a.exam_id = ? AND a.candidate_id = ? AND a.question_id = ?"
        " AND a.status = 'closed_changed' AND r.decision = 'changed'"
        " ORDER BY r.id DESC LIMIT 1",
        (exam_id, candidate_id, question_id),
    ).fetchone()


def _ordinary_manual_blocker(
    conn,
    exam_id: int,
    candidate_id: int,
    question_id: int,
    actor_id: int,
):
    """Return a 409 ServiceError if ordinary manual scoring is forbidden."""
    ruling = _effective_appeal_ruling(
        conn, exam_id, candidate_id, question_id
    )
    if ruling is not None:
        return ServiceError(
            409,
            "question has a final appeal score ruling; ordinary manual scores"
            " and revocations cannot override it",
        )
    assignment = conn.execute(
        "SELECT a.id AS appeal_id FROM score_appeals a"
        " JOIN appeal_assignments aa ON aa.appeal_id = a.id"
        " WHERE a.exam_id = ? AND a.candidate_id = ? AND a.question_id = ?"
        " AND aa.grader_id = ? AND a.status != 'closed_rejected'"
        " AND a.status != 'closed_changed'"
        " LIMIT 1",
        (exam_id, candidate_id, question_id, actor_id),
    ).fetchone()
    if assignment is not None:
        return ServiceError(
            409,
            "assigned appeal reviewers cannot use the ordinary manual-score"
            " entry for this question before the appeal is closed",
        )
    return None


def _auto_score_one(qtype: str, selected: list[str], correct: list[str], max_score: int) -> int:
    # Single and multi use the same all-or-nothing rule: exact set equality.
    if set(selected) == set(correct):
        return max_score
    return 0


def _auto_grade(
    conn,
    exam_id: int,
    candidate_id: int,
    *,
    trigger: str,
    scheme_row=None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a NEW pending result version from the given (or active) scheme.

    Must run inside a write transaction. Effective manual scores are carried
    forward; automatic recomputation never overwrites them.
    """
    attempt = conn.execute(
        "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
        (exam_id, candidate_id),
    ).fetchone()
    if attempt is None:
        raise ServiceError(404, "no attempt for this candidate on this exam")
    if attempt["status"] != "submitted":
        raise ServiceError(409, "attempt has not been submitted yet")

    scheme = scheme_row if scheme_row is not None else _active_scheme(conn, exam_id)
    if created_at is None:
        created_at = attempt["graded_at"] or attempt["submitted_at"]
    answer_key = _loads(scheme["answer_key_json"])
    saved_answers = _loads(attempt["answers_json"])

    next_version_row = conn.execute(
        "SELECT COALESCE(MAX(result_version), 0) + 1 AS v FROM result_versions"
        " WHERE exam_id = ? AND candidate_id = ?",
        (exam_id, candidate_id),
    ).fetchone()
    result_version = next_version_row["v"]

    cur = conn.execute(
        "INSERT INTO result_versions"
        " (exam_id, candidate_id, result_version, status, total_score,"
        "  scheme_id, created_at)"
        " VALUES (?, ?, ?, 'pending', 0, ?, ?)",
        (exam_id, candidate_id, result_version, scheme["id"], created_at),
    )
    result_id = cur.lastrowid

    total = 0
    items = []
    for eq in _get_exam_questions(conn, exam_id):
        qid = eq["question_id"]
        selected = saved_answers.get(str(qid), [])
        correct = answer_key.get(str(qid), [])
        auto = _auto_score_one(eq["qtype"], selected, correct, eq["max_score"])

        manual_action = _latest_manual(conn, exam_id, candidate_id, qid)
        manual_action_id = None
        manual_score = None
        manual_reason = None
        if manual_action is not None and manual_action["score"] is not None:
            manual_action_id = manual_action["id"]
            manual_score = manual_action["score"]
            manual_reason = manual_action["reason"]

        appeal_ruling = _effective_appeal_ruling(
            conn, exam_id, candidate_id, qid
        )
        appeal_ruling_id = None
        if manual_score is None:
            effective = auto
            source_type = "auto"
            source_id = None
        else:
            effective = manual_score
            source_type = "manual"
            source_id = manual_action_id
        if appeal_ruling is not None:
            # A changed appeal ruling is permanent: regrades still apply it,
            # and ordinary manual actions are rejected before insertion.
            appeal_ruling_id = appeal_ruling["id"]
            effective = appeal_ruling["final_score"]
            source_type = "appeal_ruling"
            source_id = appeal_ruling["id"]
        total += effective

        conn.execute(
            "INSERT INTO result_items"
            " (result_version_id, question_id, auto_score, manual_action_id,"
            "  manual_score, manual_reason, effective_score,"
            "  appeal_ruling_id, score_source_type, score_source_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result_id,
                qid,
                auto,
                manual_action_id,
                manual_score,
                manual_reason,
                effective,
                appeal_ruling_id,
                source_type,
                source_id,
            ),
        )
        items.append(
            {
                "question_id": qid,
                "auto_score": auto,
                "manual_action_id": manual_action_id,
                "manual_score": manual_score,
                "manual_reason": manual_reason,
                "effective_score": effective,
                "appeal_ruling_id": appeal_ruling_id,
                "score_source_type": source_type,
                "score_source_id": source_id,
            }
        )

    conn.execute(
        "UPDATE result_versions SET total_score = ? WHERE id = ?", (total, result_id)
    )
    conn.execute(
        "UPDATE attempts SET graded_at = ? WHERE id = ?", (created_at, attempt["id"])
    )
    conn.execute(
        "INSERT INTO grading_runs"
        " (exam_id, candidate_id, scheme_id, trigger, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (exam_id, candidate_id, scheme["id"], trigger, created_at),
    )
    return {
        "result_version": result_version,
        "status": "pending",
        "total_score": total,
        "scheme_version": scheme["scheme_version"],
        "items": items,
        "created_at": created_at,
    }


def regrade(
    db: Database,
    exam_id: int,
    candidate_id: int,
    trigger: str = "manual regrade",
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        if conn.execute("SELECT 1 FROM exams WHERE id = ?", (exam_id,)).fetchone() is None:
            raise ServiceError(404, f"exam {exam_id} not found")
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()
        if attempt is not None:
            attempt = _settle_if_expired(conn, clock, attempt, clock.now())
        return _auto_grade(conn, exam_id, candidate_id, trigger=trigger)


def edit_answer_key(
    db: Database,
    exam_id: int,
    answer_key_items: list[dict],
    reason: str,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    """Create a new grading scheme. Exam content and max scores never change."""
    with db.write() as conn:
        exam = conn.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
        if exam is None:
            raise ServiceError(404, f"exam {exam_id} not found")
        eq_rows = _get_exam_questions(conn, exam_id)
        eq_by_qid = {r["question_id"]: r for r in eq_rows}

        incoming = {item["question_id"]: item["correct_option_ids"] for item in answer_key_items}
        if set(incoming) != set(eq_by_qid):
            raise ServiceError(
                422,
                "answer key must cover exactly the exam's questions: "
                f"{sorted(eq_by_qid)}",
            )
        new_key: dict[str, list[str]] = {}
        for qid, correct_ids in incoming.items():
            valid_options = {
                o["option_id"] for o in _loads(eq_by_qid[qid]["options_json"])
            }
            unknown = set(correct_ids) - valid_options
            if unknown:
                raise ServiceError(
                    422, f"unknown option ids {sorted(unknown)} for question {qid}"
                )
            if len(correct_ids) != len(set(correct_ids)):
                raise ServiceError(422, f"duplicate correct ids for question {qid}")
            new_key[str(qid)] = sorted(correct_ids)

        current = _active_scheme(conn, exam_id)
        scheme_id = _insert_scheme(
            conn,
            exam_id,
            current["scheme_version"] + 1,
            new_key,
            f"answer key correction: {reason}",
            clock,
        )
        scheme = conn.execute(
            "SELECT * FROM grading_schemes WHERE id = ?", (scheme_id,)
        ).fetchone()
        return {
            "exam_id": exam_id,
            "scheme_version": scheme["scheme_version"],
            "answer_key": _loads(scheme["answer_key_json"]),
            "source": scheme["source"],
            "created_at": scheme["created_at"],
            "note": "existing pending/published results are untouched until regraded",
        }


def set_manual_score(
    db: Database,
    exam_id: int,
    candidate_id: int,
    question_id: int,
    score: Optional[int],
    reason: Optional[str],
    grader_id: int,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    """Apply (score >= 0) or explicitly revoke (score is None) a manual score.

    A new pending result version is produced each time; the score the
    candidate has already seen does not change until that version is
    published.
    """
    with db.write() as conn:
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()
        if attempt is None:
            raise ServiceError(404, "no attempt for this candidate on this exam")
        attempt = _settle_if_expired(conn, clock, attempt, clock.now())
        eq = conn.execute(
            "SELECT * FROM exam_questions WHERE exam_id = ? AND question_id = ?",
            (exam_id, question_id),
        ).fetchone()
        if eq is None:
            raise ServiceError(422, f"question {question_id} is not on exam {exam_id}")
        blocker = _ordinary_manual_blocker(
            conn, exam_id, candidate_id, question_id, grader_id
        )
        if blocker is not None:
            raise blocker
        if score is not None and score > eq["max_score"]:
            raise ServiceError(
                422, f"manual score {score} exceeds question max_score {eq['max_score']}"
            )
        if score is not None and not reason:
            raise ServiceError(422, "a reason is required when setting a manual score")

        conn.execute(
            "INSERT INTO manual_score_actions"
            " (exam_id, candidate_id, question_id, score, reason, grader_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (exam_id, candidate_id, question_id, score, reason, grader_id, clock.now_iso()),
        )
        result = _auto_grade(
            conn,
            exam_id,
            candidate_id,
            trigger="manual score revoke" if score is None else "manual score set",
        )
        result["manual_action"] = {
            "question_id": question_id,
            "score": score,
            "reason": reason,
            "revoked": score is None,
        }
        return result


# =========================== Results & publishing ===========================


def list_result_versions(
    db: Database, exam_id: int, candidate_id: int, clock: Clock = _REAL_CLOCK
) -> list[dict[str, Any]]:
    _settle_candidate(db, exam_id, candidate_id, clock)
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM result_versions WHERE exam_id = ? AND candidate_id = ?"
            " ORDER BY result_version",
            (exam_id, candidate_id),
        ).fetchall()
        return [_result_summary(conn, r) for r in rows]


def _result_summary(conn, rv_row) -> dict[str, Any]:
    items = conn.execute(
        "SELECT ri.question_id, ri.auto_score, ri.manual_action_id, ri.manual_score,"
        " ri.manual_reason, ri.effective_score, ri.appeal_ruling_id,"
        " ri.score_source_type, ri.score_source_id, ar.appeal_id"
        " FROM result_items ri"
        " LEFT JOIN appeal_rulings ar ON ar.id = ri.appeal_ruling_id"
        " WHERE ri.result_version_id = ? ORDER BY ri.question_id",
        (rv_row["id"],),
    ).fetchall()
    scheme = conn.execute(
        "SELECT scheme_version, source FROM grading_schemes WHERE id = ?",
        (rv_row["scheme_id"],),
    ).fetchone()
    return {
        "result_version": rv_row["result_version"],
        "status": rv_row["status"],
        "total_score": rv_row["total_score"],
        "scheme_version": scheme["scheme_version"],
        "scheme_source": scheme["source"],
        "created_at": rv_row["created_at"],
        "published_at": rv_row["published_at"],
        "items": [
            {
                **_row_to_dict(i),
                "score_source": i["score_source_type"],
            }
            for i in items
        ],
    }


def publish_result(
    db: Database,
    exam_id: int,
    candidate_id: int,
    result_version: int,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()
        if attempt is not None:
            _settle_if_expired(conn, clock, attempt, clock.now())
        target = conn.execute(
            "SELECT * FROM result_versions WHERE exam_id = ? AND candidate_id = ?"
            " AND result_version = ?",
            (exam_id, candidate_id, result_version),
        ).fetchone()
        if target is None:
            raise ServiceError(404, f"result version {result_version} not found")

        latest = conn.execute(
            "SELECT MAX(result_version) AS v FROM result_versions"
            " WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()["v"]
        if result_version != latest:
            # A newer grading action exists: publishing the stale candidate
            # must conflict rather than silently swallow the newer action.
            raise ServiceError(
                409,
                f"refusing to publish stale result version {result_version};"
                f" newest grading action produced version {latest}",
            )
        if target["status"] == "published":
            return _result_summary(
                conn,
                conn.execute(
                    "SELECT * FROM result_versions WHERE id = ?", (target["id"],)
                ).fetchone(),
            ) | {"idempotent_replay": True}

        conn.execute(
            "UPDATE result_versions SET status = 'superseded'"
            " WHERE exam_id = ? AND candidate_id = ? AND status = 'published'",
            (exam_id, candidate_id),
        )
        published_at = clock.now_iso()
        conn.execute(
            "UPDATE result_versions SET status = 'published', published_at = ?"
            " WHERE id = ?",
            (published_at, target["id"]),
        )
        _mark_published_appeal_corrections(conn, target["id"], published_at)
        # A new published total may void pending makeup waitlists/invitations
        # that rely on this exam as their eligibility basis (same transaction).
        _makeup_on_result_published(conn, exam_id, candidate_id, clock)
        return _result_summary(
            conn,
            conn.execute(
                "SELECT * FROM result_versions WHERE id = ?", (target["id"],)
            ).fetchone(),
        )


def get_published_result(
    db: Database, exam_id: int, candidate_id: int, clock: Clock = _REAL_CLOCK
) -> dict[str, Any]:
    """Candidate-facing: only a published result is visible."""
    _settle_candidate(db, exam_id, candidate_id, clock)
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM result_versions WHERE exam_id = ? AND candidate_id = ?"
            " AND status = 'published'",
            (exam_id, candidate_id),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "no published result for this attempt yet")
        return _result_summary(conn, row)


def get_grading_history(
    db: Database,
    exam_id: int,
    candidate_id: int,
    clock: Clock = _REAL_CLOCK,
    viewer_id: int | None = None,
    viewer_role: str = "admin",
) -> dict[str, Any]:
    _settle_candidate(db, exam_id, candidate_id, clock)
    with db.read() as conn:
        runs = conn.execute(
            "SELECT scheme_id, trigger, created_at FROM grading_runs"
            " WHERE exam_id = ? AND candidate_id = ? ORDER BY id",
            (exam_id, candidate_id),
        ).fetchall()
        actions = conn.execute(
            "SELECT m.question_id, m.score, m.reason, u.username AS grader, m.created_at"
            " FROM manual_score_actions m JOIN users u ON u.id = m.grader_id"
            " WHERE m.exam_id = ? AND m.candidate_id = ? ORDER BY m.id",
            (exam_id, candidate_id),
        ).fetchall()
        schemes = conn.execute(
            "SELECT scheme_version, answer_key_json, source, created_at"
            " FROM grading_schemes WHERE exam_id = ? ORDER BY scheme_version",
            (exam_id,),
        ).fetchall()
        appeal_rows = conn.execute(
            "SELECT * FROM score_appeals WHERE exam_id = ? AND candidate_id = ?"
            " ORDER BY id",
            (exam_id, candidate_id),
        ).fetchall()
        appeals = [
            _appeal_view(
                conn,
                row,
                viewer_id=viewer_id,
                viewer_role=viewer_role,
            )
            for row in appeal_rows
        ]
        return {
            "grading_runs": [_row_to_dict(r) for r in runs],
            "manual_actions": [_row_to_dict(a) for a in actions],
            "appeals": appeals,
            "grading_schemes": [
                {
                    "scheme_version": s["scheme_version"],
                    "answer_key": _loads(s["answer_key_json"]),
                    "source": s["source"],
                    "created_at": s["created_at"],
                }
                for s in schemes
            ],
        }


# =========================== Score appeals ===========================


def _idempotency_record(
    conn,
    scope: str,
    op_id: str | None,
    request_payload: dict[str, Any],
    actor_id: int,
    clock: Clock,
    *,
    appeal_id: int | None = None,
):
    """Return an existing operation row, or insert this new operation.

    A reused ``(scope, op_id)`` with a different canonical request is a 409.
    The global write lock / SQLite write transaction makes check-then-insert
    atomic for both threaded and restarted callers.
    """
    if not op_id or not str(op_id).strip():
        raise ServiceError(422, "op_id is required for retry-safe operations")
    op_id = str(op_id)
    canonical = _dumps({"actor_id": actor_id, "request": request_payload})
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    global_row = conn.execute(
        "SELECT * FROM appeal_operations WHERE op_id = ?", (op_id,)
    ).fetchone()
    if global_row is not None:
        if global_row["scope"] != scope or global_row["request_hash"] != request_hash:
            raise ServiceError(
                409,
                f"op_id '{op_id}' was already used for a different request;"
                " use a new op_id for a new operation",
            )
        return global_row

    row = conn.execute(
        "SELECT * FROM appeal_operations WHERE scope = ? AND op_id = ?",
        (scope, op_id),
    ).fetchone()
    if row is not None:
        if row["request_hash"] != request_hash:
            raise ServiceError(
                409,
                f"op_id '{op_id}' was already used for a different request;"
                " use a new op_id for a new operation",
            )
        return row

    conn.execute(
        "INSERT INTO appeal_operations"
        " (scope, op_id, request_hash, appeal_id, actor_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (scope, op_id, request_hash, appeal_id, actor_id, clock.now_iso()),
    )
    return None


def _get_appeal(conn, appeal_id: int):
    row = conn.execute(
        "SELECT * FROM score_appeals WHERE id = ?", (appeal_id,)
    ).fetchone()
    if row is None:
        raise ServiceError(404, f"appeal {appeal_id} not found")
    return row


def _appeal_status_label(row) -> str:
    status = row["status"]
    if status == "pending_assignment":
        return "待管理员指定复核阅卷员"
    if status == "in_review":
        return "阅卷员复核中"
    if status == "pending_arbitration":
        return "复核意见分歧，待管理员仲裁"
    if status == "closed_rejected":
        return "已结案：驳回，成绩不变"
    if status == "closed_changed":
        if row["correction_published_at"]:
            return "已结案：改分已发布"
        return "已结案：改分待发布"
    return status


def _appeal_view(
    conn,
    row,
    *,
    viewer_id: int | None = None,
    viewer_role: str | None = None,
) -> dict[str, Any]:
    a = _row_to_dict(row)
    appeal_id = a["id"]
    candidate = conn.execute(
        "SELECT id, username, display_name FROM users WHERE id = ?",
        (a["candidate_id"],),
    ).fetchone()
    exam = conn.execute(
        "SELECT id, title FROM exams WHERE id = ?", (a["exam_id"],)
    ).fetchone()

    assignments = [
        _row_to_dict(r)
        for r in conn.execute(
            "SELECT aa.id, aa.slot, aa.grader_id, aa.created_at,"
            " u.username AS grader_username, u.display_name AS grader_name"
            " FROM appeal_assignments aa JOIN users u ON u.id = aa.grader_id"
            " WHERE aa.appeal_id = ? ORDER BY aa.slot",
            (appeal_id,),
        ).fetchall()
    ]
    all_opinions = [
        _row_to_dict(r)
        for r in conn.execute(
            "SELECT o.id, o.grader_id, o.decision, o.new_score, o.reason,"
            " o.created_at, u.username AS grader_username,"
            " u.display_name AS grader_name"
            " FROM appeal_opinions o JOIN users u ON u.id = o.grader_id"
            " WHERE o.appeal_id = ? ORDER BY o.id",
            (appeal_id,),
        ).fetchall()
    ]
    is_assigned = any(x["grader_id"] == viewer_id for x in assignments)
    opinions_complete = len(all_opinions) >= 2

    if viewer_role == "candidate" or viewer_role is None:
        visible_opinions: list[dict[str, Any]] = []
    elif viewer_role == "admin":
        visible_opinions = all_opinions
    elif viewer_role == "grader":
        if opinions_complete:
            visible_opinions = all_opinions
        elif is_assigned:
            visible_opinions = [
                o for o in all_opinions if o["grader_id"] == viewer_id
            ]
        else:
            # Unassigned graders have no need to read sealed opinions while
            # the two assigned reviewers are still working.
            visible_opinions = []
    else:
        visible_opinions = []

    own_opinion = next(
        (o for o in all_opinions if o["grader_id"] == viewer_id), None
    )
    ruling = None
    if a["final_ruling_id"] is not None:
        rr = conn.execute(
            "SELECT r.*, u.username AS decider_username"
            " FROM appeal_rulings r LEFT JOIN users u ON u.id = r.decider_id"
            " WHERE r.id = ?",
            (a["final_ruling_id"],),
        ).fetchone()
        if rr is not None:
            ruling = _row_to_dict(rr)

    basis = _loads(a["basis_json"])
    q_snapshot = basis.get("question", {})
    public_question = {
        k: q_snapshot.get(k)
        for k in (
            "question_id",
            "version",
            "qtype",
            "stem",
            "options",
            "option_order",
            "max_score",
        )
        if k in q_snapshot
    }

    correction: dict[str, Any] = {"status": "not_required"}
    if a["final_decision"] == "changed" and ruling is not None:
        published = conn.execute(
            "SELECT rv.result_version, rv.published_at"
            " FROM result_items ri"
            " JOIN result_versions rv ON rv.id = ri.result_version_id"
            " WHERE ri.appeal_ruling_id = ? AND rv.status = 'published'"
            " ORDER BY rv.result_version DESC LIMIT 1",
            (ruling["id"],),
        ).fetchone()
        if published is not None:
            correction = {
                "status": "published",
                "result_version": a["correction_result_version"],
                "generated_result_version": a["correction_result_version"],
                "published_result_version": published["result_version"],
                "published_at": published["published_at"],
            }
        else:
            correction = {
                "status": "pending_publication",
                "result_version": a["correction_result_version"],
                "generated_result_version": a["correction_result_version"],
                "published_result_version": None,
                "published_at": None,
            }

    coarse_phase = {
        "pending_assignment": "pending",
        "in_review": "reviewing",
        "pending_arbitration": "arbitration",
        "closed_rejected": "closed",
        "closed_changed": "closed",
    }[a["status"]]

    out: dict[str, Any] = {
        "appeal_id": appeal_id,
        "id": appeal_id,
        "exam_id": a["exam_id"],
        "exam_title": exam["title"] if exam is not None else None,
        "candidate_id": a["candidate_id"],
        "candidate": _row_to_dict(candidate) if candidate is not None else None,
        "question_id": a["question_id"],
        "question": public_question,
        "result_version": a["result_version"],
        "appealed_result_version": a["result_version"],
        "appealed_score": a["appealed_score"],
        "reason": a["reason"],
        "status": a["status"],
        "state": a["status"],
        "stage": a["status"],
        "phase": coarse_phase,
        "is_closed": a["status"].startswith("closed_"),
        "status_label": _appeal_status_label(a),
        "created_at": a["created_at"],
        "assigned_at": a["assigned_at"],
        "closed_at": a["closed_at"],
        "assignments": assignments,
        "opinions": visible_opinions,
        "own_opinion": own_opinion
        if viewer_role in ("grader", "admin") and own_opinion is not None
        else None,
        "opinions_received": len(all_opinions),
        "opinions_complete": opinions_complete,
        "is_assigned_reviewer": is_assigned,
        "ruling": ruling if viewer_role != "candidate" else {
            # Candidates receive the formal conclusion only, never reviewer
            # opinion rows or internal basis data.
            "kind": ruling["kind"] if ruling else None,
            "decision": a["final_decision"],
            "final_score": a["final_score"],
            "reason": a["final_reason"],
            "created_at": ruling["created_at"] if ruling else a["closed_at"],
        }
        if a["final_decision"] is not None
        else None,
        "conclusion": {
            "decision": a["final_decision"],
            "final_score": a["final_score"],
            "reason": a["final_reason"],
            "closure_kind": a["closure_kind"],
            "closed_at": a["closed_at"],
        }
        if a["final_decision"] is not None
        else None,
        "closure": {
            "decision": a["final_decision"],
            "final_score": a["final_score"],
            "reason": a["final_reason"],
            "kind": a["closure_kind"],
            "closed_at": a["closed_at"],
        }
        if a["final_decision"] is not None
        else None,
        "correction": correction,
        "correction_status": correction["status"],
        "correction_result_version": a["correction_result_version"],
        "correction_published_at": a["correction_published_at"],
        "final_decision": a["final_decision"],
        "final_score": a["final_score"],
    }
    out["resolution"] = out["conclusion"]
    out["score_correction"] = out["correction"]
    out["score_change_status"] = out["correction_status"]

    # Staff trace view gets the frozen full basis. Candidate output deliberately
    # does not include it because it contains staff-only answer-key provenance.
    if viewer_role in ("admin", "grader"):
        out["basis"] = basis
        out["arbitrator_id"] = a["arbitrator_id"]
        if viewer_role == "admin":
            out["all_opinions"] = all_opinions
    return out


def create_appeal(
    db: Database,
    exam_id: int,
    candidate_id: int,
    op_id: str | None,
    result_version: int | None,
    question_id: int,
    reason: str,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    reason = (reason or "").strip()
    if not reason:
        raise ServiceError(422, "appeal reason is required")
    with db.write() as conn:
        if conn.execute(
            "SELECT 1 FROM exams WHERE id = ?", (exam_id,)
        ).fetchone() is None:
            raise ServiceError(404, f"exam {exam_id} not found")

        if result_version is None:
            current_published = conn.execute(
                "SELECT result_version FROM result_versions"
                " WHERE exam_id = ? AND candidate_id = ? AND status = 'published'"
                " ORDER BY result_version DESC LIMIT 1",
                (exam_id, candidate_id),
            ).fetchone()
            if current_published is None:
                raise ServiceError(409, "no current published result to appeal yet")
            result_version = current_published["result_version"]

        scope = f"appeal-open:{exam_id}:{candidate_id}"
        idem = _idempotency_record(
            conn,
            scope,
            op_id,
            {
                "path": f"/exams/{exam_id}/appeals",
                "result_version": result_version,
                "question_id": question_id,
                "reason": reason,
            },
            candidate_id,
            clock,
        )
        if idem is not None:
            row = _get_appeal(conn, idem["appeal_id"])
            view = _appeal_view(
                conn, row, viewer_id=candidate_id, viewer_role="candidate"
            )
            view["idempotent_replay"] = True
            view["note"] = "op_id already processed; appeal was not opened twice"
            return view

        published = conn.execute(
            "SELECT * FROM result_versions"
            " WHERE exam_id = ? AND candidate_id = ? AND result_version = ?"
            " AND status = 'published'",
            (exam_id, candidate_id, result_version),
        ).fetchone()
        if published is None:
            # Tolerate callers that name the result row primary key in an
            # optional *_id field rather than the candidate-visible version.
            published = conn.execute(
                "SELECT * FROM result_versions"
                " WHERE exam_id = ? AND candidate_id = ? AND id = ?"
                " AND status = 'published'",
                (exam_id, candidate_id, result_version),
            ).fetchone()
        if published is None:
            any_version = conn.execute(
                "SELECT status FROM result_versions"
                " WHERE exam_id = ? AND candidate_id = ? AND result_version = ?",
                (exam_id, candidate_id, result_version),
            ).fetchone()
            if any_version is None:
                raise ServiceError(
                    404,
                    f"published result version {result_version} not found",
                )
            raise ServiceError(
                409,
                "only the candidate's current published result may be appealed;"
                f" version {result_version} has status {any_version['status']}",
            )
        result_version = published["result_version"]

        item = conn.execute(
            "SELECT * FROM result_items"
            " WHERE result_version_id = ? AND question_id = ?",
            (published["id"], question_id),
        ).fetchone()
        if item is None:
            raise ServiceError(
                422,
                f"question {question_id} is not in result version {result_version}",
            )
        eq = conn.execute(
            "SELECT * FROM exam_questions WHERE exam_id = ? AND question_id = ?",
            (exam_id, question_id),
        ).fetchone()
        if eq is None:
            raise ServiceError(
                422, f"question {question_id} is not on exam {exam_id}"
            )
        attempt = conn.execute(
            "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()
        if attempt is None or attempt["status"] != "submitted":
            raise ServiceError(409, "a submitted answer sheet is required")

        duplicate = conn.execute(
            "SELECT id FROM score_appeals"
            " WHERE exam_id = ? AND candidate_id = ? AND question_id = ?",
            (exam_id, candidate_id, question_id),
        ).fetchone()
        if duplicate is not None:
            raise ServiceError(
                409,
                "this question on this answer sheet has already been appealed"
                f" as appeal {duplicate['id']}; only one appeal is accepted",
            )

        scheme = conn.execute(
            "SELECT * FROM grading_schemes WHERE id = ?",
            (published["scheme_id"],),
        ).fetchone()
        answers = _loads(attempt["answers_json"])
        q_key = str(question_id)
        question_snapshot = _row_to_dict(eq)
        question_snapshot["options"] = _loads(eq["options_json"])
        question_snapshot["option_order"] = _loads(eq["option_order_json"])
        basis = {
            "appealed_at": clock.now_iso(),
            "result_version": result_version,
            "result_version_id": published["id"],
            "result_item": _row_to_dict(item),
            "question": question_snapshot,
            "attempt_id": attempt["id"],
            "attempt_revision": attempt["revision"],
            "submitted_at": attempt["submitted_at"],
            "submitted_answers": answers,
            "selected_answer": answers.get(q_key, []),
            "scheme": {
                "id": scheme["id"],
                "scheme_version": scheme["scheme_version"],
                "answer_key": _loads(scheme["answer_key_json"]),
                "source": scheme["source"],
            }
            if scheme is not None
            else None,
        }

        cur = conn.execute(
            "INSERT INTO score_appeals"
            " (exam_id, candidate_id, attempt_id, result_version_id,"
            "  result_version, question_id, appealed_score, reason, basis_json,"
            "  status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_assignment', ?)",
            (
                exam_id,
                candidate_id,
                attempt["id"],
                published["id"],
                result_version,
                question_id,
                item["effective_score"],
                reason,
                _dumps(basis),
                clock.now_iso(),
            ),
        )
        appeal_id = cur.lastrowid
        conn.execute(
            "UPDATE appeal_operations SET appeal_id = ?"
            " WHERE scope = ? AND op_id = ?",
            (
                appeal_id,
                scope,
                op_id,
            ),
        )
        row = _get_appeal(conn, appeal_id)
        return _appeal_view(
            conn, row, viewer_id=candidate_id, viewer_role="candidate"
        )


def get_appeal_for_candidate(
    db: Database, exam_id: int, appeal_id: int, candidate_id: int
):
    with db.read() as conn:
        row = _get_appeal(conn, appeal_id)
        if row["exam_id"] != exam_id or row["candidate_id"] != candidate_id:
            # Do not reveal whether an id belonging to another candidate exists.
            raise ServiceError(404, f"appeal {appeal_id} not found")
        return _appeal_view(
            conn, row, viewer_id=candidate_id, viewer_role="candidate"
        )


def get_my_appeal(db: Database, appeal_id: int, candidate_id: int):
    with db.read() as conn:
        row = _get_appeal(conn, appeal_id)
        if row["candidate_id"] != candidate_id:
            raise ServiceError(404, f"appeal {appeal_id} not found")
        return _appeal_view(
            conn, row, viewer_id=candidate_id, viewer_role="candidate"
        )


def list_candidate_appeals(
    db: Database, exam_id: int | None, candidate_id: int
) -> dict[str, Any]:
    with db.read() as conn:
        if exam_id is None:
            rows = conn.execute(
                "SELECT * FROM score_appeals WHERE candidate_id = ?"
                " ORDER BY id DESC",
                (candidate_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM score_appeals WHERE candidate_id = ? AND exam_id = ?"
                " ORDER BY id DESC",
                (candidate_id, exam_id),
            ).fetchall()
        return {
            "appeals": [
                _appeal_view(
                    conn, r, viewer_id=candidate_id, viewer_role="candidate"
                )
                for r in rows
            ]
        }


def get_appeal_for_staff(
    db: Database, appeal_id: int, user_id: int, role: str
) -> dict[str, Any]:
    with db.read() as conn:
        row = _get_appeal(conn, appeal_id)
        return _appeal_view(
            conn, row, viewer_id=user_id, viewer_role=role
        )


def list_staff_appeals(
    db: Database,
    viewer_id: int,
    viewer_role: str,
    *,
    exam_id: int | None = None,
    candidate_id: int | None = None,
    status: str | None = None,
    assigned_to_me: bool = False,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if exam_id is not None:
        clauses.append("a.exam_id = ?")
        params.append(exam_id)
    if candidate_id is not None:
        clauses.append("a.candidate_id = ?")
        params.append(candidate_id)
    if status is not None:
        clauses.append("a.status = ?")
        params.append(status)
    if assigned_to_me:
        clauses.append(
            "EXISTS (SELECT 1 FROM appeal_assignments aa"
            " WHERE aa.appeal_id = a.id AND aa.grader_id = ?)"
        )
        params.append(viewer_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.read() as conn:
        rows = conn.execute(
            f"SELECT a.* FROM score_appeals a{where} ORDER BY a.id DESC",
            params,
        ).fetchall()
        return {
            "appeals": [
                _appeal_view(
                    conn, r, viewer_id=viewer_id, viewer_role=viewer_role
                )
                for r in rows
            ]
        }


def _resolve_grader_refs(
    conn, refs: list[Any], ids: list[int] | None = None
) -> list[int]:
    resolved: list[int] = []
    for gid in ids or []:
        row = conn.execute(
            "SELECT id, role FROM users WHERE id = ?", (gid,)
        ).fetchone()
        if row is None:
            raise ServiceError(422, f"grader user id {gid} not found")
        if row["role"] != "grader":
            raise ServiceError(422, f"user id {gid} is not a grader")
        resolved.append(row["id"])

    for ref in refs or []:
        if isinstance(ref, int):
            row = conn.execute(
                "SELECT id, role FROM users WHERE id = ?", (ref,)
            ).fetchone()
        else:
            text = str(ref).strip()
            row = conn.execute(
                "SELECT id, role FROM users WHERE username = ?", (text,)
            ).fetchone()
            if row is None and text.isdigit():
                row = conn.execute(
                    "SELECT id, role FROM users WHERE id = ?", (int(text),)
                ).fetchone()
        if row is None:
            raise ServiceError(422, f"grader '{ref}' not found")
        if row["role"] != "grader":
            raise ServiceError(422, f"user '{ref}' is not a grader")
        resolved.append(row["id"])

    if len(resolved) != 2:
        raise ServiceError(
            422,
            "exactly two different graders must be assigned in one assignment"
            f" action; received {len(resolved)}",
        )
    if len(set(resolved)) != 2:
        raise ServiceError(422, "the two assigned graders must be different users")
    return resolved


def _manual_conflict_user(conn, appeal, grader_id: int):
    return conn.execute(
        "SELECT m.*, u.username FROM manual_score_actions m"
        " JOIN users u ON u.id = m.grader_id"
        " WHERE m.exam_id = ? AND m.candidate_id = ? AND m.question_id = ?"
        " AND m.grader_id = ? LIMIT 1",
        (
            appeal["exam_id"],
            appeal["candidate_id"],
            appeal["question_id"],
            grader_id,
        ),
    ).fetchone()


def assign_appeal(
    db: Database,
    appeal_id: int,
    op_id: str | None,
    grader_refs: list[Any] | None,
    grader_ids: list[int] | None,
    admin_id: int,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    with db.write() as conn:
        appeal = _get_appeal(conn, appeal_id)
        scope = f"appeal-assign:{appeal_id}"
        refs = list(grader_refs or [])
        ids = list(grader_ids or [])
        idem = _idempotency_record(
            conn,
            scope,
            op_id,
            {"grader_refs": refs, "grader_ids": ids},
            admin_id,
            clock,
            appeal_id=appeal_id,
        )
        if idem is not None:
            view = _appeal_view(
                conn, appeal, viewer_id=admin_id, viewer_role="admin"
            )
            view["idempotent_replay"] = True
            return view

        if appeal["status"] != "pending_assignment":
            raise ServiceError(
                409,
                f"appeal {appeal_id} is already {appeal['status']};"
                " graders can only be assigned once before review starts",
            )
        grader_ids_resolved = _resolve_grader_refs(conn, refs, ids)
        for gid in grader_ids_resolved:
            conflict = _manual_conflict_user(conn, appeal, gid)
            if conflict is not None:
                raise ServiceError(
                    409,
                    f"grader '{conflict['username']}' previously gave an ordinary"
                    " manual score or revoked a score for this candidate and"
                    " question, and cannot review or arbitrate this appeal",
                )

        now = clock.now_iso()
        for slot, gid in enumerate(grader_ids_resolved, start=1):
            conn.execute(
                "INSERT INTO appeal_assignments"
                " (appeal_id, grader_id, slot, assigned_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (appeal_id, gid, slot, admin_id, now),
            )
        conn.execute(
            "UPDATE score_appeals SET status = 'in_review', assigned_at = ?"
            " WHERE id = ?",
            (now, appeal_id),
        )
        fresh = _get_appeal(conn, appeal_id)
        return _appeal_view(
            conn, fresh, viewer_id=admin_id, viewer_role="admin"
        )


def _validate_decision_score(
    conn, appeal, decision: str, new_score: int | None
) -> int | None:
    if decision == "rejected":
        return None
    if new_score is None:
        raise ServiceError(
            422, "a changed-score conclusion requires an integer new_score"
        )
    try:
        score = int(new_score)
    except (TypeError, ValueError):
        raise ServiceError(422, "new_score must be an integer")
    basis = _loads(appeal["basis_json"])
    max_score = basis.get("question", {}).get("max_score")
    if max_score is None:
        eq = conn.execute(
            "SELECT max_score FROM exam_questions"
            " WHERE exam_id = ? AND question_id = ?",
            (appeal["exam_id"], appeal["question_id"]),
        ).fetchone()
        max_score = eq["max_score"] if eq is not None else None
    if max_score is None or score < 0 or score > max_score:
        raise ServiceError(
            422,
            f"new_score must be an integer in [0, {max_score}] for the original question",
        )
    if score == appeal["appealed_score"]:
        raise ServiceError(
            422,
            f"new_score {score} must differ from the appealed score"
            f" {appeal['appealed_score']}",
        )
    return score


def _create_appeal_correction_version(
    conn, appeal, ruling_id: int, final_score: int, now_iso: str
) -> Any:
    existing_vid = appeal["correction_result_version_id"]
    if existing_vid is not None:
        return conn.execute(
            "SELECT * FROM result_versions WHERE id = ?", (existing_vid,)
        ).fetchone()

    latest = conn.execute(
        "SELECT * FROM result_versions"
        " WHERE exam_id = ? AND candidate_id = ?"
        " ORDER BY result_version DESC LIMIT 1",
        (appeal["exam_id"], appeal["candidate_id"]),
    ).fetchone()
    if latest is None:
        raise ServiceError(409, "cannot create correction without a result version")

    next_v = conn.execute(
        "SELECT COALESCE(MAX(result_version), 0) + 1 AS v FROM result_versions"
        " WHERE exam_id = ? AND candidate_id = ?",
        (appeal["exam_id"], appeal["candidate_id"]),
    ).fetchone()["v"]
    cur = conn.execute(
        "INSERT INTO result_versions"
        " (exam_id, candidate_id, result_version, status, total_score,"
        "  scheme_id, created_at)"
        " VALUES (?, ?, ?, 'pending', 0, ?, ?)",
        (
            appeal["exam_id"],
            appeal["candidate_id"],
            next_v,
            latest["scheme_id"],  # Errata not yet regraded do not take effect.
            now_iso,
        ),
    )
    new_id = cur.lastrowid

    old_items = conn.execute(
        "SELECT * FROM result_items WHERE result_version_id = ? ORDER BY id",
        (latest["id"],),
    ).fetchall()
    total = 0
    target_found = False
    for old in old_items:
        qid = old["question_id"]
        effective = old["effective_score"]
        appeal_ruling_id = old["appeal_ruling_id"]
        source_type = old["score_source_type"] or "auto"
        source_id = old["score_source_id"]
        if qid == appeal["question_id"]:
            target_found = True
            effective = final_score
            appeal_ruling_id = ruling_id
            source_type = "appeal_ruling"
            source_id = ruling_id
        total += effective
        conn.execute(
            "INSERT INTO result_items"
            " (result_version_id, question_id, auto_score, manual_action_id,"
            "  manual_score, manual_reason, effective_score,"
            "  appeal_ruling_id, score_source_type, score_source_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id,
                qid,
                old["auto_score"],
                old["manual_action_id"],
                old["manual_score"],
                old["manual_reason"],
                effective,
                appeal_ruling_id,
                source_type,
                source_id,
            ),
        )
    if not target_found:
        raise ServiceError(
            409, "latest result version does not contain the appealed question"
        )
    conn.execute(
        "UPDATE result_versions SET total_score = ? WHERE id = ?",
        (total, new_id),
    )
    conn.execute(
        "INSERT INTO grading_runs"
        " (exam_id, candidate_id, scheme_id, trigger, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            appeal["exam_id"],
            appeal["candidate_id"],
            latest["scheme_id"],
            f"appeal ruling {ruling_id}: replace question"
            f" {appeal['question_id']} score with {final_score}",
            now_iso,
        ),
    )
    return conn.execute(
        "SELECT * FROM result_versions WHERE id = ?", (new_id,)
    ).fetchone()


def _close_appeal(
    conn,
    appeal,
    *,
    decision: str,
    final_score: int | None,
    reason: str,
    kind: str,
    decider_id: int | None,
    clock: Clock,
):
    now = clock.now_iso()
    cur = conn.execute(
        "INSERT INTO appeal_rulings"
        " (appeal_id, kind, decision, final_score, reason, decider_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (appeal["id"], kind, decision, final_score, reason, decider_id, now),
    )
    ruling_id = cur.lastrowid
    correction = None
    correction_v = None
    if decision == "changed":
        correction = _create_appeal_correction_version(
            conn, appeal, ruling_id, final_score, now
        )
        correction_v = correction["result_version"]
        conn.execute(
            "UPDATE appeal_rulings SET result_version_id = ? WHERE id = ?",
            (correction["id"], ruling_id),
        )

    status = "closed_rejected" if decision == "rejected" else "closed_changed"
    conn.execute(
        "UPDATE score_appeals"
        " SET status = ?, final_decision = ?, final_score = ?,"
        "     final_reason = ?, closure_kind = ?, final_ruling_id = ?,"
        "     arbitrator_id = ?, closed_at = ?,"
        "     correction_result_version_id = ?,"
        "     correction_result_version = ?"
        " WHERE id = ?",
        (
            status,
            decision,
            final_score,
            reason,
            kind,
            ruling_id,
            decider_id if kind == "arbitration" else None,
            now,
            correction["id"] if correction is not None else None,
            correction_v,
            appeal["id"],
        ),
    )
    return ruling_id


def submit_appeal_opinion(
    db: Database,
    appeal_id: int,
    grader_id: int,
    op_id: str | None,
    decision_raw: str,
    new_score_raw: int | None,
    reason: str,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    reason = (reason or "").strip()
    if not reason:
        raise ServiceError(422, "opinion reason is required")
    decision = str(decision_raw)
    with db.write() as conn:
        appeal = _get_appeal(conn, appeal_id)
        scope = f"appeal-opinion:{appeal_id}:{grader_id}"
        idem = _idempotency_record(
            conn,
            scope,
            op_id,
            {
                "decision": decision,
                "new_score": new_score_raw,
                "reason": reason,
            },
            grader_id,
            clock,
            appeal_id=appeal_id,
        )
        if idem is not None:
            fresh = _get_appeal(conn, appeal_id)
            view = _appeal_view(
                conn, fresh, viewer_id=grader_id, viewer_role="grader"
            )
            view["idempotent_replay"] = True
            return view

        assignment = conn.execute(
            "SELECT 1 FROM appeal_assignments"
            " WHERE appeal_id = ? AND grader_id = ?",
            (appeal_id, grader_id),
        ).fetchone()
        if assignment is None:
            raise ServiceError(
                403, "only the two graders assigned to this appeal may submit opinions"
            )
        conflict = _manual_conflict_user(conn, appeal, grader_id)
        if conflict is not None:
            raise ServiceError(
                403,
                "a grader who previously manually scored or revoked this question"
                " cannot review it",
            )
        if appeal["status"] not in ("in_review",):
            raise ServiceError(
                409,
                f"appeal {appeal_id} is {appeal['status']}; opinions are no longer accepted",
            )
        existing = conn.execute(
            "SELECT id FROM appeal_opinions WHERE appeal_id = ? AND grader_id = ?",
            (appeal_id, grader_id),
        ).fetchone()
        if existing is not None:
            raise ServiceError(409, "each assigned grader may submit only one opinion")

        final_score = _validate_decision_score(
            conn, appeal, decision, new_score_raw
        )
        conn.execute(
            "INSERT INTO appeal_opinions"
            " (appeal_id, grader_id, decision, new_score, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                appeal_id,
                grader_id,
                decision,
                final_score,
                reason,
                clock.now_iso(),
            ),
        )

        opinions = conn.execute(
            "SELECT * FROM appeal_opinions WHERE appeal_id = ? ORDER BY id",
            (appeal_id,),
        ).fetchall()
        if len(opinions) == 2:
            first, second = opinions[0], opinions[1]
            same = first["decision"] == second["decision"] and (
                first["decision"] == "rejected"
                or first["new_score"] == second["new_score"]
            )
            if same:
                if first["decision"] == "rejected":
                    formal_reason = "两名指派阅卷员结论一致：驳回申诉，维持该版得分。"
                    _close_appeal(
                        conn,
                        appeal,
                        decision="rejected",
                        final_score=None,
                        reason=formal_reason,
                        kind="reviewer_agreement",
                        decider_id=None,
                        clock=clock,
                    )
                else:
                    score = first["new_score"]
                    formal_reason = (
                        f"两名指派阅卷员结论一致：将该题分数改为 {score} 分。"
                    )
                    _close_appeal(
                        conn,
                        appeal,
                        decision="changed",
                        final_score=score,
                        reason=formal_reason,
                        kind="reviewer_agreement",
                        decider_id=None,
                        clock=clock,
                    )
            else:
                conn.execute(
                    "UPDATE score_appeals SET status = 'pending_arbitration'"
                    " WHERE id = ?",
                    (appeal_id,),
                )

        fresh = _get_appeal(conn, appeal_id)
        return _appeal_view(
            conn, fresh, viewer_id=grader_id, viewer_role="grader"
        )


def arbitrate_appeal(
    db: Database,
    appeal_id: int,
    admin_id: int,
    op_id: str | None,
    decision_raw: str,
    new_score_raw: int | None,
    reason: str,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    reason = (reason or "").strip()
    if not reason:
        raise ServiceError(422, "arbitration reason is required")
    decision = str(decision_raw)
    with db.write() as conn:
        appeal = _get_appeal(conn, appeal_id)
        scope = f"appeal-arbitrate:{appeal_id}"
        idem = _idempotency_record(
            conn,
            scope,
            op_id,
            {
                "decision": decision,
                "new_score": new_score_raw,
                "reason": reason,
            },
            admin_id,
            clock,
            appeal_id=appeal_id,
        )
        if idem is not None:
            fresh = _get_appeal(conn, appeal_id)
            view = _appeal_view(
                conn, fresh, viewer_id=admin_id, viewer_role="admin"
            )
            view["idempotent_replay"] = True
            return view

        conflict = _manual_conflict_user(conn, appeal, admin_id)
        if conflict is not None:
            raise ServiceError(
                403,
                "an administrator who previously manually scored or revoked this"
                " question cannot arbitrate this appeal",
            )
        if appeal["status"] != "pending_arbitration":
            raise ServiceError(
                409,
                f"appeal {appeal_id} is {appeal['status']}; arbitration is allowed"
                " only after the two reviewers disagree",
            )
        final_score = _validate_decision_score(
            conn, appeal, decision, new_score_raw
        )
        _close_appeal(
            conn,
            appeal,
            decision=decision,
            final_score=final_score,
            reason=reason,
            kind="arbitration",
            decider_id=admin_id,
            clock=clock,
        )
        fresh = _get_appeal(conn, appeal_id)
        return _appeal_view(
            conn, fresh, viewer_id=admin_id, viewer_role="admin"
        )


def _mark_published_appeal_corrections(
    conn, result_version_id: int, published_at: str
) -> None:
    ruling_ids = [
        r["appeal_ruling_id"]
        for r in conn.execute(
            "SELECT DISTINCT appeal_ruling_id FROM result_items"
            " WHERE result_version_id = ? AND appeal_ruling_id IS NOT NULL",
            (result_version_id,),
        ).fetchall()
    ]
    for ruling_id in ruling_ids:
        conn.execute(
            "UPDATE score_appeals SET correction_published_at = ?"
            " WHERE final_ruling_id = ? AND correction_published_at IS NULL",
            (published_at, ruling_id),
        )


# =========================== Makeup reservations (补考分场预约) ===========================
#
# A makeup campaign turns a fresh, not-yet-timed exam into a timed exam whose
# sessions have a seat capacity. There is no pre-filled roster: candidates
# whose CURRENTLY PUBLISHED total on the basis exam is below the pass line
# apply for a session. A free seat immediately produces an invitation (which
# holds the seat until its deadline); otherwise the application waits in the
# session's FIFO queue. Confirming an invitation mirrors the candidate into
# exam_enrollments, so the existing timed machinery (roster gate, two-layer
# pause, lazy auto-submit) applies unchanged.
#
# All seat accounting is derived from application/invitation state inside the
# global write transaction, so a restart can never overbook a session or
# leave a session switch half-applied. Expiry/eligibility/promotion are
# settled lazily: every makeup read or write first settles the campaign, so
# invitations expire on schedule even while the service was offline (and are
# never re-issued afterwards).

_MAKEUP_ACTIVE_STATES = ("waitlisted", "invited")
_MAKEUP_SEAT_STATES = ("invited", "confirmed", "completed")


def _get_makeup_campaign(conn, exam_id: int):
    return conn.execute(
        "SELECT * FROM makeup_campaigns WHERE exam_id = ?", (exam_id,)
    ).fetchone()


def _require_makeup_campaign(conn, exam_id: int):
    campaign = _get_makeup_campaign(conn, exam_id)
    if campaign is None:
        raise ServiceError(
            404, f"exam {exam_id} has no makeup reservation campaign"
        )
    return campaign


def _makeup_published_total(conn, basis_exam_id: int, candidate_id: int) -> int | None:
    row = conn.execute(
        "SELECT total_score FROM result_versions"
        " WHERE exam_id = ? AND candidate_id = ? AND status = 'published'",
        (basis_exam_id, candidate_id),
    ).fetchone()
    return row["total_score"] if row is not None else None


def _makeup_eligibility(conn, campaign, candidate_id: int) -> tuple[bool, int | None]:
    """Eligible iff the basis exam's published total is below the pass line.

    An unpublished result does not count in either direction.
    """
    total = _makeup_published_total(conn, campaign["basis_exam_id"], candidate_id)
    return (total is not None and total < campaign["pass_score"]), total


def _makeup_session_counts(conn, session_id: int, capacity: int | None) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM makeup_applications"
        " WHERE session_id = ? AND state IN ('invited', 'confirmed', 'completed', 'waitlisted')"
        " GROUP BY state",
        (session_id,),
    ).fetchall()
    by_state = {r["state"]: r["n"] for r in rows}
    invited = by_state.get("invited", 0)
    confirmed = by_state.get("confirmed", 0)
    completed = by_state.get("completed", 0)
    waitlisted = by_state.get("waitlisted", 0)
    free_seats = None
    if capacity is not None:
        # A pending invitation holds a seat exactly like a confirmed one.
        free_seats = max(0, capacity - invited - confirmed - completed)
    return {
        "confirmed": confirmed,
        "completed": completed,
        "invited": invited,
        "waitlisted": waitlisted,
        "free_seats": free_seats,
    }


def _promote_makeup_session_locked(
    conn, clock: Clock, campaign, session_id: int, now: datetime
) -> None:
    """Invite waitlisted candidates (application order) into free seats."""
    session = conn.execute(
        "SELECT * FROM exam_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if session is None or session["capacity"] is None:
        return
    counts = _makeup_session_counts(conn, session_id, session["capacity"])
    free = counts["free_seats"]
    if free <= 0:
        return
    waiting = conn.execute(
        "SELECT * FROM makeup_applications"
        " WHERE session_id = ? AND state = 'waitlisted' ORDER BY id",
        (session_id,),
    ).fetchall()
    now_iso = clock.iso(now)
    expires_iso = clock.iso(now + timedelta(seconds=campaign["invite_ttl_seconds"]))
    for application in waiting[:free]:
        conn.execute(
            "INSERT INTO makeup_invitations"
            " (application_id, campaign_id, session_id, candidate_id, seq,"
            "  status, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, 1, 'pending', ?, ?)",
            (
                application["id"],
                campaign["id"],
                session_id,
                application["candidate_id"],
                now_iso,
                expires_iso,
            ),
        )
        conn.execute(
            "UPDATE makeup_applications SET state = 'invited', updated_at = ?"
            " WHERE id = ?",
            (now_iso, application["id"]),
        )


def _settle_makeup_campaign_locked(
    conn, clock: Clock, campaign, now: datetime
) -> None:
    """Apply expirations, eligibility voiding and promotions for a campaign.

    Idempotent and lazy: safe to run on every touch. Expired invitations keep
    their original expiry instant as the resolution time, so a service that
    was offline still expires them on schedule and never re-issues them.
    """
    now_iso = clock.iso(now)
    now_us = clock.micros(now)

    # 1. Expire pending invitations whose confirmation deadline has passed.
    pending = conn.execute(
        "SELECT * FROM makeup_invitations WHERE campaign_id = ? AND status = 'pending'",
        (campaign["id"],),
    ).fetchall()
    for invitation in pending:
        if clock.micros(clock.parse(invitation["expires_at"])) <= now_us:
            conn.execute(
                "UPDATE makeup_invitations SET status = 'expired',"
                " resolved_at = expires_at WHERE id = ?",
                (invitation["id"],),
            )
            # The application itself ends when its invitation expires.
            conn.execute(
                "UPDATE makeup_applications SET state = 'expired', updated_at = ?"
                " WHERE id = ? AND state = 'invited'",
                (invitation["expires_at"], invitation["application_id"]),
            )

    # 2. Void active applications whose candidate lost eligibility. Confirmed
    #    seats are never revoked; a voided application stays void even if
    #    eligibility later returns (the candidate must re-apply).
    active = conn.execute(
        "SELECT * FROM makeup_applications WHERE campaign_id = ?"
        " AND state IN ('waitlisted', 'invited')",
        (campaign["id"],),
    ).fetchall()
    for application in active:
        eligible, _ = _makeup_eligibility(conn, campaign, application["candidate_id"])
        if eligible:
            continue
        if application["state"] == "invited":
            conn.execute(
                "UPDATE makeup_invitations SET status = 'voided', resolved_at = ?"
                " WHERE application_id = ? AND status = 'pending'",
                (now_iso, application["id"]),
            )
        conn.execute(
            "UPDATE makeup_applications SET state = 'voided', updated_at = ?"
            " WHERE id = ?",
            (now_iso, application["id"]),
        )

    # 3. Promote the queue into whatever seats are now free.
    sessions = conn.execute(
        "SELECT id FROM exam_sessions WHERE exam_id = ?", (campaign["exam_id"],)
    ).fetchall()
    for session in sessions:
        _promote_makeup_session_locked(conn, clock, campaign, session["id"], now)


def _settle_makeup(db: Database, exam_id: int, clock: Clock) -> None:
    """Settle a campaign from read endpoints (no-op for non-makeup exams)."""
    with db.write() as conn:
        campaign = _get_makeup_campaign(conn, exam_id)
        if campaign is not None:
            _settle_makeup_campaign_locked(conn, clock, campaign, clock.now())


def _makeup_op_replay(conn, scope: str, op_id: str | None, request_payload: dict,
                      actor_id: int):
    """Idempotency check for makeup writes.

    Returns ``(replay_result, op_id, request_hash)``; the replay result is
    the recorded first response and is returned without touching any seat.
    """
    if not op_id or not str(op_id).strip():
        raise ServiceError(422, "op_id is required for retry-safe operations")
    op_id = str(op_id)
    canonical = _dumps(
        {"actor_id": actor_id, "request": request_payload, "scope": scope}
    )
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    row = conn.execute(
        "SELECT * FROM makeup_operations WHERE op_id = ?", (op_id,)
    ).fetchone()
    if row is None:
        return None, op_id, request_hash
    if row["scope"] != scope or row["request_hash"] != request_hash:
        raise ServiceError(
            409,
            f"op_id '{op_id}' was already used for a different request;"
            " use a new op_id for a new operation",
        )
    result = _loads(row["result_json"])
    result["idempotent_replay"] = True
    result["note"] = "op_id already processed; replay changes nothing"
    return result, op_id, request_hash


def _makeup_op_store(conn, scope: str, op_id: str, request_hash: str,
                     actor_id: int, result: dict[str, Any], clock: Clock):
    conn.execute(
        "INSERT INTO makeup_operations"
        " (scope, op_id, request_hash, actor_id, result_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (scope, op_id, request_hash, actor_id, _dumps(result), clock.now_iso()),
    )
    return result


def _makeup_invitation_view(row) -> dict[str, Any]:
    return {
        "invitation_id": row["id"],
        "application_id": row["application_id"],
        "session_id": row["session_id"],
        "status": row["status"],
        "issued_at": row["issued_at"],
        "expires_at": row["expires_at"],
        "resolved_at": row["resolved_at"],
    }


def _makeup_application_view(conn, row) -> dict[str, Any]:
    session = conn.execute(
        "SELECT id, name FROM exam_sessions WHERE id = ?", (row["session_id"],)
    ).fetchone()
    invitation = conn.execute(
        "SELECT * FROM makeup_invitations WHERE application_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    queue_position = None
    if row["state"] == "waitlisted":
        queue_position = conn.execute(
            "SELECT COUNT(*) + 1 AS pos FROM makeup_applications"
            " WHERE session_id = ? AND state = 'waitlisted' AND id < ?",
            (row["session_id"], row["id"]),
        ).fetchone()["pos"]
    return {
        "application_id": row["id"],
        "campaign_id": row["campaign_id"],
        "candidate_id": row["candidate_id"],
        "session_id": row["session_id"],
        "session_name": session["name"] if session is not None else None,
        "state": row["state"],
        "queue_position": queue_position,
        "invitation": _makeup_invitation_view(invitation)
        if invitation is not None
        else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _makeup_campaign_view(conn, campaign, *, include_applications: bool) -> dict[str, Any]:
    sessions = conn.execute(
        "SELECT * FROM exam_sessions WHERE exam_id = ? ORDER BY id",
        (campaign["exam_id"],),
    ).fetchall()
    session_views = []
    for s in sessions:
        counts = _makeup_session_counts(conn, s["id"], s["capacity"])
        session_views.append(
            {
                "session_id": s["id"],
                "name": s["name"],
                "duration_seconds": s["duration_seconds"],
                "capacity": s["capacity"],
                "paused": bool(s["paused"]),
                **counts,
            }
        )
    view: dict[str, Any] = {
        "campaign_id": campaign["id"],
        "exam_id": campaign["exam_id"],
        "basis_exam_id": campaign["basis_exam_id"],
        "pass_score": campaign["pass_score"],
        "invite_ttl_seconds": campaign["invite_ttl_seconds"],
        "created_at": campaign["created_at"],
        "sessions": session_views,
    }
    if include_applications:
        rows = conn.execute(
            "SELECT a.*, u.username FROM makeup_applications a"
            " JOIN users u ON u.id = a.candidate_id"
            " WHERE a.campaign_id = ? ORDER BY a.id",
            (campaign["id"],),
        ).fetchall()
        view["applications"] = [
            {**_makeup_application_view(conn, r), "username": r["username"]}
            for r in rows
        ]
    return view


def create_makeup_campaign(
    db: Database,
    exam_id: int,
    op_id: str | None,
    basis_exam_id: int,
    pass_score: int,
    invite_ttl_seconds: int,
    sessions_in: list[dict[str, Any]],
    admin_id: int,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    """Open the (single) makeup reservation campaign for an exam.

    The exam must not be timed-configured yet and must have no attempts; it
    becomes a timed exam whose sessions carry the given capacities.
    """
    with db.write() as conn:
        exam = conn.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
        if exam is None:
            raise ServiceError(404, f"exam {exam_id} not found")
        scope = f"makeup-create:{exam_id}"
        request = {
            "basis_exam_id": basis_exam_id,
            "pass_score": pass_score,
            "invite_ttl_seconds": invite_ttl_seconds,
            "sessions": sessions_in,
        }
        replay, op_id, request_hash = _makeup_op_replay(
            conn, scope, op_id, request, admin_id
        )
        if replay is not None:
            return replay
        if exam["timed_mode"]:
            raise ServiceError(
                409,
                f"exam {exam_id} is already configured with timed sessions;"
                " makeup reservations require an unconfigured exam",
            )
        if _get_makeup_campaign(conn, exam_id) is not None:
            raise ServiceError(
                409, f"exam {exam_id} already has a makeup reservation campaign"
            )
        existing = conn.execute(
            "SELECT 1 FROM attempts WHERE exam_id = ? LIMIT 1", (exam_id,)
        ).fetchone()
        if existing is not None:
            raise ServiceError(
                409, "exam already has attempts; makeup reservations must precede any start"
            )
        if basis_exam_id == exam_id:
            raise ServiceError(422, "the basis exam must be a different exam")
        basis = conn.execute(
            "SELECT id FROM exams WHERE id = ?", (basis_exam_id,)
        ).fetchone()
        if basis is None:
            raise ServiceError(422, f"basis exam {basis_exam_id} not found")
        names = [s["name"] for s in sessions_in]
        if len(names) != len(set(names)):
            raise ServiceError(422, "session names must be unique")

        now_iso = clock.now_iso()
        cur = conn.execute(
            "INSERT INTO makeup_campaigns"
            " (exam_id, basis_exam_id, pass_score, invite_ttl_seconds, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (exam_id, basis_exam_id, pass_score, invite_ttl_seconds, now_iso),
        )
        campaign_id = cur.lastrowid
        for s in sessions_in:
            conn.execute(
                "INSERT INTO exam_sessions"
                " (exam_id, name, duration_seconds, paused, created_at, capacity)"
                " VALUES (?, ?, ?, 0, ?, ?)",
                (exam_id, s["name"], s["duration_seconds"], now_iso, s["capacity"]),
            )
        conn.execute("UPDATE exams SET timed_mode = 1 WHERE id = ?", (exam_id,))
        campaign = conn.execute(
            "SELECT * FROM makeup_campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()
        result = _makeup_campaign_view(conn, campaign, include_applications=True)
        result["idempotent_replay"] = False
        return _makeup_op_store(
            conn, scope, op_id, request_hash, admin_id, result, clock
        )


def apply_makeup(
    db: Database,
    exam_id: int,
    candidate_id: int,
    op_id: str | None,
    session_id: int,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    """Apply for a makeup session. A free seat invites immediately."""
    with db.write() as conn:
        campaign = _require_makeup_campaign(conn, exam_id)
        now = clock.now()
        _settle_makeup_campaign_locked(conn, clock, campaign, now)
        scope = f"makeup-apply:{campaign['id']}:{candidate_id}"
        replay, op_id, request_hash = _makeup_op_replay(
            conn, scope, op_id, {"session_id": session_id}, candidate_id
        )
        if replay is not None:
            return replay
        session = conn.execute(
            "SELECT * FROM exam_sessions WHERE id = ? AND exam_id = ?",
            (session_id, exam_id),
        ).fetchone()
        if session is None:
            raise ServiceError(404, f"session {session_id} not found on exam {exam_id}")
        eligible, total = _makeup_eligibility(conn, campaign, candidate_id)
        if not eligible:
            if total is None:
                raise ServiceError(
                    403,
                    "no published result on the basis exam; only candidates whose"
                    " current published total is below the pass line may apply",
                )
            raise ServiceError(
                403,
                f"published total {total} is not below the pass line"
                f" {campaign['pass_score']}",
            )
        attempt = conn.execute(
            "SELECT 1 FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()
        if attempt is not None:
            raise ServiceError(
                409, "the makeup exam has already been started; applications are closed"
            )
        active = conn.execute(
            "SELECT * FROM makeup_applications WHERE campaign_id = ?"
            " AND candidate_id = ? AND state IN ('waitlisted', 'invited')",
            (campaign["id"], candidate_id),
        ).fetchone()
        if active is not None:
            raise ServiceError(
                409,
                f"application {active['id']} is already {active['state']};"
                " only one session may be pursued at a time",
            )
        seated = conn.execute(
            "SELECT * FROM makeup_applications WHERE campaign_id = ?"
            " AND candidate_id = ? AND state IN ('confirmed', 'completed')",
            (campaign["id"], candidate_id),
        ).fetchone()
        if seated is not None:
            if seated["state"] == "completed":
                raise ServiceError(
                    409,
                    "the makeup exam has already been started; applications are closed",
                )
            if seated["session_id"] == session_id:
                raise ServiceError(
                    409,
                    "already confirmed in this session; only other sessions"
                    " may be waitlisted",
                )

        now_iso = clock.now_iso()
        cur = conn.execute(
            "INSERT INTO makeup_applications"
            " (campaign_id, candidate_id, session_id, state, created_at, updated_at)"
            " VALUES (?, ?, ?, 'waitlisted', ?, ?)",
            (campaign["id"], candidate_id, session_id, now_iso, now_iso),
        )
        application_id = cur.lastrowid
        # A free seat promotes the new application into an invitation at once.
        _promote_makeup_session_locked(conn, clock, campaign, session_id, now)
        row = conn.execute(
            "SELECT * FROM makeup_applications WHERE id = ?", (application_id,)
        ).fetchone()
        result = _makeup_application_view(conn, row)
        result["idempotent_replay"] = False
        return _makeup_op_store(
            conn, scope, op_id, request_hash, candidate_id, result, clock
        )


def confirm_makeup_invitation(
    db: Database,
    exam_id: int,
    invitation_id: int,
    candidate_id: int,
    op_id: str | None,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    """Confirm one specific invitation; grants the roster seat for its session.

    If the candidate already holds a confirmed seat in another session, the
    switch is atomic: the old seat is released (and its queue promoted) in
    the same transaction that confirms the new one.
    """
    with db.write() as conn:
        campaign = _require_makeup_campaign(conn, exam_id)
        now = clock.now()
        _settle_makeup_campaign_locked(conn, clock, campaign, now)
        invitation = conn.execute(
            "SELECT * FROM makeup_invitations WHERE id = ? AND campaign_id = ?",
            (invitation_id, campaign["id"]),
        ).fetchone()
        if invitation is None or invitation["candidate_id"] != candidate_id:
            # Do not reveal other candidates' invitations.
            raise ServiceError(404, f"invitation {invitation_id} not found")
        scope = f"makeup-confirm:{invitation_id}"
        replay, op_id, request_hash = _makeup_op_replay(
            conn, scope, op_id, {"invitation_id": invitation_id}, candidate_id
        )
        if replay is not None:
            return replay
        if invitation["status"] != "pending":
            raise ServiceError(
                409,
                f"invitation {invitation_id} is {invitation['status']};"
                " it cannot confirm a seat",
            )
        application = conn.execute(
            "SELECT * FROM makeup_applications WHERE id = ?",
            (invitation["application_id"],),
        ).fetchone()
        if application["state"] != "invited":
            raise ServiceError(
                409,
                f"application {application['id']} is {application['state']};"
                " the invitation is no longer valid",
            )
        eligible, total = _makeup_eligibility(conn, campaign, candidate_id)
        if not eligible:
            raise ServiceError(
                403,
                "eligibility requires a current published total below the"
                " pass line at confirmation time",
            )

        now_iso = clock.now_iso()
        current = conn.execute(
            "SELECT * FROM makeup_applications WHERE campaign_id = ?"
            " AND candidate_id = ? AND state = 'confirmed'",
            (campaign["id"], candidate_id),
        ).fetchone()
        switched_from = None
        if current is not None:
            # Atomic switch: release the old seat only as the new one is taken.
            conn.execute(
                "UPDATE makeup_applications SET state = 'switched', updated_at = ?"
                " WHERE id = ?",
                (now_iso, current["id"]),
            )
            conn.execute(
                "UPDATE exam_enrollments SET session_id = ?"
                " WHERE exam_id = ? AND candidate_id = ?",
                (application["session_id"], exam_id, candidate_id),
            )
            switched_from = current
        else:
            conn.execute(
                "INSERT INTO exam_enrollments (exam_id, session_id, candidate_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (exam_id, application["session_id"], candidate_id, now_iso),
            )
        conn.execute(
            "UPDATE makeup_applications SET state = 'confirmed', updated_at = ?"
            " WHERE id = ?",
            (now_iso, application["id"]),
        )
        conn.execute(
            "UPDATE makeup_invitations SET status = 'confirmed', resolved_at = ?"
            " WHERE id = ?",
            (now_iso, invitation_id),
        )
        if switched_from is not None:
            _promote_makeup_session_locked(
                conn, clock, campaign, switched_from["session_id"], now
            )
        fresh = conn.execute(
            "SELECT * FROM makeup_applications WHERE id = ?", (application["id"],)
        ).fetchone()
        result = _makeup_application_view(conn, fresh)
        result["switched_from_session_id"] = (
            switched_from["session_id"] if switched_from is not None else None
        )
        result["idempotent_replay"] = False
        return _makeup_op_store(
            conn, scope, op_id, request_hash, candidate_id, result, clock
        )


def withdraw_makeup_application(
    db: Database,
    exam_id: int,
    application_id: int,
    candidate_id: int,
    op_id: str | None,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    """Candidate withdraws a waitlisted/invited application (退出候补)."""
    with db.write() as conn:
        campaign = _require_makeup_campaign(conn, exam_id)
        now = clock.now()
        _settle_makeup_campaign_locked(conn, clock, campaign, now)
        application = conn.execute(
            "SELECT * FROM makeup_applications WHERE id = ? AND campaign_id = ?",
            (application_id, campaign["id"]),
        ).fetchone()
        if application is None or application["candidate_id"] != candidate_id:
            raise ServiceError(404, f"application {application_id} not found")
        scope = f"makeup-withdraw:{application_id}"
        replay, op_id, request_hash = _makeup_op_replay(
            conn, scope, op_id, {"application_id": application_id}, candidate_id
        )
        if replay is not None:
            return replay
        if application["state"] not in _MAKEUP_ACTIVE_STATES:
            raise ServiceError(
                409,
                f"application {application_id} is {application['state']};"
                " only a waitlisted or invited application can be withdrawn",
            )
        now_iso = clock.now_iso()
        if application["state"] == "invited":
            conn.execute(
                "UPDATE makeup_invitations SET status = 'cancelled', resolved_at = ?"
                " WHERE application_id = ? AND status = 'pending'",
                (now_iso, application_id),
            )
        conn.execute(
            "UPDATE makeup_applications SET state = 'cancelled', updated_at = ?"
            " WHERE id = ?",
            (now_iso, application_id),
        )
        if application["state"] == "invited":
            # The held seat is released; promote the queue.
            _promote_makeup_session_locked(
                conn, clock, campaign, application["session_id"], now
            )
        fresh = conn.execute(
            "SELECT * FROM makeup_applications WHERE id = ?", (application_id,)
        ).fetchone()
        result = _makeup_application_view(conn, fresh)
        result["idempotent_replay"] = False
        return _makeup_op_store(
            conn, scope, op_id, request_hash, candidate_id, result, clock
        )


def unsubscribe_makeup(
    db: Database,
    exam_id: int,
    candidate_id: int,
    op_id: str | None,
    clock: Clock = _REAL_CLOCK,
) -> dict[str, Any]:
    """Give up a confirmed seat before the exam starts (退订).

    Releases the seat (promoting the queue) and cancels the candidate's own
    waitlisted/invited application as well.
    """
    with db.write() as conn:
        campaign = _require_makeup_campaign(conn, exam_id)
        now = clock.now()
        _settle_makeup_campaign_locked(conn, clock, campaign, now)
        scope = f"makeup-unsubscribe:{campaign['id']}:{candidate_id}"
        replay, op_id, request_hash = _makeup_op_replay(
            conn, scope, op_id, {}, candidate_id
        )
        if replay is not None:
            return replay
        confirmed = conn.execute(
            "SELECT * FROM makeup_applications WHERE campaign_id = ?"
            " AND candidate_id = ? AND state = 'confirmed'",
            (campaign["id"], candidate_id),
        ).fetchone()
        if confirmed is None:
            completed = conn.execute(
                "SELECT 1 FROM makeup_applications WHERE campaign_id = ?"
                " AND candidate_id = ? AND state = 'completed'",
                (campaign["id"], candidate_id),
            ).fetchone()
            if completed is not None:
                raise ServiceError(
                    409,
                    "the makeup exam has already been started;"
                    " unsubscribe is no longer possible",
                )
            raise ServiceError(409, "no confirmed seat to unsubscribe")

        now_iso = clock.now_iso()
        cancelled_ids = [confirmed["id"]]
        conn.execute(
            "UPDATE makeup_applications SET state = 'cancelled', updated_at = ?"
            " WHERE id = ?",
            (now_iso, confirmed["id"]),
        )
        conn.execute(
            "DELETE FROM exam_enrollments WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        )
        active = conn.execute(
            "SELECT * FROM makeup_applications WHERE campaign_id = ?"
            " AND candidate_id = ? AND state IN ('waitlisted', 'invited')",
            (campaign["id"], candidate_id),
        ).fetchone()
        if active is not None:
            cancelled_ids.append(active["id"])
            if active["state"] == "invited":
                conn.execute(
                    "UPDATE makeup_invitations SET status = 'cancelled',"
                    " resolved_at = ? WHERE application_id = ? AND status = 'pending'",
                    (now_iso, active["id"]),
                )
            conn.execute(
                "UPDATE makeup_applications SET state = 'cancelled', updated_at = ?"
                " WHERE id = ?",
                (now_iso, active["id"]),
            )
        _promote_makeup_session_locked(
            conn, clock, campaign, confirmed["session_id"], now
        )
        if active is not None and active["state"] == "invited":
            _promote_makeup_session_locked(
                conn, clock, campaign, active["session_id"], now
            )
        result = {
            "exam_id": exam_id,
            "campaign_id": campaign["id"],
            "unsubscribed_application_id": confirmed["id"],
            "released_session_id": confirmed["session_id"],
            "cancelled_application_ids": cancelled_ids,
            "idempotent_replay": False,
        }
        return _makeup_op_store(
            conn, scope, op_id, request_hash, candidate_id, result, clock
        )


def get_my_makeup(
    db: Database, exam_id: int, candidate_id: int, clock: Clock = _REAL_CLOCK
) -> dict[str, Any]:
    """Candidate view: own applications/invitations only, after settlement."""
    _settle_makeup(db, exam_id, clock)
    with db.read() as conn:
        campaign = _require_makeup_campaign(conn, exam_id)
        eligible, total = _makeup_eligibility(conn, campaign, candidate_id)
        view = _makeup_campaign_view(conn, campaign, include_applications=False)
        view["my_eligibility"] = {
            "eligible": eligible,
            "published_total_score": total,
            "pass_score": campaign["pass_score"],
        }
        rows = conn.execute(
            "SELECT * FROM makeup_applications WHERE campaign_id = ?"
            " AND candidate_id = ? ORDER BY id DESC",
            (campaign["id"], candidate_id),
        ).fetchall()
        view["my_applications"] = [_makeup_application_view(conn, r) for r in rows]
        return view


def get_makeup_for_staff(
    db: Database, exam_id: int, clock: Clock = _REAL_CLOCK
) -> dict[str, Any]:
    """Staff view: full campaign with every application, after settlement."""
    _settle_makeup(db, exam_id, clock)
    with db.read() as conn:
        campaign = _require_makeup_campaign(conn, exam_id)
        return _makeup_campaign_view(conn, campaign, include_applications=True)


def _makeup_on_exam_start(
    conn, exam_id: int, candidate_id: int, clock: Clock
) -> None:
    """Finalize a candidate's reservation state on the first successful start.

    Runs inside the attempt-creation transaction: the confirmed application
    becomes completed (seat stays occupied) and any unfinished switch
    application (waitlist/invitation) is cancelled, releasing a held seat.
    """
    campaign = _get_makeup_campaign(conn, exam_id)
    if campaign is None:
        return
    now = clock.now()
    _settle_makeup_campaign_locked(conn, clock, campaign, now)
    now_iso = clock.now_iso()
    active = conn.execute(
        "SELECT * FROM makeup_applications WHERE campaign_id = ?"
        " AND candidate_id = ? AND state IN ('waitlisted', 'invited')",
        (campaign["id"], candidate_id),
    ).fetchone()
    released_session_id = None
    if active is not None:
        if active["state"] == "invited":
            conn.execute(
                "UPDATE makeup_invitations SET status = 'cancelled', resolved_at = ?"
                " WHERE application_id = ? AND status = 'pending'",
                (now_iso, active["id"]),
            )
            released_session_id = active["session_id"]
        conn.execute(
            "UPDATE makeup_applications SET state = 'cancelled', updated_at = ?"
            " WHERE id = ?",
            (now_iso, active["id"]),
        )
    conn.execute(
        "UPDATE makeup_applications SET state = 'completed', updated_at = ?"
        " WHERE campaign_id = ? AND candidate_id = ? AND state = 'confirmed'",
        (now_iso, campaign["id"], candidate_id),
    )
    if released_session_id is not None:
        _promote_makeup_session_locked(conn, clock, campaign, released_session_id, now)


def _makeup_on_result_published(
    conn, exam_id: int, candidate_id: int, clock: Clock
) -> None:
    """Void pending waitlists/invitations that lost eligibility via this publish."""
    campaigns = conn.execute(
        "SELECT * FROM makeup_campaigns WHERE basis_exam_id = ?", (exam_id,)
    ).fetchall()
    if not campaigns:
        return
    now = clock.now()
    for campaign in campaigns:
        _settle_makeup_campaign_locked(conn, clock, campaign, now)

