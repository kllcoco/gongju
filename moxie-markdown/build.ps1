$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
    }
    $pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    & $pythonPath -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $pythonPath -m PyInstaller --noconfirm --distpath dist --workpath build moxie.spec
    if ($LASTEXITCODE -ne 0) { throw 'Application build failed.' }
    & $pythonPath tools\prepare_distribution.py
    if ($LASTEXITCODE -ne 0) { throw 'Distribution preparation failed.' }
    Write-Host 'Ready: dist\Moxie\Moxie-2.0.5.exe'
} finally {
    Pop-Location
}
