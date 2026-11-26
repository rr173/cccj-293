"""FastAPI application: routes, auth wiring and error handling."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from . import service
from .clock import Clock, RealClock
from .db import Database
from .schemas import (
    AnswerKeyEditIn,
    AnswerSaveIn,
    ExamCreateIn,
    ManualScoreIn,
    ProctorActionIn,
    PublishIn,
    QuestionVersionIn,
    SubmitIn,
    TimedConfigIn,
)
from .schema import init_db
from .security import (
    CurrentUser,
    get_current_user_factory,
    require_roles_factory,
)


def create_app(
    db_path: str | Path = "exam.db", clock: Clock | None = None
) -> FastAPI:
    db = Database(db_path)
    init_db(db)
    clock = clock or RealClock()

    app = FastAPI(
        title="Exam Service",
        version="1.1",
        description="Issue -> answer -> grade -> publish, with snapshots,"
        " optimistic revisions, idempotent submit, versioned results and"
        " timed sessions with two-layer pause/resume.",
    )
    authenticate = get_current_user_factory(db)
    require_roles = require_roles_factory(authenticate)
    admin_only = require_roles("admin")
    staff = require_roles("admin", "grader")
    candidate_only = require_roles("candidate")

    @app.exception_handler(service.ServiceError)
    async def _service_error(_request, exc: service.ServiceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    def _resolve_candidate(conn, username: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT id, username, role, display_name FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            raise service.ServiceError(404, f"candidate user '{username}' not found")
        if row["role"] != "candidate":
            raise service.ServiceError(422, f"user '{username}' is not a candidate")
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"]}

    # ---------------- health / identity ----------------

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/server-time")
    def server_time():
        # Explicit server clock; timed attempts also echo it in their timing block.
        return {"now": clock.now_iso()}

    @app.get("/me")
    def me(user: CurrentUser = Depends(authenticate)):
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "display_name": user.display_name,
        }

    # ---------------- Question bank (admin) ----------------

    @app.post("/admin/questions", status_code=201)
    def create_question(
        payload: QuestionVersionIn, _: CurrentUser = Depends(admin_only)
    ):
        return service.create_question(db, payload, clock)

    @app.post("/admin/questions/{question_id}/versions", status_code=201)
    def edit_question(
        question_id: int,
        payload: QuestionVersionIn,
        _: CurrentUser = Depends(admin_only),
    ):
        return service.edit_question(db, question_id, payload, clock)

    @app.get("/admin/questions")
    def list_questions(_: CurrentUser = Depends(staff)):
        return {"questions": service.list_questions(db)}

    # ---------------- Exam issuance (admin) ----------------

    @app.post("/admin/exams", status_code=201)
    def create_exam(payload: ExamCreateIn, _: CurrentUser = Depends(admin_only)):
        pins = [p.model_dump() for p in payload.questions]
        return service.create_exam(db, payload.title, pins, payload.shuffle, clock)

    @app.get("/admin/exams/{exam_id}")
    def get_exam(exam_id: int, _: CurrentUser = Depends(staff)):
        return service.get_exam(db, exam_id)

    # ---------------- Timed sessions & roster (admin) ----------------

    @app.post("/admin/exams/{exam_id}/timed-config", status_code=201)
    def configure_timed_exam(
        exam_id: int,
        payload: TimedConfigIn,
        _: CurrentUser = Depends(admin_only),
    ):
        sessions = [s.model_dump() for s in payload.sessions]
        return service.configure_timed_exam(db, exam_id, sessions, clock)

    @app.post("/admin/exams/{exam_id}/sessions/{session_id}/pause")
    def pause_session(
        exam_id: int,
        session_id: int,
        payload: ProctorActionIn,
        user: CurrentUser = Depends(admin_only),
    ):
        return service.pause_session(
            db, exam_id, session_id, payload.op_id, payload.reason, user.id, clock
        )

    @app.post("/admin/exams/{exam_id}/sessions/{session_id}/resume")
    def resume_session(
        exam_id: int,
        session_id: int,
        payload: ProctorActionIn,
        user: CurrentUser = Depends(admin_only),
    ):
        return service.resume_session(
            db, exam_id, session_id, payload.op_id, payload.reason, user.id, clock
        )

    @app.post("/admin/exams/{exam_id}/candidates/{username}/pause")
    def pause_candidate(
        exam_id: int,
        username: str,
        payload: ProctorActionIn,
        user: CurrentUser = Depends(admin_only),
    ):
        with db.read() as conn:
            candidate = _resolve_candidate(conn, username)
        return service.pause_candidate(
            db, exam_id, candidate["id"], payload.op_id, payload.reason, user.id, clock
        )

    @app.post("/admin/exams/{exam_id}/candidates/{username}/resume")
    def resume_candidate(
        exam_id: int,
        username: str,
        payload: ProctorActionIn,
        user: CurrentUser = Depends(admin_only),
    ):
        with db.read() as conn:
            candidate = _resolve_candidate(conn, username)
        return service.resume_candidate(
            db, exam_id, candidate["id"], payload.op_id, payload.reason, user.id, clock
        )

    @app.get("/admin/exams/{exam_id}/proctor-events")
    def list_proctor_events(exam_id: int, _: CurrentUser = Depends(staff)):
        return service.list_proctor_events(db, exam_id)

    # ---------------- Candidate answering ----------------

    @app.get("/exams/{exam_id}/paper")
    def get_paper(exam_id: int, user: CurrentUser = Depends(candidate_only)):
        return service.get_paper(db, exam_id, user.id, clock)

    @app.post("/exams/{exam_id}/answers")
    def save_answers(
        exam_id: int,
        payload: AnswerSaveIn,
        user: CurrentUser = Depends(candidate_only),
    ):
        return service.save_answers(
            db,
            exam_id,
            user.id,
            payload.op_id,
            payload.base_revision,
            payload.answers,
            clock,
        )

    @app.post("/exams/{exam_id}/submit")
    def submit_attempt(
        exam_id: int,
        payload: SubmitIn,
        user: CurrentUser = Depends(candidate_only),
    ):
        return service.submit_attempt(db, exam_id, user.id, payload.op_id, clock)

    @app.get("/exams/{exam_id}/result")
    def get_my_result(exam_id: int, user: CurrentUser = Depends(candidate_only)):
        return service.get_published_result(db, exam_id, user.id, clock)

    # ---------------- Grading (staff) ----------------

    @app.post("/admin/exams/{exam_id}/answer-key")
    def edit_answer_key(
        exam_id: int,
        payload: AnswerKeyEditIn,
        _: CurrentUser = Depends(admin_only),
    ):
        return service.edit_answer_key(
            db,
            exam_id,
            [item.model_dump() for item in payload.answer_key],
            payload.reason,
            clock,
        )

    @app.post(
        "/grading/exams/{exam_id}/candidates/{username}/regrade", status_code=201
    )
    def regrade(
        exam_id: int,
        username: str,
        user: CurrentUser = Depends(staff),
    ):
        with db.read() as conn:
            candidate = _resolve_candidate(conn, username)
        return service.regrade(
            db, exam_id, candidate["id"], trigger=f"regrade by {user.username}", clock=clock
        )

    @app.put(
        "/grading/exams/{exam_id}/candidates/{username}/questions/{question_id}/manual-score"
    )
    def set_manual_score(
        exam_id: int,
        username: str,
        question_id: int,
        payload: ManualScoreIn,
        user: CurrentUser = Depends(staff),
    ):
        with db.read() as conn:
            candidate = _resolve_candidate(conn, username)
        return service.set_manual_score(
            db,
            exam_id,
            candidate["id"],
            question_id,
            payload.score,
            payload.reason,
            user.id,
            clock,
        )

    @app.get("/grading/exams/{exam_id}/candidates/{username}/results")
    def list_results(
        exam_id: int,
        username: str,
        _: CurrentUser = Depends(staff),
    ):
        with db.read() as conn:
            candidate = _resolve_candidate(conn, username)
        return {
            "candidate": candidate,
            "results": service.list_result_versions(db, exam_id, candidate["id"], clock),
        }

    @app.post("/grading/exams/{exam_id}/candidates/{username}/publish")
    def publish(
        exam_id: int,
        username: str,
        payload: PublishIn,
        _: CurrentUser = Depends(staff),
    ):
        with db.read() as conn:
            candidate = _resolve_candidate(conn, username)
        return service.publish_result(
            db, exam_id, candidate["id"], payload.result_version, clock
        )

    @app.get("/grading/exams/{exam_id}/candidates/{username}/history")
    def grading_history(
        exam_id: int,
        username: str,
        _: CurrentUser = Depends(staff),
    ):
        with db.read() as conn:
            candidate = _resolve_candidate(conn, username)
        return service.get_grading_history(db, exam_id, candidate["id"], clock)

    return app


# EXAM_DB_PATH lets deployments point at an explicit SQLite file.
app = create_app(os.environ.get("EXAM_DB_PATH", "exam.db"))
