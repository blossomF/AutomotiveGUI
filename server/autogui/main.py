"""Automotive GUI Agent —— 命令行入口。

    python main.py "在音乐App里播放周杰伦的《七里香》"   # 跑一个任务
    python main.py --packages                     # 列出已安装的包（用于填 config.yaml）
    python main.py --check                        # 设备 + 抓帧 + 输入自检
    python main.py --dump-report                  # 逐 App 控件树质量报告
    python main.py --mcp-check                     # 列出 MCP 工具面
    python main.py --selftest                      # 不需要车机、不需要网络

``--dump-report`` 遍历已配置 App，dump 真实 UI 树，统计可寻址候选并写出
``artifacts/dump-report.md``，供定位质量排查与验收使用。

``--check`` 包含一条专门的 IME 往返断言，对应 技术方案评估.md §2 A-2 的 P0 风险：
移植来的 ADBKeyboard 路径原本会还原成写死的 Gboard，而车机上并没有 Gboard。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _bootstrap(cfg):
    from autogui.bridge import tools

    tools.bind_device(cfg.device_serial, input_chain=cfg.input_chain,
                      display=cfg.display_id, verbose=cfg.verbose)
    return tools


# ── 命令 ─────────────────────────────────────────────────────────────────────


def cmd_run(args, cfg) -> int:
    from autogui.brain.glm_client import GLMClient
    from autogui.core.graph import Runner

    tools = _bootstrap(cfg)
    task = args.task.strip()
    if args.skill_only:
        return _run_skill_direct(args, cfg, tools)

    glm = GLMClient(cfg.glm)
    runner = Runner(cfg=cfg, task=task, glm=glm)
    result = runner.execute()
    return 0 if result["success"] else 1


def _run_skill_direct(args, cfg, tools) -> int:
    """显式 --skill-only 调试入口：跳过模型直接执行配方。"""
    from autogui.skills.base import SkillBook, SkillMiss

    book = SkillBook(Path(__file__).resolve().parent / cfg.skills.directory, tools.dev(), tools.text_input())
    print("可用配方：\n" + "\n".join(sorted(book.by_id)) + "\n")

    key, _, param = args.task.partition("=")
    key = key.strip()
    skill = book.get(key)
    if skill is None:
        print(f"找不到技能 {key!r}")
        return 1
    params = {}
    if param:
        if not skill.params:
            print(f"{key} 不接收参数")
            return 1
        params[skill.params[0]] = param.strip()
    try:
        for line in book.run(key, params):
            print(line)
        return 0
    except SkillMiss as e:
        print(f"SKILL_MISS: {e}")
        return 1
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return 1


def cmd_packages(cfg) -> int:
    tools = _bootstrap(cfg)
    pkgs = tools.list_apps("").splitlines()
    interesting = [p for p in pkgs if not p.startswith(("com.android.", "android.", "com.google."))]
    print(f"共 {len(pkgs)} 个包，非系统前缀 {len(interesting)} 个：\n")
    for p in interesting:
        print(f"  {p}")
    print("\n把需要的那几个填进 config.yaml 的 apps: [{name, package}]")
    return 0


def cmd_check(cfg) -> int:
    """设备自检：硬故障记失败，定位质量和耗时问题记警告；不请求模型 API。"""
    from autogui.bridge.adb import ADBError, png_size
    from autogui.perception.grid import auto_grid_dimensions, grid_dimensions
    from autogui.perception.tree import candidates_from_xml

    tools = _bootstrap(cfg)
    device = tools.dev()
    ok = True

    def report(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  [{'✓' if passed else '✗'}] {label:<28} {detail}")

    def warn(label: str, detail: str) -> None:
        print(f"  [!] {label:<28} {detail}")

    print("=" * 72)
    print("环境自检")
    print("=" * 72)
    report("ADB 连接", True, device.serial)
    report("型号", True, device.get_prop("ro.product.model"))
    report("Android 版本", True, device.get_prop("ro.build.version.release"))

    t = time.perf_counter()
    png = device.screenshot()
    shot_ms = (time.perf_counter() - t) * 1000
    size = png_size(png)
    report("exec-out 抓帧", bool(size), f"{size} {shot_ms:.0f}ms")
    if shot_ms >= 2500:
        warn("抓帧速度", f"{shot_ms:.0f}ms ≥ 2500ms，任务响应可能变慢")
    displays = device.list_displays()
    if len(displays) > 1:
        # 规则 4.2 把范围限定在中控屏；多屏车机可能抓到的是仪表屏，
        # 那会悄悄把错误的屏幕喂给模型。
        warn("多屏提示", f"发现 {len(displays)} 块屏 {displays}；确认截图是中控，必要时设 display_id")
    if size:
        cols, rows = grid_dimensions(*size, mode=cfg.grid.mode,
                                     cols=cfg.grid.cols, rows=cfg.grid.rows)
        recommended = auto_grid_dimensions(*size)
        cw, ch = size[0] / cols, size[1] / rows
        report("网格", True, f"{cfg.grid.mode} {cols}×{rows} → cell {cw:.0f}×{ch:.0f}px")
        if not (60 <= cw <= 220 and 60 <= ch <= 220):
            warn("网格密度", f"当前单格超出建议 60–220px；自动建议 {recommended[0]}×{recommended[1]}")

    t = time.perf_counter()
    xml = device.dump_xml()
    dump_ms = (time.perf_counter() - t) * 1000
    cands = candidates_from_xml(xml, limit=200, screen=size)
    report("uiautomator dump", bool(xml), f"{len(xml)} bytes {dump_ms:.0f}ms")
    if dump_ms >= 3000:
        warn("控件树速度", f"{dump_ms:.0f}ms ≥ 3000ms，任务响应可能变慢")
    report("可寻址候选", True, f"{len(cands)} 个（≥3 可走 tree，否则走 VLM）")
    if len(cands) < 3:
        warn("tree 不可用", "当前屏幕候选不足；主流程会使用 VLM 网格")

    # P0：IME 必须在一轮 Unicode 输入尝试后保持完好。
    try:
        before = device.adb_soft("shell", "settings", "get", "secure", "default_input_method").stdout
        result = tools.text_input().probe()
        report("输入策略", True, f"{result}（链：{'>'.join(cfg.input_chain)}）")
        if str(result).strip().lower() == "ascii":
            warn("中文输入能力", "当前仅确认 ASCII 回退；尚未证明中文输入可用")
        after = device.adb_soft("shell", "settings", "get", "secure", "default_input_method").stdout
        # yadb/ascii 不会碰 IME；adbkeyboard 必须精确还原。
        same = (before or "").strip() == (after or "").strip()
        report("IME 未被改坏", same, f"{before!r} -> {after!r}" if not same else before)
        ok = ok and same
    except ADBError as e:
        report("输入探测", False, str(e))

    total = shot_ms + dump_ms
    report("自检串行采集耗时", True, f"≈{total:.0f}ms（截图 + XML；主流程另行并行采集）")
    if total >= 4500:
        warn("采集速度", f"串行合计 {total:.0f}ms ≥ 4500ms")

    # 最后核对设置页维护的全部目标应用；不记录其他 App。
    selected = [app for app in cfg.apps if app.in_scope]
    if selected:
        try:
            installed = set(device.list_packages())
            for app in selected:
                if app.package and app.package in installed:
                    report(f"应用 {app.name}", True, f"{app.package} 已安装")
                else:
                    warn(f"应用 {app.name}", f"配置包名 {app.package or '(空)'} 未安装或未配置")
        except ADBError as e:
            warn("应用包名查询", f"失败：{e}")
    else:
        print("  [i] 应用包名                     未配置应用，跳过")
    print("=" * 72)
    print("设备硬检查通过；请核对上方警告" if ok else "存在设备硬故障，请先处理 ✗ 项")
    return 0 if ok else 1


def cmd_dump_report(cfg) -> int:
    """遍历已配置的 App，实测控件树质量。"""
    from autogui.perception.tree import candidates_from_xml

    tools = _bootstrap(cfg)
    device = tools.dev()
    if not cfg.apps:
        print("config.yaml 里没有 apps: 列表。先运行 `python main.py --packages`。")
        return 1

    rows = []
    for app in cfg.apps:
        if not app.package:
            rows.append((app.name, "-", "-", "-", "包名未填"))
            print(f"  {app.name:<14} 跳过（包名为空）")
            continue
        print(f"  {app.name:<14} {app.package}")
        try:
            device.launch_app(app.package, delay=2.5)
            xml = device.dump_xml()
            size = device.screen_size()
            cands = candidates_from_xml(xml, limit=200, screen=size)
            labelled = sum(1 for c in cands if c.clickable)
            usable = len(cands) >= 5
            rows.append((app.name, len(xml), len(cands), labelled, "可用" if usable else "不可用"))
            device.home(delay=0.8)
        except Exception as e:
            rows.append((app.name, "-", "-", "-", f"错误: {type(e).__name__}"))
        time.sleep(0.5)

    usable_count = sum(1 for r in rows if r[4] == "可用")
    out = Path(__file__).resolve().parent / "artifacts" / "dump-report.md"
    out.parent.mkdir(exist_ok=True)
    lines = [
        "# 控件树质量实测", "",
        f"- 设备：`{device.serial}`  屏幕：`{device.screen_size()}`",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", "",
        "| App | dump bytes | 可寻址候选 | 其中 clickable | 结论 |",
        "|---|---|---|---|---|",
    ]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |" for r in rows]
    lines += ["", f"**可用 App 数：{usable_count}**", "",
              "> 运行时固定控件树优先；树不可用时降级到 VLM。"]
    out.write_text("\n".join(lines), encoding="utf-8")

    print("\n" + "\n".join(lines[6:]))
    print(f"\n报告已写入 {out}")
    return 0


def cmd_mcp_check(cfg) -> int:
    from autogui.core.executor import MCPExecutor, LocalExecutor

    tools = _bootstrap(cfg)
    from autogui.mcp_server import build_server

    server, _ = build_server()
    ex = MCPExecutor(server)
    names = ex.list_tools()
    print(f"MCP 会话建立成功，暴露 {len(names)} 个工具：")
    for n in sorted(names):
        print(f"  {n}")
    probe = ex.call("get_screen_size")
    print(f"\n往返验证 get_screen_size() -> {probe!r}")
    print(f"local 与 mcp 同源校验：{LocalExecutor().call('get_screen_size')!r} == {probe!r} -> "
          f"{LocalExecutor().call('get_screen_size') == probe}")
    ex.close()
    return 0


def cmd_selftest(cfg) -> int:
    """跑离线测试套件——不需要车机、不需要 API Key。这是 CI 门禁。"""
    import subprocess

    root = Path(__file__).resolve().parent
    return subprocess.call([sys.executable, "-m", "pytest", "-q", str(root / "tests")], cwd=str(root))


def cmd_voice_test(args, cfg) -> int:
    """--voice-test：麦克风 + 讯飞 ASR，只验证语音通道（不需要车机、不需要 GLM）。"""
    from autogui.voice.asr import VoiceError, transcribe_from_mic

    try:
        text = transcribe_from_mic(cfg, max_seconds=args.voice_seconds, device=args.voice_device).strip()
    except VoiceError as e:
        print(f"\n✗ {e}")
        return 2
    print(f"\n识别结果：{text or '（空）'}")
    return 0 if text else 2


def cmd_voice(args, cfg) -> int:
    """--voice：麦克风 + 讯飞 ASR，把识别文本作为任务执行（需要车机 + GLM）。"""
    from autogui.voice.asr import VoiceError, transcribe_from_mic

    try:
        task = transcribe_from_mic(cfg, max_seconds=args.voice_seconds, device=args.voice_device).strip()
    except VoiceError as e:
        print(f"\n✗ {e}")
        return 2
    if not task:
        print("✗ 识别结果为空，未执行")
        return 2
    print(f"\n任务（语音识别）：{task}\n")
    args.task = task
    return cmd_run(args, cfg)


# ── argparse 参数解析 ─────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autogui",
        description="Automotive GUI Agent — see README.md before the first run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("task", nargs="?", default="", help='natural-language task, e.g. "在B站搜索并播放..."; use with --packages/--check/--dump-report/--mcp-check/--selftest"')
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("--exec-mode", choices=["local", "mcp"], default=None, help="override config exec_mode")
    p.add_argument("--skill-only", action="store_true", help='run one recipe with no model, e.g. --skill-only "music.play_song=七里香"')
    p.add_argument("--packages", action="store_true", help="list installed packages and exit")
    p.add_argument("--check", action="store_true", help="device/capture/input self-test and exit")
    p.add_argument("--dump-report", action="store_true", help="measure tree quality per app; write artifacts/dump-report.md")
    p.add_argument("--mcp-check", action="store_true", help="open a real MCP session and list tools")
    p.add_argument("--selftest", action="store_true", help="run offline tests")
    p.add_argument("--voice-test", action="store_true", help="麦克风+讯飞ASR，只打印识别文本（不需要车机/网络GLM）")
    p.add_argument("--voice", action="store_true", help="麦克风+讯飞ASR，识别文本作为任务执行")
    p.add_argument("--voice-seconds", type=float, default=None, help="--voice 最长录音秒数（默认取 config.yaml voice.max_seconds，15）")
    p.add_argument("--voice-device", type=int, default=None, help="麦克风设备序号（python -m sounddevice 查看列表）")
    return p


def main(argv: list[str] | None = None) -> int:
    from autogui.config import Config

    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    if args.exec_mode:
        cfg.exec_mode = args.exec_mode

    try:
        if args.selftest:
            return cmd_selftest(cfg)
        if args.mcp_check:
            return cmd_mcp_check(cfg)
        if args.packages:
            return cmd_packages(cfg)
        if args.check:
            return cmd_check(cfg)
        if args.dump_report:
            return cmd_dump_report(cfg)
        if args.voice_test:
            return cmd_voice_test(args, cfg)
        if args.voice:
            return cmd_voice(args, cfg)
        if not args.task:
            build_parser().print_help()
            return 2
        return cmd_run(args, cfg)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except RuntimeError as e:
        # 设备缺失是首次运行最常见的失败；把原因说清楚，
        # 而不是给照着 README 操作的 Demo Day 现场的人看一堆 traceback。
        print(f"\n✗ {e}")
        if "adb" in str(e).lower() or "device" in str(e).lower():
            print("  下一步：`adb devices` 确认车机在线；首次连接先 `adb connect <车机IP>`。")
            print("  不需要车机也能跑的检查：`python main.py --selftest`")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
