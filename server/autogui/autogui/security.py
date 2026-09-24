# -*- coding: utf-8 -*-
"""敏感凭证本地加密存储（Windows DPAPI）。

为什么是 DPAPI：
    config.yaml 需要进 git 仓库，但讯飞/GLM 的 key 不能明文落盘（验收扣分项）。
    Windows DPAPI（CryptProtectData）用当前 Windows 用户账户的登录密钥派生加密密钥，
    密文可以安全提交——换机器、换 Windows 用户都解不开，也就不存在"密钥文件和
    密文一起被拷走"的问题。零新增依赖（ctypes 调 crypt32.dll）、零密钥文件。

密文格式：
    config.yaml 中写成  api_secret: "ENC(base64密文)"
    前缀后缀用常量标记，config.py 的 validator 识别后透明解密，业务侧无感。

CLI（在 server/autogui 目录下）：
    python -m autogui.security migrate
        扫描 config.yaml 已知敏感字段，把明文就地加密（保留注释/格式/空值）
    python -m autogui.security set voice.api_key
        getpass 不回显录入新凭证（两遍确认），加密写入 config.yaml
    python -m autogui.security encrypt
        从 stdin 读一行明文，打印 ENC() 串（手工粘贴用）

注意：DPAPI 仅 Windows；非 Windows 调用 encrypt/decrypt 会抛 RuntimeError。
"""
from __future__ import annotations

import base64
import ctypes
import sys
from pathlib import Path

from ctypes import wintypes

PREFIX = "ENC("
SUFFIX = ")"

# config.yaml 中受保护的字段：section -> [字段名]
SECRET_FIELDS: dict[str, list[str]] = {
    "voice": ["appid", "api_key", "api_secret"],
    "glm": ["api_key"],
}


# ── DPAPI 原语（仅 Windows）────────────────────────────────────────────────────


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _require_windows():
    if sys.platform != "win32":
        raise RuntimeError(
            "DPAPI 凭证加密仅支持 Windows；非 Windows 平台请改用环境变量"
            "（IFLYTEK_APPID/IFLYTEK_API_KEY/IFLYTEK_API_SECRET/GLM_API_KEY）"
        )


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    _require_windows()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = _DATA_BLOB(len(data), ctypes.cast(ctypes.c_char_p(data),
                                               ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if decrypt:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError()  # 含系统错误码与中文描述
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ── 对外接口 ────────────────────────────────────────────────────────────────────


def is_encrypted(value: str) -> bool:
    """值是否为 ENC(...) 密文标记。"""
    return bool(value) and value.startswith(PREFIX) and value.endswith(SUFFIX) \
        and len(value) > len(PREFIX) + len(SUFFIX)


def encrypt(plaintext: str) -> str:
    """明文 → 'ENC(base64密文)'。空串原样返回（空值不加密）。"""
    if not plaintext:
        return ""
    blob = _dpapi(plaintext.encode("utf-8"), decrypt=False)
    return PREFIX + base64.b64encode(blob).decode("ascii") + SUFFIX


def decrypt(token: str) -> str:
    """'ENC(base64密文)' → 明文。非 ENC 标记原样返回（兼容明文/环境变量值）。"""
    if not is_encrypted(token):
        return token
    body = token[len(PREFIX):len(token) - len(SUFFIX)]
    blob = base64.b64decode(body)
    return _dpapi(blob, decrypt=True).decode("utf-8")


def resolve_secret(value: str) -> str:
    """配置加载用：ENC 密文解密，其他值（明文/空）原样返回。"""
    return decrypt(value) if is_encrypted(value) else value


# ── config.yaml 行级改写（保留注释与格式）───────────────────────────────────────


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def rewrite_config(config_path: Path, updates: dict[tuple[str, str], str]) -> list[str]:
    """按 (section, field) -> 新明文 的映射，加密后行级替换 config.yaml。

    只改命中的  key: value 行，其余字节（含注释/空行/顺序）原样保留。
    返回实际被改写的 "section.field" 列表。
    """
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    section = None
    changed: list[str] = []
    pending = dict(updates)

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        # 顶层 section（无缩进、以冒号结尾）
        if indent == "" and stripped.rstrip().endswith(":") and not stripped.startswith(("-", "#")):
            section = stripped.split(":", 1)[0].strip()
            continue
        if section not in SECRET_FIELDS:
            continue
        for field in SECRET_FIELDS[section]:
            key = (section, field)
            if key not in pending:
                continue
            # 精确匹配 "  field: ..."（field 后必须直接跟冒号）
            prefix = field + ":"
            if not stripped.startswith(prefix):
                continue
            rest = stripped[len(prefix):]
            # 分离行尾注释
            comment = ""
            hash_pos = -1
            in_s = in_d = False
            for j, ch in enumerate(rest):
                if ch == "'" and not in_d:
                    in_s = not in_s
                elif ch == '"' and not in_s:
                    in_d = not in_d
                elif ch == "#" and not in_s and not in_d:
                    hash_pos = j
                    break
            if hash_pos >= 0:
                comment = rest[hash_pos:]
                value_raw = rest[:hash_pos].rstrip()
                if comment.strip():
                    comment = " " + comment.rstrip("\r\n")
            else:
                value_raw = rest.rstrip("\r\n").rstrip()
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            token = encrypt(pending.pop(key))
            if token:
                new_line = f'{indent}{field}: "{token}"{comment}{newline}'
            else:  # 写入空值
                new_line = f"{indent}{field}: {comment.lstrip() or newline}"
            lines[i] = new_line
            changed.append(f"{section}.{field}")
            break

    if pending:
        missing = ", ".join(f"{s}.{f}" for s, f in pending)
        raise ValueError(f"config.yaml 中未找到字段：{missing}")

    config_path.write_text("".join(lines), encoding="utf-8", newline="")
    return changed


# ── CLI ─────────────────────────────────────────────────────────────────────────


def _default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.yaml"


def _cmd_migrate(config_path: Path) -> int:
    """把 config.yaml 中已有明文敏感值就地加密；空值/已加密跳过。"""
    import re

    text = config_path.read_text(encoding="utf-8")
    section = None
    updates: dict[tuple[str, str], str] = {}
    field_re = re.compile(r"^\s+([A-Za-z_][\w]*):\s*(.*?)\s*(?:#.*)?$")
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            section = line.split(":", 1)[0].strip()
            continue
        m = field_re.match(line)
        if not m or section not in SECRET_FIELDS:
            continue
        field, raw_val = m.group(1), _unquote(m.group(2))
        if field in SECRET_FIELDS[section] and raw_val and not is_encrypted(raw_val):
            updates[(section, field)] = raw_val
    if not updates:
        print("没有需要加密的明文敏感字段（空值或已是 ENC()）。")
        return 0
    print("将加密以下字段：")
    for (s, f), v in updates.items():
        print(f"  {s}.{f} = {v[:4]}…（{len(v)} 字符）")
    changed = rewrite_config(config_path, updates)
    print(f"完成，已就地加密 {len(changed)} 个字段：{', '.join(changed)}")
    return 0


def _cmd_set(config_path: Path, dotted: str) -> int:
    import getpass

    if dotted.count(".") != 1:
        print("字段名格式应为 section.field，例如 voice.api_key")
        return 2
    section, field = dotted.split(".")
    if section not in SECRET_FIELDS or field not in SECRET_FIELDS[section]:
        print(f"非敏感字段：{dotted}（受保护字段："
              + ", ".join(f"{s}.{f}" for s, fs in SECRET_FIELDS.items() for f in fs) + "）")
        return 2
    v1 = getpass.getpass(f"录入 {dotted}（不回显）：")
    if not v1:
        print("未输入，取消。")
        return 2
    v2 = getpass.getpass("再输一遍确认：")
    if v1 != v2:
        print("两次输入不一致，取消。")
        return 2
    changed = rewrite_config(config_path, {(section, field): v1})
    print(f"完成：{changed[0]} 已加密写入 {config_path.name}")
    return 0


def _cmd_encrypt_stdin() -> int:
    plaintext = sys.stdin.readline().rstrip("\r\n")
    print(encrypt(plaintext))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config_path = _default_config_path()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "migrate":
        return _cmd_migrate(config_path)
    if cmd == "set":
        if len(argv) != 2:
            print("用法：python -m autogui.security set <section.field>")
            return 2
        return _cmd_set(config_path, argv[1])
    if cmd == "encrypt":
        return _cmd_encrypt_stdin()
    print(f"未知子命令：{cmd}（可用：migrate / set / encrypt）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
