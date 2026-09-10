"""Talking to devices: ADB discovery and a thin uiautomator2 adapter.

Two things live here, deliberately apart from each other:

* ``parse_adb_devices`` and ``bluestacks_ports`` are pure and tested.
* ``AndroidDevice`` is the only place uiautomator2 is touched, and it imports
  lazily so the rest of the package (and its tests) work without a device or
  the dependency installed.

There is no module-level "current device". The sibling repo this lives in
exists because ``mcp-chrome-bridge`` kept one process-wide ``Server`` and so
could only ever serve one client; the same mistake here would be a single
implicit device shared across every connected session. Callers name the
device they mean, and the registry only caches connections.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

ADB_TIMEOUT = 20.0

# BlueStacks hands each instance an ADB port ten apart, starting at 5555.
BLUESTACKS_FIRST_PORT = 5555
BLUESTACKS_PORT_STEP = 10


class DeviceError(RuntimeError):
    """Anything that went wrong reaching a device."""


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: str
    model: str = ""
    product: str = ""

    @property
    def usable(self) -> bool:
        """``offline`` and ``unauthorized`` devices are listed but unusable."""
        return self.state == "device"

    def render(self) -> str:
        bits = [self.serial, self.state]
        if self.model:
            bits.append(f"model={self.model}")
        return "  ".join(bits)


def parse_adb_devices(output: str) -> list[DeviceInfo]:
    """Parse ``adb devices -l``.

    Lines look like::

        emulator-5554   device product:sdk_gphone model:Pixel_6 transport_id:1
        127.0.0.1:5565  device product:BlueStacks model:BlueStacks
        ABC123          unauthorized
    """
    devices: list[DeviceInfo] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        if line.startswith("*"):
            continue  # "* daemon started successfully *"
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        tags = {}
        for token in parts[2:]:
            if ":" in token:
                key, _, value = token.partition(":")
                tags[key] = value
        devices.append(
            DeviceInfo(
                serial=serial,
                state=state,
                model=tags.get("model", ""),
                product=tags.get("product", ""),
            )
        )
    return devices


def bluestacks_ports(count: int = 10) -> list[int]:
    """The ADB ports BlueStacks would use for the first ``count`` instances."""
    if count < 0:
        raise ValueError("count must not be negative")
    return [
        BLUESTACKS_FIRST_PORT + BLUESTACKS_PORT_STEP * n for n in range(count)
    ]


def _adb_path() -> str:
    path = shutil.which("adb")
    if not path:
        raise DeviceError(
            "adb is not on PATH. Install platform-tools, or for BlueStacks use "
            "the adb.exe shipped in its install directory."
        )
    return path


def adb(*args: str, timeout: float = ADB_TIMEOUT) -> str:
    """Run adb and return stdout, raising with stderr attached on failure."""
    try:
        proc = subprocess.run(
            [_adb_path(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeviceError(f"adb {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise DeviceError(f"adb {' '.join(args)} failed: {detail}")
    return proc.stdout


def list_devices() -> list[DeviceInfo]:
    return parse_adb_devices(adb("devices", "-l"))


def connect_bluestacks(count: int = 10) -> list[DeviceInfo]:
    """Try to attach the first ``count`` BlueStacks instances, then list.

    Instances that are not running simply refuse the connection, so failures
    here are expected and ignored rather than raised.
    """
    for port in bluestacks_ports(count):
        try:
            adb("connect", f"127.0.0.1:{port}", timeout=5.0)
        except DeviceError:
            continue
    return list_devices()


def shell_runner(serial: str):
    """A callable running ``adb -s <serial> shell ...``.

    Profile management goes through raw adb rather than uiautomator2: its agent
    runs inside one user, and the point here is to act on the others.
    """

    def run(*args: str) -> str:
        return adb("-s", serial, "shell", *args)

    return run


class AndroidDevice:
    """Adapter over one uiautomator2 connection.

    Every method the action layer needs is declared here, so the action layer
    can be driven by a fake in tests.
    """

    def __init__(self, serial: str, impl=None):
        self.serial = serial
        self._impl = impl

    @property
    def impl(self):
        if self._impl is None:
            try:
                import uiautomator2
            except ImportError as exc:  # pragma: no cover - dependency missing
                raise DeviceError(
                    "uiautomator2 is not installed. pip install 'android-mcp[device]'"
                ) from exc
            try:
                self._impl = uiautomator2.connect(self.serial)
            except Exception as exc:  # pragma: no cover - needs a device
                raise DeviceError(
                    f"could not connect to {self.serial}: {exc}"
                ) from exc
        return self._impl

    # --- reading -------------------------------------------------------
    def dump(self) -> str:
        return self.impl.dump_hierarchy(compressed=False)

    def current_app(self) -> dict:
        try:
            return dict(self.impl.app_current())
        except Exception:  # noqa: BLE001 - see below  # pragma: no cover
            # app_current throws transiently while an app is switching. An
            # unknown foreground package is worth reporting as empty; it is not
            # worth failing the caller's whole screen read over.
            return {}

    def screenshot_png(self) -> bytes:
        import io

        image = self.impl.screenshot()
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    def window_size(self) -> tuple[int, int]:
        width, height = self.impl.window_size()
        return int(width), int(height)

    # --- acting --------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        self.impl.click(x, y)

    def long_click(self, x: int, y: int, duration: float = 0.8) -> None:
        self.impl.long_click(x, y, duration)

    def swipe(self, sx: int, sy: int, ex: int, ey: int, duration: float = 0.2) -> None:
        self.impl.swipe(sx, sy, ex, ey, duration)

    def press(self, key: str) -> None:
        self.impl.press(key)

    def send_keys(self, text: str, clear: bool = False) -> None:
        self.impl.send_keys(text, clear=clear)

    def clear_text(self) -> None:
        self.impl.clear_text()

    def app_start(self, package: str, activity: str | None = None) -> None:
        self.impl.app_start(package, activity=activity, wait=True)

    def app_stop(self, package: str) -> None:
        self.impl.app_stop(package)


class DeviceRegistry:
    """Caches connections by serial. Holds no notion of a "current" device."""

    def __init__(self):
        self._devices: dict[str, AndroidDevice] = {}

    def get(self, serial: str) -> AndroidDevice:
        if serial not in self._devices:
            self._devices[serial] = AndroidDevice(serial)
        return self._devices[serial]

    def put(self, device: AndroidDevice) -> None:
        """Inject a device, for tests and for pre-opened connections."""
        self._devices[device.serial] = device

    def forget(self, serial: str) -> None:
        self._devices.pop(serial, None)

    def resolve(self, serial: str | None, available: list[DeviceInfo]) -> str:
        """Turn an optional serial into a definite one.

        Defaulting is allowed only when it cannot be wrong: exactly one usable
        device attached. With several, the caller must say which, because
        silently picking one is how an automation run ends up driving the
        wrong instance.
        """
        usable = [d for d in available if d.usable]
        if serial:
            known = {d.serial for d in available}
            if known and serial not in known:
                raise DeviceError(
                    f"{serial} is not attached. Attached: "
                    + (", ".join(sorted(known)) or "none")
                )
            unusable = [d for d in available if d.serial == serial and not d.usable]
            if unusable:
                raise DeviceError(
                    f"{serial} is {unusable[0].state}, not ready. "
                    "Authorise the USB debugging prompt, or restart the instance."
                )
            return serial
        if not usable:
            raise DeviceError(
                "no usable device attached. Start a BlueStacks instance or plug "
                "in a phone, then call list_devices."
            )
        if len(usable) > 1:
            raise DeviceError(
                "several devices attached, so pass serial= explicitly: "
                + ", ".join(d.serial for d in usable)
            )
        return usable[0].serial
