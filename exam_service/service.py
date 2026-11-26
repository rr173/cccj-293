"""Core domain logic.

Concurrency model: every public mutating function runs inside
``Database.write()``, i.e. one global ``BEGIN IMMEDIATE`` transaction at a
time. Read-modify-write checks (revision, idempotency keys, result-version
conflicts) are therefore atomic. Callers see strict serial ordering.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from typing import Any, Optional

from .db import Database
from .schemas import QuestionVersionIn


class ServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: str) -> Any:
    return json.loads(raw)


def _row_to_dict(row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# =========================== Question bank ===========================


def create_question(db: Database, payload: QuestionVersionIn) -> dict[str, Any]:
    with db.write() as conn:
        cur = conn.execute("INSERT INTO questions (current_version) VALUES (1)")
        question_id = cur.lastrowid
        _insert_question_version(conn, question_id, 1, payload)
        return _get_question_out(conn, question_id, 1)


def edit_question(
    db: Database, question_id: int, payload: QuestionVersionIn
) -> dict[str, Any]:
    with db.write() as conn:
        row = conn.execute(
            "SELECT current_version FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(404, f"question {question_id} not found")
        new_version = row["current_version"] + 1
        _insert_question_version(conn, question_id, new_version, payload)
        conn.execute(
            "UPDATE questions SET current_version = ? WHERE id = ?",
            (new_version, question_id),
        )
        return _get_question_out(conn, question_id, new_version)


def _insert_question_version(
    conn, question_id: int, version: int, payload: QuestionVersionIn
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
            _now(),
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
    db: Database, title: str, pins: list[dict[str, int]], shuffle: bool
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
            " question_order_json) VALUES (?, ?, ?, ?, ?)",
            (
                title,
                _now(),
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
        scheme_id = _insert_scheme(
            conn, exam_id, 1, answer_key, "initial at exam creation"
        )
        return _exam_out(conn, exam_id)


def _insert_scheme(
    conn, exam_id: int, scheme_version: int, answer_key: dict, source: str
) -> int:
    cur = conn.execute(
        "INSERT INTO grading_schemes"
        " (exam_id, scheme_version, answer_key_json, source, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (exam_id, scheme_version, _dumps(answer_key), source, _now()),
    )
    return cur.lastrowid


def _exam_out(conn, exam_id: int) -> dict[str, Any]:
    exam = conn.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
    scheme = conn.execute(
        "SELECT * FROM grading_schemes WHERE exam_id = ?"
        " ORDER BY scheme_version DESC LIMIT 1",
        (exam_id,),
    ).fetchone()
    return {
        "exam_id": exam["id"],
        "title": exam["title"],
        "total_score": exam["total_score"],
        "created_at": exam["created_at"],
        "question_pins": _loads(exam["question_pins_json"]),
        "active_scheme_version": scheme["scheme_version"],
        "active_answer_key": _loads(scheme["answer_key_json"]),
    }


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


# =========================== Attempt / paper ===========================


def _get_or_create_attempt(conn, exam_id: int, candidate_id: int):
    row = conn.execute(
        "SELECT * FROM attempts WHERE exam_id = ? AND candidate_id = ?",
        (exam_id, candidate_id),
    ).fetchone()
    if row is not None:
        return row, False
    exam = conn.execute("SELECT 1 FROM exams WHERE id = ?", (exam_id,)).fetchone()
    if exam is None:
        raise ServiceError(404, f"exam {exam_id} not found")
    cur = conn.execute(
        "INSERT INTO attempts (exam_id, candidate_id, status, answers_json, revision,"
        " created_at) VALUES (?, ?, 'in_progress', '{}', 0, ?)",
        (exam_id, candidate_id, _now()),
    )
    return conn.execute("SELECT * FROM attempts WHERE id = ?", (cur.lastrowid,)).fetchone(), True


def get_paper(db: Database, exam_id: int, candidate_id: int) -> dict[str, Any]:
    """Candidate view: never contains the answer key."""
    with db.write() as conn:
        attempt, _created = _get_or_create_attempt(conn, exam_id, candidate_id)
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
        }


# =========================== Save / submit ===========================


def save_answers(
    db: Database,
    exam_id: int,
    candidate_id: int,
    op_id: str,
    base_revision: int,
    answers: dict[int, list[str]],
) -> dict[str, Any]:
    with db.write() as conn:
        attempt, _ = _get_or_create_attempt(conn, exam_id, candidate_id)

        # Idempotent retry of the same operation id: replay the recorded result.
        existing = conn.execute(
            "SELECT * FROM answer_operations WHERE attempt_id = ? AND op_id = ?",
            (attempt["id"], op_id),
        ).fetchone()
        if existing is not None:
            return _attempt_state(
                conn, attempt, idempotent=True, note="op_id already processed"
            )

        if attempt["status"] == "submitted":
            raise ServiceError(409, "attempt already submitted; answers are locked")
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
            (attempt["id"], op_id, new_revision, _dumps(normalized), _now()),
        )
        fresh = conn.execute(
            "SELECT * FROM attempts WHERE id = ?", (attempt["id"],)
        ).fetchone()
        return _attempt_state(conn, fresh, idempotent=False)


def submit_attempt(
    db: Database, exam_id: int, candidate_id: int, op_id: str
) -> dict[str, Any]:
    with db.write() as conn:
        attempt, _ = _get_or_create_attempt(conn, exam_id, candidate_id)

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
                conn, fresh, idempotent=True, note="submit op_id already processed"
            )

        if attempt["status"] == "submitted":
            raise ServiceError(409, "attempt already submitted")

        submitted_at = _now()
        conn.execute(
            "UPDATE attempts SET status = 'submitted', submitted_at = ? WHERE id = ?",
            (submitted_at, attempt["id"]),
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
            conn, exam_id, candidate_id, trigger="auto at submit"
        )
        state = _attempt_state(conn, fresh, idempotent=False)
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


def _attempt_state(conn, attempt, *, idempotent: bool, note: str | None = None) -> dict[str, Any]:
    return {
        "attempt_id": attempt["id"],
        "exam_id": attempt["exam_id"],
        "status": attempt["status"],
        "revision": attempt["revision"],
        "answers": _loads(attempt["answers_json"]),
        "submitted_at": attempt["submitted_at"],
        "idempotent_replay": idempotent,
        "note": note,
    }


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


def _auto_grade(conn, exam_id: int, candidate_id: int, *, trigger: str) -> dict[str, Any]:
    """Build a NEW pending result version from the active scheme.

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

    scheme = _active_scheme(conn, exam_id)
    answer_key = _loads(scheme["answer_key_json"])
    saved_answers = _loads(attempt["answers_json"])

    next_version_row = conn.execute(
        "SELECT COALESCE(MAX(result_version), 0) + 1 AS v FROM result_versions"
        " WHERE exam_id = ? AND candidate_id = ?",
        (exam_id, candidate_id),
    ).fetchone()
    result_version = next_version_row["v"]
    created_at = _now()

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


def regrade(db: Database, exam_id: int, candidate_id: int, trigger: str = "manual regrade") -> dict[str, Any]:
    with db.write() as conn:
        if conn.execute("SELECT 1 FROM exams WHERE id = ?", (exam_id,)).fetchone() is None:
            raise ServiceError(404, f"exam {exam_id} not found")
        return _auto_grade(conn, exam_id, candidate_id, trigger=trigger)


def edit_answer_key(
    db: Database, exam_id: int, answer_key_items: list[dict], reason: str
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
) -> dict[str, Any]:
    """Apply (score >= 0) or explicitly revoke (score is None) a manual score.

    A new pending result version is produced each time; the score the
    candidate has already seen does not change until that version is
    published.
    """
    with db.write() as conn:
        attempt = conn.execute(
            "SELECT 1 FROM attempts WHERE exam_id = ? AND candidate_id = ?",
            (exam_id, candidate_id),
        ).fetchone()
        if attempt is None:
            raise ServiceError(404, "no attempt for this candidate on this exam")
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
            (exam_id, candidate_id, question_id, score, reason, grader_id, _now()),
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


def list_result_versions(db: Database, exam_id: int, candidate_id: int) -> list[dict[str, Any]]:
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
    db: Database, exam_id: int, candidate_id: int, result_version: int
) -> dict[str, Any]:
    with db.write() as conn:
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
        published_at = _now()
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


def get_published_result(db: Database, exam_id: int, candidate_id: int) -> dict[str, Any]:
    """Candidate-facing: only a published result is visible."""
    with db.read() as conn:
        row = conn.execute(
            "SELECT * FROM result_versions WHERE exam_id = ? AND candidate_id = ?"
            " AND status = 'published'",
            (exam_id, candidate_id),
        ).fetchone()
        if row is None:
            raise ServiceError(404, "no published result for this attempt yet")
        return _result_summary(conn, row)


def get_grading_history(db: Database, exam_id: int, candidate_id: int) -> dict[str, Any]:
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
