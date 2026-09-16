# brain 大脑层技术文档

> 对应代码：`server/autogui/autogui/brain/`（`glm_client.py` / `prompts.py` / `context.py`），动作空间定义在 `autogui/actions.py`
> 定位：整个 Agent 的"大脑"。负责与 GLM 5.3 模型的全部交互（决策 / 反思）、提示词组装、上下文分层与 token 预算管理。它不感知设备，只输入"屏幕观测文本/图 + 任务上下文"，输出"一个动作"。

---

## 1. 设计思想

1. **GLM 5.3 走 OpenAI 兼容端点，绑定 `openai` SDK**。
   LightGUIAgent 的 `claude_client.py` 绑定 `anthropic` SDK（`from anthropic import Anthropic` @ :11），换模型即要重写客户端。本层直接对 OpenAI 兼容协议编程，具体差异有三处：
   - 图像载荷：`{type:"image_url", image_url:{url:"data:image/...;base64,..."}}`；
   - 用量字段：`prompt_tokens` / `completion_tokens`（而非 `input_tokens` / `output_tokens`）；
   - 计费：LightGUIAgent 硬编码 Claude $5/$25 每百万 token 并输出美元（`config.py:162-165`）；GLM 使用组委会发放的 quota，美元数字无意义 → **只报 token 数与调用次数，不报钱**（技术方案评估.md §2 C-11）。
   同时刻意不用 `langchain-openai`：少一层依赖（依赖冻结清单共 6 个，§6），且不需要链式组合——控制流归 LangGraph 所有。

2. **严格 JSON，畸形输出不进入图**。
   `chat_json` 的契约：解析失败 → 带着"抱怨"（失败原因 + 原回复前 1500 字符 + 输出规则）**只重问一次**。`first_json_object` 容忍模型常见的包装：```json 围栏、对象前后的散文、嵌套花括号。再失败才抛 `ValueError` 给上层（core 的 decide 节点）走降级，而不是在解析层死循环。

3. **动作空间是三方唯一事实来源（`actions.py`）**。
   `ACTIONS` 表同时驱动：prompt 里"可用动作"的枚举、`parse_action` 的校验器、core 的 `_dispatch` 执行器。共 11 个动作：
   `CLICK / LONG_PRESS / TYPE / SCROLL / BACK / HOME / LAUNCH / SKILL / WAIT / COMPLETE / FAIL`。
   对比 LightGUIAgent 只有 5 个（`config.py:150-156` 的 `CLICK/TYPE/SCROLL/AWAKE/COMPLETE`）：
   - `BACK` / `HOME`：LightGUIAgent 的"恢复"靠提示词让模型*去左上角 A1-B3 找返回键*（`claude_client.py:151-156`），在带常驻 dock 的横屏中控上该假设不成立（§2 C-10）。这里 BACK/HOME 是一等动作，走 keyevent；
   - `FAIL`：没有显式"放弃"动作时，卡住的 agent 只能硬撑到 max_steps（慢且贵）或谎报 COMPLETE。FAIL 让模型能体面认错；
   - `LAUNCH` / `SKILL` / `WAIT` / `LONG_PRESS`：分别对应程序化点图标（= `am start`）、声明式配方、等动画/网络、长按。
   解析规则刻意保守：`node_ref` 必须是整数（`#12` 前缀可容）、`grid` 统一大写、SCROLL 方向白名单、WAIT 上限 5 秒（防模型要求等 600 秒吃掉 Demo 时间槽）、explain 截断 200 字符。

4. **提示词结构沿用 LightGUIAgent 骨架，App 内容全部重写**。
   保留的是结构（§8 登记表）：系统规则 + 每动作 JSON 示例 + 语言跟随规则（`LANGUAGE_RULE`：explain/progress/entities 与任务同语言）+ 严格输出规则（`OUTPUT_RULE`：只输出一个 JSON 对象）。
   重写的是内容：LightGUIAgent 写死的美团/淘宝/微信/支付宝（`claude_client.py:160-163`）是手机 App，CEA1.3 车机上没有；规则针对车机场景重写（弹窗优先点"同意/以后再说/关闭"、不可逆操作前确认界面到位等）。

5. **反思 = 前后双图 A/B/C，且严格解析（借鉴 Mobile-Agent-v2 的思想，修掉它的 bug）**。
   `REFLECT_SYSTEM` 让模型对"同一车机、一个动作前后"两张截图回答 A（生效）/ B（生效但进错页）/ C（无可见变化），**只回答一个大写字母**。
   关键在 `parse_reflection` 的严格性：Mobile-Agent-v2 `run.py:401` 用 `if 'A' in reflect` 子串匹配，模型自由思考里的 "ADB" / "Activity" / "App" 都会误判成成功（§2 B-5，回归用例已固定为测试）。本层只认三种形态：裸字母、句首字母（`A. ...`）、显式 `回答：A` / `ANSWER: A`；其余一律 `UNKNOWN`，由 core 的 verify 节点**保守视为生效**（错误的恢复比浪费一次迭代更贵）。

6. **上下文四层预算（Context 10 分的正面战场，技术方案评估.md §5.1）**。

   | 层 | 内容 | 预算 | 超支时先丢 |
   |---|---|---|---|
   | permanent | 任务、声明路径、动作 schema、Skill 清单 | ~600 tok | 不丢 |
   | sliding | 最近 N 步（默认 5）纯文本，**含失败步** | ~500 tok | 丢最旧 |
   | entities | 跨步关键事实（时间/地点/歌名…） | ~150 tok | 不丢 |
   | observation | tree 模式：候选文本列表（**不发图**）；vlm 模式：1 张网格图 | 1–2k | 候选截断 top-K |

   三条硬规则：
   - **tree 模式永远不发截图**——这是 Context 分最大单项来源（省掉约 90% 视觉 token）；
   - **候选先过滤再喂**（过滤在 perception 层做），输出 `#12 搜索 [960,86]` 式紧凑行，模型只回 `node_ref`，**从不复制坐标**；
   - **反思不带记忆体**：reflect 只给前后两图 + 一个动作。
   `trim()` 的牺牲顺序：先丢最旧历史行，再截尾部候选；任务与实体永不丢弃。
   `merge_entities` 只做合并、空值不擦除（防止模型某一步的 `null` 悄悄删掉已记住的会议时间）。
   `estimate_tokens` 是启发式（CJK ~1/字，ASCII ~1/4 字符），只用于"发送前裁什么"，对外报告一律用 provider 返回的真实用量。

---

## 2. 运行逻辑

```
core.decide 节点
   │
   ├─ context.trim(cands) ──────────────► context_text（四层拼接，≤ token_budget）
   │
   ├─ route == "tree"
   │     build_decide_messages: [system 规则] + [候选列表文本] + [输出示例]   （无图）
   │  route == "vlm"
   │     build_decide_messages: [system 规则] + [网格说明] + [带网格 JPEG 图] + [输出示例]
   │
   ├─ chat_json(messages)
   │     ├─ chat(): openai SDK 补全，传输错误退避重试（≤ max_retries）
   │     │          每次逻辑调用独立 Usage（含重试），镜像到 last_call
   │     ├─ first_json_object() 解析
   │     └─ 解析失败 → 带抱怨重问一次（Usage 累加两次）
   │
   ├─ parse_action(raw)（actions.py 校验/归一化）
   │     └─ 仍不可用 → ValueError 上抛 → core 降级 tree→vlm / 计无进展步
   │
   └─ 返回 (action, last_call)；action 附带 _progress / _entities
         → core 把 entities 并入 context，progress 更新滑动层

core.verify 节点（反思路径）
   glm.reflect(action_desc, goal, before_b64, after_b64)
     → chat(REFLECT_SYSTEM + 双图) → parse_reflection → "A" | "B" | "C" | "UNKNOWN"
```

用量记账规则：`Usage` 区分"本次逻辑调用"（`last_call`，决策日志每步引用它）与"全程累计"（`self.usage`，收尾汇总表用）。若 `chat()` 返回累计值，决策日志里每步 token 数会逐步递增，对排查的人是误导。

---

## 3. 借鉴来源

| 本层内容 | 来源 | 借鉴什么 | 改动 |
|---|---|---|---|
| `prompts.py` 系统提示词结构 | `LightGUIAgent/lightguiagent/claude_client.py:18-164` | SYSTEM_PROMPT 骨架：动作 JSON 约定、语言跟随、explain/summary 字段 | 横屏网格语义；动作空间扩到 11（C-10）；车机 App 场景规则；删手机 App 列表 |
| `glm_client.py` JSON 解析容错 | `LightGUIAgent/lightguiagent/claude_client.py:343-393` | 对模型输出的校验/重试思路 | 改为"带抱怨重问一次"并隔离在图外 |
| 反思 A/B/C 双图 | `MobileAgent/Mobile-Agent-v2/MobileAgent/prompt.py:77-129` | 反思语义（前后双图判定生效性） | 只收裸字母；`parse_reflection` 严格枚举，修 `if 'A' in reflect` bug（B-5） |
| entities 实体抽取 | `Mobile-Agent-v2/MobileAgent/prompt.py:132-180` | completed_requirements / memory 思想 | 不独立调用模型，合并进 decide 单次输出（B-7） |
| 上下文分层与预算 | `技术方案评估.md §5.1`（aidoc 提出） | 四层结构、tree 不发图、top-K 候选 | 按 16 天工期定稿为可实现的 `Context` 类 |

---

## 4. 与"只用 LightGUIAgent"相比的提升

| 维度 | LightGUIAgent（原状） | 本层 |
|---|---|---|
| 动作空间 | 5 个，无 BACK/HOME/FAIL，恢复靠猜"左上角 A1-B3" | 11 个，BACK/HOME 一等动作，FAIL 可体面认错 |
| 模型绑定 | anthropic SDK 硬绑定，换模型重写客户端 | OpenAI 兼容协议，换 endpoint/key 即可 |
| JSON 输出 | 解析失败处理分散 | 严格 JSON + 一次带抱怨重试，畸形不出脑 |
| 反思 | 无严格反思解析（或 v2 式子串误判） | 双图 A/B/C + 严格解析，UNKNOWN 保守处理 |
| 上下文 | 无分层预算，整树/整图直喂 | 四层预算，tree 模式 0 图，失败步留痕，实体只合并不删 |
| 计费口径 | 硬编码美元 | 只报 token/调用次数，匹配组委会 quota 口径 |

一句话：**LightGUIAgent 的"大脑"是"把屏幕和提示词丢给 Claude 等答案"；本层是"预算受控、输出可校验、失败可归因"的决策器**——这是 Context 10 分与准确率 15 分的基础。
