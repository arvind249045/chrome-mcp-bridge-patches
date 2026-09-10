"""Composite actions: the level a model should actually work at.

Two ideas carry most of the value here.

**Settling beats sleeping.** Every action returns the screen as it is once it
has stopped changing, found by polling the hierarchy until consecutive reads
match and no spinner is up. A screen that settles in 300ms costs 300ms, and
nobody has to guess a sleep. When settling times out we say so rather than
pretending the screen is ready.

**One call, one round trip.** Each action returns the resulting screen, and a
failed lookup returns the screen too, so a model that guessed wrong can correct
itself without first asking what happened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .device import AndroidDevice
from .ui import Element, fingerprint, is_busy, parse_hierarchy, render

DEFAULT_POLL_INTERVAL = 0.2
DEFAULT_SETTLE_TIMEOUT = 5.0
DEFAULT_STABLE_READS = 2


class ActionError(RuntimeError):
    """A request that could not be carried out as asked."""


class ElementNotFound(ActionError):
    pass


class AmbiguousSelector(ActionError):
    pass


class StaleState(ActionError):
    pass


# --------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------


@dataclass
class Snapshot:
    serial: str
    elements: list[Element]
    state_id: str
    package: str = ""
    activity: str = ""
    settled: bool = True
    busy: bool = False
    reads: int = 1

    def header(self) -> str:
        where = self.package or "unknown"
        if self.activity:
            where = f"{where}/{self.activity}"
        bits = [where, f"state={self.state_id}"]
        if self.busy:
            bits.append("BUSY (spinner on screen)")
        if not self.settled:
            bits.append("NOT SETTLED (screen still changing)")
        return "  ".join(bits)

    def render(self, include_system: bool = False) -> str:
        return f"{self.header()}\n{render(self.elements, include_system)}"


def _read(device: AndroidDevice) -> tuple[list[Element], str]:
    elements = parse_hierarchy(device.dump())
    return elements, fingerprint(elements)


def _snapshot(
    device: AndroidDevice,
    elements: list[Element],
    state_id: str,
    *,
    settled: bool,
    reads: int,
) -> Snapshot:
    current = device.current_app()
    return Snapshot(
        serial=device.serial,
        elements=elements,
        state_id=state_id,
        package=current.get("package", ""),
        activity=current.get("activity", ""),
        settled=settled,
        busy=is_busy(elements),
        reads=reads,
    )


def settle(
    device: AndroidDevice,
    *,
    interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_SETTLE_TIMEOUT,
    stable_reads: int = DEFAULT_STABLE_READS,
    wait_for_idle: bool = True,
    clock=time.monotonic,
    sleep=time.sleep,
) -> Snapshot:
    """Read the screen until it stops changing.

    Returns as soon as ``stable_reads`` consecutive reads agree and nothing is
    spinning. On timeout it returns the last read with ``settled=False``: the
    caller gets real state plus the fact that it may still be moving.
    """
    if stable_reads < 1:
        raise ValueError("stable_reads must be at least 1")

    elements, current = _read(device)
    reads = 1
    streak = 1
    deadline = clock() + timeout

    while True:
        busy = wait_for_idle and is_busy(elements)
        if streak >= stable_reads and not busy:
            return _snapshot(device, elements, current, settled=True, reads=reads)
        if clock() >= deadline:
            return _snapshot(device, elements, current, settled=False, reads=reads)

        sleep(interval)
        elements, state_id = _read(device)
        reads += 1
        if state_id == current:
            streak += 1
        else:
            streak = 1
            current = state_id


def snapshot(device: AndroidDevice, **kwargs) -> Snapshot:
    """The current screen, settled."""
    return settle(device, **kwargs)


# --------------------------------------------------------------------------
# selectors
# --------------------------------------------------------------------------


@dataclass
class Selector:
    """How to name an element. Fields combine with AND."""

    index: int | None = None
    text: str | None = None
    resource_id: str | None = None
    desc: str | None = None
    cls: str | None = None
    exact: bool = False

    def __post_init__(self):
        if not self.described():
            raise ValueError(
                "give at least one of index, text, resource_id, desc or cls"
            )

    def described(self) -> bool:
        return any(
            v is not None
            for v in (self.index, self.text, self.resource_id, self.desc, self.cls)
        )

    def describe(self) -> str:
        bits = []
        for name in ("index", "text", "resource_id", "desc", "cls"):
            value = getattr(self, name)
            if value is not None:
                bits.append(f"{name}={value!r}")
        return ", ".join(bits)


def _matches_string(haystack: str, needle: str, exact: bool) -> bool:
    if exact:
        return haystack == needle
    return needle.casefold() in haystack.casefold()


def _candidates(elements: list[Element], selector: Selector) -> list[Element]:
    out = []
    for el in elements:
        if selector.text is not None and not _matches_string(
            el.label, selector.text, selector.exact
        ):
            continue
        if selector.desc is not None and not _matches_string(
            el.desc, selector.desc, selector.exact
        ):
            continue
        if selector.resource_id is not None and not _matches_string(
            el.resource_id, selector.resource_id, selector.exact
        ):
            continue
        if selector.cls is not None and el.cls != selector.cls:
            continue
        out.append(el)
    return out


def _narrow(candidates: list[Element], selector: Selector) -> list[Element]:
    """Resolve the common kinds of ambiguity without guessing wildly."""
    if len(candidates) <= 1:
        return candidates

    # An exact label match beats a substring one: searching "Pay" should not be
    # ambiguous just because "Payment methods" is also on screen.
    if selector.text is not None and not selector.exact:
        exact = [e for e in candidates if e.label == selector.text]
        if exact:
            candidates = exact
    if len(candidates) <= 1:
        return candidates

    # Prefer something actually tappable.
    tappable = [e for e in candidates if e.clickable or e.long_clickable]
    if tappable:
        candidates = tappable
    return candidates


def find(elements: list[Element], selector: Selector) -> Element:
    """Resolve a selector to exactly one element, or explain why not."""
    if selector.index is not None:
        for el in elements:
            if el.index == selector.index:
                return el
        raise ElementNotFound(
            f"no element with index {selector.index}; "
            f"the screen has {len(elements)} elements.\n{render(elements)}"
        )

    candidates = _narrow(_candidates(elements, selector), selector)
    if not candidates:
        raise ElementNotFound(
            f"nothing matches {selector.describe()} on this screen.\n"
            f"{render(elements)}"
        )
    if len(candidates) > 1:
        listed = "\n".join(e.render() for e in candidates)
        raise AmbiguousSelector(
            f"{len(candidates)} elements match {selector.describe()}. "
            f"Retry with index=, or a resource_id.\n{listed}"
        )
    return candidates[0]


def locate(elements: list[Element], selector: Selector) -> Element | None:
    """Like ``find``, but absence is an answer rather than an error."""
    try:
        return find(elements, selector)
    except ActionError:
        return None


# --------------------------------------------------------------------------
# acting
# --------------------------------------------------------------------------


@dataclass
class Settle:
    """Settling options, passed through the action functions."""

    interval: float = DEFAULT_POLL_INTERVAL
    timeout: float = DEFAULT_SETTLE_TIMEOUT
    stable_reads: int = DEFAULT_STABLE_READS
    wait_for_idle: bool = True
    clock: object = field(default=time.monotonic)
    sleep: object = field(default=time.sleep)

    def kwargs(self) -> dict:
        return {
            "interval": self.interval,
            "timeout": self.timeout,
            "stable_reads": self.stable_reads,
            "wait_for_idle": self.wait_for_idle,
            "clock": self.clock,
            "sleep": self.sleep,
        }


def _resolve_for_action(
    device: AndroidDevice,
    selector: Selector,
    expect_state: str | None,
    settling: Settle,
) -> tuple[Element, Snapshot]:
    """Find the target on a freshly read screen.

    Indices only mean anything relative to the dump they came from, so before
    acting on one we re-read and check the screen is still that screen. Tapping
    index 7 on a screen that moved underneath is how automation buys something
    nobody ordered.
    """
    current = settle(device, **settling.kwargs())
    if expect_state is not None and expect_state != current.state_id:
        raise StaleState(
            f"the screen changed (expected state={expect_state}, "
            f"now state={current.state_id}). Re-read it and retry.\n"
            f"{current.render()}"
        )
    return find(current.elements, selector), current


def tap(
    device: AndroidDevice,
    selector: Selector,
    *,
    long: bool = False,
    expect_state: str | None = None,
    settling: Settle | None = None,
) -> Snapshot:
    settling = settling or Settle()
    element, _ = _resolve_for_action(device, selector, expect_state, settling)
    if not element.enabled:
        raise ActionError(
            f"{element.render()} is disabled, so tapping it would do nothing."
        )
    x, y = element.bounds.center
    if long:
        device.long_click(x, y)
    else:
        device.click(x, y)
    return settle(device, **settling.kwargs())


def type_text(
    device: AndroidDevice,
    selector: Selector,
    text: str,
    *,
    clear: bool = True,
    submit: bool = False,
    expect_state: str | None = None,
    settling: Settle | None = None,
) -> Snapshot:
    """Focus a field and type into it.

    Uses uiautomator2's IME rather than ``input text``, which mangles spaces
    and anything non-ASCII.
    """
    settling = settling or Settle()
    element, _ = _resolve_for_action(device, selector, expect_state, settling)
    x, y = element.bounds.center
    device.click(x, y)
    settle(device, **settling.kwargs())
    device.send_keys(text, clear=clear)
    if submit:
        device.press("enter")
    return settle(device, **settling.kwargs())


_DIRECTIONS = ("up", "down", "left", "right")


def swipe(
    device: AndroidDevice,
    direction: str,
    *,
    fraction: float = 0.5,
    settling: Settle | None = None,
) -> Snapshot:
    """Scroll the content.

    ``direction`` is where the content goes, the way a person would say it:
    ``down`` reveals what is further down the page.
    """
    settling = settling or Settle()
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(_DIRECTIONS)}")
    if not 0 < fraction <= 0.9:
        raise ValueError("fraction must be between 0 and 0.9")

    width, height = device.window_size()
    cx, cy = width // 2, height // 2
    dx = int(width * fraction / 2)
    dy = int(height * fraction / 2)
    moves = {
        "down": (cx, cy + dy, cx, cy - dy),
        "up": (cx, cy - dy, cx, cy + dy),
        "right": (cx + dx, cy, cx - dx, cy),
        "left": (cx - dx, cy, cx + dx, cy),
    }
    device.swipe(*moves[direction])
    return settle(device, **settling.kwargs())


def scroll_until(
    device: AndroidDevice,
    selector: Selector,
    *,
    direction: str = "down",
    max_swipes: int = 8,
    settling: Settle | None = None,
) -> Snapshot:
    """Swipe until the element appears, or until the list stops moving.

    The end of a list is detected by the screen fingerprint not changing across
    a swipe, which is cheaper and more honest than swiping ``max_swipes`` times
    and hoping.
    """
    settling = settling or Settle()
    current = settle(device, **settling.kwargs())
    if locate(current.elements, selector):
        return current

    for _ in range(max_swipes):
        before = current.state_id
        current = swipe(device, direction, settling=settling)
        if locate(current.elements, selector):
            return current
        if current.state_id == before:
            raise ElementNotFound(
                f"reached the end of the list without finding "
                f"{selector.describe()}.\n{current.render()}"
            )
    raise ElementNotFound(
        f"{selector.describe()} not found after {max_swipes} swipes "
        f"{direction}.\n{current.render()}"
    )


def wait_for(
    device: AndroidDevice,
    selector: Selector,
    *,
    timeout: float = 10.0,
    interval: float = DEFAULT_POLL_INTERVAL,
    settling: Settle | None = None,
    clock=time.monotonic,
    sleep=time.sleep,
) -> Snapshot:
    """Poll until the element is on screen."""
    settling = settling or Settle()
    deadline = clock() + timeout
    while True:
        current = settle(device, **settling.kwargs())
        if locate(current.elements, selector):
            return current
        if clock() >= deadline:
            raise ElementNotFound(
                f"{selector.describe()} did not appear within {timeout}s.\n"
                f"{current.render()}"
            )
        sleep(interval)


def press(
    device: AndroidDevice, key: str, *, settling: Settle | None = None
) -> Snapshot:
    settling = settling or Settle()
    device.press(key)
    return settle(device, **settling.kwargs())


def open_app(
    device: AndroidDevice,
    package: str,
    *,
    activity: str | None = None,
    timeout: float = 15.0,
    interval: float = 0.3,
    settling: Settle | None = None,
    clock=time.monotonic,
    sleep=time.sleep,
) -> Snapshot:
    """Launch an app, wait for it to be in front, and return its first screen.

    This is the tool that replaces six round trips with one.
    """
    settling = settling or Settle()
    device.app_start(package, activity)
    deadline = clock() + timeout
    while True:
        current = settle(device, **settling.kwargs())
        if current.package == package:
            return current
        if clock() >= deadline:
            raise ActionError(
                f"{package} did not come to the foreground within {timeout}s; "
                f"{current.package or 'nothing'} is in front.\n{current.render()}"
            )
        sleep(interval)
