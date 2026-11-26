# 测评服务（发卷 → 作答 → 评分 → 发布）

Python + FastAPI + SQLite 实现的最小服务端能力，无前端、无复杂题型、无自动监考。
单选/多选统一按「所选选项 ID 集合与正确集合完全相等」给满分，否则 0 分。

支持两种测评：

- **普通测评（默认）**：不计时，行为与最初版本完全一致。
- **限时测评**：管理员把考生分入一个或多个**场次**，每人一个场次和独立答题时长；
  服务端计时，支持**场次级 / 考生级两层暂停与续考**，时间用尽由服务端惰性自动交卷。

另支持**补考分场预约**：为尚未开考、未配置限时场次的测评开设一次补考预约，
按另一份测评的已发布总分划定资格，考生自助申请场次、候补递补、邀请确认、
换场与退订；确认后沿用限时场次的全部开考/暂停/计时规则。

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

# 补考分场预约演示（候补→确认→换场→到期→资格作废→开考锁定，全走 HTTP）
.venv/bin/python scripts/demo_makeup_http.py

# 测试（含真实线程并发、幂等重试、冲突拒绝、重叠暂停、服务重启后惰性交卷、申诉结案/改分/发布竞争、补考预约）
.venv/bin/python -m pytest tests/ -v

# 只跑单题成绩申诉的端到端 HTTP 验证
.venv/bin/python -m pytest tests/test_score_appeals.py -q
```

## 最小账号（HTTP Basic，启动时自动播种）

| 角色 | 用户名 | 密码 | 能做什么 |
| --- | --- | --- | --- |
| 管理员 | `admin` | `admin-pass-123` | 题库增改、发卷、配置限时场次、暂停/恢复、勘误答案键、重算、发布、指派申诉阅卷员、仲裁 |
| 阅卷员 | `grader` | `grader-pass-123` | 人工分/撤销、重算、发布、查全部结果与历史 |
| 阅卷员 | `grader2` / `grader3` | `grader2-pass-123` / `grader3-pass-123` | 申诉复核演示账号；管理员一次指派其中两名不同阅卷员 |
| 考生 | `candidate` | `candidate-pass-123` | 取卷、保存/提交答案、只看已发布成绩、提交/查看本人申诉 |
| 考生 | `candidate2` | `candidate2-pass-123` | 用于验证不能查看他人申诉 |

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

## 单题成绩申诉 HTTP 流程

申诉围绕一份答卷的一道题展开；`op_id` 与现有答案/监考接口一样是重试幂等键，
也可改用 `Idempotency-Key` 请求头。同一个 `op_id` 用不同请求体再次提交会得到
`409`。

| 步骤 | 方法与路径 | 角色 | 关键语义 |
| --- | --- | --- | --- |
| 开案 | `POST /exams/{eid}/appeals` | candidate | body：`op_id/result_version/question_id/reason`；只能针对本人**当前已发布**版本中的题；每份答卷每题仅一次 |
| 我的申诉 | `GET /exams/{eid}/appeals`、`GET /exams/{eid}/appeals/{aid}` | candidate | 只返回本人申诉、办理阶段和正式结案结论；不含阅卷意见 |
| 全部我的 | `GET /my/appeals` | candidate | 跨考试查看本人申诉 |
| 指派 | `POST /admin/appeals/{aid}/assign` | admin | body：`op_id/grader_usernames=[u1,u2]`；必须两名不同 `grader`；有该题普通人工给分/撤销历史者 `409` |
| 办理详情 | `GET /grading/appeals/{aid}` | staff | 管理员可见全部意见；意见汇齐前，被指派阅卷员只见本人意见；考生走考生路由 |
| 复核意见 | `POST /grading/appeals/{aid}/opinions` | assigned grader | body：`op_id/decision/reason/new_score?`；每人一次；`decision` 为 `rejected` 或 `changed` |
| 仲裁 | `POST /admin/appeals/{aid}/arbitrate` | admin | 两人结论或改分不一致时进入 `pending_arbitration`；body 同意见；有利益冲突的管理员 `403` |
| 列表/追溯 | `GET /grading/appeals`、`GET /grading/exams/{eid}/appeals` | staff | 可按 `exam_id/username/status/mine` 过滤；历史接口也在 `appeals` 键返回完整追溯 |
| 发布 | 原入口 `POST /grading/exams/{eid}/candidates/{username}/publish` | staff | 申诉改判只生成新的 `pending` 结果版本，仍须显式发布最新版本 |

开案时会冻结：申诉指定的成绩版本、该版单题得分、发卷试题快照、考生已交答案、
对应评分方案。之后即使有更新版本发布，复核和改分校验仍以该冻结依据中的
`appealed_score` 与原题 `max_score` 为准。

### 结论与发布状态

- 两名阅卷员均 `rejected`：直接 `closed_rejected`，不生成成绩版本。
- 两名阅卷员均 `changed` 且 `new_score` 相同：`closed_changed`，以该分值结案。
- 其他组合：状态变为 `pending_arbitration`，由管理员仲裁为 `rejected` 或
  `changed`。
- `changed` 的 `new_score` 必须是整数，满足 `0 <= new_score <= 原题满分`，且
  `new_score != appealed_score`。
- 改判结案时，服务复制该考生**最新生成的结果版本**（可能仍是 `pending`），
  仅替换申诉题的 `effective_score` 并写入 `appeal_ruling` 评分来源；其他题的
  未发布人工分原样保留，最新结果未使用的答案键勘误也不会借此次复制生效。
- 申诉响应中的 `correction_status`：
  - `not_required`：驳回；
  - `pending_publication`：已结案且已生成待发布改分；
  - `published`：当前已发布结果中包含该裁决（之后的重算/发布也会继续携带它）。

### HTTP 请求示例

```bash
# 1) 考生对当前已发布的 v1 第 12 题申诉
curl -u candidate:candidate-pass-123 \
  -X POST http://127.0.0.1:8000/exams/3/appeals \
  -H 'Content-Type: application/json' \
  -d '{
    "op_id": "appeal-20261126-q12-01",
    "result_version": 1,
    "question_id": 12,
    "reason": "题干表述使我选择 B，申请复核得分"
  }'
# -> 201；响应中的 appeal_id 用于后续步骤

# 2) 管理员一次指派两名不同阅卷员
curl -u admin:admin-pass-123 \
  -X POST http://127.0.0.1:8000/admin/appeals/7/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "op_id": "assign-appeal-7-01",
    "grader_usernames": ["grader2", "grader3"]
  }'

# 3) 第一名阅卷员要求改为 4 分
curl -u grader2:grader2-pass-123 \
  -X POST http://127.0.0.1:8000/grading/appeals/7/opinions \
  -H 'Content-Type: application/json' \
  -d '{
    "op_id": "review-g2-appeal-7",
    "decision": "changed",
    "new_score": 4,
    "reason": "干扰项表述有歧义，按作答要点应给 4 分"
  }'

# 4) 第二名阅卷员驳回；结论分歧，等待管理员仲裁
curl -u grader3:grader3-pass-123 \
  -X POST http://127.0.0.1:8000/grading/appeals/7/opinions \
  -H 'Content-Type: application/json' \
  -d '{
    "op_id": "review-g3-appeal-7",
    "decision": "rejected",
    "reason": "作答与正确选项集合不一致，维持 0 分"
  }'

# 5) 管理员仲裁改满分 5；响应给出 correction_result_version（例如 v3）
curl -u admin:admin-pass-123 \
  -X POST http://127.0.0.1:8000/admin/appeals/7/arbitrate \
  -H 'Content-Type: application/json' \
  -d '{
    "op_id": "arbitrate-appeal-7-01",
    "decision": "changed",
    "new_score": 5,
    "reason": "核对快照试题与答案后，仲裁认定应给满分"
  }'

# 6) 仍走原发布入口，发布包含裁决的最新结果版本
curl -u grader:grader-pass-123 \
  -X POST http://127.0.0.1:8000/grading/exams/3/candidates/candidate/publish \
  -H 'Content-Type: application/json' \
  -d '{"result_version": 3}'
```

改判后普通重算仍会重新计算机评分并带入其他有效人工分，但申诉题会被永久标记为
`score_source_type: "appeal_ruling"` 并使用裁决分；任何普通人工给分或撤销请求
修改该题都会返回 `409`。完整 HTTP 行为与竞争验证见
`tests/test_score_appeals.py`。

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

## 补考分场预约 HTTP 用法

管理员为一份**尚未开考、未配置限时场次**的测评开设**一次**补考预约：指定另一份
测评作为资格依据、合格线、各补考场次的名称/答题时长/名额，以及邀请确认时限。
**不预填考生名单**——资格完全由「原测评当前已发布总分 < 合格线」动态决定
（未发布成绩不算）。开设后该测评变为限时测评，补考场次带名额；考生确认邀请后
被写入名单，之后的开考、两层暂停、限时自动交卷完全沿用既有规则。

| 步骤 | 方法与路径 | 角色 | 关键语义 |
| --- | --- | --- | --- |
| 开设补考预约 | `POST /admin/exams/{eid}/makeup` | admin | 带 `op_id`；body：`basis_exam_id/pass_score/invite_ttl_seconds/sessions[{name,duration_seconds,capacity}]`；已开考、已配置限时或已有补考预约时 `409` |
| 管理员视图 | `GET /admin/exams/{eid}/makeup` | staff | 各场次名额占用与全部申请；查询前先惰性结算到期/资格/递补 |
| 我的预约 | `GET /exams/{eid}/makeup` | candidate | 本人资格、各场次空位、本人申请与邀请；不含他人信息 |
| 申请场次 | `POST /exams/{eid}/makeup/applications` | candidate | 带 `op_id` + `session_id`；有空位立即发邀请，否则按申请先后排队候补 |
| 确认邀请 | `POST /exams/{eid}/makeup/invitations/{iid}/confirm` | candidate | 带 `op_id`；确认该场开考资格；已确认他场者确认新邀请即**原子换场**并释放原位 |
| 退出候补 | `POST /exams/{eid}/makeup/applications/{aid}/withdraw` | candidate | 带 `op_id`；候补/被邀请状态可退，持有名额立即释放并递补 |
| 退订座位 | `POST /exams/{eid}/makeup/unsubscribe` | candidate | 带 `op_id`；开考前退订释放座位，并同时取消本人的候补或邀请 |

规则要点：

- **一次只追一个场次**：同一考生在同一补考预约下最多一个进行中（候补/被邀请）
  申请；已确认者只能候补**其他**场次，候补期间原位不丢，确认新邀请时才换场
  （换场前两边各占一个名额）。
- **邀请保留一个名额**：从实际发出时起算，确认时限内未确认则到期释放，该次申请
  同时结束；每张邀请单独可识别，旧邀请不能确认新席位。服务离线期间照常到期，
  不补发已过去的邀请；任何查询/办理都会先看到已处理的到期、资格变动与递补结果。
- **资格变动即作废**：发布新版成绩后，失去资格者的候补与未确认邀请作废（已确认
  座位不撤销）；即使后来恢复资格也须重新申请。申请与确认两个时点都校验资格。
- **开考即锁定**：第一次成功开考后，未完成的换场申请取消，不能再退订或换场；
  一次开考只认最终确认的场次，沿用该场的暂停与限时规则。
- **幂等**：所有写操作带 `op_id`（或 `Idempotency-Key` 头）；同标识重放返回首次
  结果且不再占用/释放/移动席位，同标识换请求返回 `409`。

请求体示例：

```json
// POST /admin/exams/2/makeup
{
  "op_id": "mk-open-01",
  "basis_exam_id": 1,
  "pass_score": 5,
  "invite_ttl_seconds": 300,
  "sessions": [
    {"name": "补考甲场", "duration_seconds": 600, "capacity": 1},
    {"name": "补考乙场", "duration_seconds": 900, "capacity": 1}
  ]
}
// POST /exams/2/makeup/applications
{"op_id": "app-c1-01", "session_id": 3}
```

申请响应（有空位时直接获得邀请；否则 `state: "waitlisted"` 并给出 `queue_position`）：

```json
{
  "application_id": 1, "session_id": 3, "session_name": "补考甲场",
  "state": "invited", "queue_position": null,
  "invitation": {"invitation_id": 1, "status": "pending",
                 "issued_at": "...", "expires_at": "...", "resolved_at": null},
  "idempotent_replay": false
}
```

完整演示（候补→确认→换场→到期→资格作废→开考锁定）见
`scripts/demo_makeup_http.py`，自动化验证见 `tests/test_makeup.py`。

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
15. **申诉依据在开案时冻结，且每题一次。** `score_appeals` 对
    `(exam_id,candidate_id,question_id)` 建唯一约束；只有当前 `published`
    结果版本中的题能开案。`basis_json` 保存该版单题分、试题快照、已交答案和
    评分方案，后续新版成绩发布不改变复核基准。
16. **双阅卷员意见在汇齐前互相不可见。** 管理员一次写入两条不同指派；
    `appeal_opinions` 对 `(appeal_id,grader_id)` 唯一。序列化层在两意见汇齐前
    只向被指派者返回其本人意见，对考生始终剥离意见与内部 basis。曾对该
    考生该题写入 `manual_score_actions`（包括撤销）的用户不能被指派或仲裁。
17. **改判只生成新的待发布版本，并永久约束该题分数。** 结案与结果版本生成在
    同一写事务中完成；改判版本复制最新结果版本的其他题目和 `scheme_id`，只
    替换申诉题。之后 `_auto_grade` 始终应用生效的 `appeal_rulings` 改判分，
    普通人工给分/撤销入口对该题返回 `409`。
18. **申诉操作幂等，竞争由全局写锁和事务给出顺序。** 开案、指派、意见、仲裁
    均在 `appeal_operations` 记录全局 `op_id` 与请求哈希；同请求重放不重复
    结案或生成结果，同 `op_id` 不同请求返回 `409`。结案、其他题改分和发布
    并发时，全局 `BEGIN IMMEDIATE` 事务序列化为一个一致先后顺序。
19. **补考资格只认当前已发布总分，作废即时且不可复活。**
    `makeup_campaigns` 每份测评至多一条（`UNIQUE(exam_id)`）；申请与确认时
    都校验「原测评当前 published 总分 < 合格线」；`publish_result` 在同一
    事务内调用结算，把失去资格者的候补/未确认邀请置为 `voided`（已确认座位
    不动），恢复资格也只能重新申请。
20. **名额由状态推导，邀请占座，换场原子。** 座位占用 = 该场次
    `invited/confirmed/completed` 申请数（无冗余计数器，重启后不会超额）；
    部分唯一索引保证每考生至多一个进行中申请和一个座位。确认新邀请时，旧
    申请转 `switched`、名单改场、旧场递补在同一事务完成，不存在换场一半的
    中间态。邀请自实际发出时占一个名额，到期（含服务离线期间）惰性置
    `expired` 并结束该次申请，递补邀请以实际发出时刻重新计时。
21. **开考定终局。** 首次成功开考（创建答卷）的同一事务内：已确认申请转
    `completed`、未完成的换场申请取消并释放所持名额；此后退订/换场/新申请
    一律 `409`。补考写操作（开设/申请/确认/退出/退订）均经
    `makeup_operations` 记录 `op_id` 与请求哈希：重放返回首次结果、不再
    占用/释放/移动席位，同 `op_id` 换请求返回 `409`。

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
score_appeals ──< appeal_assignments     # 单题申诉、冻结依据、两名指派阅卷员
score_appeals ──< appeal_opinions        # 每人一次的复核意见（汇齐前保密）
score_appeals ── appeal_rulings          # 一致结论或管理员仲裁的正式裁决
appeal_operations                        # 开案/指派/复核/仲裁 op_id 幂等与请求哈希
makeup_campaigns                         # 补考预约（每测评一次）：资格依据/合格线/确认时限
exam_sessions.capacity                   # 补考场次名额（普通限时场次为 NULL）
makeup_applications                      # 申请：候补/邀请/确认/完成 + 各终态
makeup_invitations                       # 邀请：单独可识别，pending 期间占一个名额
makeup_operations                        # 补考写操作 op_id 幂等与请求哈希
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
