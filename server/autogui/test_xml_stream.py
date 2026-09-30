r"""Compare uiautomator XML output via a device file and direct stdout.

Run from the repository root on Windows:
    .\server\autogui\.venv\Scripts\python.exe .\server\autogui\test_xml_stream.py

This diagnostic only dumps the current UI. It does not tap or change apps.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def find_adb(override: str | None) -> str:
    if override:
        return override
    found = shutil.which("adb")
    if found:
        return found
    root = Path(__file__).resolve().parents[2]
    bundled = root / "env" / "platform-tools" / "adb.exe"
    if bundled.is_file():
        return str(bundled)
    raise SystemExit("找不到 adb；请用 --adb 指定 adb.exe 路径。")


def run(adb: str, serial: str | None, timeout: float, *args: str) -> tuple[int | None, bytes, bytes, float]:
    command = [adb, *(["-s", serial] if serial else []), *args]
    started = time.perf_counter()
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        return result.returncode, result.stdout, result.stderr, time.perf_counter() - started
    except subprocess.TimeoutExpired as exc:
        return None, exc.stdout or b"", exc.stderr or b"", time.perf_counter() - started


def xml_status(data: bytes) -> tuple[bool, int, str]:
    start = data.find(b"<?xml")
    if start < 0:
        start = data.find(b"<hierarchy")
    if start < 0:
        return False, 0, "未找到 XML 起始标签"
    payload = data[start:].strip()
    trailing = b""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        end = payload.find(b"</hierarchy>")
        if end < 0:
            return False, 0, f"XML 不完整或混入输出：{exc}"
        end += len(b"</hierarchy>")
        trailing = payload[end:].strip()
        try:
            root = ET.fromstring(payload[:end])
        except ET.ParseError:
            return False, 0, f"XML 不完整或混入输出：{exc}"
    if root.tag != "hierarchy":
        return False, 0, f"根节点不是 hierarchy：{root.tag}"
    note = "完整 XML"
    if trailing:
        note += f"；XML 后附 {len(trailing)} bytes：{trailing[:160].decode('utf-8', 'replace')!r}"
    return True, sum(1 for _ in root.iter("node")), note


def show(label: str, result: tuple[int | None, bytes, bytes, float]) -> None:
    code, stdout, stderr, elapsed = result
    valid, nodes, note = xml_status(stdout)
    print(f"{label}: {elapsed:.2f}s | exit={code if code is not None else '超时'} | "
          f"stdout={len(stdout)} bytes | XML={'是' if valid else '否'} | 节点={nodes}")
    print(f"  判断：{note}")
    if stderr:
        print(f"  stderr：{stderr.decode('utf-8', 'replace')[:300].strip()}")
    if not valid and stdout:
        print(f"  stdout 前 200 字符：{stdout[:200].decode('utf-8', 'replace').strip()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="测试车机 XML 能否不经临时文件直接输出")
    parser.add_argument("--adb", help="adb.exe 的完整路径；默认从 PATH 或项目 env/platform-tools 寻找")
    parser.add_argument("--serial", help="设备序列号；仅连接一台设备时可省略")
    parser.add_argument("--timeout", type=float, default=8, help="每条 ADB 命令超时秒数，默认 8")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    adb = find_adb(args.adb)
    print(f"ADB：{adb}")
    code, output, error, _ = run(adb, args.serial, args.timeout, "devices")
    if code != 0 or not any(b"\tdevice" in line for line in output.splitlines()):
        raise SystemExit("没有可用设备。请检查 adb devices。" + error.decode("utf-8", "replace"))

    remote = f"/sdcard/autogui_xml_stream_test_{os.getpid()}.xml"
    try:
        run(adb, args.serial, args.timeout, "shell", "rm", "-f", remote)
        created = run(adb, args.serial, args.timeout, "shell", "uiautomator", "dump", remote)
        show("当前方案：生成设备文件", created)
        if created[0] == 0:
            show("当前方案：读回设备文件", run(adb, args.serial, args.timeout, "exec-out", "cat", remote))
        for path in ("/dev/stdout", "/proc/self/fd/1"):
            show(f"直接输出：{path}", run(adb, args.serial, args.timeout,
                                      "exec-out", "uiautomator", "dump", path))
    finally:
        run(adb, args.serial, args.timeout, "shell", "rm", "-f", remote)
    print("结论：直接输出项若显示 XML=是，即可评估替换；否则保留当前方案。")


if __name__ == "__main__":
    main()
