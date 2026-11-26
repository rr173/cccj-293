"""Pydantic request/response models."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

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
