"""End-to-end HTTP demo against a live uvicorn server.

Run:
    .venv/bin/python scripts/demo_http.py
or against an already-running server:
    .venv/bin/python scripts/demo_http.py --base-url http://127.0.0.1:8000

The script uses ONLY HTTP (no direct DB access) and prints the real request
and response of every step, including the 409 conflict responses required by
the spec.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ADMIN = ("admin", "admin-pass-123")
GRADER = ("grader", "grader-pass-123")
CANDIDATE = ("candidate", "candidate-pass-123")


def _print_step(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show(resp: httpx.Response) -> dict:
    body = resp.json()
    print(f"-> HTTP {resp.status_code}")
    import json

    print(json.dumps(body, ensure_ascii=False, indent=2))
    return body


class Demo:
    def __init__(self, base_url: str):
        self.http = httpx.Client(base_url=base_url, timeout=10)

    def request(self, method: str, path: str, auth, json_body=None) -> httpx.Response:
        print(f"\n> {method} {path}  (as {auth[0]})")
        if json_body is not None:
            import json

            print("  body:", json.dumps(json_body, ensure_ascii=False))
        return self.http.request(method, path, auth=auth, json=json_body)

    def run(self) -> None:
        # ---------- 0. identity ----------
        _print_step("0. 最小账号凭据：HTTP Basic（admin / grader / candidate）")
        show(self.request("GET", "/me", CANDIDATE))

        # ---------- 1. question bank: two questions ----------
        _print_step("1. 管理员建两道题（单选 5 分 + 多选 10 分），选项带稳定 ID")
        q1 = show(
            self.request(
                "POST",
                "/admin/questions",
                ADMIN,
                {
                    "qtype": "single",
                    "stem": "单题：下列哪个是 Python 的 Web 框架？",
                    "options": [
                        {"option_id": "A", "text": "FastAPI"},
                        {"option_id": "B", "text": "Requests"},
                        {"option_id": "C", "text": "Pillow"},
                    ],
                    "correct_option_ids": ["A"],
                    "max_score": 5,
                },
            )
        )
        q2 = show(
            self.request(
                "POST",
                "/admin/questions",
                ADMIN,
                {
                    "qtype": "multi",
                    "stem": "多题：以下哪些是 Python 内置类型？",
                    "options": [
                        {"option_id": "X", "text": "list"},
                        {"option_id": "Y", "text": "dict"},
                        {"option_id": "Z", "text": "numpy.ndarray"},
                    ],
                    "correct_option_ids": ["X", "Y"],
                    "max_score": 10,
                },
            )
        )
        q1_id, q2_id = q1["question_id"], q2["question_id"]

        # ---------- 2. issue exam with pinned versions ----------
        _print_step("2. 发卷：指定题目版本，快照题干/选项/满分，随机但固定展示顺序")
        exam = show(
            self.request(
                "POST",
                "/admin/exams",
                ADMIN,
                {
                    "title": "2026 秋季最小演示测评",
                    "questions": [
                        {"question_id": q1_id, "version": 1},
                        {"question_id": q2_id, "version": 1},
                    ],
                    "shuffle": True,
                },
            )
        )
        exam_id = exam["exam_id"]

        # ---------- 3. reconnect: same paper order ----------
        _print_step("3. 考生取卷，重连一次确认题目/选项顺序不变（不含答案键）")
        paper1 = show(self.request("GET", f"/exams/{exam_id}/paper", CANDIDATE))
        paper2 = show(self.request("GET", f"/exams/{exam_id}/paper", CANDIDATE))
        order1 = [(q["question_id"], [o["option_id"] for o in q["options"]]) for q in paper1["questions"]]
        order2 = [(q["question_id"], [o["option_id"] for o in q["options"]]) for q in paper2["questions"]]
        assert order1 == order2, "reconnect changed the display order!"
        print(f"   两次取卷顺序一致：{order1}")

        # ---------- 4. interleaved saves: success, stale conflict, idempotent ----------
        _print_step("4. 保存与提交交错：保存成功 -> 过期修订 409 -> 同 op_id 幂等重放")
        show(
            self.request(
                "POST",
                f"/exams/{exam_id}/answers",
                CANDIDATE,
                {
                    "op_id": "demo-save-1",
                    "base_revision": 0,
                    "answers": {str(q1_id): ["A"]},
                },
            )
        )
        show(  # expected 409
            self.request(
                "POST",
                f"/exams/{exam_id}/answers",
                CANDIDATE,
                {
                    "op_id": "demo-save-stale",
                    "base_revision": 0,
                    "answers": {str(q1_id): ["B"]},
                },
            )
        )
        show(  # idempotent replay, stays at revision 1
            self.request(
                "POST",
                f"/exams/{exam_id}/answers",
                CANDIDATE,
                {
                    "op_id": "demo-save-1",
                    "base_revision": 0,
                    "answers": {str(q1_id): ["A"]},
                },
            )
        )
        show(
            self.request(
                "POST",
                f"/exams/{exam_id}/answers",
                CANDIDATE,
                {
                    "op_id": "demo-save-2",
                    "base_revision": 1,
                    "answers": {
                        str(q1_id): ["A"],
                        # deliberately incomplete multi answer -> 0 points
                        str(q2_id): ["X"],
                    },
                },
            )
        )

        # ---------- 5. submit, retry, locked save ----------
        _print_step("5. 提交即机评；重试同一 op_id 不产生第二次提交；提交后改答被拒")
        submit = show(
            self.request(
                "POST",
                f"/exams/{exam_id}/submit",
                CANDIDATE,
                {"op_id": "demo-submit-1"},
            )
        )
        assert submit["graded_result_version"] == 1
        show(  # idempotent replay
            self.request(
                "POST",
                f"/exams/{exam_id}/submit",
                CANDIDATE,
                {"op_id": "demo-submit-1"},
            )
        )
        show(  # expected 409 locked
            self.request(
                "POST",
                f"/exams/{exam_id}/answers",
                CANDIDATE,
                {
                    "op_id": "demo-save-locked",
                    "base_revision": 2,
                    "answers": {str(q1_id): ["C"]},
                },
            )
        )
        show(  # candidate cannot see anything before publish -> 404
            self.request("GET", f"/exams/{exam_id}/result", CANDIDATE)
        )

        # ---------- 6. publish v1 ----------
        _print_step("6. 阅卷端查看待发布结果 v1 并发布，考生随后可见（5 + 0 = 5 分）")
        show(
            self.request(
                "GET",
                f"/grading/exams/{exam_id}/candidates/candidate/results",
                GRADER,
            )
        )
        show(
            self.request(
                "POST",
                f"/grading/exams/{exam_id}/candidates/candidate/publish",
                GRADER,
                {"result_version": 1},
            )
        )
        show(self.request("GET", f"/exams/{exam_id}/result", CANDIDATE))

        # ---------- 7. answer key correction + regrade ----------
        _print_step("7. 管理员勘误答案键 -> 新评分方案；重算生成 v2，原卷内容不变")
        show(
            self.request(
                "POST",
                f"/admin/exams/{exam_id}/answer-key",
                ADMIN,
                {
                    "reason": "勘误：多题正确集合调整为仅 X",
                    "answer_key": [
                        {"question_id": q1_id, "correct_option_ids": ["A"]},
                        {"question_id": q2_id, "correct_option_ids": ["X"]},
                    ],
                },
            )
        )
        paper_after = show(
            self.request("GET", f"/exams/{exam_id}/paper", CANDIDATE)
        )
        assert [q["stem"] for q in paper_after["questions"]] == [
            q["stem"] for q in paper1["questions"]
        ]
        print("   原卷题干与选项未被题库/答案键编辑改动")
        show(
            self.request(
                "POST",
                f"/grading/exams/{exam_id}/candidates/candidate/regrade",
                GRADER,
            )
        )

        # ---------- 8. manual score, kept through regrade, then revoked ----------
        _print_step("8. 人工分 8/10 与理由（v3）；再次重算后人工分仍优先；撤销后回机评（v4）")
        show(
            self.request(
                "PUT",
                f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2_id}/manual-score",
                GRADER,
                {"score": 8, "reason": "考生思路正确，按人工尺度给 8 分"},
            )
        )
        show(
            self.request(
                "POST",
                f"/grading/exams/{exam_id}/candidates/candidate/regrade",
                ADMIN,
            )
        )
        show(
            self.request(
                "PUT",
                f"/grading/exams/{exam_id}/candidates/candidate/questions/{q2_id}/manual-score",
                GRADER,
                {"score": None, "reason": "撤销人工分，恢复机评"},
            )
        )

        # ---------- 9. stale publish conflict ----------
        _print_step("9. 考生已看到的仍是 v1；尝试发布旧候选 v2 必须 409 冲突")
        seen = show(self.request("GET", f"/exams/{exam_id}/result", CANDIDATE))
        assert seen["result_version"] == 1, "unpublished change leaked to candidate"
        show(  # expected 409
            self.request(
                "POST",
                f"/grading/exams/{exam_id}/candidates/candidate/publish",
                GRADER,
                {"result_version": 2},
            )
        )

        # ---------- 10. publish latest, old versions retained ----------
        _print_step("10. 发布最新候选版本；旧版本以 superseded 保留供追溯，考生看到新总分")
        latest = self.http.get(
            f"/grading/exams/{exam_id}/candidates/candidate/results",
            auth=GRADER,
        ).json()["results"][-1]["result_version"]
        show(
            self.request(
                "POST",
                f"/grading/exams/{exam_id}/candidates/candidate/publish",
                GRADER,
                {"result_version": latest},
            )
        )
        show(self.request("GET", f"/exams/{exam_id}/result", CANDIDATE))
        show(
            self.request(
                "GET",
                f"/grading/exams/{exam_id}/candidates/candidate/results",
                GRADER,
            )
        )
        show(
            self.request(
                "GET",
                f"/grading/exams/{exam_id}/candidates/candidate/history",
                GRADER,
            )
        )

        # ---------- 11. question bank edit does not touch the issued exam ----------
        _print_step("11. 题库题目编辑为 v2（新题干/新满分），验证已发卷完全不变")
        show(
            self.request(
                "POST",
                f"/admin/questions/{q1_id}/versions",
                ADMIN,
                {
                    "qtype": "single",
                    "stem": "【题库新版】这道题已经被重写，不应影响旧测评",
                    "options": [
                        {"option_id": "A", "text": "新A"},
                        {"option_id": "B", "text": "新B"},
                    ],
                    "correct_option_ids": ["B"],
                    "max_score": 99,
                },
            )
        )
        exam_view = show(self.request("GET", f"/admin/exams/{exam_id}", ADMIN))
        assert exam_view["total_score"] == 15
        print("   测评满分仍为 15（5+10），未被题库 v2 的 99 分污染")
        print("\n全部演示步骤完成。")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server_proc = None
    base_url = args.base_url
    if base_url is None:
        import os

        db_path = Path("/tmp/exam_demo") / f"demo_{uuid.uuid4().hex[:8]}.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "exam_service.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
            ],
            cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, "EXAM_DB_PATH": str(db_path)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{args.port}"
        for _ in range(50):
            try:
                httpx.get(f"{base_url}/health", timeout=1)
                break
            except httpx.TransportError:
                time.sleep(0.2)
        else:
            server_proc.terminate()
            raise RuntimeError("test server did not start")

    demo = Demo(base_url)
    try:
        demo.run()
    finally:
        demo.http.close()
        if server_proc is not None:
            server_proc.terminate()
            server_proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
