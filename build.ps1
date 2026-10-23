[CmdletBinding()]
param(
    [string]$Python = 'python',
    [switch]$SkipInstall,
    [switch]$PrepareOnly,
    [switch]$SelfTest
)
$ErrorActionPreference = 'Stop'
$editorRoot = $PSScriptRoot
$editorVenv = Join-Path $editorRoot '.venv-build'
$editorPython = Join-Path $editorVenv 'Scripts\python.exe'

function Assert-ExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)." }
}

Push-Location -LiteralPath $editorRoot
try {
    if (-not (Test-Path -LiteralPath $editorPython)) {
        & $Python -I -c "import sys,struct,tkinter; assert sys.platform == 'win32', 'Build on Windows'; assert struct.calcsize('P') == 8, 'Use 64-bit Python'; assert sys.version_info >= (3,11), 'Use Python 3.11 or newer'"
        Assert-ExitCode 'Python prerequisite check'
        & $Python -m venv $editorVenv
        Assert-ExitCode 'Creating the clean build virtual environment'
    }
    & $editorPython -I -c "import sys,struct,tkinter; assert sys.platform == 'win32' and struct.calcsize('P') == 8; assert sys.prefix != sys.base_prefix, 'Build requires a virtual environment'"
    Assert-ExitCode 'Build environment check'
    if (-not $SkipInstall) {
        & $editorPython -I -m pip --isolated install --disable-pip-version-check -r requirements-build.txt
        Assert-ExitCode 'Installing pinned build dependencies'
    }
    & $editorPython -I scripts/collect_licenses.py
    Assert-ExitCode 'Collecting original dependency licenses'
    if ($PrepareOnly) {
        Write-Output 'Pinned clean build environment and licenses are ready.'
        return
    }
    & $editorPython -I -m PyInstaller --clean --noconfirm pdf-clean-editor.spec
    Assert-ExitCode 'Building the portable Windows executable'
    & $editorPython -I scripts/write_build_manifest.py
    Assert-ExitCode 'Writing the build manifest'
    $editorExe = Join-Path $editorRoot 'dist\PDFCleanEditor-1.2.0-windows-x64.exe'
    Write-Output $editorExe
    if ($SelfTest) {
        & (Join-Path $editorRoot 'scripts\smoke_portable.ps1') -Executable $editorExe
    }
}
finally { Pop-Location }
