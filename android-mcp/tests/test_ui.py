"""Tests for the pure hierarchy-parsing layer. No device required."""

from android_mcp.ui import (
    Bounds,
    app_elements,
    fingerprint,
    is_busy,
    parse_bounds,
    parse_hierarchy,
    render,
    short_class,
    short_resource_id,
)

# A login screen, trimmed but shaped like a real dump: a status bar from
# systemui, nested layout scaffolding, a clickable row wrapping its own label,
# a zero-area node, and an off-screen node.
LOGIN_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" class="android.widget.FrameLayout" package="com.android.systemui"
        bounds="[0,0][1080,63]">
    <node index="0" text="12:04" class="android.widget.TextView"
          package="com.android.systemui" bounds="[36,18][120,45]" />
  </node>
  <node index="1" class="android.widget.FrameLayout" package="com.bistro.app"
        bounds="[0,63][1080,2400]">
    <node index="0" class="android.widget.LinearLayout" package="com.bistro.app"
          bounds="[0,63][1080,2400]">
      <node index="0" text="Log in" class="android.widget.TextView"
            package="com.bistro.app" bounds="[64,200][400,260]" />
      <node index="1" text="" resource-id="com.bistro.app:id/phone_input"
            class="android.widget.EditText" package="com.bistro.app"
            clickable="true" enabled="true" bounds="[64,580][1016,680]" />
      <node index="2" class="android.widget.FrameLayout"
            resource-id="com.bistro.app:id/continue_btn" package="com.bistro.app"
            clickable="true" enabled="true" bounds="[64,760][1016,860]">
        <node index="0" text="Continue" class="android.widget.TextView"
              package="com.bistro.app" bounds="[460,790][620,830]" />
      </node>
      <node index="3" text="Hidden" class="android.widget.TextView"
            package="com.bistro.app" bounds="[0,0][0,0]" />
      <node index="4" text="Offscreen" class="android.widget.TextView"
            package="com.bistro.app" bounds="[1200,900][1400,960]" />
      <node index="5" class="android.widget.LinearLayout" package="com.bistro.app"
            bounds="[0,1000][1080,1100]" />
    </node>
  </node>
</hierarchy>
"""


def parse(xml=LOGIN_XML):
    return parse_hierarchy(xml)


# --- small helpers --------------------------------------------------------


def test_parse_bounds_and_geometry():
    b = parse_bounds("[10,20][110,70]")
    assert b == Bounds(10, 20, 110, 70)
    assert (b.width, b.height, b.area) == (100, 50, 5000)
    assert b.center == (60, 45)


def test_parse_bounds_rejects_junk():
    assert parse_bounds("") is None
    assert parse_bounds(None) is None
    assert parse_bounds("not-bounds") is None


def test_negative_bounds_do_not_produce_negative_area():
    assert parse_bounds("[0,0][-5,-5]").area == 0


def test_name_shortening():
    assert short_class("android.widget.Button") == "Button"
    assert short_class("") == ""
    assert short_resource_id("com.bistro.app:id/continue_btn") == "continue_btn"
    assert short_resource_id("") == ""
    assert short_resource_id("no_slash") == "no_slash"


# --- filtering -----------------------------------------------------------


def test_keeps_labelled_and_actionable_nodes():
    labels = [e.label for e in parse()]
    assert "Log in" in labels
    assert "Continue" in labels


def test_drops_bare_layout_scaffolding():
    # The nested LinearLayout/FrameLayout wrappers carry no text and no
    # affordance, so none of them should reach the model.
    kept = parse()
    assert not [e for e in kept if e.cls == "LinearLayout" and not e.label]


def test_drops_zero_area_nodes():
    assert "Hidden" not in [e.label for e in parse()]


def test_drops_offscreen_nodes():
    assert "Offscreen" not in [e.label for e in parse()]


def test_edit_text_is_kept_and_marked_editable():
    field = next(e for e in parse() if e.resource_id == "phone_input")
    assert field.editable
    assert field.cls == "EditText"


# --- the wrapper merge ---------------------------------------------------


def test_clickable_wrapper_absorbs_its_label():
    """One tappable row must be one index, not two."""
    kept = parse()
    continues = [e for e in kept if e.label == "Continue"]
    assert len(continues) == 1
    btn = continues[0]
    assert btn.clickable
    # the wrapper's resource-id survives the merge
    assert btn.resource_id == "continue_btn"


def test_merge_does_not_swallow_an_edit_text():
    xml = """<hierarchy rotation="0">
      <node class="android.widget.FrameLayout" package="p" clickable="true"
            bounds="[0,0][100,100]">
        <node text="Email" class="android.widget.EditText" package="p"
              bounds="[0,0][100,100]" />
      </node>
    </hierarchy>"""
    kept = parse_hierarchy(xml)
    assert len(kept) == 2, "an editable field stays separately addressable"


def test_merge_does_not_swallow_a_nested_clickable():
    xml = """<hierarchy rotation="0">
      <node class="android.widget.FrameLayout" package="p" clickable="true"
            bounds="[0,0][100,100]">
        <node text="Delete" class="android.widget.Button" package="p"
              clickable="true" bounds="[60,0][100,100]" />
      </node>
    </hierarchy>"""
    kept = parse_hierarchy(xml)
    assert len(kept) == 2, "two independent tap targets stay distinct"


def test_wrapper_chain_collapses_to_one_element():
    xml = """<hierarchy rotation="0">
      <node class="android.widget.FrameLayout" package="p" clickable="true"
            bounds="[0,0][100,100]">
        <node class="android.widget.LinearLayout" package="p"
              bounds="[0,0][100,100]">
          <node text="Pay" class="android.widget.TextView" package="p"
                bounds="[10,10][90,90]" />
        </node>
      </node>
    </hierarchy>"""
    kept = parse_hierarchy(xml)
    assert len(kept) == 1
    assert kept[0].label == "Pay" and kept[0].clickable


# --- indices -------------------------------------------------------------


def test_indices_are_sequential_and_in_document_order():
    kept = parse()
    assert [e.index for e in kept] == list(range(len(kept)))
    labels = [e.label for e in kept if e.label]
    assert labels.index("Log in") < labels.index("Continue")


def test_indices_cover_system_nodes_too_so_rendering_stays_consistent():
    kept = parse()
    clock = next(e for e in kept if e.label == "12:04")
    # it is indexed, but excluded from the app view
    assert clock not in app_elements(kept)


# --- fingerprinting ------------------------------------------------------


def test_fingerprint_is_stable_across_identical_dumps():
    assert fingerprint(parse()) == fingerprint(parse())


def test_fingerprint_ignores_the_status_bar_clock():
    later = LOGIN_XML.replace(">12:04<", ">12:05<").replace('text="12:04"', 'text="12:05"')
    assert fingerprint(parse(later)) == fingerprint(parse())


def test_fingerprint_changes_when_app_content_changes():
    changed = LOGIN_XML.replace('text="Continue"', 'text="Verify"')
    assert fingerprint(parse(changed)) != fingerprint(parse())


def test_fingerprint_changes_mid_scroll():
    moved = LOGIN_XML.replace('bounds="[64,200][400,260]"', 'bounds="[64,150][400,210]"')
    assert fingerprint(parse(moved)) != fingerprint(parse())


# --- busy detection ------------------------------------------------------


def test_spinner_means_busy():
    assert not is_busy(parse())
    spinning = LOGIN_XML.replace(
        '<node index="3" text="Hidden" class="android.widget.TextView"\n'
        '            package="com.bistro.app" bounds="[0,0][0,0]" />',
        '<node class="android.widget.ProgressBar" package="com.bistro.app"\n'
        '            bounds="[500,1200][580,1280]" />',
    )
    assert is_busy(parse(spinning))


# --- rendering -----------------------------------------------------------


def test_render_is_one_line_per_app_element():
    out = render(parse())
    assert len(out.splitlines()) == len(app_elements(parse()))
    assert "12:04" not in out, "system chrome is hidden by default"


def test_render_line_shape():
    btn = next(e for e in parse() if e.label == "Continue")
    line = btn.render()
    assert line.startswith(f"[{btn.index}] FrameLayout \"Continue\" #continue_btn")
    assert "clickable" in line and "@540,810" in line


def test_render_can_include_system_nodes():
    assert "12:04" in render(parse(), include_system=True)


def test_render_handles_an_empty_screen():
    assert "no interactive" in render(parse_hierarchy("<hierarchy rotation='0' />"))


def test_parse_tolerates_empty_input():
    assert parse_hierarchy("") == []
    assert parse_hierarchy("   ") == []


# --- absorbing a whole row ----------------------------------------------


ROW_XML = """<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" package="p" bounds="[0,0][1080,2400]">
    <node class="android.widget.FrameLayout" package="p"
          resource-id="p:id/row_0" clickable="true" bounds="[0,300][1080,508]">
      <node class="android.widget.ImageView" package="p" bounds="[32,320][200,488]" />
      <node text="Paneer Roll" class="android.widget.TextView" package="p"
            bounds="[230,340][700,400]" />
      <node text="Rs 99" class="android.widget.TextView" package="p"
            bounds="[230,410][500,460]" />
    </node>
  </node>
</hierarchy>"""


def test_a_clickable_row_becomes_one_element_carrying_all_its_text():
    kept = parse_hierarchy(ROW_XML)
    assert len(kept) == 1, "one tappable row is one line"
    row = kept[0]
    assert row.clickable and row.resource_id == "row_0"
    assert "Paneer Roll" in row.label and "Rs 99" in row.label


def test_an_absorbed_row_is_still_findable_by_part_of_its_text():
    row = parse_hierarchy(ROW_XML)[0]
    assert "paneer" in row.label.casefold()


def test_a_spinner_child_blocks_absorption():
    """Losing the busy marker would break settling."""
    xml = ROW_XML.replace(
        '<node text="Rs 99" class="android.widget.TextView" package="p"\n'
        '            bounds="[230,410][500,460]" />',
        '<node class="android.widget.ProgressBar" package="p" '
        'bounds="[230,410][300,460]" />',
    )
    kept = parse_hierarchy(xml)
    assert len(kept) > 1
    assert is_busy(kept)


def test_a_very_long_row_label_is_truncated():
    parts = "".join(
        f'<node text="part {i} of a very wordy product card" '
        f'class="android.widget.TextView" package="p" '
        f'bounds="[0,{310 + i * 20}][900,{325 + i * 20}]" />'
        for i in range(8)
    )
    xml = (
        '<hierarchy rotation="0">'
        '<node class="android.widget.FrameLayout" package="p" bounds="[0,0][1080,2400]">'
        '<node class="android.widget.FrameLayout" package="p" clickable="true" '
        'bounds="[0,300][1080,508]">' + parts + "</node></node></hierarchy>"
    )
    row = parse_hierarchy(xml)[0]
    assert len(row.label) <= 120
    assert row.label.endswith("…")
