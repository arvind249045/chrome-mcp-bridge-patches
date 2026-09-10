"""Tests for the flow data layer: selectors, parameters and storage."""

import json

import pytest

from android_mcp.flows import (
    SCHEMA_VERSION,
    Flow,
    FlowError,
    FlowStore,
    Step,
    check_name,
    label_segments,
    parameterise,
    selector_candidates,
    stable_anchors,
    stable_part,
    substitute,
)
from android_mcp.ui import Bounds, Element, parse_hierarchy


def element(**kw):
    base = {
        "index": 0,
        "cls": "TextView",
        "text": "",
        "desc": "",
        "resource_id": "",
        "package": "com.bistro.app",
        "bounds": Bounds(0, 0, 100, 50),
    }
    base.update(kw)
    return Element(**base)


# --- labels --------------------------------------------------------------


def test_label_segments_splits_an_absorbed_row():
    assert label_segments("Paneer Roll · ₹99") == ["Paneer Roll", "₹99"]
    assert label_segments("Continue") == ["Continue"]
    assert label_segments("") == []


def test_stable_part_skips_the_price():
    assert stable_part("Paneer Roll · ₹99") == "Paneer Roll"


def test_stable_part_skips_a_leading_number():
    assert stable_part("2 items · Cold Coffee") == "Cold Coffee"


def test_stable_part_gives_up_cleanly_when_everything_is_volatile():
    assert stable_part("₹99 · 20% off") == ""


# --- ranked selectors ----------------------------------------------------


def test_resource_id_ranks_first():
    candidates = selector_candidates(
        element(text="Continue", resource_id="continue_btn")
    )
    assert candidates[0] == {"resource_id": "continue_btn", "exact": True}


def test_content_desc_outranks_visible_text():
    """An accessibility label is usually steadier than the copy on screen."""
    candidates = selector_candidates(element(text="Continue", desc="Continue button"))
    assert candidates[0] == {"desc": "Continue button", "exact": True}


def test_an_absorbed_row_keeps_a_candidate_without_the_price():
    """The price will change; the dish name will not."""
    row = element(text="Paneer Roll · ₹99", resource_id="row_0")
    candidates = selector_candidates(row)
    assert {"text": "Paneer Roll"} in candidates
    # and the brittle exact match is still there, just ranked lower
    exact = candidates.index({"text": "Paneer Roll · ₹99", "exact": True})
    partial = candidates.index({"text": "Paneer Roll"})
    assert exact < partial, "exact is tried before the looser match"


def test_candidates_are_deduplicated():
    candidates = selector_candidates(element(text="Pay", resource_id="pay"))
    assert len(candidates) == len({json.dumps(c, sort_keys=True) for c in candidates})


def test_no_candidate_is_ever_an_index():
    """A position is meaningless on a screen that gained a banner."""
    candidates = selector_candidates(element(index=7, text="Pay", resource_id="pay"))
    assert all("index" not in c for c in candidates)


def test_an_element_with_nothing_to_match_on_yields_nothing():
    assert selector_candidates(element(cls="FrameLayout")) == []


# --- anchors -------------------------------------------------------------


def test_anchors_skip_volatile_labels():
    elements = parse_hierarchy(
        '<hierarchy rotation="0">'
        '<node class="android.widget.FrameLayout" package="p" bounds="[0,0][1080,2400]">'
        '<node text="Your order" class="android.widget.TextView" package="p" '
        'bounds="[0,100][500,160]" />'
        '<node text="₹348 total" class="android.widget.TextView" package="p" '
        'bounds="[0,200][500,260]" />'
        '<node text="Delivery in 12 min" class="android.widget.TextView" package="p" '
        'bounds="[0,300][500,360]" />'
        "</node></hierarchy>"
    )
    anchors = stable_anchors(elements)
    assert "Your order" in anchors
    assert not any("₹" in a or "12" in a for a in anchors)


def test_anchors_are_capped():
    elements = parse_hierarchy(
        '<hierarchy rotation="0">'
        '<node class="android.widget.FrameLayout" package="p" bounds="[0,0][1080,2400]">'
        + "".join(
            f'<node text="label {chr(97 + i)}" class="android.widget.TextView" '
            f'package="p" bounds="[0,{100 + i * 60}][500,{150 + i * 60}]" />'
            for i in range(10)
        )
        + "</node></hierarchy>"
    )
    assert len(stable_anchors(elements, limit=3)) == 3


# --- names ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "../escape", "/abs/path", "with space", "UPPER", "a" * 65, ".hidden", "a/b"],
)
def test_bad_flow_names_are_refused(bad):
    """Names become filenames and arrive from a model, so allowlist only."""
    with pytest.raises(FlowError):
        check_name(bad)


@pytest.mark.parametrize("good", ["order-usual", "a", "flow_1", "99bottles"])
def test_good_flow_names_pass(good):
    assert check_name(good) == good


def test_the_store_cannot_be_walked_out_of(tmp_path):
    with pytest.raises(FlowError):
        FlowStore(tmp_path).path_for("../../etc/passwd")


# --- steps ---------------------------------------------------------------


def test_an_unknown_action_is_refused():
    with pytest.raises(FlowError) as exc:
        Step(action="teleport")
    assert "not a known action" in str(exc.value)


def test_a_tap_step_needs_a_selector():
    with pytest.raises(FlowError):
        Step(action="tap")


def test_a_press_step_needs_no_selector():
    assert Step(action="press_key", options={"key": "back"}).action == "press_key"


def test_a_step_describes_itself():
    step = Step(action="tap", selectors=[{"text": "Continue"}])
    assert "Continue" in step.describe()


# --- parameters ----------------------------------------------------------


def flow_with(value):
    return Flow(
        name="order",
        steps=[
            Step(action="open_app", options={"package": "com.bistro.app"}),
            Step(
                action="type_text",
                selectors=[{"resource_id": "search"}],
                options={"value": value},
            ),
        ],
    )


def test_parameterise_replaces_a_recorded_literal():
    flow = parameterise(flow_with("Paneer Roll"), {"dish": "Paneer Roll"})
    assert flow.steps[1].options["value"] == "{{dish}}"
    assert flow.params == ["dish"]
    assert flow.placeholders() == {"dish"}


def test_parameterise_refuses_a_literal_that_was_never_recorded():
    """Usually a typo, and silently ignoring it yields a flow that lies."""
    with pytest.raises(FlowError) as exc:
        parameterise(flow_with("Paneer Roll"), {"dish": "Veg Biryani"})
    assert "nothing recorded" in str(exc.value)


def test_parameterise_rejects_a_bad_parameter_name():
    with pytest.raises(FlowError):
        parameterise(flow_with("Paneer Roll"), {"my dish": "Paneer Roll"})


def test_substitute_fills_the_placeholder():
    flow = parameterise(flow_with("Paneer Roll"), {"dish": "Paneer Roll"})
    filled = substitute(flow, {"dish": "Cold Coffee"})
    assert filled.steps[1].options["value"] == "Cold Coffee"


def test_substitute_leaves_the_stored_flow_alone():
    flow = parameterise(flow_with("Paneer Roll"), {"dish": "Paneer Roll"})
    substitute(flow, {"dish": "Cold Coffee"})
    assert flow.steps[1].options["value"] == "{{dish}}"


def test_substitute_reports_a_missing_parameter():
    flow = parameterise(flow_with("Paneer Roll"), {"dish": "Paneer Roll"})
    with pytest.raises(FlowError) as exc:
        substitute(flow, {})
    assert "needs dish" in str(exc.value)


def test_substitute_rejects_a_parameter_the_flow_does_not_take():
    with pytest.raises(FlowError) as exc:
        substitute(flow_with("Paneer Roll"), {"nonsense": "x"})
    assert "does not take nonsense" in str(exc.value)


def test_a_flow_with_no_placeholders_needs_no_arguments():
    assert substitute(flow_with("Paneer Roll"), {}).steps[1].options["value"] == (
        "Paneer Roll"
    )


# --- storage -------------------------------------------------------------


def test_save_then_load_round_trips(tmp_path):
    store = FlowStore(tmp_path)
    original = parameterise(flow_with("Paneer Roll"), {"dish": "Paneer Roll"})
    original.description = "the usual"
    store.save(original)
    loaded = store.load("order")
    assert loaded.name == "order"
    assert loaded.description == "the usual"
    assert loaded.params == ["dish"]
    assert [s.action for s in loaded.steps] == ["open_app", "type_text"]
    assert loaded.steps[1].selectors == [{"resource_id": "search"}]


def test_saved_flows_are_human_readable_json(tmp_path):
    store = FlowStore(tmp_path)
    store.save(flow_with("Paneer Roll"))
    raw = json.loads((tmp_path / "order.json").read_text(encoding="utf-8"))
    assert raw["version"] == SCHEMA_VERSION
    assert raw["steps"][1]["options"]["value"] == "Paneer Roll"


def test_names_lists_what_is_saved(tmp_path):
    store = FlowStore(tmp_path)
    store.save(flow_with("x"))
    second = flow_with("y")
    second.name = "another"
    store.save(second)
    assert store.names() == ["another", "order"]


def test_an_empty_store_lists_nothing(tmp_path):
    assert FlowStore(tmp_path / "missing").names() == []


def test_loading_an_unknown_flow_says_what_is_there(tmp_path):
    store = FlowStore(tmp_path)
    store.save(flow_with("x"))
    with pytest.raises(FlowError) as exc:
        store.load("nope")
    assert "order" in str(exc.value)


def test_broken_json_is_reported_clearly(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(FlowError) as exc:
        FlowStore(tmp_path).load("broken")
    assert "not valid JSON" in str(exc.value)


def test_a_flow_from_the_future_is_refused(tmp_path):
    (tmp_path / "ahead.json").write_text(
        json.dumps({"name": "ahead", "steps": [], "version": SCHEMA_VERSION + 1}),
        encoding="utf-8",
    )
    with pytest.raises(FlowError) as exc:
        FlowStore(tmp_path).load("ahead")
    assert "newer version" in str(exc.value)


def test_a_malformed_step_is_reported(tmp_path):
    (tmp_path / "bad.json").write_text(
        json.dumps({"name": "bad", "steps": [{"action": "tap", "nonsense": 1}]}),
        encoding="utf-8",
    )
    with pytest.raises(FlowError) as exc:
        FlowStore(tmp_path).load("bad")
    assert "malformed" in str(exc.value)


def test_delete_removes_the_file(tmp_path):
    store = FlowStore(tmp_path)
    store.save(flow_with("x"))
    store.delete("order")
    assert store.names() == []
    with pytest.raises(FlowError):
        store.delete("order")


def test_the_flow_directory_is_created_on_save(tmp_path):
    store = FlowStore(tmp_path / "nested" / "deeper")
    store.save(flow_with("x"))
    assert store.load("order").name == "order"
