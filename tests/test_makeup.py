"""End-to-end HTTP tests for makeup-session reservations (补考分场预约).

Covers: campaign creation rules, eligibility gating on the basis exam's
published total, waitlist FIFO with invitation/confirm, invitation expiry
(including across a restart), withdraw/unsubscribe, atomic session switch,
eligibility-loss voiding on publish, exam-start locking, op_id idempotency,
candidate isolation and real-thread concurrency (no overbooking).

Everything goes through HTTP (FastAPI TestClient) with a VirtualClock; extra
candidate users are inserted directly only because the service has no
user-administration API.
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from exam_service.clock import VirtualClock
from exam_service.main import create_app
from exam_service.security import hash_password

ADMIN = ("admin", "admin-pass-123")
GRADER = ("grader", "grader-pass-123")


def C(i: int):
    return (f"cand{i}", f"pass-{i}")


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def harness(tmp_path):
    db_path = tmp_path / "makeup.db"
    clock = VirtualClock(datetime(2026, 11, 26, 9, 0, 0, tzinfo=timezone.utc))
    app = create_app(db_path, clock=clock)
    raw = sqlite3.connect(db_path)
    for i in range(1, 7):
        raw.execute(
            "INSERT INTO users (username, password_hash, role, display_name)"
            " VALUES (?, ?, 'candidate', ?)",
            (f"cand{i}", hash_password(f"pass-{i}"), f"考生{i}"),
        )
    raw.commit()
    raw.close()
    with TestClient(app) as client:
        yield client, clock, db_path


def _question(client, correct="A", max_score=10):
    return client.post(
        "/admin/questions",
        auth=ADMIN,
        json={
            "qtype": "single",
            "stem": "样例题",
            "options": [
                {"option_id": "A", "text": "甲"},
                {"option_id": "B", "text": "乙"},
            ],
            "correct_option_ids": [correct],
            "max_score": max_score,
        },
    ).json()


def _exam(client, title, qid):
    return client.post(
        "/admin/exams",
        auth=ADMIN,
        json={
            "title": title,
            "questions": [{"question_id": qid, "version": 1}],
            "shuffle": False,
        },
    ).json()["exam_id"]


def _sit(client, exam_id, qid, auth, tag, options):
    """Open the paper, save the given options and submit."""
    assert client.get(f"/exams/{exam_id}/paper", auth=auth).status_code == 200
    r = client.post(
        f"/exams/{exam_id}/answers",
        auth=auth,
        json={"op_id": f"{tag}-save", "base_revision": 0, "answers": {str(qid): options}},
    )
    assert r.status_code == 200, r.text
    r = client.post(f"/exams/{exam_id}/submit", auth=auth, json={"op_id": f"{tag}-submit"})
    assert r.status_code == 200, r.text


def _publish(client, exam_id, username, version=1):
    r = client.post(
        f"/grading/exams/{exam_id}/candidates/{username}/publish",
        auth=GRADER,
        json={"result_version": version},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _apply(client, makeup_id, auth, session_id, op):
    return client.post(
        f"/exams/{makeup_id}/makeup/applications",
        auth=auth,
        json={"op_id": op, "session_id": session_id},
    )


def _confirm(client, makeup_id, auth, invitation_id, op):
    return client.post(
        f"/exams/{makeup_id}/makeup/invitations/{invitation_id}/confirm",
        auth=auth,
        json={"op_id": op},
    )


def _withdraw(client, makeup_id, auth, application_id, op):
    return client.post(
        f"/exams/{makeup_id}/makeup/applications/{application_id}/withdraw",
        auth=auth,
        json={"op_id": op},
    )


def _unsubscribe(client, makeup_id, auth, op):
    return client.post(
        f"/exams/{makeup_id}/makeup/unsubscribe", auth=auth, json={"op_id": op}
    )


def _my(client, makeup_id, auth):
    r = client.get(f"/exams/{makeup_id}/makeup", auth=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _staff(client, makeup_id):
    r = client.get(f"/admin/exams/{makeup_id}/makeup", auth=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _session(view, name):
    return next(s for s in view["sessions"] if s["name"] == name)


@pytest.fixture()
def makeup(harness):
    """Basis exam with four failing (score 0) published candidates, plus a
    makeup exam whose campaign has two capacity-1 sessions."""
    client, clock, db_path = harness
    q = _question(client)
    basis_id = _exam(client, "原测评", q["question_id"])
    for i in (1, 2, 3, 4):
        _sit(client, basis_id, q["question_id"], C(i), f"basis-c{i}", ["B"])  # wrong -> 0
        _publish(client, basis_id, f"cand{i}")
    makeup_id = _exam(client, "补考测评", q["question_id"])
    r = client.post(
        f"/admin/exams/{makeup_id}/makeup",
        auth=ADMIN,
        json={
            "op_id": "mk-create",
            "basis_exam_id": basis_id,
            "pass_score": 5,
            "invite_ttl_seconds": 300,
            "sessions": [
                {"name": "补考甲场", "duration_seconds": 600, "capacity": 1},
                {"name": "补考乙场", "duration_seconds": 900, "capacity": 1},
            ],
        },
    )
    assert r.status_code == 201, r.text
    campaign = r.json()
    return {
        "basis_id": basis_id,
        "makeup_id": makeup_id,
        "qid": q["question_id"],
        "sessions": {s["name"]: s for s in campaign["sessions"]},
    }


# ---------------------------------------------------------------- creation rules


def test_campaign_creation_rules(harness):
    client, clock, _ = harness
    q = _question(client)
    basis_id = _exam(client, "原测评", q["question_id"])
    makeup_id = _exam(client, "补考", q["question_id"])
    body = {
        "op_id": "mk-1",
        "basis_exam_id": basis_id,
        "pass_score": 5,
        "invite_ttl_seconds": 300,
        "sessions": [{"name": "唯一场", "duration_seconds": 600, "capacity": 2}],
    }
    r = client.post(f"/admin/exams/{makeup_id}/makeup", auth=ADMIN, json=body)
    assert r.status_code == 201, r.text
    campaign = r.json()
    assert campaign["pass_score"] == 5 and campaign["invite_ttl_seconds"] == 300
    assert _session(campaign, "唯一场")["capacity"] == 2
    # The exam became a timed exam with capacity-carrying sessions.
    exam_view = client.get(f"/admin/exams/{makeup_id}", auth=ADMIN).json()
    assert exam_view["timed_mode"] is True
    assert exam_view["sessions"][0]["capacity"] == 2

    # One campaign per exam.
    r = client.post(f"/admin/exams/{makeup_id}/makeup", auth=ADMIN, json={**body, "op_id": "mk-2"})
    assert r.status_code == 409
    # Timed-config is now rejected too (and vice versa).
    r = client.post(
        f"/admin/exams/{makeup_id}/timed-config",
        auth=ADMIN,
        json={"sessions": [{"name": "x", "duration_seconds": 10, "candidate_usernames": ["cand1"]}]},
    )
    assert r.status_code == 409

    # Replay of the creating op returns the recorded campaign, not a 409.
    replay = client.post(f"/admin/exams/{makeup_id}/makeup", auth=ADMIN, json=body)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["campaign_id"] == campaign["campaign_id"]
    # Same op_id with a different request is a conflict.
    r = client.post(
        f"/admin/exams/{makeup_id}/makeup",
        auth=ADMIN,
        json={**body, "pass_score": 6},
    )
    assert r.status_code == 409

    # A timed-configured exam cannot open a campaign.
    timed_id = _exam(client, "限时", q["question_id"])
    r = client.post(
        f"/admin/exams/{timed_id}/timed-config",
        auth=ADMIN,
        json={"sessions": [{"name": "s", "duration_seconds": 60, "candidate_usernames": ["cand1"]}]},
    )
    assert r.status_code == 201
    r = client.post(
        f"/admin/exams/{timed_id}/makeup", auth=ADMIN, json={**body, "op_id": "mk-3"}
    )
    assert r.status_code == 409

    # An exam that already has attempts cannot open a campaign.
    started_id = _exam(client, "已开考", q["question_id"])
    client.get(f"/exams/{started_id}/paper", auth=C(1))
    r = client.post(
        f"/admin/exams/{started_id}/makeup", auth=ADMIN, json={**body, "op_id": "mk-4"}
    )
    assert r.status_code == 409

    # Basis exam must exist and differ from the makeup exam.
    fresh_id = _exam(client, "另一份", q["question_id"])
    r = client.post(
        f"/admin/exams/{fresh_id}/makeup",
        auth=ADMIN,
        json={**body, "op_id": "mk-5", "basis_exam_id": 9999},
    )
    assert r.status_code == 422
    r = client.post(
        f"/admin/exams/{fresh_id}/makeup",
        auth=ADMIN,
        json={**body, "op_id": "mk-6", "basis_exam_id": fresh_id},
    )
    assert r.status_code == 422
    # Candidates cannot open campaigns.
    r = client.post(f"/admin/exams/{fresh_id}/makeup", auth=C(1), json={**body, "op_id": "mk-7"})
    assert r.status_code == 403


# ---------------------------------------------------------------- eligibility


def test_eligibility_gates_application(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    basis_id = makeup["basis_id"]
    qid = makeup["qid"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]

    # cand5 never took the basis exam: no published result -> not eligible.
    r = _apply(client, m_id, C(5), s_a, "app-c5")
    assert r.status_code == 403 and "published" in r.json()["detail"]

    # cand1 published 0 (< 5): eligible.
    r = _apply(client, m_id, C(1), s_a, "app-c1")
    assert r.status_code == 201, r.text

    # cand2 raised to exactly the pass line (5) via manual score: not below.
    r = client.put(
        f"/grading/exams/{basis_id}/candidates/cand2/questions/{qid}/manual-score",
        auth=GRADER,
        json={"score": 5, "reason": "边界分"},
    )
    assert r.status_code == 200
    _publish(client, basis_id, "cand2", version=2)
    r = _apply(client, m_id, C(2), s_a, "app-c2")
    assert r.status_code == 403

    # cand3 raised above the line (10): not eligible either.
    client.put(
        f"/grading/exams/{basis_id}/candidates/cand3/questions/{qid}/manual-score",
        auth=GRADER,
        json={"score": 10, "reason": "改判"},
    )
    _publish(client, basis_id, "cand3", version=2)
    r = _apply(client, m_id, C(3), s_a, "app-c3")
    assert r.status_code == 403

    mine = _my(client, m_id, C(1))
    assert mine["my_eligibility"] == {
        "eligible": True,
        "published_total_score": 0,
        "pass_score": 5,
    }


# ---------------------------------------------------------------- apply / confirm


def test_apply_invites_immediately_and_confirm_grants_roster(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]

    r = _apply(client, m_id, C(1), s_a, "app-1")
    assert r.status_code == 201, r.text
    app = r.json()
    assert app["state"] == "invited"
    invitation = app["invitation"]
    assert invitation["status"] == "pending"
    assert invitation["issued_at"] < invitation["expires_at"]

    # Not confirmed yet: the roster gate still rejects the paper open.
    r = client.get(f"/exams/{m_id}/paper", auth=C(1))
    assert r.status_code == 403

    r = _confirm(client, m_id, C(1), invitation["invitation_id"], "cfm-1")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "confirmed"
    assert r.json()["switched_from_session_id"] is None
    # The seat is occupied in the staff view right after confirmation.
    assert _session(_staff(client, m_id), "补考甲场")["confirmed"] == 1

    # Now the paper opens, timed with the session's duration.
    paper = client.get(f"/exams/{m_id}/paper", auth=C(1)).json()
    assert paper["timing"]["timed_mode"] is True
    assert paper["timing"]["session_id"] == s_a
    assert paper["timing"]["duration_seconds"] == 600
    # Starting the exam moved the application to completed (seat still held).
    session = _session(_staff(client, m_id), "补考甲场")
    assert session["completed"] == 1 and session["free_seats"] == 0


def test_waitlist_fifo_and_promotion_on_unsubscribe(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]

    # cand1 takes the only seat of session A.
    app1 = _apply(client, m_id, C(1), s_a, "app-1").json()
    _confirm(client, m_id, C(1), app1["invitation"]["invitation_id"], "cfm-1")

    # cand2 and cand3 queue behind (FIFO by application order).
    app2 = _apply(client, m_id, C(2), s_a, "app-2").json()
    app3 = _apply(client, m_id, C(3), s_a, "app-3").json()
    assert app2["state"] == "waitlisted" and app2["queue_position"] == 1
    assert app3["state"] == "waitlisted" and app3["queue_position"] == 2

    # cand1 unsubscribes: the seat is released and cand2 is invited at once.
    r = _unsubscribe(client, m_id, C(1), "unsub-1")
    assert r.status_code == 200, r.text
    assert r.json()["released_session_id"] == s_a
    mine2 = _my(client, m_id, C(2))
    promoted = mine2["my_applications"][0]
    assert promoted["state"] == "invited"
    assert promoted["invitation"]["status"] == "pending"
    # cand3 moved up to the head of the queue but is not invited yet.
    mine3 = _my(client, m_id, C(3))
    assert mine3["my_applications"][0]["state"] == "waitlisted"
    assert mine3["my_applications"][0]["queue_position"] == 1

    # cand2 confirms; cand1's old seat is now cand2's; counts stay consistent.
    _confirm(client, m_id, C(2), promoted["invitation"]["invitation_id"], "cfm-2")
    session = _session(_staff(client, m_id), "补考甲场")
    assert session["confirmed"] == 1 and session["invited"] == 0
    assert session["waitlisted"] == 1 and session["free_seats"] == 0

    # cand1 has no seat anymore and cannot unsubscribe twice (new op id).
    r = _unsubscribe(client, m_id, C(1), "unsub-1b")
    assert r.status_code == 409


def test_invitation_expiry_releases_seat_and_ends_application(harness, makeup):
    client, clock, _ = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]

    app1 = _apply(client, m_id, C(1), s_a, "app-1").json()
    inv1 = app1["invitation"]["invitation_id"]
    app2 = _apply(client, m_id, C(2), s_a, "app-2").json()
    assert app2["state"] == "waitlisted"

    clock.advance(300)  # exactly at the confirmation deadline
    # The next touch settles: cand1's invitation expired, cand2 was promoted.
    mine1 = _my(client, m_id, C(1))
    expired = mine1["my_applications"][0]
    assert expired["state"] == "expired"
    assert expired["invitation"]["status"] == "expired"
    assert expired["invitation"]["resolved_at"] == expired["invitation"]["expires_at"]
    mine2 = _my(client, m_id, C(2))
    assert mine2["my_applications"][0]["state"] == "invited"

    # The old invitation cannot confirm anything anymore.
    r = _confirm(client, m_id, C(1), inv1, "cfm-late")
    assert r.status_code == 409

    # cand1 must re-apply (new application, back of the queue).
    r = _apply(client, m_id, C(1), s_a, "app-1b")
    assert r.status_code == 201
    assert r.json()["state"] == "waitlisted"
    staff = _staff(client, m_id)
    session = _session(staff, "补考甲场")
    assert session["invited"] == 1 and session["waitlisted"] == 1
    assert session["free_seats"] == 0  # never over capacity


def test_expiry_settles_after_restart(harness, makeup):
    client, clock, db_path = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]

    _apply(client, m_id, C(1), s_a, "app-1")
    _apply(client, m_id, C(2), s_a, "app-2")
    clock.advance(400)  # invitation expired while "the service was down"

    # Full restart: new app instance, new clock at the advanced time.
    new_clock = VirtualClock(datetime(2026, 11, 26, 9, 6, 40, tzinfo=timezone.utc))
    app2 = create_app(db_path, clock=new_clock)
    with TestClient(app2) as client2:
        mine2 = _my(client2, m_id, C(2))
        assert mine2["my_applications"][0]["state"] == "invited"
        mine1 = _my(client2, m_id, C(1))
        assert mine1["my_applications"][0]["state"] == "expired"
        # The expired invitation was not re-issued; the new one started when
        # it was actually sent (at settlement time).
        inv2 = mine2["my_applications"][0]["invitation"]
        assert inv2["issued_at"] == new_clock.now_iso()
        session = _session(_staff(client2, m_id), "补考甲场")
        assert session["invited"] == 1 and session["free_seats"] == 0


def test_withdraw_releases_held_seat(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]

    app1 = _apply(client, m_id, C(1), s_a, "app-1").json()
    _apply(client, m_id, C(2), s_a, "app-2")

    r = _withdraw(client, m_id, C(1), app1["application_id"], "wd-1")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    # The held seat moved to cand2 immediately.
    assert _my(client, m_id, C(2))["my_applications"][0]["state"] == "invited"
    # Withdrawing again (new op id) is a conflict.
    r = _withdraw(client, m_id, C(1), app1["application_id"], "wd-1b")
    assert r.status_code == 409


# ---------------------------------------------------------------- switch


def test_switch_sessions_is_atomic_and_releases_old_seat(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]
    s_b = makeup["sessions"]["补考乙场"]["session_id"]

    # cand1 confirmed in A; cand3 queues for A.
    app1 = _apply(client, m_id, C(1), s_a, "app-1").json()
    _confirm(client, m_id, C(1), app1["invitation"]["invitation_id"], "cfm-1")
    _apply(client, m_id, C(3), s_a, "app-3")

    # A confirmed candidate may waitlist for ANOTHER session; the old seat
    # stays theirs while they wait (both seats are held once invited).
    r = _apply(client, m_id, C(1), s_a, "app-1-same")
    assert r.status_code == 409  # cannot waitlist the session they hold
    app1b = _apply(client, m_id, C(1), s_b, "app-1b").json()
    assert app1b["state"] == "invited"
    staff = _staff(client, m_id)
    assert _session(staff, "补考甲场")["confirmed"] == 1
    assert _session(staff, "补考乙场")["invited"] == 1  # two seats held at once

    # Confirming the new invitation switches atomically: A released, B taken.
    r = _confirm(client, m_id, C(1), app1b["invitation"]["invitation_id"], "cfm-1b")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "confirmed"
    assert r.json()["switched_from_session_id"] == s_a
    staff = _staff(client, m_id)
    assert _session(staff, "补考乙场")["confirmed"] == 1
    # The freed A seat promoted cand3.
    assert _my(client, m_id, C(3))["my_applications"][0]["state"] == "invited"
    old = [a for a in _staff(client, m_id)["applications"] if a["username"] == "cand1"]
    assert {a["state"] for a in old} == {"switched", "confirmed"}

    # The paper now belongs to session B with its 900s duration.
    paper = client.get(f"/exams/{m_id}/paper", auth=C(1)).json()
    assert paper["timing"]["session_id"] == s_b
    assert paper["timing"]["duration_seconds"] == 900


def test_only_one_active_application_at_a_time(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]
    s_b = makeup["sessions"]["补考乙场"]["session_id"]

    app1 = _apply(client, m_id, C(1), s_a, "app-1").json()
    _confirm(client, m_id, C(1), app1["invitation"]["invitation_id"], "cfm-1")
    app2 = _apply(client, m_id, C(2), s_a, "app-2").json()
    assert app2["state"] == "waitlisted"
    # cand2 is already queued for A: cannot also queue for B.
    r = _apply(client, m_id, C(2), s_b, "app-2b")
    assert r.status_code == 409
    # After withdrawing from A, applying for B works.
    _withdraw(client, m_id, C(2), app2["application_id"], "wd-2")
    r = _apply(client, m_id, C(2), s_b, "app-2c")
    assert r.status_code == 201 and r.json()["state"] == "invited"


# ---------------------------------------------------------------- eligibility loss


def test_losing_eligibility_voids_pending_but_keeps_confirmed(harness):
    client, clock, _ = harness
    q = _question(client)
    qid = q["question_id"]
    basis_id = _exam(client, "原测评", qid)
    for i in (1, 2, 3):
        _sit(client, basis_id, qid, C(i), f"basis-c{i}", ["B"])
        _publish(client, basis_id, f"cand{i}")
    m_id = _exam(client, "补考", qid)
    r = client.post(
        f"/admin/exams/{m_id}/makeup",
        auth=ADMIN,
        json={
            "op_id": "mk-cap2",
            "basis_exam_id": basis_id,
            "pass_score": 5,
            "invite_ttl_seconds": 300,
            "sessions": [{"name": "大场", "duration_seconds": 600, "capacity": 2}],
        },
    )
    assert r.status_code == 201, r.text
    s = r.json()["sessions"][0]["session_id"]

    app1 = _apply(client, m_id, C(1), s, "app-1").json()
    app2 = _apply(client, m_id, C(2), s, "app-2").json()
    app3 = _apply(client, m_id, C(3), s, "app-3").json()
    assert app1["state"] == app2["state"] == "invited"
    assert app3["state"] == "waitlisted"
    _confirm(client, m_id, C(1), app1["invitation"]["invitation_id"], "cfm-1")

    # cand2 and cand3 are re-scored above the pass line and re-published.
    for i in (2, 3):
        client.put(
            f"/grading/exams/{basis_id}/candidates/cand{i}/questions/{qid}/manual-score",
            auth=GRADER,
            json={"score": 10, "reason": "复核加分"},
        )
        _publish(client, basis_id, f"cand{i}", version=2)

    staff = _staff(client, m_id)
    by_user = {}
    for a in staff["applications"]:
        by_user.setdefault(a["username"], []).append(a["state"])
    assert by_user["cand1"] == ["confirmed"]  # confirmed seats are never revoked
    assert by_user["cand2"] == ["voided"]
    assert by_user["cand3"] == ["voided"]
    assert _session(staff, "大场")["free_seats"] == 1

    # While ineligible, re-applying is rejected...
    r = _apply(client, m_id, C(2), s, "app-2b")
    assert r.status_code == 403
    # ...and even after eligibility returns, a brand-new application is needed.
    client.put(
        f"/grading/exams/{basis_id}/candidates/cand2/questions/{qid}/manual-score",
        auth=GRADER,
        json={"score": None, "reason": "撤销加分"},
    )
    _publish(client, basis_id, "cand2", version=3)
    r = _apply(client, m_id, C(2), s, "app-2c")
    assert r.status_code == 201 and r.json()["state"] == "invited"
    states = [a["state"] for a in _staff(client, m_id)["applications"] if a["username"] == "cand2"]
    assert states == ["voided", "invited"]  # the voided one stays void


def test_confirm_requires_current_eligibility(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    basis_id = makeup["basis_id"]
    qid = makeup["qid"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]

    app1 = _apply(client, m_id, C(1), s_a, "app-1").json()
    inv1 = app1["invitation"]["invitation_id"]
    # A new published version pushes cand1 above the pass line before they confirm.
    client.put(
        f"/grading/exams/{basis_id}/candidates/cand1/questions/{qid}/manual-score",
        auth=GRADER,
        json={"score": 10, "reason": "复核加分"},
    )
    _publish(client, basis_id, "cand1", version=2)
    r = _confirm(client, m_id, C(1), inv1, "cfm-1")
    assert r.status_code == 409  # the invitation was voided by the publish
    assert _my(client, m_id, C(1))["my_applications"][0]["state"] == "voided"


# ---------------------------------------------------------------- exam start


def test_exam_start_cancels_pending_switch_and_locks_everything(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]
    s_b = makeup["sessions"]["补考乙场"]["session_id"]

    app1 = _apply(client, m_id, C(1), s_a, "app-1").json()
    _confirm(client, m_id, C(1), app1["invitation"]["invitation_id"], "cfm-1")
    # Pending switch: invited for B; cand2 queues for B behind them.
    app1b = _apply(client, m_id, C(1), s_b, "app-1b").json()
    inv_b = app1b["invitation"]["invitation_id"]
    _apply(client, m_id, C(2), s_b, "app-2")

    # First successful start (in the finally confirmed session A).
    paper = client.get(f"/exams/{m_id}/paper", auth=C(1)).json()
    assert paper["timing"]["session_id"] == s_a

    mine = _my(client, m_id, C(1))
    states = {a["session_id"]: a["state"] for a in mine["my_applications"]}
    assert states[s_a] == "completed"
    assert states[s_b] == "cancelled"  # unfinished switch application cancelled
    # The released B invitation promoted cand2.
    assert _my(client, m_id, C(2))["my_applications"][0]["state"] == "invited"

    # No more unsubscribe / switch / apply / confirm after the start.
    assert _unsubscribe(client, m_id, C(1), "unsub-late").status_code == 409
    assert _apply(client, m_id, C(1), s_b, "app-late").status_code == 409
    assert _confirm(client, m_id, C(1), inv_b, "cfm-late").status_code == 409


def test_makeup_session_pause_and_time_limit_still_apply(harness, makeup):
    client, clock, _ = harness
    m_id = makeup["makeup_id"]
    qid = makeup["qid"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]

    app1 = _apply(client, m_id, C(1), s_a, "app-1").json()
    _confirm(client, m_id, C(1), app1["invitation"]["invitation_id"], "cfm-1")

    # Session-level pause blocks the start, resume re-enables it.
    client.post(
        f"/admin/exams/{m_id}/sessions/{s_a}/pause",
        auth=ADMIN,
        json={"op_id": "mp-1", "reason": "设备检查"},
    )
    assert client.get(f"/exams/{m_id}/paper", auth=C(1)).status_code == 409
    client.post(
        f"/admin/exams/{m_id}/sessions/{s_a}/resume", auth=ADMIN, json={"op_id": "mr-1"}
    )
    paper = client.get(f"/exams/{m_id}/paper", auth=C(1)).json()
    assert paper["timing"]["remaining_seconds"] == pytest.approx(600.0)

    # The session's time limit lazily auto-submits, exactly like timed exams.
    client.post(
        f"/exams/{m_id}/answers",
        auth=C(1),
        json={"op_id": "mk-save", "base_revision": 0, "answers": {str(qid): ["A"]}},
    )
    clock.advance(600)
    paper = client.get(f"/exams/{m_id}/paper", auth=C(1)).json()
    assert paper["status"] == "submitted"
    assert paper["timing"]["auto_submitted_at"] is not None


# ---------------------------------------------------------------- idempotency


def test_op_id_replays_do_not_move_seats_twice(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]
    s_b = makeup["sessions"]["补考乙场"]["session_id"]

    r1 = _apply(client, m_id, C(1), s_a, "op-apply")
    assert r1.status_code == 201
    r2 = _apply(client, m_id, C(1), s_a, "op-apply")
    assert r2.status_code == 200 and r2.json()["idempotent_replay"] is True
    assert r2.json()["application_id"] == r1.json()["application_id"]
    # Same op_id with a different request body is rejected.
    assert _apply(client, m_id, C(1), s_b, "op-apply").status_code == 409
    # Only one application exists.
    assert len(_my(client, m_id, C(1))["my_applications"]) == 1

    inv = r1.json()["invitation"]["invitation_id"]
    c1 = _confirm(client, m_id, C(1), inv, "op-confirm")
    assert c1.status_code == 200
    c2 = _confirm(client, m_id, C(1), inv, "op-confirm")
    assert c2.json()["idempotent_replay"] is True
    # The replay did not create a second enrollment or move anything.
    exam_view = client.get(f"/admin/exams/{m_id}", auth=ADMIN).json()
    enrolled = exam_view["sessions"][0]["candidates"]
    assert enrolled == ["cand1"]
    assert _session(_staff(client, m_id), "补考甲场")["confirmed"] == 1

    u1 = _unsubscribe(client, m_id, C(1), "op-unsub")
    assert u1.status_code == 200
    u2 = _unsubscribe(client, m_id, C(1), "op-unsub")
    assert u2.json()["idempotent_replay"] is True
    session = _session(_staff(client, m_id), "补考甲场")
    assert session["confirmed"] == 0 and session["free_seats"] == 1

    # op_id is required for every write.
    r = client.post(
        f"/exams/{m_id}/makeup/applications", auth=C(2), json={"session_id": s_a}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------- isolation


def test_candidates_only_see_and_touch_their_own(harness, makeup):
    client, _, _ = harness
    m_id = makeup["makeup_id"]
    s_a = makeup["sessions"]["补考甲场"]["session_id"]

    app1 = _apply(client, m_id, C(1), s_a, "app-1").json()
    inv1 = app1["invitation"]["invitation_id"]

    # Another candidate cannot confirm/withdraw cand1's objects (404, not 403).
    assert _confirm(client, m_id, C(2), inv1, "cfm-x").status_code == 404
    assert _withdraw(client, m_id, C(2), app1["application_id"], "wd-x").status_code == 404
    # Their own view lists only their own applications.
    _apply(client, m_id, C(2), s_a, "app-2")
    mine2 = _my(client, m_id, C(2))
    assert len(mine2["my_applications"]) == 1
    assert mine2["my_applications"][0]["candidate_id"] != app1["candidate_id"]
    # Staff see everything, candidates cannot use the staff view.
    assert len(_staff(client, m_id)["applications"]) == 2
    assert client.get(f"/admin/exams/{m_id}/makeup", auth=C(1)).status_code == 403


# ---------------------------------------------------------------- concurrency


def test_concurrent_applications_never_overbook(harness):
    client, clock, _ = harness
    q = _question(client)
    qid = q["question_id"]
    basis_id = _exam(client, "原测评", qid)
    for i in range(1, 7):
        _sit(client, basis_id, qid, C(i), f"basis-c{i}", ["B"])
        _publish(client, basis_id, f"cand{i}")
    m_id = _exam(client, "补考", qid)
    r = client.post(
        f"/admin/exams/{m_id}/makeup",
        auth=ADMIN,
        json={
            "op_id": "mk-race",
            "basis_exam_id": basis_id,
            "pass_score": 5,
            "invite_ttl_seconds": 300,
            "sessions": [{"name": "双人场", "duration_seconds": 600, "capacity": 2}],
        },
    )
    assert r.status_code == 201, r.text
    s = r.json()["sessions"][0]["session_id"]

    barrier = threading.Barrier(6)

    def apply(i):
        barrier.wait()
        return _apply(client, m_id, C(i), s, f"race-app-{i}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(apply, range(1, 7)))
    assert all(r.status_code == 201 for r in results)
    states = [r.json()["state"] for r in results]
    assert sorted(states) == ["invited", "invited"] + ["waitlisted"] * 4
    session = _session(_staff(client, m_id), "双人场")
    assert session["invited"] == 2 and session["waitlisted"] == 4
    assert session["free_seats"] == 0

    # Both invitees confirm concurrently: exactly two seats, no overbooking.
    invited = [r.json()["invitation"]["invitation_id"] for r in results if r.json()["state"] == "invited"]
    assert len(invited) == 2

    def confirm(pair):
        i, inv = pair
        barrier2.wait()
        return _confirm(client, m_id, C(i), inv, f"race-cfm-{i}")

    barrier2 = threading.Barrier(2)
    invited_users = [
        i for i, r in zip(range(1, 7), results) if r.json()["state"] == "invited"
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        confirms = list(pool.map(confirm, zip(invited_users, invited)))
    assert all(r.status_code == 200 for r in confirms)
    session = _session(_staff(client, m_id), "双人场")
    assert session["confirmed"] == 2 and session["invited"] == 0
    assert session["free_seats"] == 0
