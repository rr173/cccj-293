"""HTTP demo for timed sessions, two-layer pause/resume and lazy auto-submit.

Unlike ``demo_http.py`` this script drives a deterministic in-process server
(FastAPI TestClient) whose clock is a VirtualClock, so a 10-minute exam can be
demonstrated without sleeping. Every interaction is still a real HTTP call
with HTTP Basic auth, and the real request/response bodies are printed.

Run:
    .venv/bin/python scripts/demo_timed_http.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
from fastapi.testclient import TestClient

from exam_service.clock import VirtualClock
from exam_service.main import create_app
from exam_service.security import hash_password

ADMIN = ("admin", "admin-pass-123")
GRADER = ("grader", "grader-pass-123")
CAND1 = ("cand1", "pass-1")
CAND2 = ("cand2", "pass-2")


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
        # ---------- 1. question + timed exam with two sessions ----------
        _print_step("1. 发卷并配置两个限时场次（各 600 秒），名单外考生无法开考")
        q = self.request(
            "POST",
            "/admin/questions",
            ADMIN,
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
        exam_id = self.request(
            "POST",
            "/admin/exams",
            ADMIN,
            {
                "title": "2026 限时测评",
                "questions": [{"question_id": q["question_id"], "version": 1}],
                "shuffle": False,
            },
        ).json()["exam_id"]
        config = self.request(
            "POST",
            f"/admin/exams/{exam_id}/timed-config",
            ADMIN,
            {
                "sessions": [
                    {
                        "name": "上午场",
                        "duration_seconds": 600,
                        "candidate_usernames": ["cand1"],
                    },
                    {
                        "name": "下午场",
                        "duration_seconds": 600,
                        "candidate_usernames": ["cand2"],
                    },
                ]
            },
        ).json()
        session_1 = next(s for s in config["sessions"] if s["name"] == "上午场")["session_id"]
        session_2 = next(s for s in config["sessions"] if s["name"] == "下午场")["session_id"]

        # ---------- 2. roster gate ----------
        _print_step("2. 名单外考生 cand3 开考被拒（403）；名单内考生首次开考才计时")
        self.request("GET", f"/exams/{exam_id}/paper", ("cand3", "x"))
        paper1 = self.request("GET", f"/exams/{exam_id}/paper", CAND1).json()
        self.request("GET", f"/exams/{exam_id}/paper", CAND2)
        print(f"   cand1 开考时间：{paper1['timing']['started_at']}，剩余 {paper1['timing']['remaining_seconds']}s")

        # ---------- 3. save, then session pause / resume ----------
        _print_step("3. cand1 保存一版答案；100s 后监考暂停上午场（设备故障）")
        self.request(
            "POST",
            f"/exams/{exam_id}/answers",
            CAND1,
            {"op_id": "c1-save-1", "base_revision": 0,
             "answers": {str(q["question_id"]): ["A"]}},
        )
        self.tick(100, "cand1 正常作答中")
        self.request(
            "POST",
            f"/admin/exams/{exam_id}/sessions/{session_1}/pause",
            ADMIN,
            {"op_id": "pause-s1", "reason": "考场设备故障"},
        )

        # ---------- 4. overlapping candidate pause; writes blocked ----------
        _print_step("4. 场次暂停期间再暂停 cand1 本人（两层重叠只停一次表）；保存/交卷均被拒")
        self.tick(50)
        self.request(
            "POST",
            f"/admin/exams/{exam_id}/candidates/cand1/pause",
            ADMIN,
            {"op_id": "pause-c1", "reason": "个人终端死机"},
        )
        self.request(
            "POST",
            f"/exams/{exam_id}/answers",
            CAND1,
            {"op_id": "blocked-save", "base_revision": 1,
             "answers": {str(q["question_id"]): ["B"]}},
        )  # expected 409
        # cand2 belongs to another session: completely unaffected.
        paper2 = self.request("GET", f"/exams/{exam_id}/paper", CAND2).json()
        print(f"   别场 cand2 仍在计时，剩余 {paper2['timing']['remaining_seconds']}s，paused={paper2['timing']['paused']}")

        self.tick(50)
        self.request(
            "POST",
            f"/admin/exams/{exam_id}/sessions/{session_1}/resume",
            ADMIN,
            {"op_id": "resume-s1"},
        )
        # Session resumed but candidate layer is still paused.
        self.request(
            "POST",
            f"/exams/{exam_id}/answers",
            CAND1,
            {"op_id": "still-blocked", "base_revision": 1,
             "answers": {str(q["question_id"]): ["B"]}},
        )  # expected 409
        self.tick(50)
        self.request(
            "POST",
            f"/admin/exams/{exam_id}/candidates/cand1/resume",
            ADMIN,
            {"op_id": "resume-c1"},
        )
        paper1 = self.request("GET", f"/exams/{exam_id}/paper", CAND1).json()
        print(
            "   暂停并集 150s（[100,200]∪[150,250]），已作答 100s，"
            f"剩余应为 500s，实际 {paper1['timing']['remaining_seconds']}s"
        )

        # ---------- 5. idempotent replay of a pause ----------
        _print_step("5. 重放旧的暂停 op_id：标记 idempotent_replay，不会重新停场")
        replay = self.request(
            "POST",
            f"/admin/exams/{exam_id}/sessions/{session_1}/pause",
            ADMIN,
            {"op_id": "pause-s1", "reason": "考场设备故障"},
        ).json()
        print(f"   重放结果 idempotent_replay={replay['idempotent_replay']}，事件表不新增行")

        # ---------- 6. time exhaustion -> lazy auto submit ----------
        _print_step("6. 时钟推过截止时刻；期间无任何请求，首次取卷即看到自动交卷")
        self.tick(500, "cand1 继续作答直至超时（含前面 100s，共 600s 作答时间）")
        paper1 = self.request("GET", f"/exams/{exam_id}/paper", CAND1).json()
        print(
            f"   状态={paper1['status']}，自动交卷时刻={paper1['timing']['auto_submitted_at']}，"
            f"已保存答案保留={paper1['saved_answers']}"
        )
        self.request(
            "POST",
            f"/exams/{exam_id}/answers",
            CAND1,
            {"op_id": "too-late", "base_revision": 1,
             "answers": {str(q["question_id"]): ["B"]}},
        )  # expected 409 locked

        # ---------- 7. grading pipeline on the auto result ----------
        _print_step("7. 自动生成的待发布成绩沿用既有重算/人工分/发布流程")
        results = self.request(
            "GET",
            f"/grading/exams/{exam_id}/candidates/cand1/results",
            GRADER,
        ).json()["results"]
        print(f"   自动成绩版本 v{results[0]['result_version']}，总分 {results[0]['total_score']}")
        self.request(
            "POST",
            f"/grading/exams/{exam_id}/candidates/cand1/publish",
            GRADER,
            {"result_version": results[0]["result_version"]},
        )
        self.request("GET", f"/exams/{exam_id}/result", CAND1)
        self.request(
            "GET", f"/admin/exams/{exam_id}/proctor-events", GRADER
        )
        print("\n限时场次演示完成。")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "timed_demo.db"
        clock = VirtualClock(datetime(2026, 11, 26, 9, 0, 0, tzinfo=timezone.utc))
        app = create_app(db_path, clock=clock)
        # Extra candidate accounts (no user-admin API exists in this service).
        raw = sqlite3.connect(db_path)
        for username, password, display in [
            ("cand1", "pass-1", "考生甲"),
            ("cand2", "pass-2", "考生乙"),
            ("cand3", "pass-3", "考生丙"),
        ]:
            raw.execute(
                "INSERT INTO users (username, password_hash, role, display_name)"
                " VALUES (?, ?, 'candidate', ?)",
                (username, hash_password(password), display),
            )
        raw.commit()
        raw.close()
        with TestClient(app) as client:
            Demo(client, clock).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
