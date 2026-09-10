"""Tests for settling, selector resolution and the composite actions."""

import pytest
from conftest import SPINNER, FakeClock, FakeDevice, screen

from android_mcp.actions import (
    ActionError,
    AmbiguousSelector,
    ElementNotFound,
    Selector,
    Settle,
    StaleState,
    find,
    locate,
    open_app,
    press,
    scroll_until,
    settle,
    swipe,
    tap,
    type_text,
    wait_for,
)
from android_mcp.ui import parse_hierarchy

LOGIN = screen(
    "Log in",
    {"text": "", "cls": "android.widget.EditText", "rid": "phone", "clickable": True},
    {"text": "Continue", "rid": "continue_btn", "clickable": True},
)


def settling(clock, **kw):
    opts = {
        "interval": 0.2,
        "timeout": 1.0,
        "clock": clock.monotonic,
        "sleep": clock.sleep,
    }
    opts.update(kw)
    return Settle(**opts)


# --- settling ------------------------------------------------------------


def test_settle_returns_once_two_reads_agree():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    snap = settle(device, **settling(clock).kwargs())
    assert snap.settled
    assert snap.reads == 2, "one confirming read, not a fixed sleep"
    assert clock.slept == pytest.approx(0.2)


def test_settle_waits_through_a_changing_screen_then_reports_settled():
    clock = FakeClock()
    device = FakeDevice([screen("one"), screen("two"), screen("three")])
    snap = settle(device, **settling(clock).kwargs())
    assert snap.settled
    assert "three" in snap.render()
    assert snap.reads == 4


def test_settle_reports_not_settled_when_the_screen_never_stops():
    clock = FakeClock()
    device = FakeDevice([screen(f"frame {i}") for i in range(50)])
    snap = settle(device, **settling(clock).kwargs())
    assert not snap.settled
    assert "NOT SETTLED" in snap.header()


def test_settle_keeps_waiting_while_a_spinner_is_up():
    clock = FakeClock()
    device = FakeDevice([screen(SPINNER, "Loading")])
    snap = settle(device, **settling(clock).kwargs())
    assert snap.busy
    assert not snap.settled, "a still hierarchy with a spinner is not ready"
    assert "BUSY" in snap.header()


def test_settle_succeeds_once_the_spinner_clears():
    clock = FakeClock()
    device = FakeDevice(
        [screen(SPINNER, "Loading"), screen(SPINNER, "Loading"), screen("Done")]
    )
    snap = settle(device, **settling(clock).kwargs())
    assert snap.settled and not snap.busy
    assert "Done" in snap.render()


def test_settle_can_ignore_spinners_when_asked():
    clock = FakeClock()
    device = FakeDevice([screen(SPINNER, "Loading")])
    snap = settle(device, **settling(clock, wait_for_idle=False).kwargs())
    assert snap.settled and snap.busy


def test_settle_rejects_a_nonsense_stable_reads():
    with pytest.raises(ValueError):
        settle(FakeDevice([LOGIN]), stable_reads=0)


def test_snapshot_header_carries_location_and_state():
    clock = FakeClock()
    snap = settle(FakeDevice([LOGIN]), **settling(clock).kwargs())
    assert "com.bistro.app/.MainActivity" in snap.header()
    assert f"state={snap.state_id}" in snap.header()


# --- selectors -----------------------------------------------------------


def elements(xml=LOGIN):
    return parse_hierarchy(xml)


def test_find_by_index():
    els = elements()
    assert find(els, Selector(index=0)).label == "Log in"


def test_find_by_resource_id():
    assert find(elements(), Selector(resource_id="continue_btn")).label == "Continue"


def test_find_by_partial_text_is_case_insensitive():
    assert find(elements(), Selector(text="contin")).label == "Continue"


def test_find_exact_mode_does_not_match_substrings():
    with pytest.raises(ElementNotFound):
        find(elements(), Selector(text="contin", exact=True))


def test_an_exact_label_beats_a_substring_match():
    els = elements(
        screen(
            {"text": "Pay", "clickable": True},
            {"text": "Payment methods", "clickable": True},
        )
    )
    assert find(els, Selector(text="Pay")).label == "Pay"


def test_a_tappable_match_beats_an_inert_one():
    els = elements(screen("Total", {"text": "Total", "clickable": True}))
    assert find(els, Selector(text="Total")).clickable


def test_genuine_ambiguity_is_an_error_listing_the_candidates():
    els = elements(
        screen({"text": "Add", "clickable": True}, {"text": "Add", "clickable": True})
    )
    with pytest.raises(AmbiguousSelector) as exc:
        find(els, Selector(text="Add"))
    assert "2 elements match" in str(exc.value)
    assert str(exc.value).count('"Add"') == 2, "both candidates are shown"


def test_a_missed_lookup_reports_the_whole_screen():
    """So a model can correct itself without a second round trip."""
    with pytest.raises(ElementNotFound) as exc:
        find(elements(), Selector(text="Checkout"))
    message = str(exc.value)
    assert "Continue" in message and "Log in" in message


def test_a_bad_index_reports_the_whole_screen():
    with pytest.raises(ElementNotFound) as exc:
        find(elements(), Selector(index=99))
    assert "Continue" in str(exc.value)


def test_a_selector_must_say_something():
    with pytest.raises(ValueError):
        Selector()


def test_locate_treats_absence_as_an_answer():
    assert locate(elements(), Selector(text="Checkout")) is None
    assert locate(elements(), Selector(text="Continue")) is not None


# --- tapping -------------------------------------------------------------


def test_tap_hits_the_element_centre_and_returns_the_next_screen():
    clock = FakeClock()
    device = FakeDevice([LOGIN], then=[screen("Enter OTP")])
    snap = tap(device, Selector(text="Continue"), settling=settling(clock))
    assert device.clicks == [(540, 490)]
    assert "Enter OTP" in snap.render()


def test_long_tap_uses_a_long_press():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    tap(device, Selector(text="Continue"), long=True, settling=settling(clock))
    assert device.long_clicks and not device.clicks


def test_tapping_a_disabled_control_is_refused_rather_than_silently_lost():
    clock = FakeClock()
    device = FakeDevice(
        [screen({"text": "Place order", "clickable": True, "enabled": False})]
    )
    with pytest.raises(ActionError) as exc:
        tap(device, Selector(text="Place order"), settling=settling(clock))
    assert "disabled" in str(exc.value)
    assert device.clicks == []


def test_a_stale_state_id_blocks_the_tap():
    """The guard that stops a tap landing on a screen that moved."""
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    with pytest.raises(StaleState) as exc:
        tap(
            device,
            Selector(index=2),
            expect_state="deadbeef",
            settling=settling(clock),
        )
    assert "the screen changed" in str(exc.value)
    assert device.clicks == []


def test_a_matching_state_id_allows_the_tap():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    current = settle(device, **settling(clock).kwargs())
    tap(
        device,
        Selector(index=2),
        expect_state=current.state_id,
        settling=settling(clock),
    )
    assert device.clicks == [(540, 490)]


# --- typing --------------------------------------------------------------


def test_type_text_focuses_the_field_then_uses_the_ime():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    type_text(device, Selector(resource_id="phone"), "9876543210", settling=settling(clock))
    assert device.clicks == [(540, 370)], "the field is tapped to focus it"
    assert device.keys == [("9876543210", True)]


def test_type_text_can_submit():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    type_text(
        device,
        Selector(resource_id="phone"),
        "123",
        submit=True,
        settling=settling(clock),
    )
    assert device.presses == ["enter"]


def test_type_text_can_append_instead_of_replacing():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    type_text(
        device, Selector(resource_id="phone"), "45", clear=False, settling=settling(clock)
    )
    assert device.keys == [("45", False)]


# --- swiping -------------------------------------------------------------


def test_swipe_down_drags_upward_to_reveal_lower_content():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    swipe(device, "down", settling=settling(clock))
    (sx, sy, ex, ey) = device.swipes[0]
    assert (sx, ex) == (540, 540)
    assert sy > ey, "content moves up, so the finger travels up"
    assert (sy, ey) == (1800, 600)


def test_swipe_up_is_the_mirror_image():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    swipe(device, "up", settling=settling(clock))
    (_, sy, _, ey) = device.swipes[0]
    assert sy < ey


def test_swipe_rejects_a_bad_direction():
    with pytest.raises(ValueError):
        swipe(FakeDevice([LOGIN]), "sideways")


def test_swipe_rejects_an_out_of_range_fraction():
    with pytest.raises(ValueError):
        swipe(FakeDevice([LOGIN]), "down", fraction=1.5)


# --- scrolling to something ---------------------------------------------


def test_scroll_until_does_not_swipe_if_it_is_already_visible():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    scroll_until(device, Selector(text="Continue"), settling=settling(clock))
    assert device.swipes == []


def test_scroll_until_finds_the_target_after_a_swipe():
    clock = FakeClock()
    device = FakeDevice([LOGIN], then=[screen("Checkout", {"text": "Pay now"})])
    snap = scroll_until(device, Selector(text="Pay now"), settling=settling(clock))
    assert len(device.swipes) == 1
    assert "Pay now" in snap.render()


def test_scroll_until_stops_at_the_end_of_the_list():
    """An unchanged screen after a swipe means the list is done."""
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    with pytest.raises(ElementNotFound) as exc:
        scroll_until(device, Selector(text="Pay now"), settling=settling(clock))
    assert "end of the list" in str(exc.value)
    assert len(device.swipes) == 1, "it does not keep swiping a list that cannot move"


# --- waiting -------------------------------------------------------------


def test_wait_for_returns_as_soon_as_it_is_there():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    snap = wait_for(
        device,
        Selector(text="Continue"),
        timeout=1.0,
        clock=clock.monotonic,
        sleep=clock.sleep,
        settling=settling(clock),
    )
    assert "Continue" in snap.render()


def test_wait_for_gives_up_and_shows_what_is_there_instead():
    clock = FakeClock()
    device = FakeDevice([LOGIN])
    with pytest.raises(ElementNotFound) as exc:
        wait_for(
            device,
            Selector(text="Order placed"),
            timeout=1.0,
            clock=clock.monotonic,
            sleep=clock.sleep,
            settling=settling(clock),
        )
    assert "did not appear" in str(exc.value)
    assert "Continue" in str(exc.value)


# --- launching -----------------------------------------------------------


def test_open_app_waits_for_the_package_to_be_in_front():
    clock = FakeClock()
    device = FakeDevice([LOGIN], package="com.bistro.app")
    snap = open_app(
        device,
        "com.bistro.app",
        clock=clock.monotonic,
        sleep=clock.sleep,
        settling=settling(clock),
    )
    assert device.started == [("com.bistro.app", None)]
    assert snap.package == "com.bistro.app"


def test_open_app_says_what_is_in_front_when_the_launch_fails():
    clock = FakeClock()
    device = FakeDevice([LOGIN], package="com.android.launcher")
    with pytest.raises(ActionError) as exc:
        open_app(
            device,
            "com.bistro.app",
            timeout=1.0,
            clock=clock.monotonic,
            sleep=clock.sleep,
            settling=settling(clock),
        )
    assert "com.android.launcher is in front" in str(exc.value)


def test_press_returns_the_resulting_screen():
    clock = FakeClock()
    device = FakeDevice([LOGIN], then=[screen("Home")])
    snap = press(device, "back", settling=settling(clock))
    assert device.presses == ["back"]
    assert "Home" in snap.render()
