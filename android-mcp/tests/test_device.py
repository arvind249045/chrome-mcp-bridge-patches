"""Tests for ADB output parsing and device resolution. No adb required."""

import pytest

from android_mcp.device import (
    BLUESTACKS_FIRST_PORT,
    AndroidDevice,
    DeviceError,
    DeviceInfo,
    DeviceRegistry,
    bluestacks_ports,
    parse_adb_devices,
)

ADB_OUTPUT = """* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
emulator-5554          device product:sdk_gphone64 model:Pixel_6 transport_id:1
127.0.0.1:5565         device product:BlueStacks model:BlueStacks transport_id:3
ABC123DEF              unauthorized
127.0.0.1:5575         offline
"""


# --- parsing adb ---------------------------------------------------------


def test_parses_every_attached_device():
    devices = parse_adb_devices(ADB_OUTPUT)
    assert [d.serial for d in devices] == [
        "emulator-5554",
        "127.0.0.1:5565",
        "ABC123DEF",
        "127.0.0.1:5575",
    ]


def test_pulls_out_the_model_tag():
    devices = {d.serial: d for d in parse_adb_devices(ADB_OUTPUT)}
    assert devices["emulator-5554"].model == "Pixel_6"
    assert devices["emulator-5554"].product == "sdk_gphone64"


def test_skips_the_daemon_chatter_and_the_header():
    assert all(
        not d.serial.startswith(("*", "List")) for d in parse_adb_devices(ADB_OUTPUT)
    )


def test_only_devices_in_state_device_are_usable():
    devices = {d.serial: d for d in parse_adb_devices(ADB_OUTPUT)}
    assert devices["127.0.0.1:5565"].usable
    assert not devices["ABC123DEF"].usable, "unauthorized is attached but unusable"
    assert not devices["127.0.0.1:5575"].usable, "offline is attached but unusable"


def test_handles_no_devices():
    assert parse_adb_devices("List of devices attached\n\n") == []
    assert parse_adb_devices("") == []


def test_render_is_readable():
    rendered = DeviceInfo("127.0.0.1:5565", "device", model="BlueStacks").render()
    assert "127.0.0.1:5565" in rendered and "model=BlueStacks" in rendered


# --- BlueStacks ports ----------------------------------------------------


def test_bluestacks_ports_step_by_ten():
    assert bluestacks_ports(4) == [5555, 5565, 5575, 5585]
    assert bluestacks_ports(1) == [BLUESTACKS_FIRST_PORT]
    assert bluestacks_ports(0) == []


def test_bluestacks_ports_rejects_a_negative_count():
    with pytest.raises(ValueError):
        bluestacks_ports(-1)


# --- resolving which device ---------------------------------------------


def attached(*pairs):
    return [DeviceInfo(serial, state) for serial, state in pairs]


def test_a_single_usable_device_needs_no_serial():
    registry = DeviceRegistry()
    assert registry.resolve(None, attached(("127.0.0.1:5555", "device"))) == (
        "127.0.0.1:5555"
    )


def test_several_devices_force_an_explicit_choice():
    """Silently picking one is how a run drives the wrong instance."""
    registry = DeviceRegistry()
    available = attached(("127.0.0.1:5555", "device"), ("127.0.0.1:5565", "device"))
    with pytest.raises(DeviceError) as exc:
        registry.resolve(None, available)
    assert "pass serial=" in str(exc.value)
    assert "127.0.0.1:5565" in str(exc.value)


def test_an_offline_device_does_not_count_as_the_only_one():
    registry = DeviceRegistry()
    available = attached(("127.0.0.1:5555", "device"), ("127.0.0.1:5565", "offline"))
    assert registry.resolve(None, available) == "127.0.0.1:5555"


def test_no_usable_device_is_a_clear_error():
    registry = DeviceRegistry()
    with pytest.raises(DeviceError) as exc:
        registry.resolve(None, attached(("ABC", "unauthorized")))
    assert "no usable device" in str(exc.value)


def test_an_unknown_serial_lists_what_is_actually_attached():
    registry = DeviceRegistry()
    with pytest.raises(DeviceError) as exc:
        registry.resolve("127.0.0.1:9999", attached(("127.0.0.1:5555", "device")))
    assert "not attached" in str(exc.value) and "127.0.0.1:5555" in str(exc.value)


def test_naming_an_unauthorized_device_explains_the_fix():
    registry = DeviceRegistry()
    with pytest.raises(DeviceError) as exc:
        registry.resolve("ABC", attached(("ABC", "unauthorized")))
    assert "unauthorized" in str(exc.value)


def test_an_empty_listing_trusts_an_explicit_serial():
    """adb may list nothing for a device reached directly over wifi."""
    registry = DeviceRegistry()
    assert registry.resolve("10.0.0.5:5555", []) == "10.0.0.5:5555"


# --- the registry itself -------------------------------------------------


def test_the_registry_caches_one_connection_per_serial():
    registry = DeviceRegistry()
    first = registry.get("127.0.0.1:5555")
    assert registry.get("127.0.0.1:5555") is first
    assert registry.get("127.0.0.1:5565") is not first


def test_a_device_can_be_injected_and_forgotten():
    registry = DeviceRegistry()
    injected = AndroidDevice("127.0.0.1:5555", impl=object())
    registry.put(injected)
    assert registry.get("127.0.0.1:5555") is injected
    registry.forget("127.0.0.1:5555")
    assert registry.get("127.0.0.1:5555") is not injected


def test_forgetting_an_unknown_serial_is_harmless():
    DeviceRegistry().forget("nope")


def test_the_registry_holds_no_current_device():
    """The bug the parent repo exists to patch, in its device-shaped form."""
    public = {name for name in vars(DeviceRegistry) if not name.startswith("_")}
    assert public == {"get", "put", "forget", "resolve"}
