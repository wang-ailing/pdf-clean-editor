[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Executable)
$ErrorActionPreference = 'Stop'
$editorSourceExe = (Resolve-Path -LiteralPath $Executable).Path
$editorProbeRoot = Join-Path ([IO.Path]::GetTempPath()) ('PDF portable test 中文 ' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($editorProbeRoot) | Out-Null
$editorProbeExe = Join-Path $editorProbeRoot 'PDFCleanEditor.exe'
$editorReport = Join-Path $editorProbeRoot 'self-test.json'
Copy-Item -LiteralPath $editorSourceExe -Destination $editorProbeExe
$editorOldPath, $editorOldHome, $editorOldPythonPath = $env:PATH, $env:PYTHONHOME, $env:PYTHONPATH
try {
    $env:PATH = Join-Path $env:SystemRoot 'System32'
    $env:PYTHONHOME = $null
    $env:PYTHONPATH = $null
    $editorArgument = '--self-test --report "' + $editorReport + '"'
    $editorProcess = Start-Process -FilePath $editorProbeExe -ArgumentList $editorArgument -WorkingDirectory $editorProbeRoot -WindowStyle Hidden -PassThru
    if (-not $editorProcess.WaitForExit(120000)) {
        Stop-Process -Id $editorProcess.Id -ErrorAction SilentlyContinue
        throw 'Portable self-test timed out after 120 seconds.'
    }
    if ($editorProcess.ExitCode -ne 0) { throw "Portable self-test failed with exit $($editorProcess.ExitCode). Report: $editorReport" }
    if (-not (Test-Path -LiteralPath $editorReport)) { throw "Self-test did not write a report: $editorReport" }
    $editorResult = Get-Content -LiteralPath $editorReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $editorResult.ok) { throw "Portable self-test report did not pass: $editorReport" }
    if (-not $editorResult.frozen) { throw 'Portable test must exercise a frozen executable.' }
    $editorPublicReport = [ordered]@{
        version = $editorResult.version
        frozen = $editorResult.frozen
        pymupdf = $editorResult.pymupdf
        pillow = $editorResult.pillow
        checks = $editorResult.checks
        preview_pixel_metrics = $editorResult.preview_pixel_metrics
        ok = $editorResult.ok
        asset = [ordered]@{
            filename = [IO.Path]::GetFileName($editorSourceExe)
            bytes = (Get-Item -LiteralPath $editorSourceExe).Length
            sha256 = (Get-FileHash -LiteralPath $editorSourceExe -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    } | ConvertTo-Json -Depth 8
    $editorPublishedReport = Join-Path (Split-Path -Parent $editorSourceExe) 'portable-self-test.json'
    [IO.File]::WriteAllText($editorPublishedReport, $editorPublicReport + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    Write-Output $editorPublicReport
    Write-Output 'Portable self-test passed; report saved beside the release executable.'
}
finally {
    $env:PATH, $env:PYTHONHOME, $env:PYTHONPATH = $editorOldPath, $editorOldHome, $editorOldPythonPath
}
