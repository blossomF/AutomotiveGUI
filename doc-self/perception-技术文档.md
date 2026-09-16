# perception 感知层技术文档

> 对应代码：`server/autogui/autogui/perception/`（`capture.py` / `tree.py` / `grid.py`）
> 定位：Agent 的"眼睛"。每步把车机屏幕变成模型可消费的形式：并行抓帧 + UI 树 → 屏幕指纹 + 压缩候选列表（tree 通道）+ 网格化截图（vlm 兜底通道）。本层是 Context 分与性能分交汇最密的地方。

---

## 1. 设计思想

1. **一次观测，并行完成。**
   `screencap` 与 `uiautomator dump` 是设备端相互独立的操作，而 `dump` 是慢的那个（车机上秒级）。LightGUIAgent 顺序执行（`agent.py:117-135`），每步延迟 = 两者**之和**；本层 `observe()` 用 2 worker 线程池并发，延迟 = 两者**最大值**。这个差值正处在性能 10 分的关键路径上，且零成本。

2. **屏幕指纹 = 程序化的"无进展"与"已生效"检测器（C-9）。**
   LightGUIAgent 的卡死处理只是 prompt 里一句话，没有任何程序机制。本层 `compute_signature()` 生成复合指纹：
   ```
   md5( 树中全部可见标签的顺序摘要 )  :  md5( 16×16 灰度降采样像素 )
   ```
   - 文本摘要部分（`screen_signature`）捕捉控件/文案变化；
   - 降采样像素部分捕捉**纯视觉变化**（视频开始播放、进度条走动）——只用文本会漏掉；16×16 是刻意有损：要的是"变没变"，不是"怎么变"，压缩噪声/时钟跳动只影响个别桶，不会让严格全图哈希过度反应。
   指纹的消费者有两个：`core/guard` 的 no-progress 上限、`core/verify` 的"指纹变了 → 0 VLM 直接继续"廉价路径。

3. **树压缩 = token 经济的关键（Context 10 分最大单项）。**
   车机上 `uiautomator dump` 原始 XML 有 40–120 KB，直喂模型第一步就击穿预算。`tree.extract()` 的过滤流水线：
   ```
   原始 XML
     ① 必须有 bounds 且面积 >0；屏外残留丢弃（多屏 dump 含其它面板）
     ② 必须带人类可见标签：text / content-desc / 有意义的 resource-id 尾部
        （结构性 rid 黑名单：content/rootview/background/mask/placeholder/
          spacer/statusbar/navbar/navigationbar/decor/overlay/window[_-]?content，
          忽略大小写、按*尾部*匹配——框架 id 大小写跨构建不一致）
     ③ 容器丢弃：宽 ≥72% 屏宽 且 高 ≥55% 屏高（全屏 FrameLayout 不是点击目标；
        通栏但矮的列表行保留——那是真目标）
     ④ 按阅读顺序排序：(y//40, x)
     ⑤ clickable 优先；超过 limit（默认 40）时优先保可点击
     ⑥ 中心点去重：同坐标合并，长标签胜出（"播放" 胜过外层包装 "btn"）
     ⑦ 重编连续 ref：#1..#N
   → 渲染：每候选一行  `#12 搜索 [960,86]`（非可点击加 (n) 标记）
   ```
   关键契约：**模型只回答 `node_ref` 整数，永远不复制坐标**——`[960,86]` 被抄成 `[960.86]` 这类错误整类消除。`by_ref` 越界返回 `None` 而不是抛异常，让 core 走 tree→vlm 降级（路径 C 证据，§5.2）。
   `tree_usable` 判据：候选 ≥3 个。自绘/SurfaceView App（大段 FrameLayout、零标签）→ 0 候选 → 报"不可用"→ 路由走 vlm 或如实降级声明。

4. **横屏网格：显式 20×10，拒绝继承 LightGUIAgent 的竖屏假设（C-11）。**
   借鉴的是 `grid_converter.py` 的*标注思想*——单字母列（A…）+ 从 1 开始的行，模型回答 "E5" 而不是一对像素，被证实能减少坐标幻觉。
   但它的自动密度算法**整体丢弃**：`settings.py:159-171` 的 `rows = max(15, min(rows, 35))` 把行数下限锁死 15（纯竖屏假设）。1920×1080 横屏下 `round(1080/120)=9` 被强拉到 15 行，格子 ~128×72px 又扁又密，模型更难选准。本层从 `config.yaml` 原样取 `cols: 20, rows: 10` → ~96×108px/格，每格约一个触摸目标，标签对 VLM 无歧义。
   两个工程细节：
   - `annotate()` 发现截图实际尺寸与网格尺寸不符时**重新同步几何**（标签与像素不错位）；
   - `encode_jpeg` 的 `max_side`（默认 1568）是**可调参数**而非常量——LightGUIAgent 把它写死为 `# Claude optimal image size`（`config.py:91`），GLM-V 的最优值是 9/19 A/B 测试要回答的问题。

---

## 2. 运行逻辑

```
core.perceive 节点
   └─ observe(device, grid_cols, grid_rows, candidate_limit, last_size)
        ├─ ThreadPool(2) 并行：
        │    shot() = device.screenshot() + 计时      → png, shot_ms
        │    dump() = device.dump_xml() + 计时        → xml, dump_ms
        ├─ _resolve_size：png_size(png)（PNG 头解码）优先，fallback 上次尺寸
        ├─ candidates = tree.extract(xml, limit, screen)
        ├─ grid = Grid(width, height, cols, rows)
        └─ Observation(png, xml, w, h,
                       signature = compute_signature(xml, png),
                       candidates, shot_ms, tree_ms)
        返回 (Observation, Grid)

Observation.tree_usable  → 候选 ≥3？（route 节点的 tree/vlm 判据之一）

tree 通道（core.decide，route=tree 时）
   context.trim(cands) → render_candidates → "#1 搜索音乐 [960,72]\n#2 播放 [140,980](n)\n..."
   模型回 {"action":"CLICK","node_ref":12} → by_ref(cands,12) → (cx,cy) → 执行

vlm 兜底通道（route=vlm 时）
   grid.annotate(png) → 叠加网格线 + A1..T10 标签
   grid.encode_jpeg(annotated, quality, max_side) → base64 → 随提示词发出
   模型回 {"action":"CLICK","grid":"J5"} → grid.to_pixel("J5") → (x,y) → 执行
   （反思双图同样经 annotate + mark_action 十字线标注点击点）
```

---

## 3. 借鉴来源

| 本层内容 | 来源 | 借鉴什么 | 改动 |
|---|---|---|---|
| `grid.py` 标签↔像素换算 | `LightGUIAgent/lightguiagent/grid_converter.py` | 字母列 + 数字行标注、cell 中心换算、"答 E5 不答坐标" | 自带实现；**修横屏 clamp 缺陷**（C-11）；max_side 可调 |
| 抓帧机制（exec-out + PNG 裁剪） | `android-agent/gitd/bots/common/adb.py:36-50`（经 bridge 复用） | 二进制流直取 + 多屏警告行裁剪 | 作为观测输入（C-8） |
| 并行观测 / 屏幕指纹 | `技术方案评估.md §2 C-9、§5.1`（aidoc 提出，自研实现） | "指纹=md5(文本+降采样像素)"方案 | `capture.py` / `tree.screen_signature` |
| 树压缩流水线 | 自研（参照 `android-agent` 树解析辅助函数） | 节点属性/bounds 提取原语 | 过滤规则、阅读序、去重、连续 ref 均为自研 |
| **整体丢弃的内容** | `LightGUIAgent/lightguiagent/settings.py:159-171` 自动密度、`config.py:91` 硬编码 1568 | — | 竖屏 clamp 假设不成立（C-11） |

---

## 4. 与"只用 LightGUIAgent"相比的提升

| 维度 | LightGUIAgent（原状） | 本层 |
|---|---|---|
| 观测通道 | 仅网格 VLM（每步必须发图） | tree 主通道（纯文本 0 图）+ vlm 网格兜底 |
| 观测耗时 | 抓帧、dump 顺序执行（延迟=和） | 并行（延迟=max） |
| 卡死/进展判定 | prompt 警告一句话，无程序机制 | 屏幕指纹 → guard 无进展上限 + verify 0-VLM 廉价路径 |
| 网格 | 自动密度，`rows=max(15,...)` 竖屏假设 → 128×72px 扁格 | 显式 20×10 横屏，~96×108px，每格一个触摸目标 |
| 图像编码 | `target_size:1568 # Claude optimal` 写死 | max_side 可调，留 9/19 A/B 测试位 |
| 定位精度保障 | 模型复制像素坐标（抄错即点错） | node_ref 整数引用，坐标抄错整类消除 |
| 屏幕尺寸 | 信 `wm size` | 以截图 PNG 头解码为准 |

一句话：**LightGUIAgent 的感知是"每步拍张图让模型看"；本层的感知是"能用文本就不用图、能并行的绝不串行、变化与否程序说了算"**——tree 模式不发图一条，就省掉约 90% 的视觉 token。
