# Runs INSIDE Windows Sandbox (logon command). Everything here changes only this throw-away guest, never the host:
# the hosts-file edit is the Sandbox's own. Mapped folders: C:\in (read-only: repo files, CA, exe, site-packages) and
# C:\py (read-only: the host's Python install), C:\out (writable evidence).
param([string]$Tag = 'v0.2.0')
$ErrorActionPreference = 'Continue'
$out = 'C:\out'
New-Item -ItemType Directory -Force "$out\s8b" | Out-Null
function Note($name, $text) { Set-Content -Path "$out\$name" -Value $text -Encoding UTF8 }

$os = Get-CimInstance Win32_OperatingSystem
Note 'machine.txt' ("user: $env:USERNAME`nOS: $($os.Caption) $($os.Version)`nRAM MB: $([math]::Round($os.TotalVisibleMemorySize / 1024))`nadmin: " +
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators') + "`nnow: $(Get-Date -Format o)")

$work = 'C:\Users\WDAGUtilityAccount\s8b'
$wd = "$work\wd"
New-Item -ItemType Directory -Force $wd | Out-Null
Copy-Item -Recurse -Force 'C:\in\repo' "$work\repo"
Copy-Item -Force 'C:\in\ca\*' $wd            # the test CA and leaf made on the host; the guest has no openssl
$exeName = "earthwave-$Tag-windows-x86_64.exe"
Copy-Item -Force "C:\in\$exeName" "$work\$exeName"
$hash = (Get-FileHash "$work\$exeName" -Algorithm SHA256).Hash.ToLower()
$listed = (Get-Content 'C:\in\SHA256SUMS' | Where-Object { $_ -match [regex]::Escape($exeName) } | ForEach-Object { ($_ -split '\s+')[0] }) -join ''
Note 'sha256.txt' "computed: $hash`nlisted:   $listed`nmatch: $($hash -eq $listed.ToLower())"

$py = 'C:\py\python.exe'
$env:PYTHONUTF8 = '1'
Remove-Item Env:NO_COLOR -ErrorAction SilentlyContinue
$env:PYTHONPATH = "C:\in\site;$work\repo\tests"
$env:S8B_DISPOSABLE = '1'
$env:EARTHWAVE_EXE = "$work\$exeName"
$env:S8B_DIR = $wd
$env:S8B_OUT = "$out\s8b"
Note 'python.txt' ((& $py -c "import sys,tui_test,psutil,pytest;print(sys.version);print(sys.executable)" 2>&1) | Out-String)

# 1. the local TLS origin on 443 (the guest's own loopback)
$origin = Start-Process -FilePath $py -ArgumentList "-u `"$work\repo\scripts\uat\s8b\origin.py`" --dir `"$wd`"" -PassThru -NoNewWindow `
    -RedirectStandardOutput "$out\origin.out" -RedirectStandardError "$out\origin.err" -WorkingDirectory $work
$null = $origin.Handle
Set-Content "$out\pids.txt" "origin $($origin.Id)"
for ($i = 0; $i -lt 40 -and -not (Test-Path "$wd\listening.json"); $i++) { Start-Sleep -Milliseconds 500 }
if (-not (Test-Path "$wd\listening.json")) { Note 'FAILED.txt' 'origin did not start'; Set-Content "$out\done.txt" failed; shutdown.exe /s /t 3; exit 1 }
Copy-Item "$wd\listening.json" "$out\listening.json"

# 2. the Sandbox's own hosts file points the six feed names at 127.0.0.1
& $py "$work\repo\scripts\uat\s8b\hosts.py" add "$wd\listening.json" 2>&1 | Out-File "$out\hosts-add.txt" -Encoding utf8
Note 'resolve.txt' ((& $py -c "import socket;print(socket.getaddrinfo('earthquake.usgs.gov',443))" 2>&1) | Out-String)

# 3. the unmodified exe through it, SSL_CERT_FILE = the test CA (set by the test from listening.json)
Set-Location "$work\repo"
$t0 = Get-Date
& $py -m pytest tests\uat\test_spike_s8b.py -p no:cacheprovider -q -s --timeout=900 2>&1 | Out-File "$out\pytest.txt" -Encoding utf8
Set-Content "$out\pytest-exit.txt" "exit $LASTEXITCODE seconds $([math]::Round(((Get-Date) - $t0).TotalSeconds))"

# 4. evidence, then clean up by PID
try { (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$((Get-Content "$wd\listening.json" | ConvertFrom-Json).control)/log" -TimeoutSec 10).Content | Set-Content "$out\s8b\origin-log.json" -Encoding utf8 } catch { Note 'origin-log-error.txt' "$_" }
& $py "$work\repo\scripts\uat\s8b\hosts.py" remove 2>&1 | Out-Null
Stop-Process -Id $origin.Id -Force -ErrorAction SilentlyContinue
Set-Content "$out\done.txt" (Get-Date -Format o)
Start-Sleep -Seconds 3
shutdown.exe /s /t 3
