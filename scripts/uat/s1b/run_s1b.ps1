# Host driver for the Windows Sandbox clean-machine spike. Run it under scripts\uat\locks.py (gilead-heavy).
#   python scripts\uat\locks.py heavy "S1b sandbox" -- pwsh -File scripts\uat\s1b\run_s1b.ps1 -Out <dir>
# For each mode: write a .wsb (4096 MB, read-only input folder, writable output folder, logon command), start Windows
# Sandbox, wait for done.txt, then close ONLY the processes this run started, by PID. No host feature or registry change.
param(
    [Parameter(Mandatory)][string]$Out,
    [string]$Tag = 'v0.2.0',
    [string[]]$Modes = @('online', 'offline'),
    [int]$TimeoutSec = 600
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$exeName = "earthwave-$Tag-windows-x86_64.exe"
$in = Join-Path $Out 'in'; $outHost = Join-Path $Out 'out'
New-Item -ItemType Directory -Force $in, $outHost | Out-Null
Copy-Item "$here\sb.ps1" $in -Force
# the offline mode needs the exe on the (read-only) input folder; it is fetched here, on the host, with the checksum file
gh release download $Tag -R WPTK/earthwave -p $exeName -p SHA256SUMS -D $in --clobber
if (Get-Process -Name 'WindowsSandbox*', 'vmmemWindowsSandbox' -ErrorAction SilentlyContinue) { throw 'a Windows Sandbox is already running; not starting another' }

foreach ($mode in $Modes) {
    $net = if ($mode -eq 'offline') { 'Disable' } else { 'Default' }
    Remove-Item "$outHost\$mode" -Recurse -Force -ErrorAction SilentlyContinue
    $wsb = Join-Path $Out "$mode.wsb"
    @"
<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>$net</Networking>
  <MemoryInMB>4096</MemoryInMB>
  <MappedFolders>
    <MappedFolder><HostFolder>$in</HostFolder><SandboxFolder>C:\in</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$outHost</HostFolder><SandboxFolder>C:\out</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -ExecutionPolicy Bypass -File C:\in\sb.ps1 -Mode $mode -Tag $Tag</Command></LogonCommand>
</Configuration>
"@ | Set-Content -Path $wsb -Encoding UTF8

    $before = @(Get-Process | ForEach-Object Id)
    Write-Host "[$mode] starting sandbox (Networking=$net)"
    Start-Process -FilePath "$env:WINDIR\System32\WindowsSandbox.exe" -ArgumentList "`"$wsb`""
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while (-not (Test-Path "$outHost\$mode\done.txt") -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 5 }
    $ok = Test-Path "$outHost\$mode\done.txt"
    Write-Host "[$mode] done marker: $ok"
    Start-Sleep -Seconds 15   # let the guest finish its own shutdown
    $mine = Get-Process | Where-Object { $_.Id -notin $before -and $_.Name -match 'WindowsSandbox' }
    foreach ($p in $mine) { Write-Host "[$mode] closing PID $($p.Id) $($p.Name)"; Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 5
}
Write-Host 'output:'; Get-ChildItem $outHost -Recurse -File | ForEach-Object FullName
