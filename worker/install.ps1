# Raji Studio helper for a Windows PC: one-time setup. The dashboard (My channels -> Helper PC -> Show) gives the
# one-line command that runs this file with the server address (the command holds NO key):
#
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((iwr -UseBasicParsing 'https://SERVER/helper/install.ps1').Content)) -Server 'https://SERVER'"
#
# It then asks for the account's helper key: press "Copy key" in the dashboard and it is taken from the clipboard
# (the clipboard is emptied afterwards), or type / paste it at the hidden prompt. So the key is never on a command
# line and never in PowerShell's history. (-Key or the RAJI_KEY variable also work for scripts; then any line of the
# PowerShell history file that contains the key is removed - close that PowerShell window afterwards.)
#
# What it does:
#  * A helper of the same account already starts with Windows on this PC (for example the owner's own copy of the
#    project): only the key is saved next to it (worker\enroll_key.txt). Nothing else changes.
#  * Otherwise: the helper is downloaded from the server (with the key in a header, never in the address) into
#    %LOCALAPPDATA%\RajiHelper\<account>, Python is found (or installed with winget), the helper's packages are
#    installed once, and it is set to start hidden at every Windows sign-in, then started.
# The helper then joins by itself with the key: nothing to approve, no pairing code. This file holds no secrets.
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$Key = "",
    [string]$Dir = ""
)
$ErrorActionPreference = "Continue"  # native programs (python, pip, winget) may write to stderr; checked by exit code
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Server = $Server.Trim().TrimEnd("/")
$KeyRx = '^rk(\d{1,9})\.[0-9a-f]{8}\.[A-Za-z0-9_-]{43}$'

function Say([string]$msg) { Write-Host ""; Write-Host "== $msg" }

if ($Server -notmatch '^https://' -and $Server -notmatch '^http://(localhost|127\.0\.0\.1)(:\d+)?$') {
    Write-Host "The server address must start with https:// - copy the command again from the dashboard."; exit 1
}

$fromLine = $false
if (-not $Key -and $env:RAJI_KEY) { $Key = $env:RAJI_KEY }
Remove-Item Env:RAJI_KEY -ErrorAction SilentlyContinue
if ($Key) { $fromLine = $true }
if (-not $Key) {
    Write-Host ""
    Write-Host "Now open the dashboard: My channels -> Helper PC -> Show -> press 'Copy key'."
    Write-Host "Waiting for the key on the clipboard (up to 3 minutes; press Enter to type or paste it instead)..."
    for ($i = 0; $i -lt 180 -and -not $Key; $i++) {
        try { $clip = [string](Get-Clipboard -Raw) } catch { $clip = "" }
        if ($clip -and $clip.Trim() -match $KeyRx) {
            $Key = $clip.Trim()
            try { Set-Clipboard -Value " " } catch { }  # the key does not stay on the clipboard
            Write-Host "Got the key from the clipboard."
            break
        }
        $typed = $false
        try { $typed = [Console]::KeyAvailable } catch { }
        if ($typed) { try { [void][Console]::ReadKey($true) } catch { }; break }
        Start-Sleep -Seconds 1
    }
    if (-not $Key) {
        $sec = Read-Host -AsSecureString "Helper key (nothing is shown)"
        $Key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    }
}
$Key = $Key.Trim()
$m = [regex]::Match($Key, $KeyRx)
if (-not $m.Success) { Write-Host "This helper key does not look right - copy it again from the dashboard (Show -> Copy key)."; exit 1 }
$tid = [int]$m.Groups[1].Value
if ($fromLine) {
    # the key was on a command line: take every line that contains it out of PowerShell's saved history
    $hist = Join-Path $env:APPDATA "Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
    if (Test-Path $hist) {
        try {
            $lines = @(Get-Content $hist | Where-Object { $_ -notlike "*$Key*" })
            Set-Content -Path $hist -Value $lines -Encoding UTF8
            Write-Host "The key was removed from PowerShell's saved history. Close this PowerShell window when done."
        } catch { Write-Host "Could not clean PowerShell's history ($hist): remove the line with the key by hand." }
    }
}

function Get-Account([string]$root) {
    # which account a helper folder belongs to: its helper code starts with the account's number; none = the owner (1)
    $code = Join-Path $root "worker\helper_code.txt"
    if (Test-Path $code) {
        $c = (Get-Content $code -Raw).Trim()
        if ($c -match '^(\d+)-') { return [int]$Matches[1] }
    }
    return 1
}

$startup = [Environment]::GetFolderPath("Startup")
$vbs = Join-Path $startup "ClipStudioHelper.vbs"
$existing = $false
if (-not $Dir) {
    # a helper of this account that already starts with Windows on this PC: give the key to that one
    foreach ($f in @($vbs, (Join-Path $startup "RajiHelper-$tid.vbs"))) {
        if (-not (Test-Path $f)) { continue }
        $hit = [regex]::Match((Get-Content $f -Raw), '"+([^"]+?)\\worker\\run_hidden\.bat"+')
        if ($hit.Success) {
            $root = $hit.Groups[1].Value
            if ((Test-Path (Join-Path $root "worker\clipstudio_worker.py")) -and ((Get-Account $root) -eq $tid)) { $Dir = $root; $vbs = $f; $existing = $true; break }
        }
    }
}
if (-not $Dir) { $Dir = Join-Path $env:LOCALAPPDATA "RajiHelper\$tid" }
if (-not $existing) { $vbs = Join-Path $startup "RajiHelper-$tid.vbs" }  # never replaces another helper's start file
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

if (-not (Test-Path (Join-Path $Dir "worker\clipstudio_worker.py"))) {
    Say "Downloading the helper from $Server"
    $zip = Join-Path $env:TEMP ("raji-helper-" + [guid]::NewGuid().ToString("N") + ".zip")
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$Server/api/worker/package" -Headers @{ "x-helper-key" = $Key } -OutFile $zip -ErrorAction Stop
    } catch {
        Write-Host "Could not download the helper: $($_.Exception.Message)"
        Write-Host "Check the internet, or copy a fresh command from the dashboard (the key may have been changed)."; exit 1
    }
    Expand-Archive -Path $zip -DestinationPath $Dir -Force -ErrorAction Stop
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
}

# the key, next to the helper (only this Windows user may read it); the helper reads it when it registers
$keyFile = Join-Path $Dir "worker\enroll_key.txt"
[IO.File]::WriteAllText($keyFile, $Key, (New-Object Text.UTF8Encoding($false)))
try { & icacls $keyFile /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null } catch { }  # again on every run
if (-not (Test-Path (Join-Path $Dir ".git"))) {
    [IO.File]::WriteAllText((Join-Path $Dir "worker\server.txt"), $Server, (New-Object Text.UTF8Encoding($false)))
}

if ($existing) {
    Say "Done. The helper key was saved next to the helper that already starts with Windows ($Dir)."
    Write-Host "The helper keeps running as before; if the server ever forgets this PC, it joins again by itself."
    exit 0
}

Say "Finding Python 3.10 or newer"
function Test-Py([string]$exe, [string[]]$pre) {
    try {
        & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}
function Find-Python {
    if (Test-Py "python" @()) { return @{ exe = "python"; pre = @() } }
    if (Test-Py "py" @("-3")) { return @{ exe = "py"; pre = @("-3") } }
    return $null
}
$py = Find-Python
if (-not $py) {
    Write-Host "Python was not found. Installing it now (one time)..."
    try { & winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements | Out-Null } catch { }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $py = Find-Python
}
if (-not $py) {
    Write-Host "Python could not be installed automatically. Install it from https://www.python.org/downloads/"
    Write-Host "(tick 'Add python.exe to PATH'), then paste the command from the dashboard again."; exit 1
}

$venvPy = Join-Path $Dir ".venv\Scripts\python.exe"
if (-not (Test-Path (Join-Path $Dir ".venv\ready.txt"))) {
    Say "Preparing the helper (one time, a few minutes)"
    $pre = $py.pre
    & $py.exe @pre -m venv (Join-Path $Dir ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Host "Python could not make the helper's folder (.venv). Paste the command again."; exit 1 }
    & $venvPy -m pip install -q --upgrade pip
    & $venvPy -m pip install -q -r (Join-Path $Dir "worker\requirements.txt")
    if ($LASTEXITCODE -ne 0) { Write-Host "Something could not be installed. Check the internet and paste the command again."; exit 1 }
    Set-Content -Path (Join-Path $Dir ".venv\ready.txt") -Value "ok"
}

Say "Starting the helper now and at every Windows sign-in (hidden)"
$bat = Join-Path $Dir "worker\run_hidden.bat"
Set-Content -Path $vbs -Value ('CreateObject("WScript.Shell").Run """' + $bat + '""", 0, False') -Encoding ASCII
& wscript.exe $vbs

$log = Join-Path $Dir "worker\helper.log"
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    if ((Test-Path $log) -and (Select-String -Path $log -Pattern "nothing to approve|running for|pairing code" -Quiet)) { break }
}
if (Test-Path $log) { Get-Content $log -Tail 4 }
Write-Host ""
if ((Test-Path $log) -and (Select-String -Path $log -Pattern "nothing to approve" -Quiet)) {
    Write-Host "Connected by itself - nothing to approve. It shows in the dashboard under My channels -> Helper PC."
} else {
    Write-Host "The helper is starting. It shows in the dashboard under My channels -> Helper PC in a minute. Log: $log"
}
