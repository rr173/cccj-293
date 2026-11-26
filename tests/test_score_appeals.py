"""HTTP tests for single-question score appeals.

The main end-to-end test covers the requested difficult path:

open -> assign two graders -> sealed opinions -> disagreement -> admin score
change -> preserve another question's pending manual score and ignore an
unregraded key correction -> publish correction -> regrade -> republish.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from exam_service.main import create_app

ADMIN = ("admin", "admin-pass-123")
GRADER = ("grader", "grader-pass-123")
GRADER2 = ("grader2", "grader2-pass-123")
GRADER3 = ("grader3", "grader3-pass-123")
CANDIDATE = ("candidate", "candidate-pass-123")
CANDIDATE2 = ("candidate2", "candidate2-pass-123")


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "appeals.db")
    with TestClient(app) as c:
        c.db_path = tmp_path / "appeals.db"
        yield c


def create_three_question_exam(client):
    q1 = client.post(
        "/admin/questions",
        auth=ADMIN,
        json={
            "qtype": "single",
            "stem": "Q1",
            "options": [
                {"option_id": "A", "text": "A"},
                {"option_id": "B", "text": "B"},
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
            "stem": "Q2 multi",
            "options": [
                {"option_id": "A", "text": "A"},
                {"option_id": "B", "text": "B"},
                {"option_id": "C", "text": "C"},
            ],
            "correct_option_ids": ["A", "C"],
            "max_score": 10,
        },
    ).json()
    q3 = client.post(
        "/admin/questions",
        auth=ADMIN,
        json={
            "qtype": "single",
            "stem": "Q3",
            "options": [
                {"option_id": "A", "text": "A"},
                {"option_id": "B", "text": "B"},
            ],
            "correct_option_ids": ["A"],
            "max_score": 3,
        },
    ).json()
    # Q2 uses exact-set multi-select grading.
    exam = client.post(
        "/admin/exams",
        auth=ADMIN,
        json={
            "title": "Appeal exam",
            "questions": [
                {"question_id": q1["question_id"], "version": 1},
                {"question_id": q2["question_id"], "version": 1},
                {"question_id": q3["question_id"], "version": 1},
            ],
            "shuffle": False,
        },
    ).json()
    return exam["exam_id"], q1["question_id"], q2["question_id"], q3["question_id"]


def submit_wrong_and_publish(client, exam_id, q1, q2, q3):
    r = client.post(
        f"/exams/{exam_id}/answers",
        auth=CANDIDATE,
        json={
            "op_id": "save-all-wrong",
            "base_revision": 0,
            "answers": {str(q1): ["B"], str(q2): ["B"], str(q3): ["B"]},
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/exams/{exam_id}/submit", auth=CANDIDATE, json={"op_id": "submit"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["graded_result_version"] == 1
    r = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=GRADER,
        json={"result_version": 1},
    )
    assert r.status_code == 200, r.text


def open_appeal(client, exam_id, q1, *, op_id="appeal-open", version=1):
    r = client.post(
        f"/exams/{exam_id}/appeals",
        auth=CANDIDATE,
        json={
            "op_id": op_id,
            "result_version": version,
            "question_id": q1,
            "reason": "我当时选 B 但题意应给部分/全部分数",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["appeal_id"], r.json()


def assign_two(client, appeal_id, *, op_id="assign", users=("grader2", "grader3")):
    r = client.post(
        f"/admin/appeals/{appeal_id}/assign",
        auth=ADMIN,
        json={"op_id": op_id, "grader_usernames": list(users)},
    )
    assert r.status_code == 200, r.text
    return r.json()


def results_by_q(client, exam_id):
    r = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/results", auth=GRADER
    )
    assert r.status_code == 200, r.text
    versions = r.json()["results"]
    latest = versions[-1]
    return latest, {i["question_id"]: i for i in latest["items"]}, versions


# ---------------------------------------------------------------- main path


def test_disagreement_arbitration_preserves_other_pending_score_then_regrade_publish(
    client,
):
    exam_id, q1, q2, q3 = create_three_question_exam(client)
    submit_wrong_and_publish(client, exam_id, q1, q2, q3)
    appeal_id, opened = open_appeal(client, exam_id, q1)
    assert opened["status"] == "pending_assignment"
    assert opened["appealed_score"] == 0

    # Opening is retry-safe and each question can be appealed only once.
    retry = client.post(
        f"/exams/{exam_id}/appeals",
        auth=CANDIDATE,
        json={
            "op_id": "appeal-open",
            "result_version": 1,
            "question_id": q1,
            "reason": "我当时选 B 但题意应给部分/全部分数",
        },
    )
    assert retry.status_code == 200
    assert retry.json()["idempotent_replay"] is True
    dup = client.post(
        f"/exams/{exam_id}/appeals",
        auth=CANDIDATE,
        json={
            "op_id": "appeal-open-different",
            "result_version": 1,
            "question_id": q1,
            "reason": "再次申诉同一题",
        },
    )
    assert dup.status_code == 409

    assigned = assign_two(client, appeal_id)
    assert [a["grader_username"] for a in assigned["assignments"]] == [
        "grader2",
        "grader3",
    ]
    assign_retry = client.post(
        f"/admin/appeals/{appeal_id}/assign",
        auth=ADMIN,
        json={"op_id": "assign", "grader_usernames": ["grader2", "grader3"]},
    )
    assert assign_retry.status_code == 200
    assert assign_retry.json()["idempotent_replay"] is True

    # Same assignment op + different request is not allowed.
    bad_reuse = client.post(
        f"/admin/appeals/{appeal_id}/assign",
        auth=ADMIN,
        json={"op_id": "assign", "grader_usernames": ["grader", "grader2"]},
    )
    assert bad_reuse.status_code == 409

    # An assignee cannot use the ordinary manual-score entry on this question.
    blocked = client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q1}/manual-score",
        auth=GRADER2,
        json={"score": 3, "reason": "试图走普通改分"},
    )
    assert blocked.status_code == 409

    # Another question gets an unpublished manual score during processing.
    manual_q2 = client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2}/manual-score",
        auth=GRADER,
        json={"score": 7, "reason": "第二题人工部分分"},
    )
    assert manual_q2.status_code == 200, manual_q2.text
    assert manual_q2.json()["result_version"] == 2

    # Answer-key errata is deliberately NOT regraded before the ruling closes.
    key = client.post(
        f"/admin/exams/{exam_id}/answer-key",
        auth=ADMIN,
        json={
            "reason": "第三题答案键勘误",
            "answer_key": [
                {"question_id": q1, "correct_option_ids": ["A"]},
                {"question_id": q2, "correct_option_ids": ["A", "C"]},
                {"question_id": q3, "correct_option_ids": ["B"]},
            ],
        },
    )
    assert key.status_code == 200, key.text
    assert key.json()["scheme_version"] == 2

    secret2 = "SECRET-OPINION-GRADER2-9f3c"
    op1 = client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER2,
        json={
            "op_id": "opinion-2",
            "decision": "change_score",
            "new_score": 4,
            "reason": secret2,
        },
    )
    assert op1.status_code == 200, op1.text
    body1 = op1.json()
    assert body1["status"] == "in_review"
    assert len(body1["opinions"]) == 1
    assert body1["opinions"][0]["reason"] == secret2

    # Sealed: grader3 sees no other opinion; candidate never sees opinion text.
    g3_view = client.get(f"/grading/appeals/{appeal_id}", auth=GRADER3).json()
    assert g3_view["opinions"] == []
    g3_history = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/history",
        auth=GRADER3,
    ).json()
    g3_history_appeal = next(
        a for a in g3_history["appeals"] if a["appeal_id"] == appeal_id
    )
    assert g3_history_appeal["opinions"] == []
    assert secret2 not in str(g3_history_appeal)
    candidate_view = client.get(
        f"/exams/{exam_id}/appeals/{appeal_id}", auth=CANDIDATE
    ).json()
    assert secret2 not in str(candidate_view)
    assert "basis" not in candidate_view

    # One opinion per assigned grader.
    twice = client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER2,
        json={
            "op_id": "opinion-2-again-new-op",
            "decision": "rejected",
            "reason": "repeat",
        },
    )
    assert twice.status_code == 409

    secret3 = "SECRET-OPINION-GRADER3-1a7d"
    op2 = client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER3,
        json={"op_id": "opinion-3", "decision": "reject", "reason": secret3},
    )
    assert op2.status_code == 200, op2.text
    assert op2.json()["status"] == "pending_arbitration"
    sealed_after = client.get(f"/grading/appeals/{appeal_id}", auth=GRADER2).json()
    assert len(sealed_after["opinions"]) == 2

    # Non-assigned grader cannot submit.
    forbidden = client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER,
        json={"op_id": "stranger-op", "decision": "reject", "reason": "x"},
    )
    assert forbidden.status_code == 403

    arb = client.post(
        f"/admin/appeals/{appeal_id}/arbitrate",
        auth=ADMIN,
        json={
            "op_id": "arb-1",
            "decision": "changed",
            "new_score": 5,
            "reason": "仲裁认为应答完全正确，改满分",
        },
    )
    assert arb.status_code == 200, arb.text
    closed = arb.json()
    assert closed["status"] == "closed_changed"
    assert closed["correction_status"] == "pending_publication"
    assert closed["correction_result_version"] == 3

    arb_retry = client.post(
        f"/admin/appeals/{appeal_id}/arbitrate",
        auth=ADMIN,
        json={
            "op_id": "arb-1",
            "decision": "changed",
            "new_score": 5,
            "reason": "仲裁认为应答完全正确，改满分",
        },
    )
    assert arb_retry.status_code == 200
    assert arb_retry.json()["idempotent_replay"] is True

    history = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/history",
        auth=ADMIN,
    ).json()
    history_appeal = next(a for a in history["appeals"] if a["appeal_id"] == appeal_id)
    assert len(history_appeal["all_opinions"]) == 2
    assert history_appeal["ruling"]["decision"] == "changed"
    assert history_appeal["appealed_score"] == 0
    assert history_appeal["basis"]["result_item"]["effective_score"] == 0

    latest, items, versions = results_by_q(client, exam_id)
    assert latest["result_version"] == 3
    assert latest["scheme_version"] == 1  # unregraded errata did not leak in
    assert items[q1]["effective_score"] == 5
    assert items[q1]["score_source_type"] == "appeal_ruling"
    assert items[q2]["manual_score"] == 7
    assert items[q2]["effective_score"] == 7  # pending other-question change kept
    assert items[q3]["effective_score"] == 0  # errata not effective via correction
    assert latest["total_score"] == 12
    assert len(versions) == 3  # arbitration retry did not create another result

    # Candidate sees closed + correction pending, but published score is still v1.
    cand_appeal = client.get(
        f"/exams/{exam_id}/appeals/{appeal_id}", auth=CANDIDATE
    ).json()
    assert cand_appeal["status"] == "closed_changed"
    assert cand_appeal["correction_status"] == "pending_publication"
    assert cand_appeal["conclusion"]["final_score"] == 5
    current = client.get(f"/exams/{exam_id}/result", auth=CANDIDATE).json()
    assert current["result_version"] == 1 and current["total_score"] == 0

    # Original publication entry publishes the generated pending version.
    pub3 = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=GRADER,
        json={"result_version": 3},
    )
    assert pub3.status_code == 200, pub3.text
    seen = client.get(f"/exams/{exam_id}/result", auth=CANDIDATE).json()
    assert seen["result_version"] == 3 and seen["total_score"] == 12
    cand_appeal = client.get(
        f"/exams/{exam_id}/appeals/{appeal_id}", auth=CANDIDATE
    ).json()
    assert cand_appeal["correction_status"] == "published"
    assert cand_appeal["correction"]["published_result_version"] == 3

    # Regrade uses the current answer key errata, but the appeal score survives.
    regrade = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/regrade",
        auth=GRADER,
    )
    assert regrade.status_code == 201, regrade.text
    assert regrade.json()["result_version"] == 4
    latest, items, versions = results_by_q(client, exam_id)
    assert latest["result_version"] == 4
    assert latest["scheme_version"] == 2
    assert items[q1]["auto_score"] == 0
    assert items[q1]["effective_score"] == 5
    assert items[q1]["score_source_type"] == "appeal_ruling"
    assert items[q2]["manual_score"] == 7
    assert items[q2]["effective_score"] == 7
    assert items[q3]["auto_score"] == 3
    assert items[q3]["effective_score"] == 3
    assert latest["total_score"] == 15

    # Ordinary manual set/revoke can never overwrite the final appeal score.
    for body in (
        {"score": 0, "reason": "try overwrite"},
        {"score": None, "reason": "try revoke appeal binding"},
    ):
        r = client.put(
            f"/grading/exams/{exam_id}/candidates/candidate/questions/{q1}/manual-score",
            auth=GRADER,
            json=body,
        )
        assert r.status_code == 409

    pub4 = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=GRADER,
        json={"result_version": 4},
    )
    assert pub4.status_code == 200, pub4.text
    final_seen = client.get(f"/exams/{exam_id}/result", auth=CANDIDATE).json()
    assert final_seen["result_version"] == 4
    assert final_seen["total_score"] == 15
    cand_appeal = client.get(
        f"/exams/{exam_id}/appeals/{appeal_id}", auth=CANDIDATE
    ).json()
    assert cand_appeal["correction_status"] == "published"
    assert cand_appeal["correction"]["published_result_version"] == 4

    # Another candidate cannot read this appeal through the candidate API.
    other = client.get(
        f"/exams/{exam_id}/appeals/{appeal_id}", auth=CANDIDATE2
    )
    assert other.status_code == 404
    staff_forbidden = client.get(
        f"/grading/appeals/{appeal_id}", auth=CANDIDATE
    )
    assert staff_forbidden.status_code == 403


# ---------------------------------------------------------------- admission


def test_only_current_published_question_once_and_basis_is_frozen(client):
    exam_id, q1, q2, _ = create_three_question_exam(client)
    submit_wrong_and_publish(client, exam_id, q1, q2, q1 + 2)

    # A pending/unpublished version cannot be used as the admission basis.
    client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2}/manual-score",
        auth=GRADER,
        json={"score": 6, "reason": "pending change"},
    )
    r = client.post(
        f"/exams/{exam_id}/appeals",
        auth=CANDIDATE,
        json={
            "op_id": "appeal-pending-version",
            "result_version": 2,
            "question_id": q2,
            "reason": "不能针对未发布版本",
        },
    )
    assert r.status_code == 409

    appeal_id, body = open_appeal(client, exam_id, q1)
    assert body["result_version"] == 1
    staff_before = client.get(f"/grading/appeals/{appeal_id}", auth=ADMIN).json()
    assert staff_before["basis"]["result_version"] == 1
    assert staff_before["basis"]["selected_answer"] == ["B"]

    # Publish a newer version after admission; the stored basis still points at v1.
    pub2 = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=ADMIN,
        json={"result_version": 2},
    )
    assert pub2.status_code == 200, pub2.text
    staff = client.get(f"/grading/appeals/{appeal_id}", auth=ADMIN).json()
    assert staff["basis"]["result_version"] == 1
    assert staff["appealed_score"] == 0

    # The old version can no longer be used for a new appeal after publication.
    r = client.post(
        f"/exams/{exam_id}/appeals",
        auth=CANDIDATE,
        json={
            "op_id": "appeal-old-now-superseded",
            "result_version": 1,
            "question_id": q2,
            "reason": "旧版本",
        },
    )
    assert r.status_code == 409

    # The same question remains one-appeal-only even with a new result version.
    r = client.post(
        f"/exams/{exam_id}/appeals",
        auth=CANDIDATE,
        json={
            "op_id": "appeal-q1-again",
            "result_version": 2,
            "question_id": q1,
            "reason": "重复",
        },
    )
    assert r.status_code == 409


def test_new_score_must_differ_and_be_in_original_range(client):
    exam_id, q1, q2, q3 = create_three_question_exam(client)
    submit_wrong_and_publish(client, exam_id, q1, q2, q3)
    appeal_id, _ = open_appeal(client, exam_id, q1)
    assign_two(client, appeal_id)

    for score in (0, 6, -1):
        r = client.post(
            f"/grading/appeals/{appeal_id}/opinions",
            auth=GRADER2,
            json={
                "op_id": f"bad-score-{score}",
                "decision": "changed",
                "new_score": score,
                "reason": "invalid",
            },
        )
        assert r.status_code == 422

    r = client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER2,
        json={
            "op_id": "valid-opinion",
            "decision": "changed",
            "new_score": 5,
            "reason": "valid",
        },
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- conflicts


def test_previous_manual_grader_cannot_be_assigned(client):
    exam_id, q1, q2, q3 = create_three_question_exam(client)
    submit_wrong_and_publish(client, exam_id, q1, q2, q3)

    # Score then explicitly revoke: both facts create a conflict history.
    for score, op_suffix in ((4, "set"), (None, "revoke")):
        r = client.put(
            f"/grading/exams/{exam_id}/candidates/candidate/questions/{q1}/manual-score",
            auth=GRADER,
            json={"score": score, "reason": f"ordinary {op_suffix}"},
        )
        assert r.status_code == 200, r.text

    appeal_id, _ = open_appeal(client, exam_id, q1)
    r = client.post(
        f"/admin/appeals/{appeal_id}/assign",
        auth=ADMIN,
        json={"op_id": "assign-conflicted", "grader_usernames": ["grader", "grader2"]},
    )
    assert r.status_code == 409
    assert "previously" in r.json()["detail"]

    # Two equal graders are rejected even without conflict.
    r = client.post(
        f"/admin/appeals/{appeal_id}/assign",
        auth=ADMIN,
        json={"op_id": "assign-same", "grader_usernames": ["grader2", "grader2"]},
    )
    assert r.status_code == 422


def test_conflicted_admin_cannot_arbitrate(client):
    exam_id, q1, q2, q3 = create_three_question_exam(client)
    submit_wrong_and_publish(client, exam_id, q1, q2, q3)
    appeal_id, _ = open_appeal(client, exam_id, q1)
    assign_two(client, appeal_id)

    # Admin performs ordinary manual scoring after assignment but before closure.
    r = client.put(
        f"/grading/exams/{exam_id}/candidates/candidate/questions/{q1}/manual-score",
        auth=ADMIN,
        json={"score": 2, "reason": "admin ordinary score creates conflict"},
    )
    assert r.status_code == 200, r.text

    client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER2,
        json={"op_id": "o1", "decision": "changed", "new_score": 4, "reason": "r1"},
    )
    client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER3,
        json={"op_id": "o2", "decision": "rejected", "reason": "r2"},
    )
    r = client.post(
        f"/admin/appeals/{appeal_id}/arbitrate",
        auth=ADMIN,
        json={
            "op_id": "blocked-arb",
            "decision": "changed",
            "new_score": 5,
            "reason": "should be blocked",
        },
    )
    assert r.status_code == 403


# ------------------------------------------------------- consensus & retries


def test_matching_change_opinions_close_without_arbitration(client):
    exam_id, q1, q2, q3 = create_three_question_exam(client)
    submit_wrong_and_publish(client, exam_id, q1, q2, q3)
    appeal_id, _ = open_appeal(client, exam_id, q1)
    assign_two(client, appeal_id)

    for user, op_id in ((GRADER2, "same-1"), (GRADER3, "same-2")):
        r = client.post(
            f"/grading/appeals/{appeal_id}/opinions",
            auth=user,
            json={
                "op_id": op_id,
                "decision": "adjust_score",
                "score": 3,
                "opinion": "结论一致",
            },
        )
        assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "closed_changed"
    assert body["closure"]["decision"] == "changed"
    assert body["conclusion"]["final_score"] == 3
    assert body["correction_result_version"] == 2


def test_restart_retry_does_not_duplicate_opinion_or_result(client, tmp_path):
    exam_id, q1, q2, q3 = create_three_question_exam(client)
    submit_wrong_and_publish(client, exam_id, q1, q2, q3)
    appeal_id, _ = open_appeal(client, exam_id, q1)
    assign_two(client, appeal_id)
    first = client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER2,
        json={"op_id": "persisted-op", "decision": "reject", "reason": "first"},
    )
    assert first.status_code == 200

    # Simulate API process restart by constructing a new app on the same file.
    restarted = create_app(tmp_path / "appeals.db")
    with TestClient(restarted) as c2:
        retry = c2.post(
            f"/grading/appeals/{appeal_id}/opinions",
            auth=GRADER2,
            json={"op_id": "persisted-op", "decision": "reject", "reason": "first"},
        )
        assert retry.status_code == 200
        assert retry.json()["idempotent_replay"] is True

        close = c2.post(
            f"/grading/appeals/{appeal_id}/opinions",
            auth=GRADER3,
            json={
                "op_id": "persisted-op-2",
                "decision": "reject",
                "reason": "second",
            },
        )
        assert close.status_code == 200
        assert close.json()["status"] == "closed_rejected"

        results = c2.get(
            f"/grading/exams/{exam_id}/candidates/candidate/results",
            auth=ADMIN,
        ).json()["results"]
        # Rejection creates no correction result: only submit-generated v1 exists.
        assert [r["result_version"] for r in results] == [1]


def test_concurrent_identical_closing_opinions_create_one_correction(client):
    exam_id, q1, q2, q3 = create_three_question_exam(client)
    submit_wrong_and_publish(client, exam_id, q1, q2, q3)
    appeal_id, _ = open_appeal(client, exam_id, q1)
    assign_two(client, appeal_id)
    client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER2,
        json={"op_id": "first-review", "decision": "changed", "new_score": 4, "reason": "x"},
    )

    barrier = threading.Barrier(2)
    outcomes = []

    def close():
        barrier.wait()
        r = client.post(
            f"/grading/appeals/{appeal_id}/opinions",
            auth=GRADER3,
            json={
                "op_id": "same-closing-op",
                "decision": "changed",
                "new_score": 4,
                "reason": "same",
            },
        )
        outcomes.append(r)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(close), pool.submit(close)]
        for f in futures:
            f.result()

    assert all(r.status_code == 200 for r in outcomes)
    assert sum(1 for r in outcomes if r.json().get("idempotent_replay")) == 1
    results = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/results", auth=ADMIN
    ).json()["results"]
    assert [r["result_version"] for r in results] == [1, 2]
    appeal = client.get(f"/grading/appeals/{appeal_id}", auth=ADMIN).json()
    assert appeal["status"] == "closed_changed"


def test_concurrent_arbitration_and_publish_have_one_order(client):
    exam_id, q1, q2, q3 = create_three_question_exam(client)
    submit_wrong_and_publish(client, exam_id, q1, q2, q3)
    appeal_id, _ = open_appeal(client, exam_id, q1)
    assign_two(client, appeal_id)
    client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER2,
        json={"op_id": "d1", "decision": "changed", "new_score": 4, "reason": "x"},
    )
    client.post(
        f"/grading/appeals/{appeal_id}/opinions",
        auth=GRADER3,
        json={"op_id": "d2", "decision": "rejected", "reason": "y"},
    )

    barrier = threading.Barrier(2)
    outcomes: dict[str, int] = {}

    def arbitrate():
        barrier.wait()
        r = client.post(
            f"/admin/appeals/{appeal_id}/arbitrate",
            auth=ADMIN,
            json={
                "op_id": "race-arb",
                "decision": "changed",
                "new_score": 5,
                "reason": "final",
            },
        )
        outcomes["arb"] = r.status_code

    def publish_old():
        barrier.wait()
        r = client.post(
            f"/grading/exams/{exam_id}/candidates/candidate/publish",
            auth=ADMIN,
            json={"result_version": 1},
        )
        outcomes["publish_v1"] = r.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(arbitrate)
        f2 = pool.submit(publish_old)
        f1.result()
        f2.result()

    assert outcomes["arb"] == 200
    assert outcomes["publish_v1"] in (200, 409)
    results = client.get(
        f"/grading/exams/{exam_id}/candidates/candidate/results", auth=ADMIN
    ).json()["results"]
    assert [r["result_version"] for r in results] == [1, 2]

    # Whatever the race order, publishing newest v2 converges consistently.
    pub = client.post(
        f"/grading/exams/{exam_id}/candidates/candidate/publish",
        auth=ADMIN,
        json={"result_version": 2},
    )
    assert pub.status_code == 200, pub.text
    seen = client.get(f"/exams/{exam_id}/result", auth=CANDIDATE).json()
    assert seen["result_version"] == 2 and seen["total_score"] == 5
