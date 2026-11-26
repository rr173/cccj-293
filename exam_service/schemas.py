"""Pydantic request/response models."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------- Question bank ----------


class OptionIn(BaseModel):
    option_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class QuestionVersionIn(BaseModel):
    qtype: Literal["single", "multi"]
    stem: str = Field(min_length=1)
    options: list[OptionIn]
    correct_option_ids: list[str] = Field(min_length=1)
    max_score: int = Field(gt=0)

    @field_validator("options")
    @classmethod
    def _unique_option_ids(cls, options: list[OptionIn]) -> list[OptionIn]:
        ids = [o.option_id for o in options]
        if len(ids) != len(set(ids)):
            raise ValueError("option_id values must be unique within a question")
        return options

    @field_validator("correct_option_ids")
    @classmethod
    def _correct_subset(cls, correct: list[str]) -> list[str]:
        if len(correct) != len(set(correct)):
            raise ValueError("correct_option_ids must not contain duplicates")
        return correct


class QuestionOut(BaseModel):
    question_id: int
    current_version: int
    version: int
    qtype: str
    stem: str
    options: list[OptionIn]
    correct_option_ids: list[str]
    max_score: int


# ---------- Exam generation ----------


class ExamQuestionPin(BaseModel):
    question_id: int
    version: int


class ExamCreateIn(BaseModel):
    title: str = Field(min_length=1)
    questions: list[ExamQuestionPin] = Field(min_length=1)
    shuffle: bool = True


# ---------- Timed sessions / roster ----------


class SessionConfigIn(BaseModel):
    name: str = Field(min_length=1)
    duration_seconds: int = Field(gt=0)
    candidate_usernames: list[str] = Field(min_length=1)


class TimedConfigIn(BaseModel):
    # One entry per session; every candidate belongs to exactly one session.
    sessions: list[SessionConfigIn] = Field(min_length=1)

    @field_validator("sessions")
    @classmethod
    def _unique_session_names_and_roster(cls, sessions: list[SessionConfigIn]):
        names = [s.name for s in sessions]
        if len(names) != len(set(names)):
            raise ValueError("session names must be unique")
        all_candidates = [u for s in sessions for u in s.candidate_usernames]
        if len(all_candidates) != len(set(all_candidates)):
            raise ValueError("a candidate may belong to only one session")
        if any(not u for u in all_candidates):
            raise ValueError("candidate username must not be empty")
        return sessions


class ProctorActionIn(BaseModel):
    op_id: str = Field(min_length=1, description="caller operation id (idempotency key)")
    reason: Optional[str] = None


# ---------- Makeup reservations (补考分场预约) ----------


class MakeupSessionIn(BaseModel):
    name: str = Field(min_length=1)
    duration_seconds: int = Field(gt=0)
    capacity: int = Field(gt=0, description="seat count (名额) of this makeup session")


class MakeupCreateIn(BaseModel):
    op_id: Optional[str] = Field(default=None, min_length=1)
    basis_exam_id: int = Field(gt=0, description="exam whose published totals decide eligibility")
    pass_score: int = Field(gt=0, description="eligible iff published total < pass_score")
    invite_ttl_seconds: int = Field(gt=0, description="confirmation deadline of each invitation")
    sessions: list[MakeupSessionIn] = Field(min_length=1)

    @field_validator("sessions")
    @classmethod
    def _unique_session_names(cls, sessions: list[MakeupSessionIn]):
        names = [s.name for s in sessions]
        if len(names) != len(set(names)):
            raise ValueError("session names must be unique")
        return sessions


class MakeupApplyIn(BaseModel):
    op_id: Optional[str] = Field(default=None, min_length=1)
    session_id: int = Field(gt=0)


class MakeupOpIn(BaseModel):
    """confirm / withdraw / unsubscribe carry only the idempotency key."""

    op_id: Optional[str] = Field(default=None, min_length=1)


# ---------- Answering ----------


class AnswerSaveIn(BaseModel):
    op_id: str = Field(min_length=1, description="caller operation id (idempotency key)")
    base_revision: int = Field(ge=0)
    answers: dict[int, list[str]] = Field(
        description="question_id -> selected stable option ids"
    )


class SubmitIn(BaseModel):
    op_id: str = Field(min_length=1, description="caller operation id (idempotency key)")


# ---------- Grading ----------


class AnswerKeyItem(BaseModel):
    question_id: int
    correct_option_ids: list[str] = Field(min_length=1)


class AnswerKeyEditIn(BaseModel):
    answer_key: list[AnswerKeyItem]
    reason: str = Field(min_length=1)


class ManualScoreIn(BaseModel):
    score: Optional[int] = Field(
        default=None, ge=0, description="null (or omitted) revokes the manual score"
    )
    reason: Optional[str] = None


class PublishIn(BaseModel):
    result_version: int = Field(gt=0)


# ---------- Score appeals ----------
#
# The documented fields are the first name in each extractor. The extra aliases
# keep callers from having to care whether a client spells the domain concept
# result_version/version, decision/conclusion, or new_score/score.


class _PermissiveIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    def first_value(self, *names: str) -> Any:
        dumped = self.model_dump(exclude_unset=False)
        for name in names:
            if name in dumped and dumped[name] is not None:
                return dumped[name]
        # pydantic extras are present in model_extra, while model_dump includes
        # them in v2; this fallback makes the helper robust across settings.
        extra = getattr(self, "model_extra", None) or {}
        for name in names:
            if name in extra and extra[name] is not None:
                return extra[name]
        return None


class AppealCreateIn(_PermissiveIn):
    op_id: Optional[str] = Field(default=None, min_length=1)
    exam_id: Optional[int] = Field(default=None, gt=0)
    result_version: Optional[int] = Field(default=None, gt=0)
    result_version_id: Optional[int] = Field(default=None, gt=0)
    version: Optional[int] = Field(default=None, gt=0)
    score_version: Optional[int] = Field(default=None, gt=0)
    question_id: Optional[int] = Field(default=None, gt=0)
    question: Optional[int] = Field(default=None, gt=0)
    qid: Optional[int] = Field(default=None, gt=0)
    reason: Optional[str] = Field(default=None, min_length=1)
    rationale: Optional[str] = Field(default=None, min_length=1)
    details: Optional[str] = None
    comment: Optional[str] = None

    def version_value(self) -> int:
        value = self.first_value(
            "result_version",
            "result_version_id",
            "score_version",
            "score_version_id",
            "published_result_version",
            "published_version",
            "version",
        )
        if value is None:
            raise ValueError("result_version is required")
        return int(value)

    def optional_version_value(self) -> Optional[int]:
        if self.first_value(
            "result_version",
            "result_version_id",
            "score_version",
            "score_version_id",
            "published_result_version",
            "published_version",
            "version",
        ) is None:
            return None
        return self.version_value()

    def question_id_value(self) -> int:
        value = self.first_value("question_id", "question", "qid")
        if value is None:
            raise ValueError("question_id is required")
        return int(value)

    def reason_value(self) -> str:
        value = self.first_value("reason", "rationale", "details", "comment")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reason is required")
        return value


class AppealAssignIn(_PermissiveIn):
    op_id: Optional[str] = Field(default=None, min_length=1)
    grader_usernames: Optional[list[Any]] = None
    reviewer_usernames: Optional[list[Any]] = None
    usernames: Optional[list[Any]] = None
    reviewers: Optional[list[Any]] = None
    assignees: Optional[list[Any]] = None
    graders: Optional[list[Any]] = None
    reviewer_ids: Optional[list[Any]] = None
    grader_ids: Optional[list[Any]] = None
    grader_a: Optional[str | int] = None
    grader_b: Optional[str | int] = None
    first_grader: Optional[str | int] = None
    second_grader: Optional[str | int] = None

    @staticmethod
    def _ref_to_string(item: Any) -> str:
        if isinstance(item, dict):
            ident = (
                item.get("username")
                or item.get("grader_username")
                or item.get("reviewer_username")
                or item.get("id")
                or item.get("user_id")
            )
            return str(ident)
        return str(item)

    def username_list(self) -> list[str]:
        for name in (
            "grader_usernames",
            "reviewer_usernames",
            "usernames",
            "reviewers",
            "assignees",
            "assigned_graders",
            "grader_list",
            "graders",
        ):
            value = self.first_value(name)
            if isinstance(value, list) and value:
                return [self._ref_to_string(v) for v in value]
        pair = [
            self.first_value(
                "grader_a",
                "first_grader",
                "grader1",
                "grader1_username",
                "grader1_id",
                "first_grader_id",
                "reviewer1",
                "reviewer1_username",
                "reviewer1_id",
            ),
            self.first_value(
                "grader_b",
                "second_grader",
                "grader2",
                "grader2_username",
                "grader2_id",
                "second_grader_id",
                "reviewer2",
                "reviewer2_username",
                "reviewer2_id",
            ),
        ]
        if all(v is not None for v in pair):
            return [str(v) for v in pair]
        return []

    def id_list(self) -> list[int]:
        value = self.first_value("grader_ids", "reviewer_ids")
        return [int(v) for v in value] if value is not None else []


class AppealDecisionIn(_PermissiveIn):
    op_id: Optional[str] = Field(default=None, min_length=1)
    decision: Optional[str] = None
    conclusion: Optional[str] = None
    verdict: Optional[str] = None
    outcome: Optional[str] = None
    action: Optional[str] = None
    new_score: Optional[int] = Field(default=None, ge=0)
    score: Optional[int] = Field(default=None, ge=0)
    changed_score: Optional[int] = Field(default=None, ge=0)
    final_score: Optional[int] = Field(default=None, ge=0)
    reason: Optional[str] = None
    opinion: Optional[str] = None
    comment: Optional[str] = None
    rationale: Optional[str] = None
    explanation: Optional[str] = None

    def decision_value(self) -> str:
        raw = self.first_value(
            "decision", "conclusion", "verdict", "outcome", "action"
        )
        if raw is None:
            raise ValueError("decision is required")
        text = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
        if "驳回" in text or "维持原" in text:
            return "rejected"
        if "改分" in text or "改判" in text or "更正" in text or "支持申诉" in text:
            return "changed"
        rejected = {
            "reject",
            "rejected",
            "dismiss",
            "dismissed",
            "uphold",
            "uphold_original",
            "uphold_score",
            "maintain_score",
            "original_score",
            "keep_score",
            "no_change",
            "unchanged",
        }
        changed = {
            "change",
            "changed",
            "change_score",
            "score_change",
            "adjust",
            "adjusted",
            "adjust_score",
            "correct",
            "corrected",
            "correction",
            "correct_score",
            "uphold_appeal",
            "approve",
            "approved",
            "allow",
            "allowed",
            "grant",
            "granted",
            "sustain",
            "sustained",
            "supported",
        }
        if text in rejected:
            return "rejected"
        if text in changed:
            return "changed"
        if any(token in text for token in ("reject", "dismiss", "deny", "keep", "maintain", "original")):
            return "rejected"
        if "uphold" in text and "appeal" not in text:
            return "rejected"
        if any(
            token in text
            for token in (
                "change",
                "adjust",
                "correct",
                "grant",
                "approv",
                "sustain",
                "support",
                "increase",
                "decrease",
            )
        ):
            return "changed"
        # A score without an explicit action is unambiguously a change.
        if self.score_value() is not None:
            return "changed"
        raise ValueError(f"unsupported appeal decision: {raw}")

    def score_value(self) -> Optional[int]:
        value = self.first_value(
            "new_score",
            "changed_score",
            "revised_score",
            "adjusted_score",
            "corrected_score",
            "target_score",
            "final_score",
            "score",
        )
        return None if value is None else int(value)

    def reason_value(self) -> str:
        value = self.first_value(
            "reason", "opinion", "comment", "rationale", "explanation"
        )
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reason is required")
        return value
