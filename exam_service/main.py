"""FastAPI application: routes, auth wiring and error handling."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import service
from .clock import Clock, RealClock
from .db import Database
from .schemas import (
    AnswerKeyEditIn,
    AnswerSaveIn,
    AppealAssignIn,
    AppealCreateIn,
    AppealDecisionIn,
    ExamCreateIn,
    MakeupApplyIn,
    MakeupCreateIn,
    MakeupOpIn,
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
        version="1.2",
        description="Issue -> answer -> grade -> publish, with snapshots,"
        " optimistic revisions, idempotent submit, versioned results,"
        " timed sessions with two-layer pause/resume, and makeup-session"
        " reservations with waitlist, invitations and seat switches.",
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

    def _op_id(payload: Any, request: Request) -> str:
        value = getattr(payload, "op_id", None)
        if not value:
            value = request.headers.get("Idempotency-Key") or request.headers.get("X-Op-Id")
        if not value or not str(value).strip():
            raise service.ServiceError(
                422, "op_id is required in the JSON body or Idempotency-Key header"
            )
        return str(value)

    def _decision_fields(payload: AppealDecisionIn):
        try:
            return payload.decision_value(), payload.score_value(), payload.reason_value()
        except ValueError as exc:
            raise service.ServiceError(422, str(exc))

    def _check_appeal_exam(appeal_id: int, exam_id: int | None, user: CurrentUser):
        if exam_id is None:
            return
        current = service.get_appeal_for_staff(db, appeal_id, user.id, user.role)
        if current["exam_id"] != exam_id:
            raise service.ServiceError(404, f"appeal {appeal_id} not found in exam {exam_id}")

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

    # ---------------- Makeup reservations (补考分场预约) ----------------

    @app.post("/admin/exams/{exam_id}/makeup", status_code=201)
    def create_makeup_campaign(
        exam_id: int,
        payload: MakeupCreateIn,
        request: Request,
        user: CurrentUser = Depends(admin_only),
    ):
        body = service.create_makeup_campaign(
            db,
            exam_id,
            _op_id(payload, request),
            payload.basis_exam_id,
            payload.pass_score,
            payload.invite_ttl_seconds,
            [s.model_dump() for s in payload.sessions],
            user.id,
            clock,
        )
        return JSONResponse(
            status_code=200 if body.get("idempotent_replay") else 201,
            content=body,
        )

    @app.get("/admin/exams/{exam_id}/makeup")
    def get_makeup_campaign_staff(exam_id: int, _: CurrentUser = Depends(staff)):
        return service.get_makeup_for_staff(db, exam_id, clock)

    @app.get("/exams/{exam_id}/makeup")
    def get_my_makeup(exam_id: int, user: CurrentUser = Depends(candidate_only)):
        return service.get_my_makeup(db, exam_id, user.id, clock)

    @app.post("/exams/{exam_id}/makeup/applications", status_code=201)
    def apply_makeup(
        exam_id: int,
        payload: MakeupApplyIn,
        request: Request,
        user: CurrentUser = Depends(candidate_only),
    ):
        body = service.apply_makeup(
            db, exam_id, user.id, _op_id(payload, request), payload.session_id, clock
        )
        return JSONResponse(
            status_code=200 if body.get("idempotent_replay") else 201,
            content=body,
        )

    @app.post("/exams/{exam_id}/makeup/invitations/{invitation_id}/confirm")
    def confirm_makeup_invitation(
        exam_id: int,
        invitation_id: int,
        payload: MakeupOpIn,
        request: Request,
        user: CurrentUser = Depends(candidate_only),
    ):
        return service.confirm_makeup_invitation(
            db, exam_id, invitation_id, user.id, _op_id(payload, request), clock
        )

    @app.post("/exams/{exam_id}/makeup/applications/{application_id}/withdraw")
    def withdraw_makeup_application(
        exam_id: int,
        application_id: int,
        payload: MakeupOpIn,
        request: Request,
        user: CurrentUser = Depends(candidate_only),
    ):
        return service.withdraw_makeup_application(
            db, exam_id, application_id, user.id, _op_id(payload, request), clock
        )

    @app.post("/exams/{exam_id}/makeup/unsubscribe")
    def unsubscribe_makeup(
        exam_id: int,
        payload: MakeupOpIn,
        request: Request,
        user: CurrentUser = Depends(candidate_only),
    ):
        return service.unsubscribe_makeup(
            db, exam_id, user.id, _op_id(payload, request), clock
        )

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

    # ---------------- Candidate score appeals ----------------

    @app.post("/exams/{exam_id}/appeals", status_code=201)
    @app.post("/exams/{exam_id}/appeal", status_code=201)
    @app.post("/exams/{exam_id}/result/appeals", status_code=201)
    @app.post("/exams/{exam_id}/result/appeal", status_code=201)
    def create_my_appeal(
        exam_id: int,
        payload: AppealCreateIn,
        request: Request,
        user: CurrentUser = Depends(candidate_only),
    ):
        try:
            result_version = payload.optional_version_value()
            question_id = payload.question_id_value()
            reason = payload.reason_value()
        except ValueError as exc:
            raise service.ServiceError(422, str(exc))
        op_id = _op_id(payload, request)
        body = service.create_appeal(
            db,
            exam_id,
            user.id,
            op_id,
            result_version,
            question_id,
            reason,
            clock,
        )
        return JSONResponse(
            status_code=200 if body.get("idempotent_replay") else 201,
            content=body,
        )

    @app.post("/exams/{exam_id}/results/{result_version}/appeals", status_code=201)
    def create_my_appeal_for_version(
        exam_id: int,
        result_version: int,
        payload: AppealCreateIn,
        request: Request,
        user: CurrentUser = Depends(candidate_only),
    ):
        try:
            question_id = payload.question_id_value()
            reason = payload.reason_value()
        except ValueError as exc:
            raise service.ServiceError(422, str(exc))
        body = service.create_appeal(
            db,
            exam_id,
            user.id,
            _op_id(payload, request),
            result_version,
            question_id,
            reason,
            clock,
        )
        return JSONResponse(
            status_code=200 if body.get("idempotent_replay") else 201,
            content=body,
        )

    @app.post(
        "/exams/{exam_id}/results/{result_version}/questions/{question_id}/appeal",
        status_code=201,
    )
    def create_my_question_appeal(
        exam_id: int,
        result_version: int,
        question_id: int,
        payload: AppealCreateIn,
        request: Request,
        user: CurrentUser = Depends(candidate_only),
    ):
        try:
            reason = payload.reason_value()
        except ValueError as exc:
            raise service.ServiceError(422, str(exc))
        body = service.create_appeal(
            db,
            exam_id,
            user.id,
            _op_id(payload, request),
            result_version,
            question_id,
            reason,
            clock,
        )
        return JSONResponse(
            status_code=200 if body.get("idempotent_replay") else 201,
            content=body,
        )

    @app.get("/exams/{exam_id}/appeals")
    def list_my_exam_appeals(
        exam_id: int, user: CurrentUser = Depends(candidate_only)
    ):
        return service.list_candidate_appeals(db, exam_id, user.id)

    @app.get("/exams/{exam_id}/appeals/{appeal_id}")
    def get_my_exam_appeal(
        exam_id: int,
        appeal_id: int,
        user: CurrentUser = Depends(candidate_only),
    ):
        return service.get_appeal_for_candidate(db, exam_id, appeal_id, user.id)

    @app.get("/my/appeals")
    def list_all_my_appeals(user: CurrentUser = Depends(candidate_only)):
        return service.list_candidate_appeals(db, None, user.id)

    @app.get("/appeals/mine")
    def list_all_my_appeals_alias(user: CurrentUser = Depends(candidate_only)):
        return service.list_candidate_appeals(db, None, user.id)

    @app.post("/appeals", status_code=201)
    def create_my_appeal_top_level(
        payload: AppealCreateIn,
        request: Request,
        user: CurrentUser = Depends(candidate_only),
    ):
        exam_id = payload.exam_id or payload.first_value("exam", "examId")
        if exam_id is None:
            raise service.ServiceError(422, "exam_id is required")
        try:
            result_version = payload.optional_version_value()
            question_id = payload.question_id_value()
            reason = payload.reason_value()
        except ValueError as exc:
            raise service.ServiceError(422, str(exc))
        body = service.create_appeal(
            db,
            int(exam_id),
            user.id,
            _op_id(payload, request),
            result_version,
            question_id,
            reason,
            clock,
        )
        return JSONResponse(
            status_code=200 if body.get("idempotent_replay") else 201,
            content=body,
        )

    @app.get("/appeals/{appeal_id}")
    def get_appeal_role_aware(
        appeal_id: int,
        user: CurrentUser = Depends(authenticate),
    ):
        if user.role == "candidate":
            return service.get_my_appeal(db, appeal_id, user.id)
        return service.get_appeal_for_staff(db, appeal_id, user.id, user.role)

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

    # ---------------- Score appeals: staff workflow ----------------

    @app.get("/grading/appeals")
    def list_all_appeals(
        username: str | None = None,
        exam_id: int | None = None,
        status: str | None = None,
        mine: bool = False,
        user: CurrentUser = Depends(staff),
    ):
        candidate_id = None
        if username is not None:
            with db.read() as conn:
                candidate_id = _resolve_candidate(conn, username)["id"]
        return service.list_staff_appeals(
            db,
            user.id,
            user.role,
            exam_id=exam_id,
            candidate_id=candidate_id,
            status=status,
            assigned_to_me=mine and user.role == "grader",
        )

    @app.get("/admin/appeals")
    def list_all_appeals_admin(
        username: str | None = None,
        exam_id: int | None = None,
        status: str | None = None,
        user: CurrentUser = Depends(admin_only),
    ):
        candidate_id = None
        if username is not None:
            with db.read() as conn:
                candidate_id = _resolve_candidate(conn, username)["id"]
        return service.list_staff_appeals(
            db,
            user.id,
            user.role,
            exam_id=exam_id,
            candidate_id=candidate_id,
            status=status,
        )

    @app.get("/grading/exams/{exam_id}/appeals")
    def list_exam_appeals(
        exam_id: int,
        status: str | None = None,
        user: CurrentUser = Depends(staff),
    ):
        return service.list_staff_appeals(
            db, user.id, user.role, exam_id=exam_id, status=status
        )

    @app.get("/grading/exams/{exam_id}/candidates/{username}/appeals")
    def list_candidate_appeals_staff(
        exam_id: int,
        username: str,
        user: CurrentUser = Depends(staff),
    ):
        with db.read() as conn:
            candidate = _resolve_candidate(conn, username)
        return service.list_staff_appeals(
            db,
            user.id,
            user.role,
            exam_id=exam_id,
            candidate_id=candidate["id"],
        )

    @app.get("/grading/appeals/{appeal_id}")
    @app.get("/admin/appeals/{appeal_id}")
    def get_staff_appeal(
        appeal_id: int, user: CurrentUser = Depends(staff)
    ):
        # /admin/appeals/{id} is also reachable by graders for their assigned
        # review; output sealing is handled in the service serializer.
        return service.get_appeal_for_staff(db, appeal_id, user.id, user.role)

    @app.post("/admin/appeals/{appeal_id}/assign", status_code=200)
    @app.post("/admin/appeals/{appeal_id}/reviewers", status_code=200)
    @app.post("/admin/appeals/{appeal_id}/assignment", status_code=200)
    @app.put("/admin/appeals/{appeal_id}/assignment", status_code=200)
    @app.post("/appeals/{appeal_id}/assign", status_code=200)
    @app.post("/grading/appeals/{appeal_id}/assign", status_code=200)
    @app.post("/grading/appeals/{appeal_id}/assignment", status_code=200)
    @app.post("/admin/exams/{exam_id}/appeals/{appeal_id}/assign", status_code=200)
    def assign_appeal(
        appeal_id: int,
        payload: AppealAssignIn,
        request: Request,
        exam_id: int | None = None,
        user: CurrentUser = Depends(admin_only),
    ):
        _check_appeal_exam(appeal_id, exam_id, user)
        refs = payload.username_list()
        ids = payload.id_list()
        return service.assign_appeal(
            db,
            appeal_id,
            _op_id(payload, request),
            refs,
            ids,
            user.id,
            clock,
        )

    @app.post("/grading/appeals/{appeal_id}/opinions", status_code=200)
    @app.post("/appeals/{appeal_id}/opinions", status_code=200)
    @app.post("/grading/appeals/{appeal_id}/opinion", status_code=200)
    @app.post("/grading/appeals/{appeal_id}/reviews", status_code=200)
    @app.post("/appeals/{appeal_id}/reviews", status_code=200)
    @app.post("/grader/appeals/{appeal_id}/opinions", status_code=200)
    def submit_appeal_opinion(
        appeal_id: int,
        payload: AppealDecisionIn,
        request: Request,
        user: CurrentUser = Depends(require_roles("grader", "admin")),
    ):
        decision, score, reason = _decision_fields(payload)
        return service.submit_appeal_opinion(
            db,
            appeal_id,
            user.id,
            _op_id(payload, request),
            decision,
            score,
            reason,
            clock,
        )

    @app.post("/admin/appeals/{appeal_id}/arbitrate", status_code=200)
    @app.post("/admin/appeals/{appeal_id}/decision", status_code=200)
    @app.post("/admin/appeals/{appeal_id}/ruling", status_code=200)
    @app.post("/appeals/{appeal_id}/arbitrate", status_code=200)
    @app.post("/admin/appeals/{appeal_id}/arbitration", status_code=200)
    @app.post("/grading/appeals/{appeal_id}/arbitrate", status_code=200)
    @app.post("/grading/appeals/{appeal_id}/decision", status_code=200)
    @app.post("/admin/exams/{exam_id}/appeals/{appeal_id}/arbitrate", status_code=200)
    def arbitrate_appeal(
        appeal_id: int,
        payload: AppealDecisionIn,
        request: Request,
        exam_id: int | None = None,
        user: CurrentUser = Depends(admin_only),
    ):
        _check_appeal_exam(appeal_id, exam_id, user)
        decision, score, reason = _decision_fields(payload)
        return service.arbitrate_appeal(
            db,
            appeal_id,
            user.id,
            _op_id(payload, request),
            decision,
            score,
            reason,
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
        user: CurrentUser = Depends(staff),
    ):
        with db.read() as conn:
            candidate = _resolve_candidate(conn, username)
        return service.get_grading_history(
            db,
            exam_id,
            candidate["id"],
            clock,
            viewer_id=user.id,
            viewer_role=user.role,
        )

    return app


# EXAM_DB_PATH lets deployments point at an explicit SQLite file.
app = create_app(os.environ.get("EXAM_DB_PATH", "exam.db"))
