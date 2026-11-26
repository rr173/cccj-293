# 测评服务（发卷 → 作答 → 评分 → 发布）

Python + FastAPI + SQLite 实现的最小服务端能力，无前端、无复杂题型。
单选/多选统一按「所选选项 ID 集合与正确集合完全相等」给满分，否则 0 分。

## 运行

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # 本机若无 pip，见文末附录

# 启动（默认 sqlite 文件 exam.db，可用 EXAM_DB_PATH 覆盖）
.venv/bin/uvicorn exam_service.main:app --host 127.0.0.1 --port 8000

# 另一个终端：全自动 HTTP 演示（自己起服务、跑完 12 个步骤并打印真实请求/响应）
.venv/bin/python scripts/demo_http.py

# 测试（含真实线程并发、幂等重试、冲突拒绝）
.venv/bin/python -m pytest tests/ -v
```

## 最小账号（HTTP Basic，启动时自动播种）

| 角色 | 用户名 | 密码 | 能做什么 |
| --- | --- | --- | --- |
| 管理员 | `admin` | `admin-pass-123` | 题库增改、发卷、勘误答案键、重算、发布 |
| 阅卷员 | `grader` | `grader-pass-123` | 人工分/撤销、重算、发布、查全部结果与历史 |
| 考生 | `candidate` | `candidate-pass-123` | 取卷、保存/提交答案、只看已发布成绩 |

所有请求带 `Authorization: Basic base64(user:pass)`。密码以 PBKDF2-SHA256
（20 万次迭代 + 每用户随机盐）存储，不存明文。

## HTTP 流程（与 `scripts/demo_http.py` 一一对应）

| 步骤 | 方法与路径 | 角色 | 关键语义 |
| --- | --- | --- | --- |
| 建题 | `POST /admin/questions` | admin | 选项带稳定 `option_id`，`max_score` 正整数 |
| 改题 | `POST /admin/questions/{qid}/versions` | admin | 追加新版本，旧版本永久保留 |
| 发卷 | `POST /admin/exams` | admin | 钉死 `(question_id, version)`，快照题干/选项/满分；`shuffle` 顺序落库后重连不变 |
| 取卷 | `GET /exams/{eid}/paper` | candidate | 不含答案键；返回当前答案 `revision` 与已存答案 |
| 保存 | `POST /exams/{eid}/answers` | candidate | 带 `op_id`（幂等键）+ `base_revision`（乐观锁） |
| 提交 | `POST /exams/{eid}/submit` | candidate | 带 `op_id`；提交即按当时生效答案键机评，生成待发布结果 v1 |
| 勘误 | `POST /admin/exams/{eid}/answer-key` | admin | 新建评分方案版本，满分与发卷内容不变，旧结果不动 |
| 重算 | `POST /grading/exams/{eid}/candidates/{user}/regrade` | staff | 按当前方案生成**新的待发布结果版本** |
| 人工分 | `PUT .../questions/{qid}/manual-score` | staff | `score` 取 0..满分；`score: null` 为明确撤销；必须给理由 |
| 结果列表 | `GET /grading/.../results` | staff | 全部 pending/published/superseded 版本可追溯 |
| 发布 | `POST /grading/.../publish` | staff | 必须显式指向 `result_version` |
| 考生成绩 | `GET /exams/{eid}/result` | candidate | 仅返回已发布版本，未发布返回 404 |
| 历史 | `GET /grading/.../history` | staff | 历次评分方案、机评 run、人工动作（含撤销）全量留痕 |

## 关键不变量与实现位置

1. **发卷即快照，题库后续编辑不影响已发卷。**
   发卷时把题干、选项、选项顺序、满分复制进 `exam_questions`
   （`service.create_exam`）；题库改题只往 `question_versions` 追加行。
2. **展示顺序可打乱但重连不变。** 题目与选项顺序在发卷时一次性随机并
   持久化，取卷只按存储顺序输出。
3. **答案按稳定选项 ID 提交；过期修订不能覆盖新答案。**
   每次保存校验 `base_revision == attempts.revision`，不符返回 `409`
   （`service.save_answers`）。
4. **调用方重试不产生重复提交/重复保存。** `answer_operations` 对
   `(attempt_id, op_id)` 建唯一索引；同 `op_id` 重放返回首次结果并标记
   `idempotent_replay: true`，不新增修订、不产生第二次提交。
5. **保存与最后提交并发有明确先后。** 所有写操作走进程级写锁 +
   `BEGIN IMMEDIATE`（`db.Database.write`），读改写序列化为严格串行：
   提交先到则后到保存收到 `409 locked`；保存先到则修订号 +1 后正常提交。
   测试用双线程 `Barrier` 实跑验证（`test_concurrent_*`）。
6. **答案键勘误产生新评分方案，不动原卷。** 新方案写 `grading_schemes`
   新行；重算只读取快照卷上的选项 ID 做集合比对，绝不回查题库当前版本。
7. **人工分优先且不会被静默覆盖。** 每次机评把最新有效人工动作带入新结果
   版本（`_auto_grade`）；重算/后台重试可改变 `auto_score`，但
   `effective_score` 在人工分有效期间始终取人工分；撤销（`score=null`）
   后才回到机评。人工动作在 `manual_score_actions` 全量留痕。
8. **发布是显式版本化的两阶段。** 任何重算/人工动作都只生成 `pending`
   新版本，考生已看到的已发布版本不变；发布时若所指版本不是最新评分动作
   的产物，返回 `409 refusing to publish stale result version ...`，
   不会吞掉较新动作。发布后旧版本转 `superseded` 但永久可查。

## 数据模型概览（`exam_service/schema.py`）

```
users
questions ──< question_versions           # 题库：只追加新版本
exams ──< exam_questions                  # 发卷快照（含固定展示顺序）
attempts ──< answer_operations            # 答案修订 + op_id 幂等/审计
grading_schemes                          # 答案键版本（v1 发卷时生成）
result_versions ──< result_items         # 每次评分动作一个不可变结果版本
manual_score_actions                     # 人工分设置/撤销全历史
grading_runs                             # 机评运行留痕
```

## 设计取舍

- SQLite WAL + 单进程写锁：题目规模为演示级，写事务极短；用全局
  `RLock` 把并发正确性做成可推理的严格串行，读请求仍走独立连接并发。
- 无 ORM：标准库 `sqlite3` + 手写 SQL，事务边界与锁语义一目了然，依赖最小。
- 多人同考：`attempts` 对 `(exam_id, candidate_id)` 唯一，每位考生独立
  修订号与结果版本流，互不干扰。

## 附录：无 pip 环境的引导

```bash
curl -O https://bootstrap.pypa.io/get-pip.py
python3 -m venv --without-pip .venv
.venv/bin/python get-pip.py
.venv/bin/python -m pip install -r requirements.txt
```
