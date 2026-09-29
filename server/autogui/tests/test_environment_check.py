"""自检应检查截图，并将性能/能力不足标为警告。"""

from types import SimpleNamespace

import main
from autogui.bridge import adb
from autogui.perception import tree


def test_check_reports_auto_grid_and_warnings_without_model(monkeypatch, capsys):
    class Device:
        serial = "phone-1"

        def get_prop(self, name):
            return {"ro.product.model": "phone", "ro.build.version.release": "14"}[name]

        def screenshot(self):
            return b"png"

        def list_displays(self):
            return [0]

        def dump_xml(self):
            return "<hierarchy/>"

        def list_packages(self):
            return ["com.example.music", "com.other.unlisted"]

        def adb_soft(self, *args):
            return SimpleNamespace(stdout="original-ime")

    device = Device()
    tools = SimpleNamespace(dev=lambda: device,
                            text_input=lambda: SimpleNamespace(probe=lambda: "ascii"))
    cfg = SimpleNamespace(grid=SimpleNamespace(mode="auto", cols=20, rows=10),
                          display_id=None, input_chain=["yadb", "adbkeyboard", "ascii"],
                          apps=[SimpleNamespace(name="QQ音乐", package="com.example.music", in_scope=True),
                                SimpleNamespace(name="飞书", package="com.example.feishu", in_scope=True),
                                SimpleNamespace(name="其他", package="com.other.unlisted", in_scope=True)],
                          )
    monkeypatch.setattr(main, "_bootstrap", lambda _: tools)
    monkeypatch.setattr(adb, "png_size", lambda _: (1080, 2400))
    monkeypatch.setattr(tree, "candidates_from_xml", lambda *args, **kwargs: [object()] * 34)

    assert main.cmd_check(cfg) == 0
    output = capsys.readouterr().out
    assert "网格" not in output
    assert "可寻址候选" in output and "34 个" in output
    assert "[!] 中文输入能力" in output
    assert "[✓] 自检串行采集耗时" in output
    assert "应用 QQ音乐" in output and "com.example.music 已安装" in output
    assert "[!] 应用 飞书" in output
    assert "应用 其他" in output
    assert output.index("自检串行采集耗时") < output.index("应用 QQ音乐")
    assert "模型" not in output
