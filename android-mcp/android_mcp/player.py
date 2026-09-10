"""Replaying a saved flow.

Replay is deterministic: it resolves each step's selectors against the screen in
front of it and acts, with no model in the loop. Two decisions shape it.

**It never falls back to a recorded index.** A position is meaningless on a
screen that gained a promo banner, and tapping the wrong row is worse than
stopping. When every selector for a step misses, the step fails and says what is
on screen instead, so a model can repair the flow.

**Healing is opt-in.** When a later, less durable selector is what actually
matched, the step is reported as healed and the flow is left alone. Pass
``heal=True`` to promote the selector that worked, so the drift is recorded
rather than rediscovered every morning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import actions
from .actions import ActionError, Selector, Settle, Snapshot
from .device import AndroidDevice, DeviceError
from .flows import Flow, FlowStore, Step, substitute

OK = "ok"
HEALED = "healed"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class StepResult:
    number: int
    description: str
    status: str
    detail: str = ""

    def render(self) -> str:
        line = f"  {self.number:>2}. {self.status:<7} {self.description}"
        if self.detail:
            line += f"\n      {self.detail}"
        return line


@dataclass
class RunResult:
    flow: str
    steps: list[StepResult] = field(default_factory=list)
    screen: str = ""
    healed: bool = False
    saved: bool = False

    @property
    def ok(self) -> bool:
        return all(step.status in (OK, HEALED) for step in self.steps)

    def render(self) -> str:
        done = sum(1 for s in self.steps if s.status in (OK, HEALED))
        total = len(self.steps)
        if self.ok:
            headline = f"{self.flow}: all {total} steps ok"
        else:
            failed = next(s for s in self.steps if s.status == FAILED)
            headline = (
                f"{self.flow}: {done} of {total} steps ok, "
                f"stopped at step {failed.number}"
            )
        lines = [headline, ""]
        lines.extend(step.render() for step in self.steps)
        if self.healed:
            note = "selectors drifted; "
            note += (
                "the flow has been updated."
                if self.saved
                else "re-run with heal=true to record the ones that worked."
            )
            lines.append(f"\n{note}")
        if self.screen:
            lines.append(f"\nscreen now:\n{self.screen}")
        return "\n".join(lines)


def _to_selectors(step: Step) -> list[Selector]:
    out = []
    for raw in step.selectors:
        try:
            out.append(Selector(**raw))
        except (TypeError, ValueError) as exc:
            raise ActionError(f"step has an unusable selector {raw!r}: {exc}") from exc
    return out


def _precondition(step: Step, screen: Snapshot) -> str | None:
    """Why this screen is not the one the step was recorded on, if it is not.

    open_app is exempt: its package is where the step is going, not where it
    must already be.
    """
    if step.action == "open_app":
        return None
    if step.expect_package and screen.package != step.expect_package:
        return (
            f"expected to be in {step.expect_package} but "
            f"{screen.package or 'nothing'} is in front"
        )
    if step.anchors:
        present = [a for a in step.anchors if a in screen.render()]
        if not present:
            return (
                "this does not look like the recorded screen; expected to see "
                + " or ".join(repr(a) for a in step.anchors)
            )
    return None


def _run_step(
    device: AndroidDevice, step: Step, settling: Settle
) -> tuple[Snapshot, int]:
    """Carry out one step, returning the resulting screen and which selector won.

    The winning index is -1 when the step takes no selector.
    """
    options = step.options
    if step.action == "open_app":
        return (
            actions.open_app(
                device,
                options["package"],
                activity=options.get("activity") or None,
                settling=settling,
            ),
            -1,
        )
    if step.action == "swipe":
        return (
            actions.swipe(
                device,
                options.get("direction", "down"),
                fraction=float(options.get("fraction", 0.5)),
                settling=settling,
            ),
            -1,
        )
    if step.action == "press_key":
        return actions.press(device, options["key"], settling=settling), -1

    selectors = _to_selectors(step)

    # These two are inherently "not there yet", so they poll every candidate
    # rather than resolving against the current screen.
    if step.action == "wait_for":
        screen = actions.wait_for(
            device,
            selectors,
            timeout=float(options.get("timeout", 10.0)),
            settling=settling,
        )
        hit = actions.locate_any(screen.elements, selectors)
        return screen, selectors.index(hit[0]) if hit else 0
    if step.action == "scroll_until":
        screen = actions.scroll_until(
            device,
            selectors,
            direction=options.get("direction", "down"),
            max_swipes=int(options.get("max_swipes", 8)),
            settling=settling,
        )
        hit = actions.locate_any(screen.elements, selectors)
        return screen, selectors.index(hit[0]) if hit else 0

    # tap and type_text resolve against the screen we already read, then hand
    # the winning selector to the action with the state id, so the action's own
    # staleness guard still applies.
    current = actions.settle(device, **settling.kwargs())
    hit = actions.locate_any(current.elements, selectors)
    if hit is None:
        raise ActionError(
            f"none of this step's selectors match: "
            f"{actions.describe_selectors(selectors)}.\n{current.render()}"
        )
    winner, _ = hit
    which = selectors.index(winner)

    if step.action == "tap":
        return (
            actions.tap(
                device,
                winner,
                long=bool(options.get("long", False)),
                expect_state=current.state_id,
                settling=settling,
            ),
            which,
        )
    if step.action == "type_text":
        return (
            actions.type_text(
                device,
                winner,
                options.get("value", ""),
                clear=bool(options.get("clear", True)),
                submit=bool(options.get("submit", False)),
                expect_state=current.state_id,
                settling=settling,
            ),
            which,
        )
    raise ActionError(f"{step.action} cannot be replayed")


def run(
    device: AndroidDevice,
    flow: Flow,
    *,
    params: dict[str, str] | None = None,
    heal: bool = False,
    store: FlowStore | None = None,
    settling: Settle | None = None,
) -> RunResult:
    """Replay a flow, stopping at the first step that fails.

    Stopping is the point: carrying on past a failed tap is how an automated run
    does something nobody asked for.
    """
    settling = settling or Settle()
    filled = substitute(flow, params or {})
    result = RunResult(flow=flow.name)
    screen: Snapshot | None = None
    winners: dict[int, int] = {}

    for number, step in enumerate(filled.steps, 1):
        if screen is not None:
            problem = _precondition(step, screen)
            if problem:
                result.steps.append(
                    StepResult(number, step.describe(), FAILED, problem)
                )
                break
        try:
            screen, which = _run_step(device, step, settling)
        except (ActionError, DeviceError, KeyError) as exc:
            detail = str(exc) if not isinstance(exc, KeyError) else f"missing {exc}"
            result.steps.append(StepResult(number, step.describe(), FAILED, detail))
            break

        if which > 0:
            used = step.selectors[which]
            first = step.selectors[0]
            result.healed = True
            winners[number - 1] = which
            result.steps.append(
                StepResult(
                    number,
                    step.describe(),
                    HEALED,
                    f"matched {used} after {first} missed",
                )
            )
        else:
            result.steps.append(StepResult(number, step.describe(), OK))

    for number in range(len(result.steps) + 1, len(filled.steps) + 1):
        step = filled.steps[number - 1]
        result.steps.append(StepResult(number, step.describe(), SKIPPED))

    if screen is not None:
        result.screen = screen.render()

    if heal and winners and store is not None:
        for position, which in winners.items():
            selectors = flow.steps[position].selectors
            selectors.insert(0, selectors.pop(which))
        store.save(flow)
        result.saved = True

    return result
