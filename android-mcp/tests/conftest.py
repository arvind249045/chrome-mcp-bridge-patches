"""Fakes that let the action layer be tested with no device attached."""

from __future__ import annotations

WINDOW = (1080, 2400)


def screen(*items, package: str = "com.bistro.app") -> str:
    """Build a hierarchy dump from a short description of each row.

    An item is either a plain label or a dict accepting ``text``, ``cls``,
    ``rid``, ``clickable``, ``enabled`` and ``bounds``.
    """
    width, height = WINDOW
    nodes = []
    for i, item in enumerate(items):
        if isinstance(item, str):
            item = {"text": item}
        top = item.get("top", 200 + i * 120)
        bounds = item.get("bounds", f"[0,{top}][{width},{top + 100}]")
        attrs = [
            f'class="{item.get("cls", "android.widget.TextView")}"',
            f'package="{package}"',
            f'text="{item.get("text", "")}"',
            f'bounds="{bounds}"',
        ]
        if item.get("rid"):
            attrs.append(f'resource-id="{package}:id/{item["rid"]}"')
        if item.get("desc"):
            attrs.append(f'content-desc="{item["desc"]}"')
        if item.get("clickable"):
            attrs.append('clickable="true"')
        if item.get("enabled") is False:
            attrs.append('enabled="false"')
        nodes.append("    <node " + " ".join(attrs) + " />")
    body = "\n".join(nodes)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n'
        '<hierarchy rotation="0">\n'
        f'  <node class="android.widget.FrameLayout" package="{package}" '
        f'bounds="[0,0][{width},{height}]">\n{body}\n  </node>\n'
        "</hierarchy>\n"
    )


SPINNER = {"cls": "android.widget.ProgressBar", "text": ""}


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


class FakeDevice:
    """Scripted device.

    ``reads`` are returned from ``dump()`` in order and the last one repeats,
    which models a screen that changes for a while and then holds still. Reads
    never advance the script on their own: only actions do, via ``then``.
    """

    def __init__(
        self,
        reads,
        *,
        package: str = "com.bistro.app",
        activity: str = ".MainActivity",
        size=WINDOW,
        then=None,
    ):
        self.serial = "fake:5555"
        self._reads = list(reads) or [screen()]
        self._package = package
        self._activity = activity
        self._size = size
        self._then = list(then) if then else None
        self.dumps = 0
        self.clicks: list[tuple[int, int]] = []
        self.long_clicks: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int]] = []
        self.keys: list[tuple[str, bool]] = []
        self.presses: list[str] = []
        self.started: list[tuple[str, str | None]] = []

    # --- reading ---
    def dump(self) -> str:
        self.dumps += 1
        if len(self._reads) > 1:
            return self._reads.pop(0)
        return self._reads[0]

    def current_app(self) -> dict:
        return {"package": self._package, "activity": self._activity}

    def window_size(self):
        return self._size

    def screenshot_png(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n"

    # --- acting ---
    def _advance(self) -> None:
        if self._then is not None:
            self._reads = list(self._then)
            self._then = None

    def click(self, x, y):
        self.clicks.append((x, y))
        self._advance()

    def long_click(self, x, y, duration=0.8):
        self.long_clicks.append((x, y))
        self._advance()

    def swipe(self, sx, sy, ex, ey, duration=0.2):
        self.swipes.append((sx, sy, ex, ey))
        self._advance()

    def press(self, key):
        self.presses.append(key)
        self._advance()

    def send_keys(self, text, clear=False):
        self.keys.append((text, clear))

    def clear_text(self):
        self.keys.append(("", True))

    def app_start(self, package, activity=None):
        self.started.append((package, activity))
        self._advance()

    def app_stop(self, package):
        pass

    def become(self, package: str, activity: str = ".MainActivity") -> None:
        self._package, self._activity = package, activity


# --- async test plumbing -------------------------------------------------
try:  # only needed for the MCP server tests
    import pytest

    @pytest.fixture
    def anyio_backend():
        """Run the async tests on asyncio only, not trio."""
        return "asyncio"

except ImportError:  # pragma: no cover
    pass
