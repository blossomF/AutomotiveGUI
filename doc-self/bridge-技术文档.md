# bridge 执行层技术文档

> 对应代码：`server/autogui/autogui/bridge/`（`adb.py` / `input.py` / `tools.py`）
> 定位：整个系统的"手脚"。所有对 CEA1.3 车机的读写操作都经过这一层；上层（perception / core / skills）**从不直接调 adb**。它是架构里最薄、但安全修复最密集的一层。

---

## 1. 设计思想

1. **单设备假设，贯穿所有调用。**
   竞赛只面对一台借来的车机。因此：
   - 启动时 `bind_device(serial, ...)` 一次性绑定，`Device` 内每条命令都带 `-s <serial>`（`_run()` 统一拼装）。LightGUIAgent 的 `settings.py:137` 探测是裸 `adb shell wm size`——PC 上同时插手机/模拟器时会探错设备（C-11）；
   - 工具签名里**没有 `device` 参数**。android-agent 因为驱动多台手机，62 个工具全都要传 serial；我们单设备，那个参数只是每次调用重复烧 token。

2. **安全第一：LLM 的输出是"不可信输入"。**
   我们往设备里送的东西（键码、文本、包名）来自模型，而 `adb shell` 的参数会被*设备端 shell 再解析一次*。模型若被屏幕上的诱导文案 prompt-inject 出 `$(reboot)` 或 `; rm -rf`，会直接在车机上执行。三道防线（全部来自 android-agent，见 §3）：
   - `normalize_keycode`：键码白名单 `KEYCODE_[A-Z0-9_]+`，`KEYCODE_HOME; reboot` 这类串直接 `ValueError`；
   - `input_text_arg`：`shlex.quote` 包裹 + 空格保留 input 自己的 `%s` 转义，保证任意 payload 在设备 shell 拆行后仍是**一个 argv token**（有参数化测试固定此性质）；
   - `launch_app` / `stop_app` / `get_prop` 对包名/属性名做正则校验。
   这条同时是"代码质量/安全"的加分证据：评审读得到，注释讲得清为什么。

3. **二进制流走 `exec-out`，并裁掉多屏警告行。**
   - 抓帧：`exec-out screencap -p` 一次往返直取字节流。LightGUIAgent 是 `screencap -p /sdcard/x.png` 再 `adb pull`（`agent.py:117-135`）——每步多一次完整文件传输、还落盘建名删文件，性能 10 分的关键路径（C-8）；
   - `strip_to_png_signature`：多屏设备的 `screencap` 会在 PNG 魔数前打一行 `[Warning] Multiple displays were found...`，朴素读取得到损坏图像。**车机正是多屏设备，这个裁剪不是优化是必需项**；
   - 屏幕尺寸以**解码 PNG 头（IHDR）的宽高**为准，不信 `wm size`（多屏车机上 `wm size` 可能不是中控屏）。

4. **错误分级：失败要么是异常，要么是数据。**
   - `adb()`：非零退出抛 `ADBError`（带完整命令、exit code、stderr）——吞掉失败返回 `""` 会在下游被读成"空的成功"，比异常糟得多；
   - `adb_soft()`：非零退出返回 `ADBResult`（不抛），用于"失败是正常答案"的探测（这个 IME 装了吗？）；
   - adb 可执行文件缺失 / 超时**永远抛异常**——那绝不是正常结果。

5. **工具本体是纯函数（架构约束 1，技术方案评估.md §3）。**
   `tools.py` 里每个函数：不 import `mcp`、不 import `langgraph`、返回**压缩后的 `str`**（不返回 dict——`{"ok":true,"code":0,...}` 包装是白烧的 token，参照 android-agent `mcp_server.py:105-106` 的实际做法）。
   由此得到两个性质：
   - `mcp_server.py` 只是给这 19 个纯函数套 `@mcp.tool()` 壳；`exec_mode: local` 时 core 直接进程内调用**同一批函数**——两条路径永不漂移，MCP 不顺时改一个配置值即降级，10 分钟切走；
   - 工具输出原样进模型上下文，所以措辞被刻意压短（`OK tap(960,86)`、`TREE_EMPTY`、`NOT_FOUND:xxx`）。

6. **中文输入：IME 安全的三级降级链。**
   移植自 android-agent 的 `type_unicode`（`adb.py:262-283`）+ LightGUIAgent 的 yadb 方案（`agent.py:295-333`），但修掉 P0：
   原版切到 ADBKeyboard 广播文本后**硬编码还原成 Gboard**（`com.google.android.inputmethod.latin`）——CEA1.3 车机没有 Gboard，执行后 `default_input_method` 指向不存在的 IME，之后所有输入静默失效，模型只看到"屏幕没变化"然后开始转圈（A-2）。
   本层改为：**输入前 `settings get secure default_input_method` 存下原值，finally 块里精确还原**（即使广播抛异常也执行——把 ADBKeyboard 留在选中状态会改变 VLM 下一步看到的屏幕键盘）。
   降级链（启动时 `probe()` 探测一次并缓存）：
   ```
   yadb（/data/local/tmp 的 jar，免装 APK）
     → ADBKeyboard（APK，ime list 探测）
       → input text（ASCII：NFKD 折叠，"café"→"cafe"；CJK 折叠为空 → 信号降级）
   ```

---

## 2. 运行逻辑

```
main.py / mcp_server.py
   └─ tools.bind_device(serial, input_chain, display, verbose)   # 启动一次
        ├─ get_device(): auto → list_connected()（adb devices）
        │                恰 1 台 → 绑定；0 台 / 多台 → RuntimeError（明示下一步）
        └─ TextInput(device, order)          # 输入策略链

Device（所有命令 = ["adb", "-s", serial, *args]，subprocess + timeout）
   ├─ adb / adb_bytes / adb_soft             # 三档错误语义
   ├─ 抓帧：screenshot() → exec-out screencap -p → strip_to_png_signature
   │        list_displays()（dumpsys SurfaceFlinger --display-id）
   ├─ 树：dump_xml() → shell rm -f → uiautomator dump /sdcard/x.xml → exec-out cat
   ├─ 输入：tap / swipe / long_press(零距离 swipe) / keyevent(白名单) / type_ascii
   ├─ 应用：launch_app（LAUNCHER intent 优先，-n 组件兜底）/ stop_app / current_app / list_packages
   └─ 信息：get_prop（正则校验）/ screen_size（PNG 头优先，wm size 兜底）/ wait_for（树轮询）

TextInput.type(text)
   ├─ probe()：yadb? → ADBKeyboard? → ascii
   ├─ _via_yadb / _via_adbkeyboard（存 IME → 广播 → finally 还原）/ _via_ascii（NFKD 折叠）
   └─ InputResult(strategy, typed, note)     # typed 可能与请求不同（折叠），上层据此判降级

tools.py（19 个纯函数 = MCP 工具面）
   感知类：get_screen_size / capture_screenshot_b64 / dump_ui_tree / ui_candidates / find_and_center
   动作类：tap / long_press / swipe / input_text / keyevent / press_back / press_home
   应用类：launch_app / stop_app / current_app / list_apps
   同步类：wait_for_text / sleep
   技能类：run_skill（延迟 import skills，保持依赖方向）
```

---

## 3. 借鉴来源

| 本层内容 | 来源 | 借鉴什么 | 改动（技术方案评估.md 编号） |
|---|---|---|---|
| `adb.py` PNG 裁剪 / exec-out 抓帧 | `android-agent/gitd/bots/common/adb.py:36-50` | `strip_to_png_signature`、`capture_screencap_png` | 保留（车机多屏必需，C-8） |
| `adb.py` keycode / text 防注入 | `adb.py:53-82` | `normalize_keycode` 白名单、`input_text_arg` 引号 | 原样保留（A-4） |
| `adb.py` 错误分级 | `adb.py:85-113` | 异常 vs 返回结果的区分 | 原样保留 |
| `adb.py` 树解析全家桶 | `adb.py:161-228, 449-463, 467-549` | 节点属性提取 / bounds 中心 / uiautomator dump 流程 | 改为模块级纯函数（无设备可用）；**删** Portal 快路径（A-1：装不了 Portal APK） |
| `adb.py` 单设备探测 | `adb.py:949-964` | `adb devices` 解析 | 所有命令带 serial（C-11） |
| `input.py` yadb / ADBKeyboard 命令 | `adb.py:262-283` + `LightGUIAgent/lightguiagent/agent.py:295-333` | 两种 Unicode 输入通道与探测 | **IME 存/复**（A-2，P0）；中文场景启动探测固化 |
| `tools.py` 纯函数工具面 | `android-agent/gitd/mcp_server.py:16,105` | `@mcp.tool()` 套壳普通函数、返回 `str` | 去掉 `device` 参数；压缩返回文本（§3 约束 2） |
| **删掉的内容** | `adb.py:357-420`（Portal）、`:245-258`（pinch）、TikTok 专区 | — | Portal 依赖装不上的 APK；pinch 是假实现（两只手都退化成单指 swipe） |

---

## 4. 与"只用 LightGUIAgent"相比的提升

| 维度 | LightGUIAgent（原状） | 本层 |
|---|---|---|
| 抓帧 | `screencap -p` 落盘 + `adb pull`，两次往返 | `exec-out` 一次往返 + PNG 签名裁剪 |
| 多屏防呆 | 无（车机抓帧可能带警告行/拿错屏） | `strip_to_png_signature` + 以截图解码尺寸为权威 |
| 设备探测 | 裸 `adb shell wm size`，可能探错设备 | 全程 `-s serial`；auto 模式 0/1/多 台都给出明确指引 |
| 中文输入 | 还原写死 Gboard → 车机 IME 被改坏（P0） | 存/复原 IME，finally 保证执行 |
| 安全 | 无注入防护（键码/文本/包名直通设备 shell） | keycode 白名单 + shlex 引用 + 包名/prop 正则 |
| 屏幕尺寸 | 信 `wm size` | PNG IHDR 优先（VLM 看到的才是权威） |
| 执行通道 | 进程内直调（无 MCP 面） | 纯函数工具面，local / mcp 双通道同源 |

一句话：**LightGUIAgent 的桥"能跑手机"；本层的桥"在车机上跑得对、跑得安全、跑得快"**——其中 IME 修复与防注入是照抄即翻车（P0）的必改项，抓帧提速是性能分的直接来源。
