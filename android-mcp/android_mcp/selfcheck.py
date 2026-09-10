"""A staged check of this stack against a real device.

Everything here is tested against fakes, which proves the logic and proves
nothing about an actual phone. This module closes that gap: run it with a device
attached and it reports, stage by stage, which assumptions hold.

    python -m android_mcp.selfcheck
    python -m android_mcp.selfcheck --serial 127.0.0.1:5565 --tap Settings

Read-only by default: it reads the screen and lists profiles, and touches
nothing. ``--tap`` opts into a single tap so the acting path is covered too.

The hierarchy stage is the interesting one. It reports the real compression
ratio on whatever is on screen, which is the design's central claim measured
against your apps rather than a fixture.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import actions, users
from .device import (
    AndroidDevice,
    DeviceError,
    DeviceRegistry,
    connect_bluestacks,
    shell_runner,
)
from .ui import app_elements, parse_hierarchy

OK = "ok"
FAIL = "fail"
SKIP = "skip"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""

    def render(self) -> str:
        mark = {OK: "PASS", FAIL: "FAIL", SKIP: "SKIP"}[self.status]
        line = f"  [{mark}] {self.name}"
        if self.detail:
            line += f"\n         {self.detail}"
        return line


@dataclass
class Context:
    """What the stages share, and what a test substitutes.

    The three callables use ``default_factory`` rather than a plain default, so
    each lands as an instance attribute. A bare function left as a dataclass
    class attribute is a descriptor, and would arrive bound with the context as
    its first argument.
    """

    serial: str | None = None
    tap_text: str | None = None
    discover: Callable[[], list] = field(default_factory=lambda: connect_bluestacks)
    connect: Callable[[str], AndroidDevice] = field(
        default_factory=lambda: AndroidDevice
    )
    shell: Callable[[str], Callable] = field(default_factory=lambda: shell_runner)
    settle_timeout: float = 6.0
    settle_interval: float = 0.2
    device: AndroidDevice | None = None
    resolved: str = ""
    notes: list[str] = field(default_factory=list)

    def settling(self) -> actions.Settle:
        return actions.Settle(
            interval=self.settle_interval, timeout=self.settle_timeout
        )


def _stage(name):
    def wrap(fn):
        fn.stage_name = name
        return fn

    return wrap


@_stage("adb sees a device")
def check_devices(ctx: Context) -> CheckResult:
    found = ctx.discover()
    if not found:
        return CheckResult(
            check_devices.stage_name,
            FAIL,
            "nothing attached. Start a BlueStacks instance, or connect a phone "
            "with USB debugging on.",
        )
    usable = [d for d in found if d.usable]
    listed = ", ".join(d.render() for d in found)
    if not usable:
        return CheckResult(
            check_devices.stage_name, FAIL, f"attached but not usable: {listed}"
        )
    registry = DeviceRegistry()
    ctx.resolved = registry.resolve(ctx.serial, found)
    return CheckResult(
        check_devices.stage_name, OK, f"using {ctx.resolved} (attached: {listed})"
    )


@_stage("uiautomator2 connects")
def check_connect(ctx: Context) -> CheckResult:
    started = time.monotonic()
    ctx.device = ctx.connect(ctx.resolved)
    size = ctx.device.window_size()
    took = time.monotonic() - started
    return CheckResult(
        check_connect.stage_name,
        OK,
        f"{size[0]}x{size[1]}, ready in {took:.1f}s "
        "(the first connect pushes the agent, so this one is the slow one)",
    )


@_stage("the hierarchy is readable and worth filtering")
def check_hierarchy(ctx: Context) -> CheckResult:
    raw = ctx.device.dump()
    if not raw.strip():
        return CheckResult(
            check_hierarchy.stage_name,
            FAIL,
            "dump_hierarchy returned nothing. A secure screen, or the agent is "
            "not running.",
        )
    elements = parse_hierarchy(raw)
    shown = app_elements(elements)
    if not shown:
        return CheckResult(
            check_hierarchy.stage_name,
            FAIL,
            f"parsed {len(raw)} characters into no app elements. If the screen "
            "is a game or WebView this is expected; otherwise the filter is "
            "wrong for this app.",
        )
    rendered = "\n".join(e.render() for e in shown)
    ratio = len(rendered) / len(raw)
    addressable = sum(1 for e in shown if e.resource_id)
    ctx.notes.append(
        f"measured on your screen: {ratio:.1%} of the raw dump "
        f"({len(raw)} chars -> {len(rendered)}), "
        f"{addressable}/{len(shown)} elements carry a resource-id"
    )
    return CheckResult(
        check_hierarchy.stage_name,
        OK,
        f"{raw.count('<node')} nodes -> {len(shown)} elements, "
        f"{ratio:.1%} of the dump",
    )


@_stage("the screen settles")
def check_settle(ctx: Context) -> CheckResult:
    started = time.monotonic()
    snap = actions.settle(ctx.device, **ctx.settling().kwargs())
    took = time.monotonic() - started
    if not snap.settled:
        return CheckResult(
            check_settle.stage_name,
            FAIL,
            f"still changing after {took:.1f}s"
            + (" with a spinner up" if snap.busy else "")
            + ". An animation or a live feed on screen; try again on a static "
            "screen before concluding anything.",
        )
    return CheckResult(
        check_settle.stage_name,
        OK,
        f"settled in {took:.1f}s over {snap.reads} reads, in {snap.package}",
    )


@_stage("profile tools work")
def check_profiles(ctx: Context) -> CheckResult:
    try:
        found = users.list_users(ctx.shell(ctx.resolved))
    except DeviceError as exc:
        return CheckResult(check_profiles.stage_name, FAIL, str(exc))
    if not found:
        return CheckResult(
            check_profiles.stage_name,
            FAIL,
            "pm listed no profiles. Some BlueStacks images do not support "
            "multiple users; the screen tools are unaffected.",
        )
    return CheckResult(
        check_profiles.stage_name,
        OK,
        f"{len(found)} profile(s): " + "; ".join(u.render() for u in found),
    )


@_stage("a tap lands")
def check_tap(ctx: Context) -> CheckResult:
    if not ctx.tap_text:
        return CheckResult(
            check_tap.stage_name,
            SKIP,
            "read-only by default. Pass --tap TEXT to tap something once.",
        )
    settling = ctx.settling()
    before = actions.settle(ctx.device, **settling.kwargs())
    after = actions.tap(
        ctx.device, actions.Selector(text=ctx.tap_text), settling=settling
    )
    if after.state_id == before.state_id:
        return CheckResult(
            check_tap.stage_name,
            FAIL,
            f"tapped {ctx.tap_text!r} but the screen did not change. It may be "
            "inert, or the tap missed.",
        )
    return CheckResult(
        check_tap.stage_name,
        OK,
        f"tapped {ctx.tap_text!r}; screen moved to {after.package}",
    )


# Each stage needs the one before it, so a failure stops the run rather than
# producing a page of errors that all say the same thing.
STAGES = [
    check_devices,
    check_connect,
    check_hierarchy,
    check_settle,
    check_profiles,
    check_tap,
]

# Stages that are informative on their own and should not halt the run.
NON_BLOCKING = {check_profiles, check_tap}


def run(ctx: Context, stages=None) -> list[CheckResult]:
    """Run the stages in order, stopping at the first blocking failure."""
    results: list[CheckResult] = []
    stopped = False
    for stage in stages or STAGES:
        if stopped:
            results.append(CheckResult(stage.stage_name, SKIP, "earlier stage failed"))
            continue
        try:
            result = stage(ctx)
        except Exception as exc:  # noqa: BLE001 - any failure is a finding here
            result = CheckResult(stage.stage_name, FAIL, f"{type(exc).__name__}: {exc}")
        results.append(result)
        if result.status == FAIL and stage not in NON_BLOCKING:
            stopped = True
    return results


def render(results: list[CheckResult], notes: list[str] | None = None) -> str:
    passed = sum(1 for r in results if r.status == OK)
    failed = [r for r in results if r.status == FAIL]
    lines = ["android-mcp self-check", ""]
    lines.extend(r.render() for r in results)
    lines.append("")
    if failed:
        lines.append(f"{passed} passed, {len(failed)} failed.")
        lines.append(f"First problem: {failed[0].name}.")
    else:
        lines.append(f"{passed} passed, nothing failed.")
    for note in notes or []:
        lines.append(f"\n{note}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m android_mcp.selfcheck",
        description="Check android-mcp against a real device.",
    )
    parser.add_argument("--serial", help="which device, if several are attached")
    parser.add_argument(
        "--tap",
        metavar="TEXT",
        help="also tap an element with this text, to cover the acting path",
    )
    parser.add_argument(
        "--settle-timeout",
        type=float,
        default=6.0,
        metavar="SECONDS",
        help="how long to allow a screen to stop changing (default 6)",
    )
    args = parser.parse_args(argv)

    ctx = Context(
        serial=args.serial, tap_text=args.tap, settle_timeout=args.settle_timeout
    )
    results = run(ctx)
    print(render(results, ctx.notes))
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
