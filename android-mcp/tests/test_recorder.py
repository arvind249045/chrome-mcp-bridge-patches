"""Tests for capturing a flow by watching it happen."""

import pytest
from conftest import screen

from android_mcp.actions import Snapshot
from android_mcp.flows import FlowError
from android_mcp.recorder import Recorder
from android_mcp.ui import fingerprint, parse_hierarchy

MENU = screen(
    "Your order",
    {"text": "Paneer Roll · ₹99", "rid": "row_0", "clickable": True},
    {"text": "Place order", "rid": "place", "clickable": True},
)


def snapshot(xml=MENU, package="com.bistro.app"):
    elements = parse_hierarchy(xml)
    return Snapshot(
        serial="fake:5555",
        elements=elements,
        state_id=fingerprint(elements),
        package=package,
        activity=".MenuActivity",
    )


def element_named(label, xml=MENU):
    return next(e for e in parse_hierarchy(xml) if label in e.label)


def test_a_fresh_recorder_is_not_recording():
    assert not Recorder().recording


def test_actions_outside_a_recording_are_ignored():
    recorder = Recorder()
    recorder.note_element("tap", element_named("Place order"), snapshot())
    recorder.note_action("press_key", {"key": "back"})
    assert recorder.steps == []


def test_start_validates_the_name():
    with pytest.raises(FlowError):
        Recorder().start("../escape")


def test_a_recorded_tap_captures_ranked_selectors():
    recorder = Recorder()
    recorder.start("order")
    recorder.note_element("tap", element_named("Place order"), snapshot())
    step = recorder.steps[0]
    assert step.action == "tap"
    assert step.selectors[0] == {"resource_id": "place", "exact": True}
    assert len(step.selectors) > 1, "a fallback is what makes replay survive"


def test_a_recorded_step_captures_where_it_happened():
    recorder = Recorder()
    recorder.start("order")
    recorder.note_element("tap", element_named("Place order"), snapshot())
    step = recorder.steps[0]
    assert step.expect_package == "com.bistro.app"
    assert "Your order" in step.anchors
    assert not any("₹" in a for a in step.anchors), "anchors avoid volatile text"


def test_a_recorded_row_keeps_a_selector_without_the_price():
    recorder = Recorder()
    recorder.start("order")
    recorder.note_element("tap", element_named("Paneer Roll"), snapshot())
    assert {"text": "Paneer Roll"} in recorder.steps[0].selectors


def test_the_launch_package_becomes_the_flows_app():
    recorder = Recorder()
    recorder.start("order")
    recorder.note_action("open_app", {"package": "com.bistro.app"})
    recorder.note_element("tap", element_named("Place order"), snapshot())
    flow = recorder.save()
    assert flow.package == "com.bistro.app"


def test_saving_produces_the_recorded_steps_in_order():
    recorder = Recorder()
    recorder.start("order")
    recorder.note_action("open_app", {"package": "com.bistro.app"})
    recorder.note_element("tap", element_named("Paneer Roll"), snapshot())
    recorder.note_element("tap", element_named("Place order"), snapshot())
    flow = recorder.save(description="the usual")
    assert [s.action for s in flow.steps] == ["open_app", "tap", "tap"]
    assert flow.description == "the usual"
    assert flow.name == "order"


def test_saving_clears_the_recorder_for_the_next_one():
    recorder = Recorder()
    recorder.start("order")
    recorder.note_element("tap", element_named("Place order"), snapshot())
    recorder.save()
    assert not recorder.recording and recorder.steps == []


def test_saving_without_recording_explains_the_order_of_operations():
    with pytest.raises(FlowError) as exc:
        Recorder().save()
    assert "not recording" in str(exc.value)


def test_saving_an_empty_recording_is_refused():
    recorder = Recorder()
    recorder.start("order")
    with pytest.raises(FlowError) as exc:
        recorder.save()
    assert "nothing was recorded" in str(exc.value)


def test_cancel_throws_the_recording_away():
    recorder = Recorder()
    recorder.start("order")
    recorder.note_element("tap", element_named("Place order"), snapshot())
    assert recorder.cancel() == "order"
    assert not recorder.recording and recorder.steps == []


def test_cancel_when_idle_reports_nothing():
    assert Recorder().cancel() is None


def test_starting_again_discards_a_half_finished_recording():
    recorder = Recorder()
    recorder.start("first")
    recorder.note_element("tap", element_named("Place order"), snapshot())
    recorder.start("second")
    assert recorder.steps == [] and recorder.name == "second"


def test_typed_text_can_be_turned_into_a_parameter_on_save():
    recorder = Recorder()
    recorder.start("search")
    recorder.note_element(
        "type_text",
        element_named("Place order"),
        snapshot(),
        {"value": "Paneer Roll", "clear": True},
    )
    flow = recorder.save(parameters={"dish": "Paneer Roll"})
    assert flow.steps[0].options["value"] == "{{dish}}"
    assert flow.params == ["dish"]
