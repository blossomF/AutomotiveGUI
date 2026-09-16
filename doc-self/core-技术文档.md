# core 控制流层技术文档

> 对应代码：`server/autogui/autogui/core/`（`graph.py` / `state.py` / `guard.py` / `decision_log.py` / `executor.py`）
> 定位：整个系统的"骨架 / Harness"。把 agent 主循环实现为 LangGraph 状态机：有界循环、可观测降级、分级反思、恢复梯子、决策链日志、local/mcp 双通道执行。章程 §4.7 要求的"推理链实时打印"也在这里落地。

---

## 1. 设计思想

1. **用状态机取代 for 循环，让"循环"本身成为可检查点、可回放的结构。**
   LightGUIAgent 的闭环是 `for step in range(1, max_steps+1)`（`agent.py:389`）+ 一堆 `self.*` 实例状态原地修改。可变实例状态对 for 循环没问题，但在图里会让 checkpoint / replay 含糊（哪个节点改了什么）。本层把状态切成两半：
   - `AgentState`（`state.py`，TypedDict）**刻意窄**：只放条件边要读的控制流事实——`step / no_progress / recover_depth / route / action / last_action / retry_decision / done / success / status`；
   - 大块数据（`Observation`、`Grid`、`Context`、`DecisionLog`、执行器）挂在 `Runner` 上，**只引用不复制**。
   路由函数读 state 即可做出分支，不必信任旁路对象；`no_progress` / `screen_sig` 必须留在 state 里，因为 guard 分支依赖它们。

2. **三重硬 guard：确定性代码，不调模型（LightGUIAgent 完全没有，纯自研工作量，C-9）。**
   LightGUIAgent 的"卡死检测"只是往 prompt 里追加一句 `⚠️ Warning`（`claude_client.py:322-338`）然后指望模型自觉——模型决定原地转圈，最多烧掉 20 步 API 额度。本层 `guard.check()` 三个独立上限，任一触发即结束运行：
   ```
   timeout      墙钟 ≥ wall_clock_limit（Demo Day 一个时间槽 8 分钟）
   max_steps    迭代步数 > max_steps（20）
   no_progress  屏幕指纹连续 N 次（3）不变 —— 抓"原地打转"，
                与模型选了哪个动作无关
   ```
   报告顺序即"最具可操作性优先"：墙钟烧完能解释为什么步数耗尽。三者都是无模型调用的确定性路径——**模型行为失控时它们依然会触发**，这正是其价值。

3. **降级是真实代码路径，且全程留痕（路径 C 声明的证据链，§5.2）。**
   声明路径 C 的唯一风险是评审判定"实际只有 B"（章程 §4.4：虚报按实际给分）。所以每一级降级都被日志记录判据：
   - `route` 节点每步打印 `skill|tree|vlm` 与依据（"关键词命中，0 次 VLM" / "控件树可用：候选 40" / "控件树不可用（候选 0）"）；
   - **tree→vlm 中途降级**：`decide[tree]` 返回的 `node_ref` 越界 → 不报错、不重试，改判 vlm 再决策一次（`decide_attempts` 每步上限 2，防止在 decide 之间乒乓直到墙钟触发）；
   - **skill→tree/vlm 降级**：`SkillMiss` 时记录"配方未命中，降级到通用路径"，且该 skill 加入 `skill_failures` 不再尝试；
   - MCP→local 降级：`make_executor` 握手失败返回可用 local 执行器 + 一行警告。
   交付物里附一次**刻意触发的降级样例**（自绘/树空界面），日志留痕比任何措辞都有说服力。

4. **反思成本分层，目标每步 VLM ≤ 1（修 Mobile-Agent-v2 的 4 次/步，B-7）。**
   v2 每步串 action + reflect + memory + planning 四次调用，reflect/planning 各带双图。本层 `verify` 节点的分支：
   ```
   终止动作（COMPLETE/FAIL）      → 0 VLM，直接 finish
   非变更型动作（WAIT 等）        → 0 VLM
   reflection == off             → 0 VLM
   指纹变了 且 cheap 模式         → 0 VLM（"指纹变化=是 → 继续"）← 多数步走这条
   其余（指纹未变 / always 模式） → 1 VLM：前后双图 + 动作 → A/B/C
       A        → 重置 no_progress，继续
       B / C    → recover_depth=1，进恢复梯子
       UNKNOWN  → 保守视为生效（错误恢复比浪费一次迭代更贵）
   ```
   memory / planning 不独立调用：合并进 decide 的同一输出（`action + progress + entities`，见 brain 文档）。

5. **恢复梯子：BACK → HOME → 重启前台 App，每次只降一级并复验。**
   v2 收到 B 判定就条件反射 `back()`（`run.py:419-421`）——在车机上可能直接退到桌面、或关掉错误的对话框，且没有任何确认它有效。本层 `recovery` 节点每访问走一级（`recover_depth` 递进），每级之后回到 `perceive` 重新观测，下一次 guard 立刻能看出是否恢复成功；`no_progress` 同时清零。

6. **决策链日志是章程 4.7 的硬要求，按一等组件建设（不是散落的 print）。**
   它同时是三项得分的证据：评委读它给 Harness 打分、时间列是性能分证明、token 列是 Context 分证明。
   - 双输出始终同步：stdout 人类行（phase 彩色 + 列对齐，可纵向扫读）+ `artifacts/logs/<task>-<ts>.jsonl` 每事件一行；
   - **每事件 flush**：演示中途崩溃不能丢链；
   - JSONL 字段在 LightGUIAgent `logger.py` 基础上增加 `route` / `sig` / `glms` / `in_tok` / `out_tok` / `shot_ms` / `dump_ms`；
   - `summary()` 收尾统计表：步数、总耗时（平均 s/步）、GLM 调用次数（反思额外 N 次）、**平均每步 VLM（目标 ≤1，对照 v2 ≈4）**、token in/out/合计、JSON 重试、路由分布、日志路径。

7. **执行器：一个调用面，两种传输（架构约束 1 的落点，§3）。**
   - `LocalExecutor`：进程内直调 `bridge/tools.py`；
   - `MCPExecutor`：走 `mcp.shared.memory` 内存传输的**真实 JSON-RPC**（无 subprocess、无 Windows 管道怪癖），异步会话固定在私有事件循环，对外同步门面；外部客户端（Claude Code 等）则通过 stdio 连 `python -m autogui.mcp_server`，两者到达完全相同的工具列表；
   - `make_executor`：mcp 握手失败 → 返回 local + 降级备注。绑定是设备的属性、不是传输的属性，所以两路结果必须一致（`--mcp-check` 做同源校验）。

8. **LangGraph 的细节：`recursion_limit = max_steps*6+20`。**
   LangGraph 默认 25 个 superstep，本循环每步约 5 个节点，20 步约需 120。不显式设置，长任务会死在一个看起来像 harness bug 的 `GraphRecursionError` 上。

---

## 2. 运行逻辑（图结构）

```
START → perceive → guard ─┬(任一上限触发)─────────────────────────→ finish → END
                          └→ route ─┬(skill 命中)→ run_skill ─┐
                                    ├(tree 可用) → decide ────┼→ act → verify ─┬(有进展/指纹变)→ perceive
                                    └(树不可用)  → decide ────┘                ├(COMPLETE/FAIL) → finish
                                                                               └(B/C)→ recovery(Back→Home→重启)→ perceive
条件边明细：
  guard   after_guard：done → finish，否则 route
  route   after_route：skill → run_skill；tree/vlm → decide
  run_skill：有动作 → act（技能执行完自带 COMPLETE）；SkillMiss 降级 → 直接 route 结果
  decide  after_decide：retry_decision → decide（vlm 重判，≤2 次/步）；否则 act
  act     after_act：有 last_action → verify；空动作 → perceive
  verify  after_verify：done → finish；recover_depth ≥1 → recovery；否则 perceive
  recovery → perceive（固定回边）
```

`execute()` 主流程：
```
prepare()
  ├─ DecisionLog(task, artifacts_dir)          # 章程 4.7
  ├─ make_executor(exec_mode)                  # local | mcp，失败降级
  └─ SkillBook(...)（skills.enabled 时）→ context.skill_index（注入永久块）
graph() → 组装 StateGraph(AgentState)（9 节点 + 条件边）
app.invoke(初始 state, recursion_limit) → final
summary() → 统计表 + JSONL 收尾记录 + close()
```

节点要点：
- `perceive`：调 `perception.observe()`（并行抓帧+dump）；更新 `no_progress`（指纹与上一步相同才 +1）；ADB 失败直接 error 结束（不重试——设备问题不该烧步数）；
- `decide`：调 `brain.decide()`；合并 `_entities` / `_progress` 进 context；tree 越界 ref → vlm 重判；
- `act`：`_dispatch` 按动作类型路由到执行器（CLICK/LONG_PRESS 先 `_resolve_point`：node_ref 优先，grid 兜底——grid 也是动作中途的 tree→vlm 逃生通道）；失败记 `failed` 入历史并计无进展步。

---

## 3. 借鉴来源

| 本层内容 | 来源 | 借鉴什么 | 改动 |
|---|---|---|---|
| 状态机载体 | `langgraph`（`StateGraph` API） | 节点/条件边/checkpoint 能力 | 图结构、节点实现全部自研 |
| 反思 A/B/C 语义、恢复概念 | `Mobile-Agent-v2/run.py:388-424` | 双图反思判定、失败恢复思路 | **严格枚举**（B-5）；**失败步保留入历史**（B-6）；盲 `back()` 改为 **Back→Home→重启** 三级梯子；**4 次调用压到 ≤1**（B-7） |
| 决策链 JSONL 落盘 | `LightGUIAgent/lightguiagent/logger.py` | JSONL 结构 | 增加 `route` / `sig` / `VLM 次数` / `token` 字段；打印格式对齐章程 4.7 |
| **guard 三重限制** | 无（LightGUIAgent 只有 `for step in range(...)` + 模型自觉） | — | 纯自研（C-9）：steps / no-progress 指纹 / 墙钟 |
| 双通道执行 | `android-agent/gitd/mcp_server.py` 的 MCP 面 + `mcp.shared.memory` | FastMCP 套壳、内存 JSON-RPC 传输 | local/mcp 同源纯函数；失败一键降级（§3 约束 1） |
| 窄 state / 数据挂 Runner | `技术方案评估.md §3` 架构约束 | "路由器读的状态"与"节点读的数据"分离 | 使 checkpoint/replay 可行 |

---

## 4. 与"只用 LightGUIAgent"相比的提升

| 维度 | LightGUIAgent（原状） | 本层 |
|---|---|---|
| 循环结构 | `for step in range(1, max_steps+1)`，`self.*` 原地修改 | LangGraph 状态机，窄 state + Runner 数据分离，可回放 |
| 停止条件 | 步数上限 + 模型说 COMPLETE；卡死=一句 prompt 警告 | 三重硬 guard（步数/指纹无进展/墙钟），确定性触发 |
| 路由 | 单通道（网格 VLM） | skill / tree / vlm 三通道，降级是真实代码路径且留痕 |
| 反思 | 无（或 v2 式每步 4 次调用） | 分层反思，多数步 0 VLM，目标 ≤1/步，数字进汇总表 |
| 恢复 | 盲 `back()`（可能退桌面/关错弹窗） | BACK→HOME→重启三级梯子，每级复验 |
| 可观测性 | 无决策链 | 章程 4.7：彩色人类行 + JSONL（每事件 flush）+ 收尾统计表 |
| 执行通道 | 进程内直调 | local / mcp 双传输同源，一键降级 |

一句话：**LightGUIAgent 的骨架是"跑满步数或听天由命"；本层的骨架是"每一步都有预算、有路由、有证据、有退路"**——这是 Harness 10 分 + 技术深度 +10 的直接载体。
