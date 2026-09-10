# android-mcp

An MCP server for driving an Android device or a BlueStacks instance, built so a
model can work the screen cheaply and without guessing.

Three layers: primitives for reading and acting on the screen, recorded flows
that replay a path through an app deterministically, and user-profile
provisioning so a fresh profile is one command instead of ten minutes.

## Get it running

On the machine with BlueStacks or the phone. Five minutes.

**1. Get the code**

```powershell
git clone https://github.com/arvind249045/chrome-mcp-bridge-patches.git
cd chrome-mcp-bridge-patches
git checkout claude/bluestacks-multi-user-automation-s40dmq
cd android-mcp
pip install -e ".[device]"
```

**2. Point it at adb**

If you have Android platform-tools, adb is already on PATH and there is nothing
to do. If you only have BlueStacks, it ships its own adb under a different name:

```powershell
$env:ANDROID_MCP_ADB = "C:\Program Files\BlueStacks_nxt\HD-Adb.exe"
```

**3. Attach a device**

BlueStacks: **Settings -> Advanced -> Android Debug Bridge**, turn it on. It
shows a port. Phone: enable USB debugging and plug it in, then accept the prompt
on the phone.

**4. Check it works**

```powershell
android-mcp-selfcheck
```

This is the important step. It walks the whole stack and tells you exactly what
works and what does not, in plain language, stopping at the first real problem.
Do not skip it — nothing here has run against real hardware yet, so this is how
you find out what holds on your setup.

**5. Connect it to Claude Code**

```powershell
claude mcp add android -- android-mcp
```

Then ask Claude to `list_android_devices` and `read_screen`. If those two work,
everything else is built on them.

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

Recording and replay:

| Tool | What it does |
| --- | --- |
| `start_recording` | Begin capturing the actions that follow into a named flow |
| `save_flow` | Save the recording, optionally turning typed text into parameters |
| `cancel_recording` | Throw the recording away |
| `list_flows` | List saved flows, or show one flow's steps |
| `run_flow` | Replay a flow and report what each step did |
| `delete_flow` | Delete a saved flow |

User profiles:

| Tool | What it does |
| --- | --- |
| `list_users` | The device's profiles, or one profile's installed apps |
| `provision_user` | Create a profile and add apps to it in one step |
| `switch_user` | Switch the device to a profile |
| `remove_user` | Delete a profile and its data |

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

## Flows

Having a model reason through twenty taps every morning is slow, costly and
nondeterministic. Do it once and save it:

```
start_recording("order-usual")
  ... carry the flow out with the tools above ...
save_flow(description="the usual lunch", parameters={"dish": "Paneer Roll"})
```

Then `run_flow("order-usual", params={"dish": "Cold Coffee"})` replays it with no
reasoning per step. Flows are one JSON file each under `~/.android-mcp/flows`
(override with `ANDROID_MCP_FLOWS`), so they can be read, edited and diffed.

**What makes a flow survive next week's app update.** Each step stores several
ways to find its element, ranked by how likely they are to still be true:

1. `resource_id` — set in code, survives copy edits and translation
2. `content-desc` — an accessibility label, usually steadier than visible copy
3. exact text — precise, but breaks on any wording change
4. the *stable part* of the text — `"Paneer Roll · ₹99"` also stores
   `"Paneer Roll"`, because the price will change and the dish name will not
5. class plus stable text — for when the same words appear twice

Replay takes the first that hits. A step that only matched on a later selector is
reported as `healed`; pass `heal=true` to promote the one that worked so the
drift is recorded rather than rediscovered daily.

**Two deliberate refusals.** Replay never falls back to a recorded *index* — a
position is meaningless on a screen that gained a banner, and tapping the wrong
row is worse than stopping. And a run stops at the first step that fails rather
than carrying on, because continuing past a failed tap is how an automated run
does something nobody asked for. Either way the failure report names the step and
shows the screen, so a model can repair the flow.

Each step also records where it happened — the expected package, plus a few
anchor labels chosen to avoid anything with a number in it, so a cart total or a
delivery estimate never becomes the thing replay depends on. If the screen in
front of a step is not the one it was recorded on, the step fails before acting.

## User profiles

Setting a profile up by hand is slow mostly because every app downloads again.
It does not have to: the APK is already on the device, so adding it to another
profile is a link rather than a download. That is what `pm install-existing`
does, and what `provision_user` is built around.

```
list_users(user_id=0)                      # what is installed for the owner
provision_user(name="Testing",
               packages=["com.bistro.app"])
switch_user(user_id=13)
```

Packages are all validated before the profile is created, so a typo fails
without leaving a half-built profile behind; an app that cannot be added is
reported without discarding the rest, because one missing app is no reason to
throw away a profile that is otherwise ready. Nothing switches profile unless
asked, and `remove_user` refuses user 0 — removing the device owner is not a
profile operation.

These go through raw adb rather than uiautomator2, whose agent lives inside one
user while the point here is to act on the others. Note that `read_screen` and
everything else only ever sees the *current* profile, so switch before driving a
newly provisioned one.

## Checking it against a real device

Everything here is tested against fakes, which proves the logic and proves
nothing about an actual phone. This closes that gap:

```bash
python -m android_mcp.selfcheck                 # read-only
python -m android_mcp.selfcheck --tap Settings  # also covers the acting path
```

It walks the stack in stages — adb sees a device, uiautomator2 connects, the
hierarchy is readable, the screen settles, the profile tools work — and stops at
the first blocking failure so one root cause does not print as six errors. Each
stage says what a failure probably means rather than just that it happened.

The hierarchy stage reports the real compression ratio on whatever is on your
screen, so the 4.6% above is a fixture measurement you can check against your own
apps, along with how many elements actually carry a `resource_id` — which is the
assumption the selector ranking rests on.

## Tests

```bash
pytest
```

271 tests, none needing a device. The parsing, selector-ranking, flow and `pm`
output logic is pure functions; the action layer and replay engine run against a
scripted fake device and a fake clock; profile operations run against a fake adb
shell that records what it was asked to run; the self-check's stages run against
injected fakes; and the MCP tools are driven end-to-end through `call_tool`,
including a record-save-replay round trip. The
server tests skip if the MCP SDK is not installed, so the core suite runs
anywhere.

## Status

Every layer is covered by tests, but none of it has run against a physical device
or a live BlueStacks instance yet — run the self-check above first, which is
built precisely to tell you what holds — there is no Android available where this was
built. The untested seams are the ones that touch hardware: uiautomator2's
`dump_hierarchy` output on a real app versus the fixtures here, IME behaviour for
`type_text`, the exact `pm` output formats across Android versions, and how
stable `resource_id`s actually are across real app updates, which is the
assumption the whole selector ranking rests on. Expect to adjust that ranking
once there is evidence.

Worth knowing before the first run: BlueStacks images vary in whether they
support multiple users at all, and some ship with `pm create-user` disabled, in
which case `provision_user` will report the device's own refusal. A physical
OnePlus is the more likely place for the profile tools to earn their keep.

## Scope

This server exposes device primitives — read the screen, tap, type, scroll. It
ships no account-creation, phone-number-provisioning, or identity-rotation
tooling, and adding any is out of scope.

MIT licensed, like the repository it sits in.
