"""Saved flows: a recorded path through an app, replayable without a model.

Having a model reason through twenty taps every morning is slow, costly and
nondeterministic. Record the path once and replay it and you get the same answer
in a fraction of the time, with the model re-entering only when a step misses.

This module is the data layer — steps, ranked selectors, storage, parameters —
and is pure apart from ``FlowStore`` touching disk. Replay lives in
``player.py`` and capture in ``recorder.py``.

The part that decides whether a flow survives next week's app update is
``selector_candidates``: each step stores several ways to find its element,
ranked most to least durable, and replay takes the first that hits.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .ui import LABEL_JOIN, Element

SCHEMA_VERSION = 1

# A flow name becomes a filename, and names arrive from a model, so the only
# safe policy is an allowlist. This also rules out "..", absolute paths and
# separators without needing to reason about them.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}", re.IGNORECASE)

# Labels holding a number are the ones that change under you: prices, counts,
# times, queue positions. They make poor anchors and poor selectors.
VOLATILE_LABEL = re.compile(r"[0-9₹$€£%]")

ACTIONS = frozenset(
    {"open_app", "tap", "type_text", "swipe", "press_key", "wait_for", "scroll_until"}
)

# Actions that need an element, and therefore a ranked selector list.
NEEDS_SELECTOR = frozenset({"tap", "type_text", "wait_for", "scroll_until"})


class FlowError(RuntimeError):
    """A flow that cannot be stored, loaded or made sense of."""


def check_name(name: str) -> str:
    if not NAME_PATTERN.match(name or ""):
        raise FlowError(
            f"{name!r} is not a usable flow name. Use lower-case letters, "
            "digits, hyphens and underscores, starting with a letter or digit."
        )
    return name


# --------------------------------------------------------------------------
# selectors
# --------------------------------------------------------------------------


def label_segments(label: str) -> list[str]:
    """Split an absorbed row label back into its parts.

    A row absorbed into ``"Paneer Roll · ₹99"`` carries a price, and the
    price will change. The first segment is the part worth matching on.
    """
    return [part.strip() for part in label.split(LABEL_JOIN.strip()) if part.strip()]


def stable_part(label: str) -> str:
    """The first segment of a label that does not look volatile."""
    for part in label_segments(label):
        if not VOLATILE_LABEL.search(part):
            return part
    return ""


def selector_candidates(element: Element) -> list[dict]:
    """Ways to find this element again, most durable first.

    The ranking is a claim about what app developers change:

    1. ``resource_id`` — set in code, survives copy edits and translation.
    2. ``desc`` — an accessibility label, usually steadier than visible copy.
    3. exact text — precise, but breaks on any wording change.
    4. the stable part of the text — survives a changing price or suffix.
    5. class plus stable text — for when the same words appear twice.

    Index is deliberately absent: a position is meaningless on a screen that has
    gained a banner. Replay falls back to it only via a step's recorded index,
    and only once the anchors say this is the right screen.
    """
    out: list[dict] = []
    seen: set[str] = set()

    def add(candidate: dict) -> None:
        key = json.dumps(candidate, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(candidate)

    if element.resource_id:
        add({"resource_id": element.resource_id, "exact": True})
    if element.desc:
        add({"desc": element.desc, "exact": True})
    if element.text:
        add({"text": element.text, "exact": True})

    core = stable_part(element.label)
    if core and core != element.text:
        add({"text": core})
    if core and element.cls:
        add({"cls": element.cls, "text": core})
    if element.resource_id and element.cls:
        add({"resource_id": element.resource_id, "cls": element.cls, "exact": True})
    return out


def stable_anchors(elements: list[Element], limit: int = 4) -> list[str]:
    """Labels that say "this is the right screen" without being brittle.

    Volatile labels are skipped, so a cart total or a delivery estimate never
    becomes the thing a replay depends on.
    """
    anchors: list[str] = []
    for element in elements:
        for part in label_segments(element.label):
            if VOLATILE_LABEL.search(part) or len(part) < 3:
                continue
            if part not in anchors:
                anchors.append(part)
            if len(anchors) >= limit:
                return anchors
    return anchors


# --------------------------------------------------------------------------
# steps and flows
# --------------------------------------------------------------------------


@dataclass
class Step:
    action: str
    selectors: list[dict] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    expect_package: str = ""
    anchors: list[str] = field(default_factory=list)
    index: int | None = None
    label: str = ""

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise FlowError(
                f"{self.action!r} is not a known action. "
                f"Expected one of: {', '.join(sorted(ACTIONS))}."
            )
        if self.action in NEEDS_SELECTOR and not self.selectors:
            raise FlowError(f"a {self.action} step needs at least one selector")

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.selectors:
            first = self.selectors[0]
            named = next(
                (first[k] for k in ("text", "resource_id", "desc") if k in first), "?"
            )
            return f"{self.action} {named!r}"
        value = self.options.get("value") or self.options.get("direction") or ""
        return f"{self.action} {value}".strip()


@dataclass
class Flow:
    name: str
    steps: list[Step] = field(default_factory=list)
    description: str = ""
    params: list[str] = field(default_factory=list)
    package: str = ""
    created: str = ""
    version: int = SCHEMA_VERSION

    def __post_init__(self):
        check_name(self.name)
        if not self.created:
            self.created = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def placeholders(self) -> set[str]:
        """Every ``{{name}}`` appearing in the steps' option values."""
        found: set[str] = set()
        for step in self.steps:
            for value in step.options.values():
                if isinstance(value, str):
                    found.update(m.lower() for m in PLACEHOLDER.findall(value))
        return found

    def describe(self) -> str:
        lines = [f"{self.name}  ({len(self.steps)} steps)"]
        if self.description:
            lines.append(self.description)
        if self.package:
            lines.append(f"app: {self.package}")
        if self.params:
            lines.append(f"parameters: {', '.join(self.params)}")
        for number, step in enumerate(self.steps, 1):
            lines.append(f"  {number}. {step.describe()}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, raw: dict) -> Flow:
        version = raw.get("version", SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            raise FlowError(
                f"this flow was written by a newer version (schema {version}, "
                f"this build understands {SCHEMA_VERSION})"
            )
        try:
            steps = [Step(**step) for step in raw.get("steps", [])]
        except TypeError as exc:
            raise FlowError(f"a step is malformed: {exc}") from exc
        known = {"name", "description", "params", "package", "created", "version"}
        return cls(steps=steps, **{k: v for k, v in raw.items() if k in known})


def parameterise(flow: Flow, mapping: dict[str, str]) -> Flow:
    """Turn recorded literals into placeholders.

    Recording captures what was actually typed. ``{"dish": "Paneer Roll"}``
    rewrites that literal to ``{{dish}}`` so the flow takes it as a parameter.
    A literal that appears nowhere is an error rather than a silent no-op, since
    the usual cause is a typo and the result would be a flow that ignores its
    own parameter.
    """
    if not mapping:
        return flow
    for name in mapping:
        check_param_name(name)

    found: set[str] = set()
    for step in flow.steps:
        for key, value in list(step.options.items()):
            if not isinstance(value, str):
                continue
            for name, literal in mapping.items():
                if literal and literal in value:
                    value = value.replace(literal, f"{{{{{name}}}}}")
                    found.add(name)
            step.options[key] = value

    missing = sorted(set(mapping) - found)
    if missing:
        details = ", ".join(f"{n}={mapping[n]!r}" for n in missing)
        raise FlowError(
            f"nothing recorded in {flow.name} contains {details}. "
            "Parameters replace text that the recording actually typed."
        )
    flow.params = sorted(set(flow.params) | found)
    return flow


def check_param_name(name: str) -> str:
    if not re.match(r"^[a-z][a-z0-9_]{0,31}$", name or "", re.IGNORECASE):
        raise FlowError(
            f"{name!r} is not a usable parameter name. Use letters, digits and "
            "underscores, starting with a letter."
        )
    return name


def substitute(flow: Flow, values: dict[str, str]) -> Flow:
    """Fill a flow's placeholders, leaving the stored flow untouched."""
    supplied = {key.lower(): value for key, value in (values or {}).items()}
    needed = flow.placeholders()
    missing = sorted(needed - supplied.keys())
    if missing:
        raise FlowError(
            f"{flow.name} needs {', '.join(missing)}. "
            f"It takes: {', '.join(sorted(needed)) or 'nothing'}."
        )
    unexpected = sorted(supplied.keys() - needed)
    if unexpected:
        raise FlowError(
            f"{flow.name} does not take {', '.join(unexpected)}. "
            f"It takes: {', '.join(sorted(needed)) or 'nothing'}."
        )

    def fill(text: str) -> str:
        return PLACEHOLDER.sub(lambda m: supplied[m.group(1).lower()], text)

    steps = []
    for step in flow.steps:
        options = {
            key: fill(value) if isinstance(value, str) else value
            for key, value in step.options.items()
        }
        steps.append(
            Step(
                action=step.action,
                selectors=[dict(s) for s in step.selectors],
                options=options,
                expect_package=step.expect_package,
                anchors=list(step.anchors),
                index=step.index,
                label=step.label,
            )
        )
    return Flow(
        name=flow.name,
        steps=steps,
        description=flow.description,
        params=list(flow.params),
        package=flow.package,
        created=flow.created,
    )


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def default_flow_dir() -> Path:
    import os

    configured = os.environ.get("ANDROID_MCP_FLOWS")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".android-mcp" / "flows"


class FlowStore:
    """Flows as one JSON file each, so they can be read, edited and diffed."""

    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory else default_flow_dir()

    def path_for(self, name: str) -> Path:
        return self.directory / f"{check_name(name)}.json"

    def names(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(
            path.stem
            for path in self.directory.glob("*.json")
            if NAME_PATTERN.match(path.stem)
        )

    def load(self, name: str) -> Flow:
        path = self.path_for(name)
        if not path.is_file():
            known = ", ".join(self.names()) or "none saved yet"
            raise FlowError(f"no flow called {name!r}. Saved flows: {known}.")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FlowError(f"{path} is not valid JSON: {exc}") from exc
        return Flow.from_dict(raw)

    def save(self, flow: Flow) -> Path:
        path = self.path_for(flow.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(flow.to_json(), encoding="utf-8")
        return path

    def delete(self, name: str) -> None:
        path = self.path_for(name)
        if not path.is_file():
            raise FlowError(f"no flow called {name!r}")
        path.unlink()
