# android-mcp

An MCP server for driving an Android device or a BlueStacks instance, built so a
model can work the screen cheaply and without guessing.

This is the foundation layer: device discovery, reading the screen, and acting on
it. Recorded flows sit on top of it and are not here yet — see
[Next](#next).

## Why it is shaped like this

**The screen is data, not a picture.** The obvious design exposes `tap(x, y)` and
`screenshot()`. It demos well and then hurts: coordinates break between
instances and resolutions, and every step costs an image. So instead the server
reads uiautomator's view hierarchy, throws away the layout scaffolding, and
returns one line per thing you can read or act on:

```
com.bistro.app/.MenuActivity  state=7c1f9a2b04
[0] TextView "Your order" @324,195
[1] ScrollView #menu scrollable @540,1230
[2] FrameLayout "Paneer Roll · ₹99" #row_0 clickable @540,404
[3] FrameLayout "Veg Biryani · ₹129" #row_1 clickable @540,644
[4] FrameLayout "Place order" #place clickable @540,2300
```

On a realistic menu screen — 23 nodes, the usual four layers of wrappers, every
node carrying its seventeen attributes — that is 381 characters against 8,290 in
the raw dump: **4.6%**. It is also selectable by name, so a flow survives a
layout change that would break any coordinate.

A tappable row becomes *one* line carrying all of its text. A clickable
container with no text of its own absorbs its inert children, because listing
the row and then its name and then its price separately is noise, and it makes a
model guess which price belongs to which row. Anything independently useful —
another tap target, a text field, a scrollable area, a spinner — is never
absorbed.

**Settling beats sleeping.** Nothing here sleeps for a fixed time. After an
action the hierarchy is polled until two consecutive reads agree and no spinner
is on screen. A screen that settles in 300ms costs 300ms. When settling runs out
of time the result says `NOT SETTLED` rather than quietly pretending the screen
is ready.

**One call, one round trip.** Every tool returns the resulting screen, so a model
never has to ask what happened. A *failed* lookup returns the screen too:

```
nothing matches text='Checkout' on this screen.
[0] TextView "Your order" @324,195
[2] FrameLayout "Paneer Roll · ₹99" #row_0 clickable @540,404
...
```

so a wrong guess is corrected from the error itself instead of costing another
read.

**No implicit current device.** Tools take `serial`, defaulted only when exactly
one usable device is attached. With several attached you must say which, because
silently picking one is how an automation run drives the wrong emulator. The
registry caches connections and holds no notion of a selected device — the
parent repository of this folder exists because `mcp-chrome-bridge` kept one
process-wide `Server` and so could only serve one client; the device-shaped
version of that mistake is a shared global device.

## Install

```bash
cd android-mcp
pip install -e '.[device,dev]'
```

Needs Python 3.10+, `adb` on PATH, and the MCP SDK v2 or newer. (In SDK v1 the
server class was `FastMCP`; v2 renamed it to `MCPServer`, which is what this
targets.)

Register it with an MCP client:

```bash
claude mcp add android -- android-mcp
```

or by hand:

```json
{ "mcpServers": { "android": { "command": "android-mcp" } } }
```

### BlueStacks

Turn on **Settings → Advanced → Android Debug Bridge**; it shows the port for
that instance. Instances get ports ten apart — 5555, 5565, 5575 — and
`list_android_devices` probes that range and attaches whatever answers, so
instances that are not running are simply skipped.

The first connection to a device pushes uiautomator2's helper agent onto it,
which takes a few seconds once per device.

## Tools

| Tool | What it does |
| --- | --- |
| `list_android_devices` | Attached devices, probing BlueStacks ports |
| `read_screen` | The current screen as an element list, with a `state_id` |
| `open_app` | Launch, wait for it to be in front, return its first screen |
| `tap` | Tap by `text` / `resource_id` / `desc` / `index`; `long` for a long press |
| `type_text` | Focus a field and type through the IME |
| `swipe` | One scroll, in the direction the content moves |
| `scroll_until` | Swipe until an element appears, stopping at the end of the list |
| `wait_for` | Poll until an element appears |
| `press_key` | back, home, recent, enter, … |
| `screenshot` | PNG, for screens the hierarchy cannot describe |

Prefer `text`, `resource_id` or `desc` over `index`. Text matching is
case-insensitive and partial; an exact label beats a partial one and a tappable
match beats an inert one, so the common ambiguities resolve themselves. Genuine
ambiguity is an error listing the candidates rather than a guess.

`index` refers to the read it came from, so pass that read's `state_id` with it.
If the screen moved in between, the tap is refused — the guard that stops a tap
landing on a screen that changed underneath it.

A few behaviours worth knowing:

- `swipe` takes the direction the **content** goes, as a person would say it:
  `down` reveals what is further down the page.
- `scroll_until` stops as soon as a swipe fails to change the screen, which
  means the list has ended. It does not swipe `max_swipes` times hoping.
- `type_text` goes through the device IME rather than `input text`, so spaces
  and non-ASCII survive. If typing silently does nothing, that is where to look.
- Tapping a disabled control is refused rather than sent and lost.

## Tests

```bash
pytest
```

104 tests, no device needed. The parsing and selector logic is pure functions
over XML, the action layer runs against a scripted fake device and a fake clock,
and the MCP tools are driven end-to-end through `call_tool`. The server tests
skip if the MCP SDK is not installed, so the core suite runs anywhere.

## Next

The piece that makes a daily assistant actually work is not here yet: recording
a successful path once as a selector sequence and replaying it deterministically,
with the model re-entering only when a selector misses. Replay is fast and gives
the same answer every time; having a model reason through twenty taps every
morning is neither. The tools above are the primitives that a recorder would
capture and a player would replay.

## Scope

This server exposes device primitives — read the screen, tap, type, scroll. It
ships no account-creation, phone-number-provisioning, or identity-rotation
tooling, and adding any is out of scope.

MIT licensed, like the repository it sits in.
