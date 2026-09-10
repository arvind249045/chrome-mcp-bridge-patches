"""Parsing and serialising an Android view hierarchy.

Everything in this module is a pure function over the XML string that
uiautomator's ``dumpWindowHierarchy`` produces, so it is testable with no
device attached. The device-facing shim lives in ``device.py``.

The central idea: never hand a screenshot to a model when the screen can be
queried as structured data. A raw dump is thousands of tokens of layout
scaffolding, so we filter it down to the nodes a user could actually interact
with or read, and emit one short line each.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from xml.etree import ElementTree

# Nodes from these packages are system chrome, not app content. The status bar
# clock ticks every minute, which would stop a screen ever looking settled.
VOLATILE_PACKAGES = frozenset({"com.android.systemui"})

# A visible node of one of these classes means "this screen is still working".
BUSY_CLASS_SUFFIXES = ("ProgressBar",)

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_TRUE = {"true", "True"}


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, other: Bounds) -> bool:
        return (
            self.left <= other.left
            and self.top <= other.top
            and self.right >= other.right
            and self.bottom >= other.bottom
        )


def parse_bounds(raw: str | None) -> Bounds | None:
    """``"[0,0][1080,2400]"`` -> Bounds. None if unparseable."""
    if not raw:
        return None
    m = _BOUNDS_RE.search(raw)
    if not m:
        return None
    left, top, right, bottom = (int(g) for g in m.groups())
    return Bounds(left, top, right, bottom)


def short_class(raw: str) -> str:
    """``android.widget.Button`` -> ``Button``."""
    return raw.rsplit(".", 1)[-1] if raw else ""


def short_resource_id(raw: str) -> str:
    """``com.example:id/continue_btn`` -> ``continue_btn``."""
    return raw.split("/", 1)[1] if raw and "/" in raw else (raw or "")


@dataclass(frozen=True)
class Element:
    """One addressable thing on screen."""

    index: int
    cls: str
    text: str
    desc: str
    resource_id: str
    package: str
    bounds: Bounds
    clickable: bool = False
    long_clickable: bool = False
    scrollable: bool = False
    checkable: bool = False
    checked: bool = False
    editable: bool = False
    enabled: bool = True
    selected: bool = False
    focused: bool = False

    @property
    def label(self) -> str:
        """Whatever a human would call this element."""
        return self.text or self.desc

    @property
    def is_busy_marker(self) -> bool:
        return self.cls.endswith(BUSY_CLASS_SUFFIXES)

    def flags(self) -> list[str]:
        out = []
        if self.clickable:
            out.append("clickable")
        if self.long_clickable:
            out.append("long-clickable")
        if self.scrollable:
            out.append("scrollable")
        if self.editable:
            out.append("editable")
        if self.checkable:
            out.append("checked" if self.checked else "unchecked")
        if self.selected:
            out.append("selected")
        if self.focused:
            out.append("focused")
        if not self.enabled:
            out.append("disabled")
        return out

    def render(self) -> str:
        """One compact line, the unit a model reads and refers back to."""
        parts = [f"[{self.index}]", self.cls or "View"]
        if self.label:
            parts.append(f'"{self.label}"')
        if self.resource_id:
            parts.append(f"#{self.resource_id}")
        flags = self.flags()
        if flags:
            parts.append(" ".join(flags))
        x, y = self.bounds.center
        parts.append(f"@{x},{y}")
        return " ".join(parts)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


@dataclass
class _Node:
    el: Element
    children: list[_Node]
    interesting: bool


def _attr(node: ElementTree.Element, name: str) -> str:
    return (node.get(name) or "").strip()


def _flag(node: ElementTree.Element, name: str) -> bool:
    return node.get(name) in _TRUE


def _to_element(node: ElementTree.Element) -> Element:
    cls_raw = _attr(node, "class")
    cls = short_class(cls_raw)
    return Element(
        index=-1,  # assigned once the tree is flattened
        cls=cls,
        text=_attr(node, "text"),
        desc=_attr(node, "content-desc"),
        resource_id=short_resource_id(_attr(node, "resource-id")),
        package=_attr(node, "package"),
        bounds=parse_bounds(node.get("bounds")) or Bounds(0, 0, 0, 0),
        clickable=_flag(node, "clickable"),
        long_clickable=_flag(node, "long-clickable"),
        scrollable=_flag(node, "scrollable"),
        checkable=_flag(node, "checkable"),
        checked=_flag(node, "checked"),
        editable=cls.endswith("EditText") or _flag(node, "password"),
        enabled=node.get("enabled") is None or _flag(node, "enabled"),
        selected=_flag(node, "selected"),
        focused=_flag(node, "focused"),
    )


def _is_interesting(el: Element) -> bool:
    """Would a user read this, or act on it?"""
    if el.bounds.area <= 0:
        return False  # laid out but not visible
    if el.label:
        return True
    if el.is_busy_marker:
        return True  # unlabelled, but it is how we know the screen is working
    return bool(
        el.clickable
        or el.long_clickable
        or el.scrollable
        or el.checkable
        or el.editable
    )


# A merged label joins the parts of one row; keep it readable rather than
# letting a dense card produce a 400-character line.
LABEL_JOIN = " \u00b7 "
MAX_LABEL = 120


def _absorbs(parent: Element, children: list[Element]) -> bool:
    """Is ``parent`` a bare tap target wrapping only inert labels?

    A tappable row is usually a clickable container with no text of its own
    around the views that carry the text: a name, a price, a subtitle. Listing
    the container and each child separately is noise, and worse, it makes a
    model guess which row a price belongs to. So the container absorbs them and
    becomes one line.

    Nothing is absorbed if a child is independently useful: another tap target,
    a scrollable area, a text field, or a spinner that tells us the screen is
    still working.
    """
    if not parent.clickable or parent.label or parent.scrollable:
        return False
    if not children:
        return False
    if any(
        child.clickable
        or child.long_clickable
        or child.scrollable
        or child.editable
        or child.is_busy_marker
        for child in children
    ):
        return False
    return any(child.label for child in children)


def _merged_label(children: list[Element]) -> str:
    label = LABEL_JOIN.join(child.label for child in children if child.label)
    if len(label) > MAX_LABEL:
        label = label[: MAX_LABEL - 1].rstrip() + "\u2026"
    return label


def _collapse(node: ElementTree.Element) -> list[Element]:
    """Depth-first, post-order: return the kept elements of this subtree.

    Post-order means child lists are already collapsed when a parent looks at
    them, so wrapper chains fall away without a second pass.
    """
    el = _to_element(node)
    kept: list[Element] = []
    for child in node:
        kept.extend(_collapse(child))

    if not _is_interesting(el):
        return kept

    if _absorbs(el, kept):
        labelled = [child for child in kept if child.label]
        merged = replace(
            el,
            text=_merged_label(kept),
            desc="",
            resource_id=el.resource_id or labelled[0].resource_id,
        )
        return [merged]

    return [el, *kept]


def parse_hierarchy(xml: str) -> list[Element]:
    """Filtered, flattened, index-assigned view of one hierarchy dump.

    Document order is close enough to reading order on real layouts that the
    indices come out roughly top-to-bottom.
    """
    if not xml or not xml.strip():
        return []
    root = ElementTree.fromstring(xml)

    elements: list[Element] = []
    for child in root:
        elements.extend(_collapse(child))

    screen = _display_bounds(root)
    if screen is not None:
        elements = [e for e in elements if _overlaps(screen, e.bounds)]

    return [replace(e, index=i) for i, e in enumerate(elements)]


def _display_bounds(root: ElementTree.Element) -> Bounds | None:
    """The display rectangle, as the union of the top-level windows.

    <hierarchy> itself carries no bounds. Its direct children are windows, so
    their union is the display; descendants are skipped on purpose, since a
    node scrolled off to the side would otherwise widen the "screen" and make
    the off-screen check vacuous.
    """
    windows = [parse_bounds(node.get("bounds")) for node in root]
    windows = [b for b in windows if b is not None and b.area > 0]
    if not windows:
        return None
    return Bounds(
        min(b.left for b in windows),
        min(b.top for b in windows),
        max(b.right for b in windows),
        max(b.bottom for b in windows),
    )


def _overlaps(screen: Bounds, other: Bounds) -> bool:
    return (
        other.right > screen.left
        and other.left < screen.right
        and other.bottom > screen.top
        and other.top < screen.bottom
    )


# --------------------------------------------------------------------------
# serialising
# --------------------------------------------------------------------------


def app_elements(elements: list[Element]) -> list[Element]:
    """Drop system chrome so a screen can be compared for equality."""
    return [e for e in elements if e.package not in VOLATILE_PACKAGES]


def fingerprint(elements: list[Element]) -> str:
    """Short stable hash of the app-owned part of the screen.

    Bounds are included deliberately: mid-scroll and mid-animation should read
    as "not settled yet".
    """
    payload = "\n".join(
        "|".join(
            (
                e.cls,
                e.label,
                e.resource_id,
                str(int(e.clickable)),
                str(int(e.checked)),
                f"{e.bounds.left},{e.bounds.top},{e.bounds.right},{e.bounds.bottom}",
            )
        )
        for e in app_elements(elements)
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def is_busy(elements: list[Element]) -> bool:
    """Is a spinner on screen?"""
    return any(e.is_busy_marker for e in app_elements(elements))


def render(elements: list[Element], include_system: bool = False) -> str:
    """The element list a model reads."""
    shown = elements if include_system else app_elements(elements)
    if not shown:
        return "(no interactive or labelled elements on screen)"
    return "\n".join(e.render() for e in shown)
