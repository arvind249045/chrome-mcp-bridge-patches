"""Tests for the device self-check, driven entirely by fakes."""

from conftest import FakeDevice, screen

from android_mcp.device import DeviceError, DeviceInfo
from android_mcp.selfcheck import (
    FAIL,
    OK,
    SKIP,
    CheckResult,
    Context,
    check_connect,
    check_devices,
    check_hierarchy,
    check_profiles,
    check_settle,
    check_tap,
    main,
    render,
    run,
)

SCREEN = screen(
    "Your order",
    {"text": "Place order", "rid": "place", "clickable": True},
)
PM_USERS = "Users:\n\tUserInfo{0:Owner:c13} running\n"


def context(device=None, attached=(("127.0.0.1:5565", "device"),), **kw):
    fake = device if device is not None else FakeDevice([SCREEN])
    options = {"settle_timeout": 0.3, "settle_interval": 0.01}
    options.update(kw)
    return Context(
        discover=lambda: [DeviceInfo(s, st) for s, st in attached],
        connect=lambda serial: fake,
        shell=lambda serial: (lambda *a: PM_USERS),
        **options,
    )


# --- individual stages ---------------------------------------------------


def test_device_stage_resolves_the_only_attached_device():
    ctx = context()
    result = check_devices(ctx)
    assert result.status == OK
    assert ctx.resolved == "127.0.0.1:5565"


def test_device_stage_fails_with_nothing_attached():
    result = check_devices(context(attached=()))
    assert result.status == FAIL
    assert "nothing attached" in result.detail


def test_device_stage_fails_when_attached_but_unauthorized():
    result = check_devices(context(attached=(("ABC", "unauthorized"),)))
    assert result.status == FAIL
    assert "not usable" in result.detail


def test_connect_stage_reports_the_screen_size():
    ctx = context()
    check_devices(ctx)
    result = check_connect(ctx)
    assert result.status == OK
    assert "1080x2400" in result.detail


def test_hierarchy_stage_measures_the_real_compression_ratio():
    """The design's central claim, checked against whatever is on screen."""
    ctx = context()
    check_devices(ctx)
    check_connect(ctx)
    result = check_hierarchy(ctx)
    assert result.status == OK
    assert "% of the dump" in result.detail
    assert any("measured on your screen" in n for n in ctx.notes)


def test_hierarchy_stage_fails_on_an_empty_dump():
    ctx = context(device=FakeDevice([""]))
    check_devices(ctx)
    check_connect(ctx)
    result = check_hierarchy(ctx)
    assert result.status == FAIL
    assert "returned nothing" in result.detail


def test_hierarchy_stage_explains_a_screen_it_cannot_describe():
    bare = '<hierarchy rotation="0"><node class="android.view.View" package="g" '
    bare += 'bounds="[0,0][1080,2400]" /></hierarchy>'
    ctx = context(device=FakeDevice([bare]))
    check_devices(ctx)
    check_connect(ctx)
    result = check_hierarchy(ctx)
    assert result.status == FAIL
    assert "game or WebView" in result.detail


def test_settle_stage_reports_how_long_it_took():
    ctx = context()
    check_devices(ctx)
    check_connect(ctx)
    result = check_settle(ctx)
    assert result.status == OK
    assert "settled in" in result.detail


def test_settle_stage_fails_on_a_screen_that_never_stops():
    ctx = context(device=FakeDevice([screen(f"frame {i}") for i in range(200)]))
    check_devices(ctx)
    check_connect(ctx)
    result = check_settle(ctx)
    assert result.status == FAIL
    assert "still changing" in result.detail


def test_profile_stage_lists_profiles():
    ctx = context()
    check_devices(ctx)
    result = check_profiles(ctx)
    assert result.status == OK and "Owner" in result.detail


def test_profile_stage_explains_an_image_without_multi_user():
    ctx = context()
    ctx.shell = lambda serial: (lambda *a: "")
    check_devices(ctx)
    result = check_profiles(ctx)
    assert result.status == FAIL
    assert "do not support multiple users" in result.detail


def test_profile_stage_reports_an_adb_failure():
    def boom(*a):
        raise DeviceError("adb lost the device")

    ctx = context()
    ctx.shell = lambda serial: boom
    check_devices(ctx)
    result = check_profiles(ctx)
    assert result.status == FAIL and "lost the device" in result.detail


def test_tap_stage_is_skipped_unless_asked():
    ctx = context()
    result = check_tap(ctx)
    assert result.status == SKIP
    assert "--tap" in result.detail


def test_tap_stage_passes_when_the_screen_moves():
    device = FakeDevice([SCREEN], sequence=[screen("Order placed")])
    ctx = context(device=device, tap_text="Place order")
    check_devices(ctx)
    check_connect(ctx)
    result = check_tap(ctx)
    assert result.status == OK
    assert device.clicks


def test_tap_stage_fails_when_nothing_changes():
    ctx = context(tap_text="Place order")
    check_devices(ctx)
    check_connect(ctx)
    result = check_tap(ctx)
    assert result.status == FAIL
    assert "did not change" in result.detail


# --- the run itself ------------------------------------------------------


def test_a_healthy_device_passes_every_blocking_stage():
    ctx = context()
    results = run(ctx)
    assert [r.status for r in results if r.status == FAIL] == []
    assert sum(1 for r in results if r.status == OK) == 5


def test_a_blocking_failure_skips_the_rest():
    """One root cause should not print as six errors."""
    results = run(context(attached=()))
    assert results[0].status == FAIL
    assert all(r.status == SKIP for r in results[1:])
    assert "earlier stage failed" in results[1].detail


def test_a_non_blocking_failure_does_not_stop_the_run():
    ctx = context()
    ctx.shell = lambda serial: (lambda *a: "")
    results = run(ctx)
    statuses = {r.name: r.status for r in results}
    assert statuses["profile tools work"] == FAIL
    assert statuses["a tap lands"] == SKIP, "reached, not skipped for a failure"


def test_an_unexpected_exception_becomes_a_finding():
    def explode(ctx):
        raise RuntimeError("something odd")

    explode.stage_name = "exploding stage"
    results = run(context(), stages=[explode])
    assert results[0].status == FAIL
    assert "RuntimeError: something odd" in results[0].detail


# --- reporting -----------------------------------------------------------


def test_report_summarises_a_clean_run():
    out = render(run(context()), notes=["a measurement"])
    assert "nothing failed" in out
    assert "a measurement" in out
    assert "[PASS]" in out


def test_report_names_the_first_problem():
    out = render(run(context(attached=())))
    assert "First problem: adb sees a device" in out
    assert "[FAIL]" in out


def test_result_renders_without_detail():
    assert CheckResult("a stage", OK).render().strip() == "[PASS] a stage"


def test_main_exits_nonzero_when_something_failed(monkeypatch, capsys):
    import android_mcp.selfcheck as sc

    monkeypatch.setattr(sc, "connect_bluestacks", lambda count=10: [])
    assert main([]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_accepts_a_serial_and_a_tap(monkeypatch):
    import android_mcp.selfcheck as sc

    seen = {}
    monkeypatch.setattr(sc, "run", lambda ctx, stages=None: seen.update(
        serial=ctx.serial, tap=ctx.tap_text
    ) or [])
    assert main(["--serial", "S1", "--tap", "Settings"]) == 0
    assert seen == {"serial": "S1", "tap": "Settings"}
