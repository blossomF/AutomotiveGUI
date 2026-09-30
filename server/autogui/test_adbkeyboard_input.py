r"""Test whether ADBKeyboard actually writes Chinese into a focused input field.

Before running, open an app search page and tap its empty search field yourself.
The script sends one test phrase, saves before/after screenshots, and restores
the previously selected IME. It does not press Enter or click anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


ADB_KEYBOARD = "com.android.adbkeyboard/.AdbIME"


def command(adb: str, serial: str | None, timeout: float, *args: str) -> dict:
    argv = [adb, *(["-s", serial] if serial else []), *args]
    started = time.perf_counter()
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
        return {"exit": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr, "seconds": round(time.perf_counter() - started, 2)}
    except subprocess.TimeoutExpired as exc:
        return {"exit": "timeout", "stdout": exc.stdout or b"",
                "stderr": exc.stderr or b"", "seconds": round(time.perf_counter() - started, 2)}


def output(result: dict) -> str:
    return (result["stdout"] + b"\n" + result["stderr"]).decode("utf-8", "replace").strip()


def screenshot(adb: str, serial: str | None, path: Path) -> dict:
    result = command(adb, serial, 15, "exec-out", "screencap", "-p")
    data = result["stdout"]
    if result["exit"] == 0 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        path.write_bytes(data)
    else:
        result["error"] = output(result)[:300] or "未返回 PNG"
    return result


def xml_readback(adb: str, serial: str | None, phrase: str, remote: str) -> dict:
    made = command(adb, serial, 8, "shell", "uiautomator", "dump", remote)
    if made["exit"] != 0:
        return {"status": "dump_timeout" if made["exit"] == "timeout" else "dump_failed",
                "seconds": made["seconds"], "detail": output(made)[:300]}
    read = command(adb, serial, 8, "exec-out", "cat", remote)
    if read["exit"] != 0:
        return {"status": "read_failed", "seconds": made["seconds"] + read["seconds"],
                "detail": output(read)[:300]}
    try:
        root = ET.fromstring(read["stdout"])
    except ET.ParseError as exc:
        return {"status": "invalid_xml", "bytes": len(read["stdout"]), "detail": str(exc)}
    fields = [node.attrib.get("text", "") for node in root.iter("node")
              if ("EditText" in node.attrib.get("class", "")
                  or node.attrib.get("editable") == "true")]
    return {"status": "ok", "bytes": len(read["stdout"]), "editable_fields": fields,
            "exact_match": phrase in fields, "seconds": made["seconds"] + read["seconds"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="聚焦输入框后，验证 ADBKeyboard 中文输入")
    parser.add_argument("--text", default="王菲 红豆", help="测试中文；默认：王菲 红豆")
    parser.add_argument("--adb", help="adb.exe 路径；默认使用 PATH 或项目 env/platform-tools")
    parser.add_argument("--serial", help="设备序列号；连接多台设备时必须填写")
    parser.add_argument("--output-dir", type=Path, help="保存截图和报告的目录")
    args = parser.parse_args()
    if not args.text or args.text.isascii():
        parser.error("--text 应包含要验证的中文")
    root = Path(__file__).resolve().parents[2]
    adb = args.adb or shutil.which("adb") or str(root / "env" / "platform-tools" / "adb.exe")
    if not Path(adb).is_file():
        parser.error("找不到 adb.exe；请用 --adb 指定完整路径")
    destination = args.output_dir or (Path(__file__).resolve().parent / "artifacts" /
                                      f"input_probe_{datetime.now():%Y%m%d_%H%M%S}")
    destination.mkdir(parents=True, exist_ok=True)

    devices = command(adb, None, 5, "devices")
    connected = re.findall(r"^([^\s]+)\s+device$", output(devices), re.M)
    if args.serial and args.serial not in connected:
        parser.error(f"设备 {args.serial!r} 未连接；请检查 adb devices")
    if not args.serial and len(connected) != 1:
        parser.error(f"当前检测到 {len(connected)} 台设备；请用 --serial 指定其中一台")
    serial = args.serial or connected[0]
    report = {"adb": adb, "serial": serial, "text": args.text,
              "adb_version": output(command(adb, None, 5, "version")),
              "ime_list": output(command(adb, serial, 5, "shell", "ime", "list", "-a"))}
    selected = command(adb, serial, 5, "shell", "settings", "get", "secure", "default_input_method")
    original_ime = output(selected)
    report["original_ime"] = original_ime
    before = screenshot(adb, serial, destination / "before.png")
    report["before_screenshot"] = "ok" if "error" not in before else before["error"]
    remote = f"/sdcard/autogui_input_probe_{os.getpid()}.xml"
    try:
        enabled = command(adb, serial, 5, "shell", "ime", "enable", ADB_KEYBOARD)
        report["ime_enable"] = {"exit": enabled["exit"], "output": output(enabled)}
        if enabled["exit"] != 0:
            raise RuntimeError("无法启用 ADBKeyboard")
        switched = command(adb, serial, 5, "shell", "ime", "set", ADB_KEYBOARD)
        report["ime_set"] = {"exit": switched["exit"], "output": output(switched)}
        if switched["exit"] != 0:
            raise RuntimeError("无法选中 ADBKeyboard")
        current = output(command(adb, serial, 5, "shell", "settings", "get",
                                 "secure", "default_input_method"))
        report["ime_after_set"] = current
        if current != ADB_KEYBOARD:
            raise RuntimeError(f"输入法切换后实际为 {current!r}")
        time.sleep(0.3)
        sent = command(adb, serial, 10, "shell", "am", "broadcast", "-a",
                       "ADB_INPUT_TEXT", "--es", "msg", args.text)
        report["broadcast"] = {"exit": sent["exit"], "output": output(sent),
                               "seconds": sent["seconds"]}
        time.sleep(0.5)
        after = screenshot(adb, serial, destination / "after.png")
        report["after_screenshot"] = "ok" if "error" not in after else after["error"]
        command(adb, serial, 5, "shell", "rm", "-f", remote)
        report["xml"] = xml_readback(adb, serial, args.text, remote)
    except RuntimeError as exc:
        report["error"] = str(exc)
    finally:
        if original_ime and original_ime != "null":
            restored = command(adb, serial, 5, "shell", "ime", "set", original_ime)
            report["ime_restore"] = {"exit": restored["exit"], "output": output(restored)}
        command(adb, serial, 5, "shell", "rm", "-f", remote)

    (destination / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"设备：{serial} | ADB：{report['adb_version'].splitlines()[0]}")
    print(f"输入法：{original_ime} → {report.get('ime_after_set', '切换失败')} → 已尝试恢复")
    print(f"广播：{report.get('broadcast', {}).get('output', report.get('error', '未执行'))}")
    print(f"XML：{report.get('xml', {}).get('status', '未执行')}；"
          f"精确读回={report.get('xml', {}).get('exact_match', '未知')}")
    print(f"前后截图及完整报告：{destination}")
    print("请检查 after.png 中输入框的文字；广播成功或 XML 为空都不能代替截图结论。")


if __name__ == "__main__":
    main()
