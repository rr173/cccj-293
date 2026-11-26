"""HTTP demo for makeup-session reservations (补考分场预约).

Drives a deterministic in-process server (FastAPI TestClient) with a
VirtualClock, so invitation deadlines expire without real waiting. Every
interaction is a real HTTP call with HTTP Basic auth; request/response bodies
are printed. The script walks through the full journey:

    开设补考预约 -> 申请 -> 邀请/确认 -> 候补递补 -> 换场 ->
    邀请到期 -> 重新申请 -> 开考锁定 -> 资格作废 -> 幂等重放

Run:
    .venv/bin/python scripts/demo_makeup_http.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from exam_service.clock import VirtualClock  # noqa: E402
from exam_service.main import create_app  # noqa: E402
from exam_service.security import hash_password  # noqa: E402

ADMIN = ("admin", "admin-pass-123")
GRADER = ("grader", "grader-pass-123")


def C(i: int):
    return (f"cand{i}", f"pass-{i}")


def _print_step(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


class Demo:
    def __init__(self, client: TestClient, clock: VirtualClock):
        self.client = client
        self.clock = clock

    def request(self, method: str, path: str, auth, json_body=None):
        print(f"\n> {method} {path}  (as {auth[0]})")
        if json_body is not None:
            print("  body:", json.dumps(json_body, ensure_ascii=False))
        resp = self.client.request(method, path, auth=auth, json=json_body)
        print(f"-> HTTP {resp.status_code}")
        print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
        return resp

    def tick(self, seconds: int, note: str = "") -> None:
        self.clock.advance(seconds)
        print(f"\n[时钟推进 {seconds}s] {note} 服务端时间现为 {self.clock.now_iso()}")

    def run(self) -> None:
        # ---------- 1. basis exam with published results ----------
        _print_step("1. 原测评：cand1~cand3 不及格（0 分），cand4 及格（10 分），成绩均已发布")
        q = self.request(
            "POST", "/admin/questions", ADMIN,
            {
                "qtype": "single",
                "stem": "Python 的 Web 框架是？",
                "options": [
                    {"option_id": "A", "text": "FastAPI"},
                    {"option_id": "B", "text": "Requests"},
                ],
                "correct_option_ids": ["A"],
                "max_score": 10,
            },
        ).json()
        qid = q["question_id"]
        basis_id = self.request(
            "POST", "/admin/exams", ADMIN,
            {"title": "2026 秋季测评（原测评）",
             "questions": [{"question_id": qid, "version": 1}], "shuffle": False},
        ).json()["exam_id"]
        for i, options in [(1, ["B"]), (2, ["B"]), (3, ["B"]), (4, ["A"])]:
            auth = C(i)
            self.client.get(f"/exams/{basis_id}/paper", auth=auth)
            self.client.post(
                f"/exams/{basis_id}/answers", auth=auth,
                json={"op_id": f"basis-c{i}-save", "base_revision": 0,
                      "answers": {str(qid): options}},
            )
            self.client.post(
                f"/exams/{basis_id}/submit", auth=auth, json={"op_id": f"basis-c{i}-submit"}
            )
            self.request(
                "POST", f"/grading/exams/{basis_id}/candidates/cand{i}/publish", GRADER,
                {"result_version": 1},
            )
        print("   cand1~cand3 已发布 0 分（低于合格线），cand4 已发布 10 分")

        # ---------- 2. open the makeup campaign ----------
        _print_step("2. 管理员为尚未开考、未配置限时场次的补考测评开设补考预约")
        makeup_id = self.request(
            "POST", "/admin/exams", ADMIN,
            {"title": "2026 秋季测评补考",
             "questions": [{"question_id": qid, "version": 1}], "shuffle": False},
        ).json()["exam_id"]
        campaign = self.request(
            "POST", f"/admin/exams/{makeup_id}/makeup", ADMIN,
            {
                "op_id": "mk-open-01",
                "basis_exam_id": basis_id,
                "pass_score": 5,
                "invite_ttl_seconds": 300,
                "sessions": [
                    {"name": "补考甲场", "duration_seconds": 600, "capacity": 1},
                    {"name": "补考乙场", "duration_seconds": 900, "capacity": 1},
                ],
            },
        ).json()
        s_a = next(s for s in campaign["sessions"] if s["name"] == "补考甲场")["session_id"]
        s_b = next(s for s in campaign["sessions"] if s["name"] == "补考乙场")["session_id"]
        print(f"   甲场 session_id={s_a}（600s/1 座），乙场 session_id={s_b}（900s/1 座），确认时限 300s")

        # ---------- 3. eligibility gate ----------
        _print_step("3. 资格门槛：已发布总分 < 合格线才可申请（cand4 及格被拒）")
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/applications", C(4),
            {"op_id": "app-c4-01", "session_id": s_a},
        )  # 403

        # ---------- 4. apply -> invited -> confirm ----------
        _print_step("4. cand1 申请甲场：有空位立即获得邀请；确认后才进入名单、才能开考")
        app1 = self.request(
            "POST", f"/exams/{makeup_id}/makeup/applications", C(1),
            {"op_id": "app-c1-01", "session_id": s_a},
        ).json()
        inv1 = app1["invitation"]["invitation_id"]
        self.request("GET", f"/exams/{makeup_id}/paper", C(1))  # 403 before confirm
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/invitations/{inv1}/confirm", C(1),
            {"op_id": "cfm-c1-01"},
        )

        # ---------- 5. waitlist -> promotion on unsubscribe ----------
        _print_step("5. 甲场已满：cand2 申请进入候补；cand1 退订后按先后递补并发出邀请")
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/applications", C(2),
            {"op_id": "app-c2-01", "session_id": s_a},
        )
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/unsubscribe", C(1),
            {"op_id": "unsub-c1-01"},
        )
        mine2 = self.request("GET", f"/exams/{makeup_id}/makeup", C(2)).json()
        inv2 = mine2["my_applications"][0]["invitation"]["invitation_id"]
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/invitations/{inv2}/confirm", C(2),
            {"op_id": "cfm-c2-01"},
        )

        # ---------- 6. switch sessions ----------
        _print_step("6. 换场：cand2 候补乙场（原位保留，两边各占一个名额），确认新邀请才释放原位")
        app2b = self.request(
            "POST", f"/exams/{makeup_id}/makeup/applications", C(2),
            {"op_id": "app-c2-02", "session_id": s_b},
        ).json()
        self.request("GET", f"/admin/exams/{makeup_id}/makeup", ADMIN)
        inv2b = app2b["invitation"]["invitation_id"]
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/invitations/{inv2b}/confirm", C(2),
            {"op_id": "cfm-c2-02"},
        )
        print("   甲场座位已释放，cand2 现在持有乙场座位")

        # ---------- 7. invitation expiry ----------
        _print_step("7. cand3 申请甲场获得邀请；时钟推过确认时限，邀请到期、申请结束")
        app3 = self.request(
            "POST", f"/exams/{makeup_id}/makeup/applications", C(3),
            {"op_id": "app-c3-01", "session_id": s_a},
        ).json()
        inv3 = app3["invitation"]["invitation_id"]
        self.tick(301, "cand3 迟迟未确认，服务期间无任何请求")
        self.request("GET", f"/exams/{makeup_id}/makeup", C(3))
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/invitations/{inv3}/confirm", C(3),
            {"op_id": "cfm-c3-late"},
        )  # 409: old invitation is dead
        print("   旧邀请已作废，必须重新申请")
        app3b = self.request(
            "POST", f"/exams/{makeup_id}/makeup/applications", C(3),
            {"op_id": "app-c3-02", "session_id": s_a},
        ).json()
        inv3b = app3b["invitation"]["invitation_id"]
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/invitations/{inv3b}/confirm", C(3),
            {"op_id": "cfm-c3-02"},
        )

        # ---------- 8. idempotent replay ----------
        _print_step("8. 幂等重放：重复确认同一张邀请返回首次结果，不再占用/释放座位")
        replay = self.request(
            "POST", f"/exams/{makeup_id}/makeup/invitations/{inv3b}/confirm", C(3),
            {"op_id": "cfm-c3-02"},
        ).json()
        print(f"   idempotent_replay={replay['idempotent_replay']}，座位数不变")

        # ---------- 9. exam start locks everything ----------
        _print_step("9. cand2 首次成功开考（乙场，900s）：未完成的换场申请取消，不能再退订/换场")
        app2c = self.request(
            "POST", f"/exams/{makeup_id}/makeup/applications", C(2),
            {"op_id": "app-c2-03", "session_id": s_a},
        ).json()
        print(f"   开考前 cand2 又候补甲场（状态 {app2c['state']}）")
        paper = self.request("GET", f"/exams/{makeup_id}/paper", C(2)).json()
        print(
            f"   开考成功：场次={paper['timing']['session_name']}，"
            f"时长={paper['timing']['duration_seconds']}s"
        )
        self.request("GET", f"/exams/{makeup_id}/makeup", C(2))
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/unsubscribe", C(2),
            {"op_id": "unsub-c2-late"},
        )  # 409

        # ---------- 10. eligibility loss voids pending applications ----------
        _print_step("10. 资格作废：cand1 重新申请后，原测评发布新成绩（10 分）使其候补作废")
        self.request(
            "POST", f"/exams/{makeup_id}/makeup/applications", C(1),
            {"op_id": "app-c1-02", "session_id": s_a},
        )
        self.request(
            "PUT",
            f"/grading/exams/{basis_id}/candidates/cand1/questions/{qid}/manual-score",
            GRADER,
            {"score": 10, "reason": "复核后应得满分"},
        )
        self.request(
            "POST", f"/grading/exams/{basis_id}/candidates/cand1/publish", GRADER,
            {"result_version": 2},
        )
        self.request("GET", f"/exams/{makeup_id}/makeup", C(1))
        print("   cand1 的候补已作废（即使以后恢复资格也须重新申请）；已确认座位不受影响")
        self.request("GET", f"/admin/exams/{makeup_id}/makeup", ADMIN)
        print("\n补考分场预约演示完成。")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "makeup_demo.db"
        clock = VirtualClock(datetime(2026, 11, 26, 9, 0, 0, tzinfo=timezone.utc))
        app = create_app(db_path, clock=clock)
        raw = sqlite3.connect(db_path)
        for i in range(1, 5):
            raw.execute(
                "INSERT INTO users (username, password_hash, role, display_name)"
                " VALUES (?, ?, 'candidate', ?)",
                (f"cand{i}", hash_password(f"pass-{i}"), f"考生{i}"),
            )
        raw.commit()
        raw.close()
        with TestClient(app) as client:
            Demo(client, clock).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
