# 测评服务（发卷 → 作答 → 评分 → 发布）

Python + FastAPI + SQLite 实现的最小服务端能力，无前端、无复杂题型、无自动监考。
单选/多选统一按「所选选项 ID 集合与正确集合完全相等」给满分，否则 0 分。

支持两种测评：

- **普通测评（默认）**：不计时，行为与最初版本完全一致。
- **限时测评**：管理员把考生分入一个或多个**场次**，每人一个场次和独立答题时长；
  服务端计时，支持**场次级 / 考生级两层暂停与续考**，时间用尽由服务端惰性自动交卷。

## 运行

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # 本机若无 pip，见文末附录

# 启动（默认 sqlite 文件 exam.db，可用 EXAM_DB_PATH 覆盖）
.venv/bin/uvicorn exam_service.main:app --host 127.0.0.1 --port 8000

# 另一个终端：全自动 HTTP 演示（自己起服务、跑完 12 个步骤并打印真实请求/响应）
.venv/bin/python scripts/demo_http.py

# 限时场次演示（进程内服务 + 可控虚拟时钟，零真实等待，同样全走 HTTP）
.venv/bin/python scripts/demo_timed_http.py

# 测试（含真实线程并发、幂等重试、冲突拒绝、重叠暂停、服务重启后惰性交卷）
.venv/bin/python -m pytest tests/ -v
```

## 最小账号（HTTP Basic，启动时自动播种）

| 角色 | 用户名 | 密码 | 能做什么 |
| --- | --- | --- | --- |
| 管理员 | `admin` | `admin-pass-123` | 题库增改、发卷、配置限时场次、暂停/恢复、勘误答案键、重算、发布 |
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

## 限时场次 HTTP 用法

| 步骤 | 方法与路径 | 角色 | 关键语义 |
| --- | --- | --- | --- |
| 配置限时场次 | `POST /admin/exams/{eid}/timed-config` | admin | 一次性配置：每个场次给名称、`duration_seconds` 和名单；每人仅属一个场次；只能在任何人开考前配置 |
| 暂停场次 | `POST /admin/exams/{eid}/sessions/{sid}/pause` | admin | 带 `op_id`；对场内已开考且未交卷的答卷统一停表 |
| 恢复场次 | `POST .../sessions/{sid}/resume` | admin | 带 `op_id`；只闭合场次层停表区间 |
| 暂停考生 | `POST /admin/exams/{eid}/candidates/{user}/pause` | admin | 带 `op_id`；个人层面停表；未开考也可暂停（不产生停表区间、不增加时长） |
| 恢复考生 | `POST .../candidates/{user}/resume` | admin | 带 `op_id`；只闭合考生层停表区间 |
| 监考留痕 | `GET /admin/exams/{eid}/proctor-events` | staff | 暂停/恢复事件全量可追溯 |
| 服务端时间 | `GET /server-time` | 任意已登录 | 返回服务端当前时间；每份卷的 `timing.now` 同样是服务端时间 |

请求体示例：

```json
// POST /admin/exams/1/timed-config
{
  "sessions": [
    {"name": "上午场", "duration_seconds": 600, "candidate_usernames": ["cand1"]},
    {"name": "下午场", "duration_seconds": 900, "candidate_usernames": ["cand2"]}
  ]
}
// POST /admin/exams/1/sessions/2/pause
{"op_id": "pause-2026-11-26-001", "reason": "考场设备故障"}
```

取卷响应新增 `timing` 块（普通测评只有 `{"timed_mode": false}`）：

```json
{
  "timing": {
    "timed_mode": true,
    "duration_seconds": 600,
    "started_at": "2026-11-26T09:00:00+00:00",
    "session_id": 1, "session_name": "上午场",
    "session_paused": false, "candidate_paused": false, "paused": false,
    "now": "2026-11-26T09:01:40+00:00",
    "remaining_seconds": 500.0,
    "deadline_at": "2026-11-26T09:12:30+00:00",
    "auto_submitted_at": null
  }
}
```

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
9. **时间一律以服务端时钟判定。** 时间读写全部经过注入的 `Clock`
   （`exam_service/clock.py`）：生产用 UTC 墙钟，测试用可显式推进的
   `VirtualClock`，测试无需 `sleep`。
10. **首次成功开考才计时；重连/重试看到同一份卷。** 限时测评只有名单内
    考生能开考（非名单 `403`）；场次或本人处于暂停时不能开考（`409`，
    且不创建答卷、不启动计时）。`started_at` 在首次成功取卷时落库，
    此后取卷始终返回相同快照卷、已存答案、修订号和剩余时间。
11. **两层暂停取并集，重叠只停一次表。** 场次层与考生层各有独立暂停标志
    和停表区间（`attempt_pause_intervals`）；剩余时间 =
    `时长 − (当前时刻 − 开考时刻 − 两层停表区间并集长度)`。区间按时间
    合并，重叠部分不重复扣减。两层都解除后才能保存或主动交卷；暂停期间
    仍可查看已保存答卷，但保存新答案/主动交卷返回 `409`。未开考的暂停
    不产生停表区间，因而不会带来额外时长。
12. **监考操作带 `op_id`，重放不再作用、不能撤销其后的动作。**
    `proctor_events` 对 `(exam_id, op_id)` 建唯一索引；重放返回
    `idempotent_replay: true` 且不新增事件、不改状态。对同一对象重复
    暂停/恢复（新 `op_id`）返回 `409`。
13. **用尽时长即自动交卷，且是惰性确定的。** 不依赖后台任务：任何读或写
    触达已超时的在考试卷时，在同一个写事务内把截止时刻确定下来——只保留
    此前已成功保存的答案，生成**一次**待发布成绩（机评 run 的 trigger 为
    `auto at time limit ...`）。因此服务器在截止期间不运行也没关系，重启
    后首次查看答卷或处理成绩同样能看到这次交卷。恰在截止时刻生效的答案
    键不参与本次自动评分（只取严格早于截止时刻的最新方案），留给之后的
    常规重算。逾期后的暂停不能重新获得作答资格；暂停动作自身会先做惰性
    结算，故迟到的停表区间不会把截止时刻往后推。
14. **已交卷者不受后续动作影响。** 已交卷（含自动交卷）的考生，保存与主动
    交卷一律 `409`；对其的考生级暂停被拒；之后的勘误、重算、人工分、发布
    完全沿用既有流程，自动成绩与手工动作一样是普通的待发布结果版本。

## 数据模型概览（`exam_service/schema.py`）

```
users
questions ──< question_versions           # 题库：只追加新版本
exams ──< exam_questions                  # 发卷快照（含固定展示顺序）
exam_sessions ──< exam_enrollments        # 限时场次与名单（普通测评无数据）
attempts ──< answer_operations            # 答案修订 + op_id 幂等/审计
attempts ──< attempt_pause_intervals      # 两层停表区间（并集合并去重）
proctor_events                            # 监考暂停/恢复留痕（exam 内 op_id 唯一）
grading_schemes                          # 答案键版本（v1 发卷时生成）
result_versions ──< result_items         # 每次评分动作一个不可变结果版本
manual_score_actions                     # 人工分设置/撤销全历史
grading_runs                             # 机评运行留痕（含 time limit 自动评分）
```

限时相关字段集中在 `attempts`：`timed_mode / duration_seconds /
session_id / started_at / candidate_paused / auto_submitted_at / graded_at`；
普通测评这些列为默认空值，旧数据库启动时通过 `init_db` 的追加式迁移
（`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN`）自动补齐。

## 设计取舍

- SQLite WAL + 单进程写锁：题目规模为演示级，写事务极短；用全局
  `RLock` 把并发正确性做成可推理的严格串行，读请求仍走独立连接并发。
  暂停与保存/交卷相撞时同样由这把锁给出确定先后（见
  `scripts/check_migration_and_race.py` 的真实线程竞争检查）。
- 无后台定时器：时间用尽的判定做成每次读写内的惰性结算，天然覆盖
  「服务器在截止期间没运行」的情形，也避免了定时器与写锁之间的竞态；
  自动交卷在每个答卷上至多发生一次（状态置为 `submitted` 后即幂等）。
- 时钟可注入：领域逻辑不直接调用 `datetime.now`，统一走 `Clock`，
  生产为 `RealClock`、测试为 `VirtualClock`，限时行为可以被确定性重放。
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
