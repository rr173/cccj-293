"""End-to-end HTTP tests for the exam service.

Covers the required demo flow and the tricky invariants:
  * snapshot stability after question-bank edits
  * save/submit interleaving with optimistic revisions (real threads)
  * idempotent submit retry (no second submission)
  * answer-key correction + regrade without touching the issued paper
  * manual score priority preserved across regrades, plus explicit revoke
  * publishing a stale candidate result version is rejected (409)
  * candidates only see published results; old versions remain for audit
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from exam_service.main import create_app


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "test_exam.db")
    with TestClient(app) as c:
        yield c


ADMIN = ("admin", "admin-pass-123")
GRADER = ("grader", "grader-pass-123")
CANDIDATE = ("candidate", "candidate-pass-123")


@pytest.fixture()
def two_question_exam(client):
    """Q1 single choice (5 pts), Q2 multi choice (10 pts), one exam."""
    q1 = client.post(
        "/admin/questions",
        auth=ADMIN,
        json={
            "qtype": "single",
            "stem": "单题：下列哪个是 Python 的 Web 框架？",
            "options": [
                {"option_id": "A", "text": "FastAPI"},
                {"option_id": "B", "text": "Django"},
                {"option_id": "C", "text": "Flask"},
                {"option_id": "D", "text": "Requests"},
            ],
            "correct_option_ids": ["A"],
            "max_score": 5,
        },
    ).json()
    q2 = client.post(
        "/admin/questions",
        auth=ADMIN,
        json={
            "qtype": "multi",
            "stem": "多题：以下哪些语言支持静态类型标注？",
            "options": [
                {"option_id": "X", "text": "Python"},
                {"option_id": "Y", "text": "Java"},
                {"option_id": "Z", "text": "HTML"},
                {"option_id": "W", "text": "Go"},
            ],
            "correct_option_ids": ["X", "Y", "W"],
            "max_score": 10,
        },
    ).json()
    exam = client.post(
        "/admin/exams",
        auth=ADMIN,
        json={
            "title": "2026 秋季测评",
            "questions": [
                {"question_id": q1["question_id"], "version": q1["version"]},
                {"question_id": q2["question_id"], "version": q2["version"]},
            ],
            "shuffle": True,
        },
    ).json()
    return exam, q1, q2


# ---------------------------------------------------------------- auth / RBAC


def test_health_and_basic_auth(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/me").status_code == 401
    me = client.get("/me", auth=CANDIDATE).json()
    assert me == {
        "id": me["id"],
        "username": "candidate",
        "role": "candidate",
        "display_name": "考生小明",
    }
    # wrong password
    assert client.get("/me", auth=("candidate", "nope")).status_code == 401


def test_role_boundaries(client, two_question_exam):
    exam, _, _ = two_question_exam
    # candidate cannot touch admin endpoints
    r = client.post(
        "/admin/exams", auth=CANDIDATE, json={"title": "x", "questions": []}
    )
    assert r.status_code == 403
    # admin cannot act as a candidate
    r = client.get(f"/exams/{exam['exam_id']}/paper", auth=ADMIN)
    assert r.status_code == 403
    # grader can read staff views but not create questions
    assert client.get("/admin/questions", auth=GRADER).status_code == 200
    r = client.post(
        "/admin/questions",
        auth=GRADER,
        json={
            "qtype": "single",
            "stem": "s",
            "options": [{"option_id": "A", "text": "a"}],
            "correct_option_ids": ["A"],
            "max_score": 1,
        },
    )
    assert r.status_code == 403


# ---------------------------------------------------------------- snapshots


def test_paper_snapshot_is_stable_after_bank_edits(client, two_question_exam):
    exam, q1, _ = two_question_exam
    exam_id = exam["exam_id"]

    paper_before = client.get(f"/exams/{exam_id}/paper", auth=CANDIDATE).json()
    assert paper_before["status"] == "in_progress"
    assert paper_before["revision"] == 0
    qids = [q["question_id"] for q in paper_before["questions"]]
    assert sorted(qids) == sorted([q1["question_id"], q1["question_id"] + 1])
    # stable display order across reconnects
    paper_reconnect = client.get(f"/exams/{exam_id}/paper", auth=CANDIDATE).json()
    assert [q["question_id"] for q in paper_reconnect["questions"]] == qids
    for a, b in zip(paper_before["questions"], paper_reconnect["questions"]):
        assert [o["option_id"] for o in a["options"]] == [
            o["option_id"] for o in b["options"]
        ]

    # Admin edits Q1 into a brand-new version (different stem/options/score).
    edited = client.post(
        f"/admin/questions/{q1['question_id']}/versions",
        auth=ADMIN,
        json={
            "qtype": "single",
            "stem": "【新版】完全不同的题干",
            "options": [
                {"option_id": "A", "text": "新选项A"},
                {"option_id": "B", "text": "新选项B"},
            ],
            "correct_option_ids": ["B"],
            "max_score": 99,
        },
    ).json()
    assert edited["version"] == 2 and edited["current_version"] == 2

    # The issued paper must be byte-for-byte unchanged.
    paper_after = client.get(f"/exams/{exam_id}/paper", auth=CANDIDATE).json()
    assert paper_after["questions"] == paper_before["questions"]
    exam_view = client.get(f"/admin/exams/{exam_id}", auth=ADMIN).json()
    assert exam_view["total_score"] == 15  # 5 + 10, not 99 + 10


def test_generation_rejects_unknown_version(client, two_question_exam):
    r = client.post(
        "/admin/exams",
        auth=ADMIN,
        json={
            "title": "bad",
            "questions": [{"question_id": 1, "version": 99}],
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------- save / submit


def test_save_submit_interleaving_and_stale_revision(client, two_question_exam):
    exam, q1, q2 = two_question_exam
    exam_id = exam["exam_id"]
    q1_id, q2_id = q1["question_id"], q2["question_id"]

    # Save #1 at revision 0.
    r = client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "save-1",
            "base_revision": 0,
            "answers": {str(q1_id): ["A"]},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1

    # Stale retry (same op_id is idempotent, but a *new* op on old rev conflicts).
    r_stale = client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "save-stale",
            "base_revision": 0,
            "answers": {str(q1_id): ["B"]},
        },
    )
    assert r_stale.status_code == 409
    assert "stale base_revision" in r_stale.json()["detail"]

    # Idempotent replay of the original op_id changes nothing.
    r_replay = client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "save-1",
            "base_revision": 0,
            "answers": {str(q1_id): ["A"]},
        },
    )
    assert r_replay.status_code == 200
    assert r_replay.json()["idempotent_replay"] is True
    assert r_replay.json()["revision"] == 1

    # Save #2 interleaved at the current revision.
    r2 = client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "save-2",
            "base_revision": 1,
            "answers": {str(q1_id): ["A"], str(q2_id): ["X", "Z"]},
        },
    )
    assert r2.status_code == 200
    assert r2.json()["revision"] == 2

    # Submit.
    r_sub = client.post(
        f"/exams/{exam_id}/submit", auth=CANDIDATE, json={"op_id": "submit-1"}
    )
    assert r_sub.status_code == 200
    body = r_sub.json()
    assert body["status"] == "submitted"
    assert body["graded_result_version"] == 1

    # Submitting again with a NEW op id is rejected; retrying the SAME op id
    # is an idempotent replay and must not create a second submission.
    r_dup = client.post(
        f"/exams/{exam_id}/submit", auth=CANDIDATE, json={"op_id": "submit-2"}
    )
    assert r_dup.status_code == 409
    r_retry = client.post(
        f"/exams/{exam_id}/submit", auth=CANDIDATE, json={"op_id": "submit-1"}
    )
    assert r_retry.status_code == 200
    assert r_retry.json()["idempotent_replay"] is True
    assert r_retry.json()["status"] == "submitted"

    # Answers are locked after submit.
    r_locked = client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "save-after-submit",
            "base_revision": 2,
            "answers": {str(q1_id): ["D"]},
        },
    )
    assert r_locked.status_code == 409
    assert "locked" in r_locked.json()["detail"]

    # Only one submit operation exists in the audit trail.
    history = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/history", auth=GRADER
    ).json()
    triggers = [run["trigger"] for run in history["grading_runs"]]
    assert triggers == ["auto at submit"]


def test_concurrent_saves_one_wins_one_conflicts(client, two_question_exam):
    """Two saves racing on the same base revision: exactly one succeeds."""
    exam, q1, _ = two_question_exam
    exam_id = exam["exam_id"]
    q1_id = q1["question_id"]
    barrier = threading.Barrier(2)
    results: list = []

    def save(op_id: str, pick: str):
        barrier.wait()
        r = client.post(
            f"/exams/{exam_id}/answers",
            auth=CANDIDATE,
            json={
                "op_id": op_id,
                "base_revision": 0,
                "answers": {str(q1_id): [pick]},
            },
        )
        results.append((op_id, r.status_code, r.json()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [
            pool.submit(save, "race-a", "A"),
            pool.submit(save, "race-b", "B"),
        ]
        for f in futs:
            f.result()

    statuses = sorted(code for _, code, _ in results)
    assert statuses == [200, 409]
    winner = next(body for _, code, body in results if code == 200)
    assert winner["revision"] == 1
    paper = client.get(f"/exams/{exam_id}/paper", auth=CANDIDATE).json()
    assert paper["revision"] == 1
    assert paper["saved_answers"][str(q1_id)] == winner["answers"][str(q1_id)]


def test_concurrent_save_and_submit_have_a_definite_order(client, two_question_exam):
    """A save and a submit fired together produce a clear winner/loser."""
    exam, q1, _ = two_question_exam
    exam_id = exam["exam_id"]
    q1_id = q1["question_id"]
    barrier = threading.Barrier(2)
    outcomes: list = []

    def do_save():
        barrier.wait()
        r = client.post(
            f"/exams/{exam_id}/answers",
            auth=CANDIDATE,
            json={
                "op_id": "concurrent-save",
                "base_revision": 0,
                "answers": {str(q1_id): ["A"]},
            },
        )
        outcomes.append(("save", r.status_code))

    def do_submit():
        barrier.wait()
        r = client.post(
            f"/exams/{exam_id}/submit",
            auth=CANDIDATE,
            json={"op_id": "concurrent-submit"},
        )
        outcomes.append(("submit", r.status_code, r.json()))

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(do_save)
        f2 = pool.submit(do_submit)
        f1.result(), f2.result()

    submit_outcome = next(o for o in outcomes if o[0] == "submit")
    assert submit_outcome[1] == 200  # submit never conflicts
    paper = client.get(f"/exams/{exam_id}/paper", auth=CANDIDATE).json()
    assert paper["status"] == "submitted"
    save_outcome = next(o for o in outcomes if o[0] == "save")
    # If the save lost the race it hit the submitted lock; if it won, revision 1 exists.
    assert save_outcome[1] in (200, 409)
    if save_outcome[1] == 200:
        assert paper["revision"] == 1
    else:
        assert paper["revision"] == 0


# ---------------------------------------------------------------- auto grading


def test_automatic_all_or_nothing_grading_on_submit(client, two_question_exam):
    exam, q1, q2 = two_question_exam
    exam_id = exam["exam_id"]
    q1_id, q2_id = q1["question_id"], q2["question_id"]

    # Q1 correct (A, 5 pts). Q2 selects only 2 of 3 correct options -> 0 pts.
    client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "s1",
            "base_revision": 0,
            "answers": {str(q1_id): ["A"], str(q2_id): ["X", "Y"]},
        },
    )
    sub = client.post(
        f"/exams/{exam_id}/submit", auth=CANDIDATE, json={"op_id": "go"}
    ).json()
    assert sub["graded_result_version"] == 1

    results = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/results", auth=GRADER
    ).json()["results"]
    assert len(results) == 1 and results[0]["status"] == "pending"
    items = {it["question_id"]: it for it in results[0]["items"]}
    assert items[q1_id]["auto_score"] == 5
    assert items[q2_id]["auto_score"] == 0  # not an exact set match
    assert results[0]["total_score"] == 5

    # Candidate sees nothing until publication.
    assert client.get(f"/exams/{exam_id}/result", auth=CANDIDATE).status_code == 404


# ---------------------------------------------------------------- answer-key fix


def test_answer_key_correction_and_regrade_keeps_paper_intact(client, two_question_exam):
    exam, q1, q2 = two_question_exam
    exam_id = exam["exam_id"]
    q1_id, q2_id = q1["question_id"], q2["question_id"]

    # Candidate answers B (wrong under key A) and the full correct multi set.
    client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "s1",
            "base_revision": 0,
            "answers": {str(q1_id): ["B"], str(q2_id): ["W", "X", "Y"]},
        },
    )
    client.post(f"/exams/{exam_id}/submit", auth=CANDIDATE, json={"op_id": "go"})
    v1 = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/results", auth=GRADER
    ).json()["results"][0]
    assert v1["total_score"] == 10  # Q1 wrong (0) + Q2 exact match (10)

    paper_before = client.get(f"/exams/{exam_id}/paper", auth=CANDIDATE).json()

    # Admin corrects the key for Q1: B is the right answer after all.
    r_key = client.post(
        f"/admin/exams/{exam_id}/answer-key",
        auth=ADMIN,
        json={
            "reason": "命题组勘误：第1题正确答案应为 B",
            "answer_key": [
                {"question_id": q1_id, "correct_option_ids": ["B"]},
                {"question_id": q2_id, "correct_option_ids": ["W", "X", "Y"]},
            ],
        },
    )
    assert r_key.status_code == 200
    assert r_key.json()["scheme_version"] == 2

    # Editing the key must not auto-mutate existing results.
    results = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/results", auth=GRADER
    ).json()["results"]
    assert len(results) == 1 and results[0]["total_score"] == 10

    # Regrade under the new scheme -> new pending version, score flips to 15.
    r_reg = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/regrade", auth=GRADER
    )
    assert r_reg.status_code == 201
    assert r_reg.json()["result_version"] == 2
    assert r_reg.json()["total_score"] == 15
    assert r_reg.json()["scheme_version"] == 2

    # The issued paper content and max scores are untouched.
    paper_after = client.get(f"/exams/{exam_id}/paper", auth=CANDIDATE).json()
    assert paper_after["questions"] == paper_before["questions"]
    assert paper_after["submitted_at"] is not None

    # The old result version still exists for audit.
    versions = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/results", auth=GRADER
    ).json()["results"]
    assert [v["result_version"] for v in versions] == [1, 2]
    assert versions[0]["total_score"] == 10 and versions[1]["total_score"] == 15

    # Answer key validation: unknown option rejected.
    r_bad = client.post(
        f"/admin/exams/{exam_id}/answer-key",
        auth=ADMIN,
        json={
            "reason": "bad",
            "answer_key": [
                {"question_id": q1_id, "correct_option_ids": ["ZZZ"]},
                {"question_id": q2_id, "correct_option_ids": ["W", "X", "Y"]},
            ],
        },
    )
    assert r_bad.status_code == 422


# ---------------------------------------------------------------- manual scores


def test_manual_score_priority_and_revoke(client, two_question_exam):
    exam, q1, q2 = two_question_exam
    exam_id = exam["exam_id"]
    q1_id, q2_id = q1["question_id"], q2["question_id"]

    client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "s1",
            "base_revision": 0,
            "answers": {str(q1_id): ["A"], str(q2_id): ["X", "Y"]},
        },
    )
    client.post(f"/exams/{exam_id}/submit", auth=CANDIDATE, json={"op_id": "go"})
    # v1: Q1 auto 5, Q2 auto 0 -> total 5.

    # Grader awards 8/10 on Q2 with a reason.
    r_man = client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2_id}/manual-score",
        auth=GRADER,
        json={"score": 8, "reason": "少选 W，但论证合理，给部分分"},
    )
    assert r_man.status_code == 200
    v2 = r_man.json()
    assert v2["result_version"] == 2
    items = {it["question_id"]: it for it in v2["items"]}
    assert items[q2_id]["auto_score"] == 0
    assert items[q2_id]["manual_score"] == 8
    assert items[q2_id]["effective_score"] == 8
    assert v2["total_score"] == 13  # 5 + 8

    # Boundary: above max_score is rejected; reason is required.
    r_over = client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2_id}/manual-score",
        auth=GRADER,
        json={"score": 11, "reason": "too much"},
    )
    assert r_over.status_code == 422
    r_noreason = client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2_id}/manual-score",
        auth=GRADER,
        json={"score": 7},
    )
    assert r_noreason.status_code == 422
    r_negative = client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2_id}/manual-score",
        auth=GRADER,
        json={"score": -1, "reason": "x"},
    )
    assert r_negative.status_code == 422

    # Admin corrects the answer key again and triggers a regrade.
    client.post(
        f"/admin/exams/{exam_id}/answer-key",
        auth=ADMIN,
        json={
            "reason": "再次勘误 Q2",
            "answer_key": [
                {"question_id": q1_id, "correct_option_ids": ["A"]},
                {"question_id": q2_id, "correct_option_ids": ["X", "Y"]},
            ],
        },
    )
    v3 = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/regrade", auth=ADMIN
    ).json()
    # Under the new key Q2 would auto-score 10, but the manual 8 must win.
    items3 = {it["question_id"]: it for it in v3["items"]}
    assert items3[q2_id]["auto_score"] == 10
    assert items3[q2_id]["manual_score"] == 8
    assert items3[q2_id]["effective_score"] == 8
    assert v3["total_score"] == 13

    # Explicit revocation: manual score removed, auto score takes over again.
    r_rev = client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2_id}/manual-score",
        auth=GRADER,
        json={"score": None, "reason": "人工分撤销，恢复机评"},
    )
    v4 = r_rev.json()
    items4 = {it["question_id"]: it for it in v4["items"]}
    assert items4[q2_id]["manual_score"] is None
    assert items4[q2_id]["effective_score"] == 10
    assert v4["total_score"] == 15

    # Full manual-action history is retained (set + revoke).
    history = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/history", auth=GRADER
    ).json()
    manual = history["manual_actions"]
    assert [(m["score"], m["grader"]) for m in manual] == [
        (8, "grader"),
        (None, "grader"),
    ]
    assert len(history["grading_schemes"]) == 2


# ---------------------------------------------------------------- publishing


def test_publish_flow_and_stale_candidate_conflict(client, two_question_exam):
    exam, q1, q2 = two_question_exam
    exam_id = exam["exam_id"]
    q1_id, q2_id = q1["question_id"], q2["question_id"]

    client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "s1",
            "base_revision": 0,
            "answers": {str(q1_id): ["A"], str(q2_id): ["X", "Y"]},
        },
    )
    client.post(f"/exams/{exam_id}/submit", auth=CANDIDATE, json={"op_id": "go"})

    # Publish v1: candidate now sees total 5.
    pub1 = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=GRADER,
        json={"result_version": 1},
    )
    assert pub1.status_code == 200
    seen = client.get(f"/exams/{exam_id}/result", auth=CANDIDATE).json()
    assert seen["result_version"] == 1 and seen["total_score"] == 5

    # Manual score produces pending v2; the candidate still sees the old total.
    client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2_id}/manual-score",
        auth=GRADER,
        json={"score": 9, "reason": "复核给分"},
    )
    still_seen = client.get(f"/exams/{exam_id}/result", auth=CANDIDATE).json()
    assert still_seen["result_version"] == 1 and still_seen["total_score"] == 5

    # Trying to publish the stale v1 while v2 exists must conflict.
    r_stale = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=GRADER,
        json={"result_version": 1},
    )
    assert r_stale.status_code == 409
    assert "stale result version 1" in r_stale.json()["detail"]
    assert "version 2" in r_stale.json()["detail"]

    # And the newer action was not swallowed: candidate view is unchanged.
    unchanged = client.get(f"/exams/{exam_id}/result", auth=CANDIDATE).json()
    assert unchanged["result_version"] == 1

    # Publish v2 properly; v1 becomes 'superseded' but remains queryable.
    pub2 = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=GRADER,
        json={"result_version": 2},
    )
    assert pub2.status_code == 200 and pub2.json()["status"] == "published"
    new_seen = client.get(f"/exams/{exam_id}/result", auth=CANDIDATE).json()
    assert new_seen["result_version"] == 2 and new_seen["total_score"] == 14

    versions = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/results", auth=GRADER
    ).json()["results"]
    statuses = {v["result_version"]: v["status"] for v in versions}
    assert statuses == {1: "superseded", 2: "published"}

    # Publishing a nonexistent version -> 404; republishing current version
    # is an idempotent no-op.
    r_missing = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=GRADER,
        json={"result_version": 99},
    )
    assert r_missing.status_code == 404
    r_again = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=GRADER,
        json={"result_version": 2},
    )
    assert r_again.status_code == 200
    assert r_again.json().get("idempotent_replay") is True


def test_regrade_before_submit_is_rejected(client, two_question_exam):
    exam, _, _ = two_question_exam
    # ensure attempt exists but not submitted
    client.get(f"/exams/{exam['exam_id']}/paper", auth=CANDIDATE)
    r = client.post(
        f"/grading/exams/{exam['exam_id']}/candidates/candidate/regrade", auth=GRADER
    )
    assert r.status_code == 409
