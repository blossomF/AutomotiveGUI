import io

from PIL import Image, ImageDraw, ImageFont

from autogui.bridge import tools
from autogui.bridge.adb import Device
from autogui.config import load
from autogui.core.executor import LocalExecutor
from autogui.core.graph import Runner

W, H = 540, 1200

HOME_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" content-desc="" clickable="false" bounds="[0,0][540,1200]">
    <node index="0" text="设置" resource-id="com.android.launcher:id/app_name" class="android.widget.TextView" content-desc="" clickable="true" bounds="[30,420][190,580]" />
    <node index="1" text="QQ音乐" resource-id="com.android.launcher:id/app_name" class="android.widget.TextView" content-desc="" clickable="true" bounds="[195,420][355,580]" />
    <node index="2" text="微信" resource-id="com.android.launcher:id/app_name" class="android.widget.TextView" content-desc="" clickable="true" bounds="[360,420][520,580]" />
  </node>
</hierarchy>"""

SETTINGS_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="设置" resource-id="android:id/title" class="android.widget.TextView" content-desc="" clickable="false" bounds="[40,60][300,160]" />
  <node index="1" text="WLAN" resource-id="com.android.settings:id/title" class="android.widget.TextView" content-desc="" clickable="true" bounds="[40,300][500,420]" />
  <node index="2" text="蓝牙" resource-id="com.android.settings:id/title" class="android.widget.TextView" content-desc="" clickable="true" bounds="[40,430][500,550]" />
  <node index="3" text="关于手机" resource-id="com.android.settings:id/title" class="android.widget.TextView" content-desc="" clickable="true" bounds="[40,560][500,680]" />
</hierarchy>"""


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_png(state: int) -> bytes:
    img = Image.new("RGB", (W, H), "white" if state == 0 else (225, 235, 255))
    d = ImageDraw.Draw(img)
    if state == 0:
        for x0, label in ((30, "设置"), (195, "QQ音乐"), (360, "微信")):
            d.rectangle([x0, 300, x0 + 160, 400], outline="black", width=2)
            d.text((x0 + 40, 430), label, fill="black", font=_font(24))
    else:
        d.text((40, 60), "设置", fill="black", font=_font(40))
        for y0, label in ((300, "WLAN"), (430, "蓝牙"), (560, "关于手机")):
            d.rectangle([40, y0, 500, y0 + 120], outline="black", width=1)
            d.text((60, y0 + 40), label, fill="black", font=_font(28))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class FakeDevice(Device):
    def __init__(self):
        super().__init__("fake-local")
        self.state = 0
        self.actions = []

    def screenshot(self, timeout: float = 15) -> bytes:
        return make_png(self.state)

    def dump_xml(self, remote: str = "/sdcard/autogui_ui.xml", timeout: float = 15) -> str:
        return HOME_XML if self.state == 0 else SETTINGS_XML

    def screen_size(self, timeout: float = 15):
        return (W, H)

    def tap(self, x: int, y: int, delay: float | None = None) -> None:
        self.actions.append(f"tap({x},{y})")
        if self.state == 0:
            self.state = 1

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400, delay: float | None = None) -> None:
        self.actions.append(f"swipe({x1},{y1})->({x2},{y2})")

    def long_press(self, x: int, y: int, ms: int = 800, delay: float | None = None) -> None:
        self.actions.append(f"long_press({x},{y},{ms})")

    def keyevent(self, key: str, delay: float | None = None) -> None:
        self.actions.append(f"keyevent({key})")

    def back(self, delay: float | None = None) -> None:
        self.actions.append("back")
        self.state = 0

    def home(self, delay: float | None = None) -> None:
        self.actions.append("home")
        self.state = 0

    def launch_app(self, package: str, activity: str | None = None, delay: float = 2.0) -> str:
        self.actions.append(f"launch({package})")
        if "settings" in package:
            self.state = 1
        return "fake"

    def current_app(self, timeout: float = 8) -> dict:
        pkg = "com.android.settings" if self.state == 1 else "com.android.launcher"
        return {"package": pkg, "activity": "FakeActivity"}

    def list_packages(self, timeout: float = 15) -> list[str]:
        return ["com.android.launcher", "com.android.settings"]

    def get_prop(self, key: str, timeout: float = 5) -> str:
        return "fake"


class _TypeResult:
    def __init__(self, text: str):
        self.strategy = "fake"
        self.note = None
        self.typed = text


class FakeTextInput:
    def type(self, text: str, clear_first: bool = False):
        return _TypeResult(text)


def main():
    tools._device = FakeDevice()
    tools._text_input = FakeTextInput()

    cfg = load()
    from autogui.brain.glm_client import GLMClient

    runner = Runner(cfg=cfg, task="打开设置App", glm=GLMClient(cfg.glm), executor=LocalExecutor())
    result = runner.execute()
    print(result)
    print("fake device actions:", tools._device.actions)


if __name__ == "__main__":
    main()
