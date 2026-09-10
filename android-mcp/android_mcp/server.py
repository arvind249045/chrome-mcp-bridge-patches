"""The MCP server: the tool surface a model sees.

Design notes, because the shape of this surface is the whole point:

* Tools return the **resulting screen**, not "ok". A model should never have to
  ask "what happened" after acting.
* Screens come back as a filtered element list, not a screenshot. A typical
  screen is a few hundred tokens instead of an image, and it is selectable and
  resolution-independent. ``screenshot`` stays available for the cases where the
  hierarchy genuinely cannot help: canvas, WebView, games.
* Nothing waits on a fixed sleep. Every tool returns once the screen has
  settled, and says so when it has not.
* There is no implicit "current device". Tools take ``serial``, defaulted only
  when exactly one device is attached, so a multi-instance setup cannot quietly
  drive the wrong emulator.
"""

from __future__ import annotations

from . import actions
from .actions import ActionError, Selector
from .device import DeviceError, DeviceRegistry, connect_bluestacks, list_devices

try:  # pragma: no cover - import shape differs across SDK majors
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "android-mcp needs the MCP Python SDK v2 or newer "
        "(`pip install 'mcp>=2'`). In v1 the class was called FastMCP; this "
        f"server targets MCPServer. Original error: {exc}"
    ) from exc


INSTRUCTIONS = """\
Drive an Android device or BlueStacks instance.

Read the screen with read_screen. It returns one line per element:

    [7] Button "Continue" #continue_btn clickable @540,810

Refer to an element by text=, resource_id= or desc= whenever you can; those
survive a layout change. Use index= only for an element you just read, and pass
the state_id from that same read so a tap cannot land on a screen that moved
underneath you.

Every tool returns the screen after the action has settled. If a header says
NOT SETTLED or BUSY, the screen was still moving when time ran out.
"""


def build_server(
    name: str = "android-mcp", registry: DeviceRegistry | None = None
) -> MCPServer:
    """Build a server with its own device registry.

    The registry is per-instance on purpose: shared mutable state across
    sessions is the bug this repository's parent patch set exists to fix. Pass
    one in to reuse already-open connections, or to drive fakes in tests.
    """
    server = MCPServer(name=name, instructions=INSTRUCTIONS)
    registry = registry if registry is not None else DeviceRegistry()

    def device_for(serial: str | None):
        try:
            resolved = registry.resolve(serial, list_devices())
        except DeviceError as exc:
            raise ToolError(str(exc)) from exc
        return registry.get(resolved)

    def selector(
        index: int | None,
        text: str | None,
        resource_id: str | None,
        desc: str | None,
        cls: str | None = None,
        exact: bool = False,
    ) -> Selector:
        try:
            return Selector(
                index=index,
                text=text,
                resource_id=resource_id,
                desc=desc,
                cls=cls,
                exact=exact,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def act(fn, *args, **kwargs) -> str:
        """Run an action, turning our errors into tool errors.

        The messages already carry the current screen, so a model that guessed
        wrong can correct itself from the error alone.
        """
        try:
            return fn(*args, **kwargs).render()
        except (ActionError, DeviceError) as exc:
            raise ToolError(str(exc)) from exc

    # --- discovery -----------------------------------------------------

    @server.tool()
    def list_android_devices(probe_bluestacks: bool = True) -> str:
        """List attached devices and emulators.

        Call this first when you do not know what is attached, or when another
        tool says several devices are available. With probe_bluestacks, also
        tries to attach BlueStacks instances on the ports it uses (5555, 5565,
        5575, ...); instances that are not running are skipped.
        """
        try:
            found = connect_bluestacks() if probe_bluestacks else list_devices()
        except DeviceError as exc:
            raise ToolError(str(exc)) from exc
        if not found:
            return (
                "No devices attached. Start a BlueStacks instance, or connect a "
                "phone with USB debugging enabled."
            )
        lines = [d.render() for d in found]
        usable = [d for d in found if d.usable]
        if len(usable) > 1:
            lines.append(
                f"\n{len(usable)} usable devices, so pass serial= to the other tools."
            )
        return "\n".join(lines)

    # --- reading -------------------------------------------------------

    @server.tool()
    def read_screen(
        serial: str | None = None,
        include_system: bool = False,
        settle_timeout: float = 5.0,
    ) -> str:
        """Read the current screen as an element list.

        Returns the foreground package/activity, a state_id for this exact
        screen, and one line per element that is readable or actionable. Set
        include_system to also show the status bar and navigation bar.
        """
        device = device_for(serial)
        snap = actions.snapshot(device, timeout=settle_timeout)
        return snap.render(include_system=include_system)

    @server.tool()
    def screenshot(serial: str | None = None) -> Image:
        """Take a PNG screenshot.

        A fallback for screens the view hierarchy cannot describe: canvas,
        WebView, games, or a custom renderer. Prefer read_screen otherwise, as
        it is far cheaper and gives you elements you can address by name.
        """
        device = device_for(serial)
        try:
            return Image(data=device.screenshot_png(), format="png")
        except DeviceError as exc:
            raise ToolError(str(exc)) from exc

    # --- acting --------------------------------------------------------

    @server.tool()
    def open_app(
        package: str,
        serial: str | None = None,
        activity: str | None = None,
        timeout: float = 15.0,
    ) -> str:
        """Launch an app and return its first settled screen.

        Waits for the package to actually reach the foreground, so you do not
        need a separate read afterwards. If another app is in front when time
        runs out, the error says which.
        """
        return act(
            actions.open_app,
            device_for(serial),
            package,
            activity=activity,
            timeout=timeout,
        )

    @server.tool()
    def tap(
        serial: str | None = None,
        index: int | None = None,
        text: str | None = None,
        resource_id: str | None = None,
        desc: str | None = None,
        exact: bool = False,
        long: bool = False,
        state_id: str | None = None,
    ) -> str:
        """Tap an element and return the resulting screen.

        Name the element by text, resource_id or desc where possible. Matching
        on text is case-insensitive and partial, unless exact is set; an exact
        label beats a partial one, and a tappable match beats an inert one.

        index refers to the last read of this screen, so pass the state_id from
        that read too: if the screen changed in between, the tap is refused
        rather than landing somewhere unintended. Set long for a long press.
        """
        return act(
            actions.tap,
            device_for(serial),
            selector(index, text, resource_id, desc, exact=exact),
            long=long,
            expect_state=state_id,
        )

    @server.tool()
    def type_text(
        value: str,
        serial: str | None = None,
        index: int | None = None,
        text: str | None = None,
        resource_id: str | None = None,
        desc: str | None = None,
        exact: bool = False,
        clear: bool = True,
        submit: bool = False,
        state_id: str | None = None,
    ) -> str:
        """Type into a field, identified the same way as in tap.

        Taps the field to focus it, then types value through the device IME, so
        spaces and non-ASCII survive. By default it replaces what is there; set
        clear=False to append. Set submit to press enter afterwards.
        """
        return act(
            actions.type_text,
            device_for(serial),
            selector(index, text, resource_id, desc, exact=exact),
            value,
            clear=clear,
            submit=submit,
            expect_state=state_id,
        )

    @server.tool()
    def swipe(
        direction: str,
        serial: str | None = None,
        fraction: float = 0.5,
    ) -> str:
        """Scroll the screen one swipe and return the result.

        direction is where the content goes, as a person would say it: "down"
        reveals what is further down the page. fraction is how much of the
        screen the swipe covers, between 0 and 0.9.
        """
        try:
            return act(actions.swipe, device_for(serial), direction, fraction=fraction)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    def scroll_until(
        serial: str | None = None,
        text: str | None = None,
        resource_id: str | None = None,
        desc: str | None = None,
        exact: bool = False,
        direction: str = "down",
        max_swipes: int = 8,
    ) -> str:
        """Swipe until an element appears, then return that screen.

        Stops early when the screen stops changing, which means the list has
        reached its end, rather than swiping max_swipes times pointlessly.
        """
        return act(
            actions.scroll_until,
            device_for(serial),
            selector(None, text, resource_id, desc, exact=exact),
            direction=direction,
            max_swipes=max_swipes,
        )

    @server.tool()
    def wait_for(
        serial: str | None = None,
        text: str | None = None,
        resource_id: str | None = None,
        desc: str | None = None,
        exact: bool = False,
        timeout: float = 10.0,
    ) -> str:
        """Wait for an element to appear, then return that screen.

        For a screen you are expecting after a slow action. On timeout the error
        shows what is on screen instead, so you can see where the flow diverged.
        """
        return act(
            actions.wait_for,
            device_for(serial),
            selector(None, text, resource_id, desc, exact=exact),
            timeout=timeout,
        )

    @server.tool()
    def press_key(key: str, serial: str | None = None) -> str:
        """Press a hardware or navigation key and return the resulting screen.

        Common keys: back, home, recent, enter, delete, search, volume_up.
        """
        return act(actions.press, device_for(serial), key)

    return server


def main() -> None:
    """Entry point for the console script and ``python -m android_mcp``."""
    build_server().run(transport="stdio")
