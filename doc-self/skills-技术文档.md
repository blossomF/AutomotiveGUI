# skills 技能层技术文档

> 对应代码：`server/autogui/autogui/skills/`（`base.py` / `router.py`）+ 配方 `skills/<app>/skill.yaml`
> 定位：Agent 的"肌肉记忆"。高频任务（播放歌曲、B 站搜索等）预编译成声明式 YAML 配方，命中后**零 VLM 调用**直接执行——性能分与准确率分最稳的来源，也是技术深度 +10（真实降级路径）的交汇点。

---

## 1. 设计思想

1. **声明式 YAML，执行器自研。**
   文件格式沿用 `android-agent/gitd/skills/<app>/skill.yaml`：包名 + 命名技能列表 + 参数 + 步骤列表：
   ```yaml
   name: music
   app: {package: com.car.music}
   popups: [{detect: "用户协议", button: "同意"}]
   skills:
     - name: play_song
       summary: 在音乐 App 搜索指定歌曲并播放
       triggers: [播放, 来一首, 听一下, play]
       params: [song]
       steps:
         - {act: launch}
         - {act: dismiss_popups}
         - {act: click, hint: {text: "搜索"}}
         - {act: type, arg: song, clear_first: true}
         - {act: wait_for, arg: song, timeout: 8}
         - {act: click, hint: {text: "{song}"}}
         - {act: complete}
   ```
   **刻意不引** android-agent 的 `Action`/`Workflow` 类体系（precondition / postcondition / rollback）——那套抽象是为跨多设备回放录制宏而建的，映射到车机 UI 上会花掉我们没有的几天，且竞赛场景用不上（§1 明确不做 macro/scheduler）。步骤就是一个 dict，由 `_run_step` dispatcher 解析。

2. **坐标永不写进配方：hint 是运行期解析的匹配器。**
   每个定位步骤用 `hint: {text|desc|rid}` 声明"找什么样的控件"，`_locate()` 在**每一步运行时**对一份新鲜的 `uiautomator dump` 做匹配（clickable 优先、未命中再放宽、支持 `nth` 索引）。收益：配方能扛住 UI 改版、分辨率变化、弹窗挤占布局——写死坐标的宏在车机上一步就死。参数用 `{song}` / `{package}` 占位符，`_sub()` 做替换（`$song` 式引用同理）。

3. **两个入口：确定性关键词路由 + 模型自主发起。**
   - 入口 ①（`router.match_keywords`）：任务文本包含技能声明的触发词（`triggers`，缺省回退技能名/App 名，归一化后子串匹配）且能提取参数 → 直接跑配方，**全程不调模型**。参数提取三种模式：引号/书名号内片段（`《七里香》`）、冒号后文本（`播放: 晴天`）、动词+宾语（`搜索/查找/找/播放/来一首 + 宾语`）。抽出的值会**剥掉触发词**（长词先剥，`播放哔哩哔哩钢铁侠第一季` → `钢铁侠第一季`——"哔哩哔哩"回答的是去哪个 App，不是要搜的内容，带着它去搜 `wait_for` 必超时降级）。这是性能分的最强杠杆：命中即 ~1s/步。
   - 入口 ②（模型自主）：`SkillBook.index()`（每技能一行紧凑签名，签名**一律用全名** `app.name`，如 `music.play_song(song=...) — 在音乐 App 搜索指定歌曲并播放`；重试次数用尽的技能就地追加 `⛔已失效(勿再选)` 标记）注入**永久提示词块**，模型在任意 decide 步可回答 `{"type":"SKILL","name":"music.play_song","params":{"song":"晴天"}}`。覆盖关键词列表没预料到的说法。清单与内部键同写法（全名），模型才不会误判"上次名字写错了"去改用短名重试——短名经 `get()` 后缀匹配解析回来还是同一个已失效技能，会白烧步数。
   两者都是**建议性的**：未命中/解析失败不报错，落回通用通道。

4. **SkillMiss 降级契约：脆弱的配方永远不比没有配方更糟。**
   以下情形一律抛 `SkillMiss`：未知技能、缺必填参数、步骤引用未声明参数、hint 解析不到节点、匹配节点无 bounds、输入策略完全失败、`wait_for` 超时。`core.run_skill` 捕获后：
   - 记一行"配方未命中，降级到通用路径：…"（决策链留痕）；
   - 该技能失败计数 +1（`MAX_SKILL_ATTEMPTS=2`）：未达上限仍可重试一次（给"瞬时故障"一次机会，日志记"失败第 n 次，仍可重试一次"）；用尽后该技能标为失效，**就地刷新提示词清单**追加 `⛔已失效(勿再选)`，本次运行不再被关键词路由或模型选中；
   - route 立即落到 tree（树可用）或 vlm，任务继续。
   这条契约让技能可以**一周内放心批量编写**（6 个配方：music.play_song / bilibili.search_video / feishu.send_message / feishu.create_event / waimai.search_and_add / navigation.navigate_to，包名借到车机后实测填）——写错了最多多花一步，不会搞坏任务。同一条降级路径也正是"双通道真实切换过"的技术深度 +10 证据。

5. **中文弹窗三级处理（A-3：原版词表在车机上全灭）。**
   android-agent 的 `_KNOWN_POPUPS` / `_DISMISS_WORDS` 全是 TikTok 英文串（"Turn on notifications"…），车机上飞书/B 站的"同意/以后再说"一个都匹配不上，`dismiss_popups()` 永远返回 False。本层保留其三级结构（`adb.py:690-739`）+ 重写中文词表：
   ```
   ① skill 自己声明的 popups（detect 字符串出现在 dump 中 → 点 button / method:back）
   ② DEFAULT_POPUPS 中文声明表（用户协议/隐私政策/开启个性化推荐/通知权限/所在位置/更新）
   ③ GENERIC_DISMISS 通用词表（以后再说/暂不/暂不开启/稍后再说/取消/关闭/跳过/我知道了/
      不同意/残忍离开/不再提醒/稍后）扫所有 clickable 节点
   都没有 → 停止（最多循环 4 轮，弹窗可能叠着出）
   ```
   保留原版的一个实用启发式（`adb.py:726`）：**宽度 >60% 屏宽的节点视为内容区而非按钮**，防止把视频字幕/整行文案当关闭按钮点掉。

---

## 2. 运行逻辑

```
加载（启动时一次）
  SkillBook(root, device, text_input)
    └─ load()：glob skills/*/skill.yaml → yaml.safe_load
       （解析失败 → 跳过该文件并提示，不致命）
       → by_id["app.name"]；index() 生成紧凑清单注入永久块

路由（每步，core.route）
  _first_available_skill(task)
    ├─ match_keywords(task, book)：触发词命中？→ (skill, params)
    ├─ 命中且 key ∉ skill_failures → pending_skill，route="skill"（日志：0 次 VLM）
    └─ 未命中 → 走 tree / vlm 判据

执行（core.run_skill → book.run(key, params)）
  ├─ 技能/必填参数校验（不满足 → SkillMiss）
  └─ 逐步 _run_step dispatch：
       launch        启动 App（LAUNCHER intent）
       stop          停 App
       dismiss_popups 三级中文弹窗处理（≤4 轮）
       click / tap   _locate() 新鲜 dump 匹配 hint → 中心点 tap
       type          arg 取参 / text 字面 → TextInput.type（IME 安全降级链）
       scroll        方向 + 幅度（屏中心 swipe）
       keyevent      键码（白名单校验）
       wait_for      空白不敏感的树轮询等待文本出现（两侧去全部空白 + 还原 XML 实体；超时 → SkillMiss）
       sleep         ≤5s
       complete/done 结束
     每步产出一行人类可读 trace（进决策链）
  返回 trace；成功 → COMPLETE；SkillMiss → core 降级 tree/vlm
```

`run_by_name()` 是 `run_skill` MCP 工具的便捷入口（延迟 import，保持 skills 不在 bridge 的依赖方向上）。

---

## 3. 借鉴来源

| 本层内容 | 来源 | 借鉴什么 | 改动（技术方案评估.md 编号） |
|---|---|---|---|
| `skill.yaml` 文件格式 | `android-agent/gitd/skills/*/skill.yaml` | 包名 + 命名技能 + params + steps 的声明式结构 | 增加 `triggers` / `summary`（供关键词路由与提示词清单） |
| 弹窗三级策略 | `android-agent/gitd/bots/common/adb.py:134-150, 690-739` | 声明→通用词→放弃的三级结构；≤N 轮循环 | **中文词表重写**（A-3）；保留"宽节点非按钮"启发式（`:726`） |
| **执行器** | 自研 | — | 不引 Action/Workflow 类体系；`SkillMiss` 降级契约为自研设计 |
| hint 运行期解析 | 自研（基于 bridge `find_nodes` 原语） | — | 坐标不进配方，扛 UI 改版/分辨率变化 |
| 关键词路由 + 参数提取 | 自研 | — | 三种模式：引号片段 / 冒号 / 动词+宾语 |

---

## 4. 与"只用 LightGUIAgent"相比的提升

| 维度 | LightGUIAgent（原状） | 本层 |
|---|---|---|
| 技能层 | **没有**——每个任务全程走模型决策 | 声明式配方，命中零 VLM（~1s/步 vs 模型步数秒~十秒级） |
| 高频任务准确率 | 依赖模型每步不犯错 | 配方确定性执行，无定位抖动 |
| 失败语义 | 无 | `SkillMiss` 契约：脆弱配方自动落回 tree/vlm，任务继续 |
| 弹窗处理 | 依赖模型识别点击 | 中文三级词表 + 宽节点防误触启发式，程序化关闭 |
| 对 UI 变化的鲁棒性 | 坐标/网格随布局变化而失效 | hint 每步对新鲜 dump 重解析 |

一句话：**LightGUIAgent 连"简单任务"也要模型一步一步想；本层把 Easy 级任务（播放歌曲、搜索视频）编译成零 VLM 的确定性路径**——性能分、准确率分的下限被技能层托住，而 SkillMiss 降级让"技能+双通道"的组合在决策链里留下可验证的真实切换记录。
