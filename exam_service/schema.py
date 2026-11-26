"""DDL, fixed seed data and lightweight additive migrations."""
from __future__ import annotations

from .db import Database
from .security import hash_password

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'grader', 'candidate')),
    display_name  TEXT NOT NULL
);

-- Question bank. A question is edited by appending a new version row;
-- old versions stay forever because issued exams point at them.
CREATE TABLE IF NOT EXISTS questions (
    id         INTEGER PRIMARY KEY,
    current_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS question_versions (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    version     INTEGER NOT NULL,
    qtype       TEXT NOT NULL CHECK (qtype IN ('single', 'multi')),
    stem        TEXT NOT NULL,
    -- options: [{"option_id": "A", "text": "..."}]
    options_json TEXT NOT NULL,
    -- correct option ids, e.g. ["A"] or ["A","C"]
    correct_json TEXT NOT NULL,
    max_score   INTEGER NOT NULL CHECK (max_score > 0),
    created_at  TEXT NOT NULL,
    UNIQUE (question_id, version)
);

-- An issued exam snapshots the exact question version set at generation
-- time. Later question-bank edits never touch these rows.
CREATE TABLE IF NOT EXISTS exams (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    total_score INTEGER NOT NULL CHECK (total_score > 0),
    -- [{"question_id":1,"version":1}]
    question_pins_json TEXT NOT NULL,
    -- display order: [question_id, ...] (stable for reconnects)
    question_order_json TEXT NOT NULL,
    -- Timed exams carry sessions/rosters; classic exams stay untimed.
    timed_mode INTEGER NOT NULL DEFAULT 0,
    default_duration_seconds INTEGER
);

CREATE TABLE IF NOT EXISTS exam_questions (
    id              INTEGER PRIMARY KEY,
    exam_id         INTEGER NOT NULL REFERENCES exams(id),
    question_id     INTEGER NOT NULL,
    version         INTEGER NOT NULL,
    position        INTEGER NOT NULL,
    qtype           TEXT NOT NULL,
    stem            TEXT NOT NULL,
    options_json    TEXT NOT NULL,
    -- per-question option display order, stable for reconnects: ["B","A"]
    option_order_json TEXT NOT NULL,
    max_score       INTEGER NOT NULL,
    UNIQUE (exam_id, question_id)
);

-- A timed exam is split into sessions (场次). Each roster candidate belongs
-- to exactly one session. paused=1 means the whole session is suspended.
CREATE TABLE IF NOT EXISTS exam_sessions (
    id               INTEGER PRIMARY KEY,
    exam_id          INTEGER NOT NULL REFERENCES exams(id),
    name             TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
    paused           INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    UNIQUE (exam_id, name)
);

-- The roster. Only enrolled candidates may open a timed exam.
CREATE TABLE IF NOT EXISTS exam_enrollments (
    id           INTEGER PRIMARY KEY,
    exam_id      INTEGER NOT NULL REFERENCES exams(id),
    session_id   INTEGER NOT NULL REFERENCES exam_sessions(id),
    candidate_id INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    UNIQUE (exam_id, candidate_id)
);

-- A candidate's attempt at one exam.
CREATE TABLE IF NOT EXISTS attempts (
    id            INTEGER PRIMARY KEY,
    exam_id       INTEGER NOT NULL REFERENCES exams(id),
    candidate_id  INTEGER NOT NULL REFERENCES users(id),
    status        TEXT NOT NULL CHECK (status IN ('in_progress', 'submitted')),
    -- current saved answers: {question_id: [option_id,...]}
    answers_json  TEXT NOT NULL DEFAULT '{}',
    revision      INTEGER NOT NULL DEFAULT 0,
    submitted_at  TEXT,
    created_at    TEXT NOT NULL,
    -- Timed-attempt bookkeeping. NULL/0 means a classic untimed attempt.
    timed_mode            INTEGER NOT NULL DEFAULT 0,
    duration_seconds      INTEGER,
    session_id            INTEGER REFERENCES exam_sessions(id),
    -- Server time of the first successful paper open.
    started_at            TEXT,
    -- Set when the time limit consumed the attempt (submitted_at == cutoff).
    auto_submitted_at     TEXT,
    -- Server time at which the pending result version was generated.
    graded_at             TEXT,
    -- Candidate-level suspension flag (session suspension lives on exam_sessions).
    candidate_paused      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (exam_id, candidate_id)
);

-- Every save / submit call is recorded with the caller's operation id.
-- The unique index makes caller retries idempotent (no second submit,
-- no duplicated save history). Automatic time-limit submissions are
-- recorded as kind='submit' with a server-generated op_id.
CREATE TABLE IF NOT EXISTS answer_operations (
    id           INTEGER PRIMARY KEY,
    attempt_id   INTEGER NOT NULL REFERENCES attempts(id),
    op_id        TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('save', 'submit')),
    revision     INTEGER NOT NULL,
    answers_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (attempt_id, op_id)
);

-- Append-only audit of proctor suspension actions. The unique (exam_id,
-- op_id) index makes pause/resume retries idempotent exam-wide.
CREATE TABLE IF NOT EXISTS proctor_events (
    id           INTEGER PRIMARY KEY,
    exam_id      INTEGER NOT NULL REFERENCES exams(id),
    scope        TEXT NOT NULL CHECK (scope IN ('session', 'candidate')),
    session_id   INTEGER,
    candidate_id INTEGER,
    action       TEXT NOT NULL CHECK (action IN ('pause', 'resume')),
    op_id        TEXT NOT NULL,
    reason       TEXT,
    actor_id     INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    UNIQUE (exam_id, op_id)
);

-- Wall-clock intervals during which an attempt's clock was stopped. One row
-- per suspension layer; overlapping session/candidate intervals are merged
-- when elapsed time is computed, so an overlap stops the clock only once.
CREATE TABLE IF NOT EXISTS attempt_pause_intervals (
    id              INTEGER PRIMARY KEY,
    attempt_id      INTEGER NOT NULL REFERENCES attempts(id),
    source          TEXT NOT NULL CHECK (source IN ('session', 'candidate')),
    pause_event_id  INTEGER NOT NULL REFERENCES proctor_events(id),
    pause_at        TEXT NOT NULL,
    resume_event_id INTEGER REFERENCES proctor_events(id),
    resume_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_pause_intervals_attempt
    ON attempt_pause_intervals(attempt_id);

-- Scoring schemes. The active one for an exam is the highest version.
-- Editing the answer key creates a new scheme; exam content and max scores
-- stay untouched.
CREATE TABLE IF NOT EXISTS grading_schemes (
    id           INTEGER PRIMARY KEY,
    exam_id      INTEGER NOT NULL REFERENCES exams(id),
    scheme_version INTEGER NOT NULL,
    -- {question_id: [correct option_id,...]}
    answer_key_json TEXT NOT NULL,
    source       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (exam_id, scheme_version)
);

-- One row per (published-or-pending result version, question).
CREATE TABLE IF NOT EXISTS result_versions (
    id              INTEGER PRIMARY KEY,
    exam_id         INTEGER NOT NULL REFERENCES exams(id),
    candidate_id    INTEGER NOT NULL REFERENCES users(id),
    result_version  INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'published', 'superseded')),
    total_score     INTEGER NOT NULL,
    scheme_id       INTEGER NOT NULL REFERENCES grading_schemes(id),
    created_at      TEXT NOT NULL,
    published_at    TEXT,
    UNIQUE (exam_id, candidate_id, result_version)
);

CREATE TABLE IF NOT EXISTS result_items (
    id               INTEGER PRIMARY KEY,
    result_version_id INTEGER NOT NULL REFERENCES result_versions(id),
    question_id      INTEGER NOT NULL,
    auto_score       INTEGER NOT NULL,
    -- NULL means no effective manual score; manual actions themselves are
    -- kept forever in manual_score_actions.
    manual_score     INTEGER,
    manual_reason    TEXT,
    effective_score  INTEGER NOT NULL
);

-- Full history of manual grading actions, including explicit revocations.
-- Re-scoring can never silently overwrite an effective manual score.
CREATE TABLE IF NOT EXISTS manual_score_actions (
    id           INTEGER PRIMARY KEY,
    exam_id      INTEGER NOT NULL REFERENCES exams(id),
    candidate_id INTEGER NOT NULL REFERENCES users(id),
    question_id  INTEGER NOT NULL,
    -- NULL score means the manual score was explicitly revoked
    score        INTEGER,
    reason       TEXT,
    grader_id    INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL
);

-- Append-only history of automatic scoring runs (regrades / retries).
CREATE TABLE IF NOT EXISTS grading_runs (
    id           INTEGER PRIMARY KEY,
    exam_id      INTEGER NOT NULL REFERENCES exams(id),
    candidate_id INTEGER NOT NULL REFERENCES users(id),
    scheme_id    INTEGER NOT NULL REFERENCES grading_schemes(id),
    trigger      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

# Additive migrations for databases created before timed sessions existed.
_ADDED_COLUMNS = {
    "exams": [
        ("timed_mode", "INTEGER NOT NULL DEFAULT 0"),
        ("default_duration_seconds", "INTEGER"),
    ],
    "attempts": [
        ("timed_mode", "INTEGER NOT NULL DEFAULT 0"),
        ("duration_seconds", "INTEGER"),
        ("session_id", "INTEGER REFERENCES exam_sessions(id)"),
        ("started_at", "TEXT"),
        ("auto_submitted_at", "TEXT"),
        ("graded_at", "TEXT"),
        ("candidate_paused", "INTEGER NOT NULL DEFAULT 0"),
    ],
}

# Minimal credentials for the demo. Passwords are PBKDF2-hashed at seed time.
SEED_USERS = [
    ("admin", "admin-pass-123", "admin", "题库管理员"),
    ("grader", "grader-pass-123", "grader", "阅卷员"),
    ("candidate", "candidate-pass-123", "candidate", "考生小明"),
]


def _existing_columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db(db: Database) -> None:
    with db.write() as conn:
        conn.executescript(SCHEMA_SQL)
        for table, columns in _ADDED_COLUMNS.items():
            present = _existing_columns(conn, table)
            for name, ddl in columns:
                if name not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        for username, password, role, display_name in SEED_USERS:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, display_name)"
                    " VALUES (?, ?, ?, ?)",
                    (username, hash_password(password), role, display_name),
                )
