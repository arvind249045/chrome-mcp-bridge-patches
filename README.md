# chrome-mcp-bridge-patches

Three fixes for [`mcp-chrome-bridge`](https://github.com/hangwin/mcp-chrome) v1.0.31, plus a
script to reapply them, because they live inside `node_modules` and any reinstall
wipes them without warning.

Upstream is MIT licensed. These patches are against its published build.

## The bugs

Without these, the bridge looks like it works and then breaks under any real use.

**1. `Server` singleton, so only one client ever works.**
`getMcpServer()` returns one process-wide instance, but an SDK `Server` can only
bind to a single transport. The second client to connect gets
`Already connected to a transport`. Opening a second Claude Code session kills
the first. Upstream [#321](https://github.com/hangwin/mcp-chrome/issues/321),
[#345](https://github.com/hangwin/mcp-chrome/issues/345).

**2. Session servers were never closed.** Disconnected clients leak their `Server`.

**3. `GET /mcp` kills the SSE stream on every connect.**
The handler sets the SSE headers and calls `flushHeaders()` before handing the
socket to the transport. The SDK then calls `writeHead()` itself and throws
`ERR_HTTP_HEADERS_SENT`. Upstream [#349](https://github.com/hangwin/mcp-chrome/issues/349),
[#378](https://github.com/hangwin/mcp-chrome/issues/378).

The visible symptom of all three together is a connection that feels "jittery":
it reconnects in a loop and randomly drops.

## Use it

```powershell
git clone https://github.com/arvind249045/chrome-mcp-bridge-patches.git
cd chrome-mcp-bridge-patches

.\apply.ps1 -Check    # is the live install patched?
.\apply.ps1           # patch it
.\apply.ps1 -Revert   # put the published files back
```

Restart Chrome afterwards so the native host reloads.

`apply.ps1` refuses to guess: it resolves the live install through `npm root -g`,
prints the path Chrome's native host manifest actually points at, warns if the
installed version is not 1.0.31, and keeps a pristine copy under `pristine/`
before touching anything.

## Setting up a machine from scratch

1. **Node 20 or newer.** `winget install OpenJS.NodeJS.LTS`

2. **The bridge.**
   ```powershell
   npm install -g mcp-chrome-bridge
   mcp-chrome-bridge register
   ```
   `register` writes Chrome's native messaging host manifest to
   `%APPDATA%\Google\Chrome\NativeMessagingHosts\com.chromemcp.nativehost.json`.

3. **The extension.** Download `chrome-mcp-server-lastest.zip` from
   [upstream releases](https://github.com/hangwin/mcp-chrome/tree/master/releases/chrome-extension/latest),
   unzip it, then `chrome://extensions` with developer mode on, **Load unpacked**.

   Its manifest pins a key, so the ID is always
   `hbdgbgagpkpjffpklnamcljpakneikee`. That is the ID the native host manifest
   allowlists, so this matches automatically. No editing.

4. **These patches.** `.\apply.ps1`

5. **Restart Chrome**, open the extension, and connect. The bridge listens on
   `http://127.0.0.1:12306/mcp`.

## Checking it is healthy

- Near-empty stderr in `%LOCALAPPDATA%\mcp-chrome-bridge\logs\`
- Exactly one native host process
- Low `TIME_WAIT` count on port 12306. A hundred or more means the stream is
  dying and reconnecting in a loop, which is bug 3 above:
  ```powershell
  (netstat -ano | Select-String ":12306.*TIME_WAIT").Count
  ```

## When upstream merges this

These patches become unnecessary. Check whether the release you are on already
contains `createMcpServer`:

```powershell
Select-String -Path "$(npm root -g)\mcp-chrome-bridge\dist\mcp\mcp-server.js" -Pattern createMcpServer
```

A hit means you can stop using this repo.
