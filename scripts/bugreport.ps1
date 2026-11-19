# bugreport.ps1 - collect the facts a maintainer needs to re-run and fix a report (Windows).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bugreport.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bugreport.ps1 -Command "convert x.mp4 to mp4"
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bugreport.ps1 -Full   # show names, no masking
#
# Prints a block to paste into the issue, and writes the same block to bugreport.txt next to it.
# Privacy: user and machine names are masked by default; file CONTENTS are never read - only
# versions, paths, sizes. Everything is shown on screen first; read it if you want.

param(
    [string]$Command = "",
    [string]$Path = "",
    [switch]$Full
)

$mask = -not $Full
function Hide([string]$s) {
    if (-not $script:mask -or -not $s) { return $s }
    ($s -replace [regex]::Escape($env:USERNAME), "<user>" `
        -replace [regex]::Escape($env:USERDOMAIN), "<machine>" `
        -replace [regex]::Escape($env:COMPUTERNAME), "<machine>")
}
function Try2([scriptblock]$b) { try { & $b | Out-String } catch { "(not available)" } }

$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
$ramGB = [math]::Round(($os.TotalVisibleMemorySize / 1MB), 1)

$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("### collected by bugreport.ps1 (paste into the issue - it adds nothing personal by default)")
[void]$sb.AppendLine("")
if ($Command) { [void]$sb.AppendLine("What I ran / did: $Command") }
[void]$sb.AppendLine("Windows: $($os.Caption) build $($os.BuildNumber) (UBR $($os.UBR)); DisplayVersion=$(Try2 { (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').DisplayVersion })")
[void]$sb.AppendLine("Machine: $(Hide $cs.Name) | CPU: $cpu | RAM: ${ramGB} GB")
[void]$sb.AppendLine("Python: $(Try2 { python --version 2>&1 })".Trim())
$tv = (Try2 { python -c "import importlib.metadata;print(importlib.metadata.version('transmute'))" 2>&1 }).Trim()
if (-not $tv -or $tv -match "Error") { $tv = "(not installed as a package - source checkout? note the app version from its title bar)" }
[void]$sb.AppendLine("Transmute version: $tv")
[void]$sb.AppendLine("Transmute root env: TRANSMUTE_ROOT=$env:TRANSMUTE_ROOT GOVERNOR=$env:TRANSMUTE_GOVERNOR DISABLE_GPU=$env:TRANSMUTE_DISABLE_GPU")
[void]$sb.AppendLine("")
[void]$sb.AppendLine("Engines found in PATH (Transmute uses these; empty = not installed or not on PATH):")
foreach ($e in "ffmpeg", "soffice", "gswin64c", "gswin64", "gs", "qpdf", "pdftoppm", "pandoc") {
    $c = Get-Command $e -ErrorAction SilentlyContinue
    if ($c) { [void]$sb.AppendLine("  $e -> $(Hide $c.Source)") } else { [void]$sb.AppendLine("  $e -> (not found)") }
}
foreach ($dir in "$env:ProgramFiles\LibreOffice\program\soffice.exe", "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe", "C:\tools\LibreOffice\program\soffice.exe") {
    if (Test-Path $dir) { [void]$sb.AppendLine("  LibreOffice on disk -> $(Hide $dir)") }
}
if ($Path -and (Test-Path $Path)) {
    $item = Get-Item $Path
    [void]$sb.AppendLine("")
    [void]$sb.AppendLine("Path facts: $(Hide $Path) | $(if ($item.PSIsContainer) { 'folder' } else { "$([math]::Round($item.Length/1MB,2)) MB" }) | drive free: $([math]::Round(((Get-PSDrive $item.Drive.Name).Free / 1GB), 1)) GB | network: $((Get-PSDrive $item.Drive.Name).DisplayRoot)")
}
$out = $sb.ToString()
Write-Host $out
Set-Content -Path (Join-Path (Get-Location) "bugreport.txt") -Value $out -Encoding UTF8
Write-Host "(also saved to $(Join-Path (Get-Location) 'bugreport.txt') - attach that file to the issue)"
