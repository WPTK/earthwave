# Runs INSIDE Windows Sandbox (logon command). Writes everything to C:\out (a mapped, writable host folder).
# -Mode online : download the release exe and its SHA256SUMS from GitHub, verify, run --version and --selftest.
# -Mode offline: Networking=Disable; the exe comes from the read-only C:\in folder; also proves the network is down.
param([ValidateSet('online', 'offline')][string]$Mode = 'online', [string]$Tag = 'v0.2.0')
$ErrorActionPreference = 'Continue'
$out = "C:\out\$Mode"
New-Item -ItemType Directory -Force $out | Out-Null
$exeName = "earthwave-$Tag-windows-x86_64.exe"
$work = 'C:\Users\WDAGUtilityAccount\ew'
New-Item -ItemType Directory -Force $work | Out-Null

function Note($name, $text) { Set-Content -Path "$out\$name" -Value $text -Encoding UTF8 }

# --- the machine ---
$os = Get-CimInstance Win32_OperatingSystem
Note 'machine.txt' ("user: $env:USERNAME`ncomputer: $env:COMPUTERNAME`nOS: $($os.Caption) $($os.Version) build $($os.BuildNumber)`n" +
    "RAM MB: $([math]::Round($os.TotalVisibleMemorySize / 1024))`nPS: $($PSVersionTable.PSVersion)`nnow: $(Get-Date -Format o)")

# --- no Python anywhere on PATH? ---
$py = @()
foreach ($n in 'python', 'python3', 'py', 'pip', 'pythonw') { $c = Get-Command $n -ErrorAction SilentlyContinue; if ($c) { $py += "$n -> $($c.Source)" } }
$where = (& where.exe python 2>&1 | Out-String).Trim()
$dirs = Get-ChildItem 'C:\Python*', "$env:LOCALAPPDATA\Programs\Python", 'C:\Program Files\Python*' -ErrorAction SilentlyContinue | ForEach-Object FullName
Note 'python.txt' ("Get-Command hits: " + $(if ($py) { $py -join '; ' } else { 'none' }) + "`nwhere python: $where`nPython install dirs: " + $(if ($dirs) { $dirs -join '; ' } else { 'none' }) +
    "`nPATH:`n" + ($env:PATH -split ';' -join "`n"))

# --- Windows Terminal present? ---
$wt = @()
$cmd = Get-Command wt.exe -ErrorAction SilentlyContinue; if ($cmd) { $wt += "wt.exe on PATH: $($cmd.Source)" }
$pkg = Get-AppxPackage -Name 'Microsoft.WindowsTerminal*' -ErrorAction SilentlyContinue; if ($pkg) { $wt += "appx: $($pkg.PackageFullName)" }
if (Test-Path "$env:LOCALAPPDATA\Microsoft\WindowsApps\wt.exe") { $wt += 'alias file present: %LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe' }
Note 'windows-terminal.txt' $(if ($wt) { $wt -join "`n" } else { 'Windows Terminal is NOT present' })

# --- network state ---
$net = @()
try { $r = Invoke-WebRequest -Uri 'https://github.com' -UseBasicParsing -TimeoutSec 15; $net += "github.com reachable: HTTP $($r.StatusCode)" } catch { $net += "github.com NOT reachable: $($_.Exception.Message)" }
$net += (Get-NetAdapter -ErrorAction SilentlyContinue | ForEach-Object { "adapter $($_.Name): $($_.Status)" })
Note 'network.txt' ($net -join "`n")

# --- get the exe ---
$exe = Join-Path $work $exeName
if ($Mode -eq 'online') {
    $base = "https://github.com/WPTK/earthwave/releases/download/$Tag"
    try {
        Invoke-WebRequest -Uri "$base/$exeName" -OutFile $exe -UseBasicParsing -TimeoutSec 300
        Invoke-WebRequest -Uri "$base/SHA256SUMS" -OutFile "$work\SHA256SUMS" -UseBasicParsing -TimeoutSec 60
    } catch { Note 'download-error.txt' "$_" }
} else {
    Copy-Item "C:\in\$exeName" $exe -ErrorAction SilentlyContinue
    Copy-Item 'C:\in\SHA256SUMS' "$work\SHA256SUMS" -ErrorAction SilentlyContinue
}
if (-not (Test-Path $exe)) { Note 'FAILED.txt' "exe missing: $exe"; Set-Content "$out\done.txt" 'failed'; exit 1 }
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
$listed = (Get-Content "$work\SHA256SUMS" | Where-Object { $_ -match [regex]::Escape($exeName) } | ForEach-Object { ($_ -split '\s+')[0] }) -join ''
Note 'sha256.txt' "computed: $hash`nlisted:   $listed`nmatch: $($hash -eq $listed.ToLower())`nsize bytes: $((Get-Item $exe).Length)"

# --- run it ---
function Run($label, $argv) {
    $o = "$out\$label.stdout.txt"; $e = "$out\$label.stderr.txt"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $exe -ArgumentList $argv -NoNewWindow -PassThru -RedirectStandardOutput $o -RedirectStandardError $e -WorkingDirectory $work
    $null = $p.Handle  # PS 5.1: without touching the handle ExitCode comes back empty
    if (-not $p.WaitForExit(180000)) { $p.Kill(); Add-Content "$out\$label.txt" 'TIMEOUT, killed' }
    Add-Content "$out\$label.txt" "exit code: $($p.ExitCode)`nseconds: $([math]::Round($sw.Elapsed.TotalSeconds, 1))"
}
Run 'version' '--version'
Run 'selftest' '--selftest'

Set-Content "$out\done.txt" (Get-Date -Format o)
Start-Sleep -Seconds 3
shutdown.exe /s /t 3
