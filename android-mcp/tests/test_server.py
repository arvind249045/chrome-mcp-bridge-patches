"""End-to-end tests through the MCP tool surface, with a fake device.

Skipped when the MCP SDK is not installed, so the core suite still runs on a
machine that only has the parsing layer.
"""

import json

import pytest

pytest.importorskip("mcp", reason="needs the MCP SDK v2")

from conftest import FakeDevice, screen
from mcp.server.mcpserver.exceptions import ToolError

from android_mcp import server as server_module
from android_mcp.device import DeviceInfo, DeviceRegistry

LOGIN = screen(
    "Log in",
    {"text": "", "cls": "android.widget.EditText", "rid": "phone", "clickable": True},
    {"text": "Continue", "rid": "continue_btn", "clickable": True},
)

SERIAL = "127.0.0.1:5565"


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """A server wired to a fake device, with adb discovery stubbed out."""

    def build(
        reads=None,
        then=None,
        sequence=None,
        attached=((SERIAL, "device"),),
        flow_dir=None,
    ):
        fake = FakeDevice(reads or [LOGIN], then=then, sequence=sequence)
        fake.serial = SERIAL
        registry = DeviceRegistry()
        # The fake stands in for a connection, so uiautomator2 is never reached.
        registry.put(fake)
        monkeypatch.setattr(
            server_module,
            "list_devices",
            lambda: [DeviceInfo(s, st) for s, st in attached],
        )
        monkeypatch.setattr(
            server_module, "connect_bluestacks", lambda count=10: server_module.list_devices()
        )
        return (
            server_module.build_server(
                registry=registry, flow_dir=flow_dir or tmp_path
            ),
            fake,
        )

    return build


async def call(server, tool, **arguments):
    """Call a tool that is expected to succeed, returning its text.

    The parameter is named tool, not name, because several tools take a name= of
    their own.
    """
    result = await server.call_tool(tool, arguments)
    assert not result.is_error, result
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", "") == "text"
    )


async def call_failing(server, tool, **arguments) -> str:
    """Call a tool expected to fail, returning the message a model would see.

    The SDK raises ToolError out of call_tool and the protocol layer turns it
    into an isError result, so the message is what matters here.
    """
    with pytest.raises(ToolError) as exc:
        await server.call_tool(tool, arguments)
    return str(exc.value)


# --- registration --------------------------------------------------------


@pytest.mark.anyio
async def test_every_tool_is_registered(harness):
    server, _ = harness()
    names = {t.name for t in await server.list_tools()}
    assert names == {
        # discovery and reading
        "list_android_devices",
        "read_screen",
        "screenshot",
        # acting
        "open_app",
        "tap",
        "type_text",
        "swipe",
        "scroll_until",
        "wait_for",
        "press_key",
        # recording and replaying
        "start_recording",
        "save_flow",
        "cancel_recording",
        "list_flows",
        "run_flow",
        "delete_flow",
    }


@pytest.mark.anyio
async def test_tool_descriptions_are_present(harness):
    """They are the only instructions a model gets about the contract."""
    server, _ = harness()
    for tool in await server.list_tools():
        assert tool.description and len(tool.description) > 40, tool.name


def test_each_server_gets_its_own_registry():
    a = server_module.build_server()
    b = server_module.build_server()
    assert a is not b


# --- reading -------------------------------------------------------------


@pytest.mark.anyio
async def test_read_screen_returns_the_element_list(harness):
    server, _ = harness()
    text = await call(server, "read_screen")
    assert "com.bistro.app" in text and "state=" in text
    assert '"Continue"' in text and "#continue_btn" in text
    assert "clickable" in text


@pytest.mark.anyio
async def test_read_screen_hides_system_chrome_by_default(harness):
    server, _ = harness()
    default = await call(server, "read_screen")
    everything = await call(server, "read_screen", include_system=True)
    assert len(everything.splitlines()) >= len(default.splitlines())


# --- acting --------------------------------------------------------------


@pytest.mark.anyio
async def test_tap_by_text_acts_and_returns_the_next_screen(harness):
    server, fake = harness(then=[screen("Enter OTP")])
    text = await call(server, "tap", text="Continue")
    assert fake.clicks == [(540, 490)]
    assert "Enter OTP" in text


@pytest.mark.anyio
async def test_type_text_goes_through_the_ime(harness):
    server, fake = harness()
    await call(server, "type_text", value="9876543210", resource_id="phone")
    assert fake.keys == [("9876543210", True)]


@pytest.mark.anyio
async def test_press_key_works(harness):
    server, fake = harness(then=[screen("Home")])
    text = await call(server, "press_key", key="back")
    assert fake.presses == ["back"]
    assert "Home" in text


@pytest.mark.anyio
async def test_open_app_returns_the_first_screen(harness):
    server, fake = harness()
    text = await call(server, "open_app", package="com.bistro.app")
    assert fake.started == [("com.bistro.app", None)]
    assert "Log in" in text


# --- errors are recoverable ---------------------------------------------


@pytest.mark.anyio
async def test_a_missed_tap_is_an_error_that_shows_the_screen(harness):
    """One round trip: the model sees why it failed and what is there."""
    server, fake = harness()
    text = await call_failing(server, "tap", text="Checkout")
    assert "nothing matches" in text
    assert "Continue" in text, "the current screen comes back with the error"
    assert fake.clicks == []


@pytest.mark.anyio
async def test_a_stale_state_id_is_refused(harness):
    server, fake = harness()
    text = await call_failing(server, "tap", index=2, state_id="deadbeef")
    assert "the screen changed" in text
    assert fake.clicks == []


@pytest.mark.anyio
async def test_an_empty_selector_is_rejected(harness):
    server, _ = harness()
    text = await call_failing(server, "tap")
    assert "at least one of" in text


@pytest.mark.anyio
async def test_a_bad_swipe_direction_is_rejected(harness):
    server, _ = harness()
    text = await call_failing(server, "swipe", direction="sideways")
    assert "direction must be one of" in text


# --- device selection ----------------------------------------------------


@pytest.mark.anyio
async def test_several_devices_force_an_explicit_serial(harness):
    server, _ = harness(
        attached=((SERIAL, "device"), ("127.0.0.1:5575", "device")),
    )
    text = await call_failing(server, "read_screen")
    assert "pass serial=" in text


@pytest.mark.anyio
async def test_naming_the_serial_resolves_the_ambiguity(harness):
    server, _ = harness(
        attached=((SERIAL, "device"), ("127.0.0.1:5575", "device")),
    )
    text = await call(server, "read_screen", serial=SERIAL)
    assert "Continue" in text


@pytest.mark.anyio
async def test_listing_devices_flags_that_a_serial_is_needed(harness):
    server, _ = harness(
        attached=((SERIAL, "device"), ("127.0.0.1:5575", "device")),
    )
    text = await call(server, "list_android_devices")
    assert "2 usable devices" in text


@pytest.mark.anyio
async def test_listing_with_nothing_attached_says_what_to_do(harness):
    server, _ = harness(attached=())
    text = await call(server, "list_android_devices")
    assert "No devices attached" in text


# --- flows, end to end ---------------------------------------------------


@pytest.mark.anyio
async def test_record_save_then_replay(harness, tmp_path):
    """The whole point: do it once by hand, then replay it deterministically."""
    server, _fake = harness(sequence=[screen("Enter OTP")], flow_dir=tmp_path)

    await call(server, "start_recording", name="login")
    await call(server, "tap", text="Continue")
    saved = await call(server, "save_flow", description="get to the OTP screen")
    assert "saved 'login'" in saved

    listed = await call(server, "list_flows")
    assert "login" in listed and "get to the OTP screen" in listed

    # A fresh device in the same starting state replays without a model.
    replay_server, replay_device = harness(
        sequence=[screen("Enter OTP")], flow_dir=tmp_path
    )
    report = await call(replay_server, "run_flow", name="login")
    assert "all 1 steps ok" in report
    assert replay_device.clicks == [(540, 490)]


@pytest.mark.anyio
async def test_a_recorded_flow_stores_ranked_selectors(harness, tmp_path):
    server, _ = harness(sequence=[screen("Enter OTP")], flow_dir=tmp_path)
    await call(server, "start_recording", name="login")
    await call(server, "tap", text="Continue")
    await call(server, "save_flow")

    detail = await call(server, "list_flows", name="login")
    assert "1 steps" in detail
    stored = json.loads((tmp_path / "login.json").read_text(encoding="utf-8"))
    selectors = stored["steps"][0]["selectors"]
    assert selectors[0] == {"resource_id": "continue_btn", "exact": True}
    assert len(selectors) > 1


@pytest.mark.anyio
async def test_a_parameterised_flow_round_trips(harness, tmp_path):
    form = screen(
        "Search",
        {"text": "", "cls": "android.widget.EditText", "rid": "q", "clickable": True},
    )
    server, _ = harness(reads=[form], flow_dir=tmp_path)
    await call(server, "start_recording", name="search")
    await call(server, "type_text", value="Paneer Roll", resource_id="q")
    await call(server, "save_flow", parameters={"dish": "Paneer Roll"})

    replay_server, replay_device = harness(reads=[form], flow_dir=tmp_path)
    await call(replay_server, "run_flow", name="search", params={"dish": "Cold Coffee"})
    assert replay_device.keys == [("Cold Coffee", True)]


@pytest.mark.anyio
async def test_replaying_with_a_missing_parameter_is_refused(harness, tmp_path):
    form = screen(
        "Search",
        {"text": "", "cls": "android.widget.EditText", "rid": "q", "clickable": True},
    )
    server, _ = harness(reads=[form], flow_dir=tmp_path)
    await call(server, "start_recording", name="search")
    await call(server, "type_text", value="Paneer Roll", resource_id="q")
    await call(server, "save_flow", parameters={"dish": "Paneer Roll"})

    replay_server, replay_device = harness(reads=[form], flow_dir=tmp_path)
    text = await call_failing(replay_server, "run_flow", name="search")
    assert "needs dish" in text
    assert replay_device.keys == []


@pytest.mark.anyio
async def test_a_failing_replay_reports_the_step_and_the_screen(harness, tmp_path):
    server, _ = harness(sequence=[screen("Enter OTP")], flow_dir=tmp_path)
    await call(server, "start_recording", name="login")
    await call(server, "tap", text="Continue")
    await call(server, "save_flow")

    # The app has changed: nothing the flow knows about is on screen.
    other, device = harness(reads=[screen("Maintenance")], flow_dir=tmp_path)
    text = await call_failing(other, "run_flow", name="login")
    assert "stopped at step 1" in text
    assert "Maintenance" in text
    assert device.clicks == []


@pytest.mark.anyio
async def test_saving_without_recording_is_an_error(harness):
    server, _ = harness()
    text = await call_failing(server, "save_flow")
    assert "not recording" in text


@pytest.mark.anyio
async def test_recording_nothing_then_saving_is_an_error(harness):
    server, _ = harness()
    await call(server, "start_recording", name="empty")
    text = await call_failing(server, "save_flow")
    assert "nothing was recorded" in text


@pytest.mark.anyio
async def test_a_bad_flow_name_is_refused(harness):
    server, _ = harness()
    text = await call_failing(server, "start_recording", name="../escape")
    assert "not a usable flow name" in text


@pytest.mark.anyio
async def test_cancelling_discards_the_recording(harness):
    server, _ = harness(sequence=[screen("Enter OTP")])
    await call(server, "start_recording", name="login")
    await call(server, "tap", text="Continue")
    assert "discarded" in await call(server, "cancel_recording")
    assert "not recording" in await call_failing(server, "save_flow")


@pytest.mark.anyio
async def test_actions_outside_a_recording_are_not_captured(harness, tmp_path):
    server, _ = harness(sequence=[screen("Enter OTP")], flow_dir=tmp_path)
    await call(server, "tap", text="Continue")
    assert "no flows saved yet" in await call(server, "list_flows")


@pytest.mark.anyio
async def test_running_an_unknown_flow_lists_what_is_saved(harness, tmp_path):
    server, _ = harness(sequence=[screen("Enter OTP")], flow_dir=tmp_path)
    await call(server, "start_recording", name="login")
    await call(server, "tap", text="Continue")
    await call(server, "save_flow")
    text = await call_failing(server, "run_flow", name="nope")
    assert "login" in text


@pytest.mark.anyio
async def test_deleting_a_flow(harness, tmp_path):
    server, _ = harness(sequence=[screen("Enter OTP")], flow_dir=tmp_path)
    await call(server, "start_recording", name="login")
    await call(server, "tap", text="Continue")
    await call(server, "save_flow")
    assert "deleted" in await call(server, "delete_flow", name="login")
    assert "no flows saved yet" in await call(server, "list_flows")
    assert "no flow called" in await call_failing(server, "delete_flow", name="login")
