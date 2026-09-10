"""Capturing a flow by watching it happen once.

The model drives the app through the normal tools; every action that succeeds is
appended here. ``start`` then ``save`` turns that into a replayable flow, which
is a far better authoring story than writing steps by hand.

A recording belongs to one server instance, like the device registry: two
sessions recording into shared state would interleave into nonsense.
"""

from __future__ import annotations

from .actions import Snapshot
from .flows import (
    Flow,
    FlowError,
    Step,
    check_name,
    parameterise,
    selector_candidates,
    stable_anchors,
)
from .ui import Element


class Recorder:
    def __init__(self):
        self.name: str | None = None
        self.steps: list[Step] = []
        self.package: str = ""

    @property
    def recording(self) -> bool:
        return self.name is not None

    def start(self, name: str) -> None:
        check_name(name)
        self.name = name
        self.steps = []
        self.package = ""

    def cancel(self) -> str | None:
        name, self.name, self.steps = self.name, None, []
        return name

    def _require(self) -> None:
        if not self.recording:
            raise FlowError(
                "not recording. Call start_recording first, then carry out the "
                "flow once, then save_flow."
            )

    def note_element(
        self,
        action: str,
        element: Element,
        before: Snapshot,
        options: dict | None = None,
    ) -> None:
        """Record a step that acted on a specific element."""
        if not self.recording:
            return
        self.steps.append(
            Step(
                action=action,
                selectors=selector_candidates(element),
                options=dict(options or {}),
                expect_package=before.package,
                anchors=stable_anchors(before.elements),
                index=element.index,
                label=f"{action} {element.label or element.resource_id or element.cls}",
            )
        )

    def note_action(
        self, action: str, options: dict, before: Snapshot | None = None
    ) -> None:
        """Record a step that takes no element: a launch, a swipe, a key."""
        if not self.recording:
            return
        if action == "open_app" and not self.package:
            self.package = options.get("package", "")
        self.steps.append(
            Step(
                action=action,
                options=dict(options),
                expect_package=before.package if before else "",
                anchors=stable_anchors(before.elements) if before else [],
            )
        )

    def save(
        self,
        description: str = "",
        parameters: dict[str, str] | None = None,
    ) -> Flow:
        """Finish the recording and return the flow, ready to store."""
        self._require()
        if not self.steps:
            raise FlowError(
                f"nothing was recorded for {self.name!r}. Carry out the flow "
                "with the other tools while recording, then save."
            )
        flow = Flow(
            name=self.name,
            steps=self.steps,
            description=description,
            package=self.package,
        )
        parameterise(flow, parameters or {})
        self.name, self.steps = None, []
        return flow
