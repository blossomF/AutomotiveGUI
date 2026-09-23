"""声明式 YAML 配方，零 VLM 调用执行。

文件格式沿用 ``android-agent/gitd/skills/<app>/skill.yaml``——包名、
带参数和步骤列表的命名技能——以及该项目的 ``popup_detectors`` 思路
（``adb.py:134-150`` 加 :690-739 的三层 ``dismiss_popups``）。我们
刻意**不**照搬的是它的 ``Action``/``Workflow`` 类体系（前置条件/
后置条件/回滚）：那套抽象是为跨设备回放录制宏而建的，把车机 UI
映射到上面会花掉我们没有的几天。步骤就是一个 dict，由 dispatcher
解析。

让这一层不只是存在、还能得分的两条规则：

  * **解析不了的 hint 抛 ``SkillMiss``，由 router 转成向通用
    tree/vlm 路由的优雅降级。** 因此一个脆弱的配方永远不会比没有
    配方更糟——这正是技能可以一周内批量编写且安全的原因。它也和
    挣 +10 技术深度的降级路径共用同一套实现。
  * **坐标永远不写进配方。** hint 是 text/desc/rid 匹配器，每步
    对*新鲜的* dump 解析，所以配方能扛住 UI 改版和分辨率变化。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from autogui.bridge.adb import Device, find_node, node_center
from autogui.bridge.input import TextInput

# 中文车机弹窗。android-agent 原版是英文/TikTok 专属的
# （{"not now","skip","cancel",...}），在这里什么都匹配不到——
# 见 技术方案评估.md §2 A-3。
DEFAULT_POPUPS: list[dict] = [
    {"detect": "用户协议", "button": "同意"},
    {"detect": "隐私政策", "button": "同意"},
    {"detect": "隐私权政策", "button": "同意"},
    {"detect": "开启个性化推荐", "button": "以后再说"},
    {"detect": "通知权限", "button": "以后再说"},
    {"detect": "所在位置", "button": "禁止"},
    {"detect": "更新", "button": "以后再说"},
]
GENERIC_DISMISS = ("以后再说", "暂不", "暂不开启", "稍后再说", "取消", "关闭", "跳过",
                   "我知道了", "不同意", "残忍离开", "不再提醒", "稍后")


class SkillMiss(RuntimeError):
    """某一步在当前屏幕上找不到目标——调用方应降级。"""


@dataclass
class Skill:
    app: str
    name: str
    summary: str
    params: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    popups: list[dict] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    package: str = ""

    def signature(self, unavailable: bool = False) -> str:
        """一行技能签名，注入永久提示词块。

        ⚠️ 必须写**全名**（``app.name``）。只写短名会让模型和内部键对不上：
        日志/上下文里失败的是 ``bilibili.search_video``，而它看到的清单是
        ``search_video`` → 模型会断定"上次名字写错了，改用 search_video"，
        可短名解析回来还是同一个技能，于是反复重试同一个跑不通的配方
        （实测白烧 6 步）。清单与内部键用同一个写法，模型才不会误诊。
        """
        args = ", ".join(f"{p}=..." for p in self.params)
        mark = "  ⛔已失效(勿再选)" if unavailable else ""
        return f"{self.app}.{self.name}({args}) — {self.summary}{mark}"

    def trigger_words(self) -> list[str]:
        """声明的触发词，缺省回退到技能名和 App 名。"""
        return [t for t in self.triggers if t] or [self.name.replace("_", ""), self.app]


class SkillBook:
    """全部已加载技能 + 执行器。"""

    def __init__(self, root: Path | str, device: Device, text_input: TextInput):
        self.device = device
        self.text_input = text_input
        self.root = Path(root)
        self.by_id: dict[str, Skill] = {}
        self.load()

    # ── 加载 ─────────────────────────────────────────────────────────────────

    def load(self) -> None:
        self.by_id.clear()
        if not self.root.exists():
            return
        for path in sorted(self.root.glob("*/skill.yaml")):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                print(f"  [skill] skipping {path}: {e}")
                continue
            package = (doc.get("app") or {}).get("package", "")
            popups = doc.get("popups") or []
            for spec in doc.get("skills") or []:
                name = spec.get("name")
                if not name:
                    continue
                skill = Skill(
                    app=doc.get("name", path.parent.name),
                    name=name,
                    summary=spec.get("summary", ""),
                    params=list(spec.get("params") or []),
                    steps=list(spec.get("steps") or []),
                    popups=popups,
                    triggers=[str(x) for x in (spec.get("triggers") or [])],
                    package=package,
                )
                self.by_id[f"{skill.app}.{skill.name}"] = skill

    def index(self, unavailable: set[str] | None = None) -> str:
        """每个技能一行，注入永久提示词块。

        ``unavailable`` 是已被判定不可用（重试次数用尽）的技能 key 集合——
        在清单里就地标出来。之前清单是静态的，模型看不到"这个已经废了"，
        于是反复选中同一个失败配方；标出来才能真正止损。
        """
        dead = unavailable or set()
        return "\n".join(
            s.signature(k in dead) for k, s in sorted(self.by_id.items(), key=lambda kv: kv[1].app)
        ) or "（无）"

    def get(self, key: str) -> Skill | None:
        if key in self.by_id:
            return self.by_id[key]
        hits = [s for k, s in self.by_id.items() if k.endswith("." + key) or s.name == key]
        return hits[0] if len(hits) == 1 else None

    # ── 执行 ─────────────────────────────────────────────────────────────────

    def run(self, key: str, params: dict | None = None, log_cb=None) -> list[str]:
        """执行配方，返回人类可读的步骤轨迹。

        ``log_cb`` 可选：每跑完一步就回调（line: str），让调用方实时落盘日志——
        否则 trace 只能在配方全跑完后一次性交付，面板在配方执行期间看不到任何进展。
        """
        skill = self.get(key)
        if skill is None:
            raise SkillMiss(f"unknown skill {key!r}")
        args = dict(params or {})
        for p in skill.params:
            if p not in args:
                raise SkillMiss(f"{key} missing required param {p!r}")

        trace: list[str] = []
        for i, step in enumerate(skill.steps, 1):
            line = self._run_step(skill, step, args)
            entry = f"  step{i} {line}"
            trace.append(entry)
            if log_cb:
                log_cb(entry)  # 实时回调：执行方在配方跑完的同时逐行写日志
        return trace

    def _sub(self, skill: Skill, value, args: dict):
        """替换 ``{param}`` 和隐式的 ``{package}``。"""
        if not isinstance(value, str):
            return value
        out = value
        if "{package}" in out:
            out = out.replace("{package}", skill.package)
        for k, v in args.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def _arg_or_text(self, skill: Skill, step: dict, args: dict) -> str:
        """一步可以取 ``arg: <参数名>`` 或字面 ``text:``。"""
        if step.get("arg"):
            key = step["arg"]
            if key not in args:
                raise SkillMiss(f"step references unknown param {key!r}")
            return str(self._sub(skill, args[key], args))
        return str(self._sub(skill, step.get("text", ""), args))

    def _run_step(self, skill: Skill, step: dict, args: dict) -> str:
        act = step.get("act", "")
        d = self.device

        if act == "launch":
            pkg = self._sub(skill, step.get("package") or skill.package, args)
            if not pkg:
                raise SkillMiss("launch step has no package to use")
            how = d.launch_app(pkg, delay=float(step.get("delay", 2.0)))
            return f"launch {pkg} via {how}"

        if act == "stop":
            pkg = self._sub(skill, step.get("package") or skill.package, args)
            d.stop_app(pkg)
            return f"stop {pkg}"

        if act == "dismiss_popups":
            n = self.dismiss(skill, extra=step.get("extra") or [])
            return f"dismiss_popups (closed {n})"

        if act in ("click", "tap"):
            node = self._locate(skill, step, args)
            c = node_center(node)
            if not c:
                raise SkillMiss("matched node has no bounds")
            d.tap(*c)
            return f"tap {self._describe(step, skill, args)} -> ({c[0]},{c[1]})"

        if act == "type":
            value = self._arg_or_text(skill, step, args)
            r = self.text_input.type(value, clear_first=bool(step.get("clear_first")))
            if not r.typed:
                raise SkillMiss(f"cannot type {value!r} on this device")
            return f"type {value!r} via {r.strategy}"

        if act == "scroll":
            direction = step.get("direction", "down")
            self._scroll(direction, step.get("amount", 0.5))
            return f"scroll {direction}"

        if act == "keyevent":
            key = step.get("key", "KEYCODE_BACK")
            d.keyevent(key)
            return f"keyevent {key}"

        if act == "wait_for":
            needle = self._arg_or_text(skill, step, args)
            try:
                d.wait_for(needle, timeout=float(step.get("timeout", 10)))
            except TimeoutError as e:
                raise SkillMiss(str(e))
            return f"wait_for {needle!r} ok"

        if act == "sleep":
            import time

            time.sleep(min(float(step.get("seconds", 1.0)), 5.0))
            return f"sleep {step.get('seconds', 1.0)}s"

        if act in ("complete", "done"):
            return "complete"

        raise SkillMiss(f"unsupported step act={act!r}")

    # ── 辅助 ─────────────────────────────────────────────────────────────────

    def _describe(self, step: dict, skill=None, args: dict | None = None) -> str:
        """生成步骤描述字符串，用于 trace 和错误信息。

        给 skill/args 时会对 hint 里的值做 ``{param}`` 替换——否则 trace
        里会看到字面量 ``{keyword}`` 而不是真实值。
        """
        hint = step.get("hint") or {}
        for k in ("text", "desc", "rid"):
            val = hint.get(k)
            if val:
                if skill is not None and args is not None:
                    val = self._sub(skill, val, args)
                return f"{k}={val!r}"
        if step.get("nth") is not None:
            return f"nth={step['nth']}"
        return "?"

    def _locate(self, skill: Skill, step: dict, args: dict) -> str:
        """对一份新鲜 dump，把 hint 解析成一个节点。"""
        hint = step.get("hint") or {}
        match = {k: self._sub(skill, v, args) for k, v in hint.items() if k in ("text", "desc", "rid")}
        if not match:
            raise SkillMiss("click step needs hint.text / hint.desc / hint.rid")
        from autogui.bridge.adb import find_nodes

        xml = self.device.dump_xml()
        nth = int(step.get("nth", hint.get("nth", 0)))
        found: list[str] = []
        for key, val in match.items():
            found = find_nodes(xml, clickable_only=True, **{key: val})
            if len(found) > nth:
                break
        if not found:  # 重试：去掉 clickable 要求
            for key, val in match.items():
                found = find_nodes(xml, **{key: val})
                if len(found) > nth:
                    break
        if len(found) <= nth:
            raise SkillMiss(f"no match for {self._describe(step, skill, args)} (found {len(found)}, need index {nth})")
        return found[nth]

    def _scroll(self, direction: str, amount: float) -> None:
        w, h = self.device.screen_size()
        cx, cy = w // 2, h // 2
        dx, dy = int(w * amount / 2), int(h * amount / 2)
        vector = {
            "down": (cx, cy + dy, cx, cy - dy),
            "up": (cx, cy - dy, cx, cy + dy),
            "left": (cx + dx, cy, cx - dx, cy),
            "right": (cx - dx, cy, cx + dx, cy),
        }
        if direction not in vector:
            raise SkillMiss(f"bad scroll direction {direction!r}")
        self.device.swipe(*vector[direction])

    def dismiss(self, skill: Skill, extra: list | None = None) -> int:
        """三层弹窗处理：声明的 -> 通用词表 -> 放弃。

        结构对齐 ``android-agent`` 的 ``dismiss_popups``
        （adb.py:690-739），换成中文词表，并保留其超大节点
        保护，避免把整宽字幕误当成关闭按钮。
        """
        closed = 0
        for _ in range(4):  # 弹窗有时会叠着出
            xml = self.device.dump_xml()
            if not xml:
                break
            from autogui.bridge.adb import find_nodes, nodes, node_bounds, node_text, node_desc

            target = None
            for popup in (skill.popups or DEFAULT_POPUPS) + (extra or []):
                if popup.get("detect") and popup["detect"] not in xml:
                    continue
                btn = popup.get("button")
                if popup.get("method") == "back":
                    self.device.back()
                    closed += 1
                    target = "back"
                    break
                cands = find_nodes(xml, text=btn) or find_nodes(xml, desc=btn)
                cands = [c for c in cands if node_bounds(c)]
                if cands:
                    cands.sort(key=lambda n: node_bounds(n)[1])  # 最靠上的实例
                    self.device.tap(*node_center(cands[0]))
                    closed += 1
                    target = btn
                    break
            if target:
                continue

            for n in nodes(xml):
                if 'clickable="true"' not in n:
                    continue
                b = node_bounds(n)
                if not b:
                    continue
                w, h = self.device.screen_size()
                if (b[2] - b[0]) > w * 0.6:  # 内容区，不是按钮
                    continue
                if b[3] < 40:
                    continue
                label = f"{node_text(n)} {node_desc(n)}".strip()
                if any(word in label for word in GENERIC_DISMISS):
                    self.device.tap(*node_center(n))
                    closed += 1
                    break
            else:
                break
        return closed


def run_by_name(key: str, params: dict, *, device: Device | None = None) -> list[str]:
    """``run_skill`` MCP 工具用的便捷入口。"""
    from autogui.config import PROJECT_ROOT, load

    if device is None:
        from autogui.bridge.tools import dev

        device = dev()
    cfg = load()
    book = SkillBook(PROJECT_ROOT / cfg.skills.directory, device, TextInput(device))
    return book.run(key, params)
