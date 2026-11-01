# Host driver for the S8b Windows Sandbox cell. Run it under the Gilead heavy lock (never steal it):
#   python scripts\uat\locks.py heavy "S8b sandbox" -- pwsh -File scripts\uat\s8b\run_s8b_sandbox.ps1 -Work <dir> -Site <dir with tui-test, psutil, pytest, pytest-timeout>
# Builds the read-only input folder (repo files, test CA made here with the host's openssl, released exe, site-packages),
# maps the host's Python install read-only, starts ONE Windows Sandbox, waits for done.txt, closes only the processes this
# run started, by PID. The hosts-file edit happens inside the Sandbox (sb_s8b.ps1). Gilead's own hosts file is never opened.
param(
    [Parameter(Mandatory)][string]$Work,
    [Parameter(Mandatory)][string]$Site,
    [string]$Python = 'C:\Python313',
    [string]$Tag = 'v0.2.0',
    [int]$TimeoutSec = 1800
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path "$here\..\..\..").Path
$exeName = "earthwave-$Tag-windows-x86_64.exe"
$in = Join-Path $Work 'in'; $outHost = Join-Path $Work 'out'
Remove-Item $in, $outHost -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$in\repo\scripts\uat", "$in\repo\tests\uat", "$in\repo\tests\fixtures", "$in\ca", $outHost | Out-Null
Copy-Item -Recurse "$repo\scripts\uat\s8b" "$in\repo\scripts\uat\s8b"
Copy-Item -Recurse "$repo\tests\uat_kit" "$in\repo\tests\uat_kit"
Copy-Item "$repo\tests\uat\test_spike_s8b.py" "$in\repo\tests\uat\"
Copy-Item -Recurse "$repo\tests\fixtures\replay" "$in\repo\tests\fixtures\replay"
Copy-Item -Recurse $Site "$in\site"
Get-ChildItem "$in\repo" -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
gh release download $Tag -R WPTK/earthwave -p $exeName -p SHA256SUMS -D $in --clobber
if ($LASTEXITCODE -ne 0) { throw 'release download failed' }

# the test CA and the leaf for the six feed names, made here with the host's openssl; only the guest trusts it (SSL_CERT_FILE)
$env:PYTHONUTF8 = '1'
& "$Python\python.exe" -c @"
import sys
sys.path.insert(0, r'$in\repo\scripts\uat\s8b')
from ca import TestCA
from origin import HOSTS
TestCA(r'$in\ca', HOSTS)
"@
if ($LASTEXITCODE -ne 0) { throw 'CA generation failed' }
Remove-Item "$in\ca\*.srl", "$in\ca\*.csr", "$in\ca\*.ext" -ErrorAction SilentlyContinue
Get-ChildItem "$in\repo" -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item "$here\sb_s8b.ps1" $in -Force

if (Get-Process -Name 'WindowsSandbox*', 'vmmemWindowsSandbox' -ErrorAction SilentlyContinue) { throw 'a Windows Sandbox is already running; not starting another' }
$wsb = Join-Path $Work 's8b.wsb'
@"
<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>Default</Networking>
  <MemoryInMB>4096</MemoryInMB>
  <MappedFolders>
    <MappedFolder><HostFolder>$in</HostFolder><SandboxFolder>C:\in</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$Python</HostFolder><SandboxFolder>C:\py</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$outHost</HostFolder><SandboxFolder>C:\out</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -ExecutionPolicy Bypass -File C:\in\sb_s8b.ps1 -Tag $Tag</Command></LogonCommand>
</Configuration>
"@ | Set-Content -Path $wsb -Encoding UTF8

$before = @(Get-Process | ForEach-Object Id)
Write-Host "starting sandbox ($wsb)"
Start-Process -FilePath "$env:WINDIR\System32\WindowsSandbox.exe" -ArgumentList "`"$wsb`""
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while (-not (Test-Path "$outHost\done.txt") -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 10 }
Write-Host "done marker: $(Test-Path "$outHost\done.txt")"
Start-Sleep -Seconds 20   # let the guest finish its own shutdown
$mine = Get-Process | Where-Object { $_.Id -notin $before -and $_.Name -match 'WindowsSandbox' }
foreach ($p in $mine) { Write-Host "closing PID $($p.Id) $($p.Name)"; Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 5
Get-ChildItem $outHost -Recurse -File | ForEach-Object FullName
