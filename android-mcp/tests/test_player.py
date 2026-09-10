"""Tests for replaying a flow against a scripted device."""

import pytest
from conftest import FakeClock, FakeDevice, screen

from android_mcp import player
from android_mcp.actions import Settle
from android_mcp.flows import Flow, FlowStore, Step

MENU = screen(
    "Your order",
    {"text": "Paneer Roll · ₹99", "rid": "row_0", "clickable": True},
    {"text": "Place order", "rid": "place", "clickable": True},
)
CONFIRM = screen("Order placed", {"text": "Track order", "rid": "track", "clickable": True})


def settling(clock):
    return Settle(
        interval=0.2, timeout=1.0, clock=clock.monotonic, sleep=clock.sleep
    )


def flow(*steps, name="order"):
    return Flow(name=name, steps=list(steps))


def run(device, the_flow, **kw):
    clock = FakeClock()
    return player.run(device, the_flow, settling=settling(clock), **kw)


# --- the happy path ------------------------------------------------------


def test_a_flow_runs_every_step():
    device = FakeDevice([MENU], sequence=[MENU, CONFIRM])
    result = run(
        device,
        flow(
            Step(action="tap", selectors=[{"resource_id": "row_0"}]),
            Step(action="tap", selectors=[{"resource_id": "place"}]),
        ),
    )
    assert result.ok, result.render()
    assert [s.status for s in result.steps] == ["ok", "ok"]
    assert len(device.clicks) == 2
    assert "Order placed" in result.screen


def test_the_report_names_the_flow_and_counts_the_steps():
    device = FakeDevice([MENU], sequence=[MENU])
    result = run(device, flow(Step(action="tap", selectors=[{"resource_id": "place"}])))
    assert "order: all 1 steps ok" in result.render()


def test_an_open_app_step_launches():
    device = FakeDevice([MENU])
    result = run(
        device,
        flow(Step(action="open_app", options={"package": "com.bistro.app"})),
    )
    assert result.ok
    assert device.started == [("com.bistro.app", None)]


def test_a_press_step_replays():
    device = FakeDevice([MENU], sequence=[CONFIRM])
    result = run(device, flow(Step(action="press_key", options={"key": "back"})))
    assert result.ok and device.presses == ["back"]


def test_a_swipe_step_replays():
    device = FakeDevice([MENU], sequence=[MENU])
    result = run(
        device, flow(Step(action="swipe", options={"direction": "down"}))
    )
    assert result.ok and len(device.swipes) == 1


# --- stopping at the first failure --------------------------------------


def test_a_failed_step_stops_the_run_and_later_steps_are_skipped():
    """Carrying on past a failed tap is how a run does something unasked."""
    device = FakeDevice([MENU], sequence=[MENU, MENU])
    result = run(
        device,
        flow(
            Step(action="tap", selectors=[{"resource_id": "nothing_like_this"}]),
            Step(action="tap", selectors=[{"resource_id": "place"}]),
        ),
    )
    assert not result.ok
    assert [s.status for s in result.steps] == ["failed", "skipped"]
    assert device.clicks == []


def test_the_failure_report_points_at_the_step_and_shows_the_screen():
    device = FakeDevice([MENU])
    result = run(
        device, flow(Step(action="tap", selectors=[{"resource_id": "gone"}]))
    )
    rendered = result.render()
    assert "stopped at step 1" in rendered
    assert "none of this step's selectors match" in rendered
    assert "Place order" in rendered, "the current screen comes back with the failure"


def test_a_flow_never_falls_back_to_a_recorded_index():
    """Index 2 still exists, but tapping it blind could hit the wrong row."""
    device = FakeDevice([MENU])
    result = run(
        device,
        flow(Step(action="tap", selectors=[{"resource_id": "vanished"}], index=2)),
    )
    assert not result.ok
    assert device.clicks == []


# --- preconditions -------------------------------------------------------


def test_a_step_recorded_in_another_app_fails_before_acting():
    device = FakeDevice([MENU], sequence=[MENU, MENU])
    result = run(
        device,
        flow(
            Step(action="tap", selectors=[{"resource_id": "row_0"}]),
            Step(
                action="tap",
                selectors=[{"resource_id": "place"}],
                expect_package="com.fampay.app",
            ),
        ),
    )
    assert [s.status for s in result.steps] == ["ok", "failed"]
    assert "com.fampay.app" in result.steps[1].detail
    assert len(device.clicks) == 1, "the second tap never happened"


def test_a_step_whose_anchors_are_absent_fails_before_acting():
    device = FakeDevice([MENU], sequence=[CONFIRM, CONFIRM])
    result = run(
        device,
        flow(
            Step(action="tap", selectors=[{"resource_id": "row_0"}]),
            Step(
                action="tap",
                selectors=[{"resource_id": "track"}],
                anchors=["Your order"],
            ),
        ),
    )
    assert [s.status for s in result.steps] == ["ok", "failed"]
    assert "does not look like the recorded screen" in result.steps[1].detail


def test_an_anchor_that_is_present_lets_the_step_through():
    device = FakeDevice([MENU], sequence=[MENU, MENU])
    result = run(
        device,
        flow(
            Step(action="tap", selectors=[{"resource_id": "row_0"}]),
            Step(
                action="tap",
                selectors=[{"resource_id": "place"}],
                anchors=["Your order"],
            ),
        ),
    )
    assert result.ok, result.render()


def test_the_first_step_is_not_held_to_a_precondition():
    """Nothing has been read yet, so there is nothing to check against."""
    device = FakeDevice([MENU], sequence=[MENU])
    result = run(
        device,
        flow(
            Step(
                action="tap",
                selectors=[{"resource_id": "place"}],
                expect_package="com.somewhere.else",
            )
        ),
    )
    assert result.ok


def test_an_open_app_step_is_exempt_from_its_own_package_check():
    device = FakeDevice([MENU], sequence=[MENU, MENU])
    result = run(
        device,
        flow(
            Step(action="tap", selectors=[{"resource_id": "row_0"}]),
            Step(
                action="open_app",
                options={"package": "com.bistro.app"},
                expect_package="com.bistro.app",
            ),
        ),
    )
    assert result.ok, result.render()


# --- healing -------------------------------------------------------------


DRIFTED = flow(
    Step(
        action="tap",
        selectors=[{"resource_id": "renamed_in_the_update"}, {"text": "Place order"}],
    )
)


def test_a_later_selector_matching_is_reported_as_healed():
    device = FakeDevice([MENU], sequence=[CONFIRM])
    result = run(device, DRIFTED)
    assert result.ok
    assert result.steps[0].status == "healed"
    assert result.healed and not result.saved
    assert "re-run with heal=true" in result.render()


def test_healing_is_not_written_back_unless_asked(tmp_path):
    store = FlowStore(tmp_path)
    store.save(DRIFTED)
    device = FakeDevice([MENU], sequence=[CONFIRM])
    run(device, store.load("order"), store=store)
    assert store.load("order").steps[0].selectors[0] == {
        "resource_id": "renamed_in_the_update"
    }


def test_heal_promotes_the_selector_that_worked(tmp_path):
    store = FlowStore(tmp_path)
    store.save(DRIFTED)
    device = FakeDevice([MENU], sequence=[CONFIRM])
    result = run(device, store.load("order"), heal=True, store=store)
    assert result.saved
    assert store.load("order").steps[0].selectors[0] == {"text": "Place order"}
    assert "the flow has been updated" in result.render()


# --- parameters ----------------------------------------------------------


def test_parameters_reach_the_typed_value():
    form = screen(
        "Search",
        {"text": "", "cls": "android.widget.EditText", "rid": "q", "clickable": True},
    )
    device = FakeDevice([form])
    result = run(
        device,
        flow(
            Step(
                action="type_text",
                selectors=[{"resource_id": "q"}],
                options={"value": "{{dish}}"},
            )
        ),
        params={"dish": "Cold Coffee"},
    )
    assert result.ok, result.render()
    assert device.keys == [("Cold Coffee", True)]


def test_a_missing_parameter_is_refused_before_anything_is_touched():
    from android_mcp.flows import FlowError

    device = FakeDevice([MENU])
    with pytest.raises(FlowError):
        run(
            device,
            flow(
                Step(
                    action="type_text",
                    selectors=[{"resource_id": "q"}],
                    options={"value": "{{dish}}"},
                )
            ),
        )
    assert device.clicks == []
