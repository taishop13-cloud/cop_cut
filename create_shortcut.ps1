# Creates a "CopCut" shortcut on the Desktop and in the Start Menu.
$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$icon    = Join-Path $root "copcut\assets\copcut.ico"
if (-not (Test-Path $pythonw)) { Write-Host "Chua co .venv - hay chay CopCut.bat mot lan truoc."; exit 1 }

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "")
)
foreach ($dir in $targets) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "CopCut.lnk"))
    $lnk.TargetPath       = $pythonw
    $lnk.Arguments        = "`"$root\run.py`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation     = "$icon,0"
    $lnk.Description      = "CopCut - trinh bien tap video gon nhe"
    $lnk.Save()
    Write-Host "Da tao: $($lnk.FullName)"
}
