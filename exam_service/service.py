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

import json
import random
from datetime import datetime
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
            "SELECT id, name, duration_seconds, paused, created_at FROM exam_sessions"
            " WHERE exam_id = ? ORDER BY id",
            (exam_id,),
        ).fetchall()
        out["sessions"] = [
            {
                "session_id": s["id"],
                "name": s["name"],
                "duration_seconds": s["duration_seconds"],
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
        manual_score = None
        manual_reason = None
        if manual_action is not None and manual_action["score"] is not None:
            manual_score = manual_action["score"]
            manual_reason = manual_action["reason"]
        effective = auto if manual_score is None else manual_score
        total += effective

        conn.execute(
            "INSERT INTO result_items"
            " (result_version_id, question_id, auto_score, manual_score,"
            "  manual_reason, effective_score)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (result_id, qid, auto, manual_score, manual_reason, effective),
        )
        items.append(
            {
                "question_id": qid,
                "auto_score": auto,
                "manual_score": manual_score,
                "manual_reason": manual_reason,
                "effective_score": effective,
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
        "SELECT question_id, auto_score, manual_score, manual_reason, effective_score"
        " FROM result_items WHERE result_version_id = ? ORDER BY question_id",
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
        "items": [_row_to_dict(i) for i in items],
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
    db: Database, exam_id: int, candidate_id: int, clock: Clock = _REAL_CLOCK
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
        return {
            "grading_runs": [_row_to_dict(r) for r in runs],
            "manual_actions": [_row_to_dict(a) for a in actions],
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
