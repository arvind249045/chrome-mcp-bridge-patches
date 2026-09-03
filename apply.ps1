# Reapply the mcp-chrome-bridge patches after an npm update wipes them.
#
#   .\apply.ps1           apply, skipping anything already applied
#   .\apply.ps1 -Check    report whether the patches are present, change nothing
#   .\apply.ps1 -Revert   restore the pristine published files
#
# These edits live inside node_modules, so any reinstall of mcp-chrome-bridge
# removes them silently and the old symptoms return: the browser connection
# breaks on every reconnect, and any second session fails outright.

param(
    [switch]$Check,
    [switch]$Revert
)

$ErrorActionPreference = "Stop"

$targetVersion = "1.0.31"
$files = @("dist/mcp/mcp-server.js", "dist/server/index.js")

# The live bridge is the WinGet global install, not any copy under the home
# directory. Chrome's native host manifest is the authority on which one runs.
$manifest = Join-Path $env:APPDATA "Google\Chrome\NativeMessagingHosts\com.chromemcp.nativehost.json"
if (Test-Path $manifest) {
    $hostPath = (Get-Content $manifest -Raw | ConvertFrom-Json).path
    Write-Host "native host: $hostPath" -ForegroundColor DarkGray
}

$root = & npm root -g
$pkg = Join-Path $root "mcp-chrome-bridge"

if (-not (Test-Path $pkg)) {
    Write-Host "mcp-chrome-bridge not found under $root" -ForegroundColor Red
    exit 1
}

$installed = (Get-Content (Join-Path $pkg "package.json") -Raw | ConvertFrom-Json).version
Write-Host "installed: $installed   patched against: $targetVersion"
if ($installed -ne $targetVersion) {
    Write-Host "Version differs. Re-diff before trusting this patch." -ForegroundColor Yellow
}

# createMcpServer is the marker: present means the patches are in.
$marker = Select-String -Path (Join-Path $pkg "dist/mcp/mcp-server.js") -Pattern "createMcpServer" -Quiet

if ($Check) {
    if ($marker) { Write-Host "PATCHED" -ForegroundColor Green } else { Write-Host "NOT PATCHED" -ForegroundColor Red }
    exit 0
}

if ($Revert) {
    foreach ($f in $files) {
        $orig = Join-Path $PSScriptRoot ("pristine/" + $f)
        if (Test-Path $orig) {
            Copy-Item $orig (Join-Path $pkg $f) -Force
            Write-Host "reverted $f"
        }
    }
    Write-Host "Restart Chrome to reload the native host." -ForegroundColor Yellow
    exit 0
}

if ($marker) {
    Write-Host "Already patched, nothing to do." -ForegroundColor Green
    exit 0
}

# Keep a pristine copy the first time, so -Revert has something to restore.
foreach ($f in $files) {
    $dest = Join-Path $PSScriptRoot ("pristine/" + $f)
    if (-not (Test-Path $dest)) {
        New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
        Copy-Item (Join-Path $pkg $f) $dest
    }
}

Push-Location $pkg
& patch -p0 --forward --input (Join-Path $PSScriptRoot "dist.patch")
$code = $LASTEXITCODE
Pop-Location

if ($code -ne 0) {
    Write-Host "patch failed. Upstream files have probably changed; re-diff against the published tarball." -ForegroundColor Red
    exit $code
}

Write-Host "Patched. Restart Chrome so the native host reloads." -ForegroundColor Green
