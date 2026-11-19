[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Invoke-BuildTool {
    & $uv run --locked --no-dev --group build @args
    if ($LASTEXITCODE -ne 0) {
        throw "Build command failed (exit $LASTEXITCODE): $args"
    }
}

if ($env:OS -ne 'Windows_NT') {
    throw 'Build DLX Tray on Windows x64.'
}

$uv = (Get-Command uv.exe -CommandType Application | Select-Object -First 1).Source
$previousBuildEnvironment = $env:UV_PROJECT_ENVIRONMENT
$previousPath = $env:PATH
$buildLock = $null
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    $workDir = (New-Item -Path 'build/pyinstaller' -ItemType Directory -Force).FullName
    $buildLock = [System.IO.File]::Open((Join-Path $workDir 'build.lock'), 'OpenOrCreate', 'Write', 'None')
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $workDir '.venv'
    $dlxLock = Get-Content -LiteralPath 'scripts/deeplx.lock.json' -Raw | ConvertFrom-Json
    Invoke-BuildTool python scripts/fetch_deeplx.py

    $catalog = Join-Path $workDir 'uk.qm'
    Invoke-BuildTool pyside6-lrelease src/dlx_tray/i18n/uk.ts src/dlx_tray/i18n/qt_uk.ts -qm $catalog

    $appName = 'DLX Tray'
    $pyInstallerArgs = @(
        '--onedir', '--windowed', '--noupx', '--noconfirm'
        '--name', $appName
        '--specpath', $workDir
        '--workpath', (Join-Path $workDir 'analysis')
        '--distpath', 'dist'
        '--copy-metadata', 'dlx-tray'
        '--add-data', "${catalog}:dlx_tray/i18n"
        'src/dlx_tray/__main__.py'
    )
    # Qt uses the system ICU. Keep unrelated DLLs on the caller's PATH out of the bundle.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    Invoke-BuildTool python -I -m PyInstaller @pyInstallerArgs

    $distDir = Join-Path 'dist' $appName
    Copy-Item -LiteralPath $dlxLock.target -Destination (Join-Path $distDir 'deeplx.exe')
    Write-Host "Built $(Join-Path $distDir "$appName.exe")"
}
finally {
    $env:PATH = $previousPath
    $env:UV_PROJECT_ENVIRONMENT = $previousBuildEnvironment
    if ($null -ne $buildLock) {
        $buildLock.Dispose()
    }
    Pop-Location
}
