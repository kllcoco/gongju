param([ValidateSet('build','test','readonly-test')][string]$Action='build')
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
& (Join-Path $root 'tools/restore-toolchain.ps1')
$finder=Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$vs=& $finder -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if(-not $vs){throw 'Install Visual Studio C++ Build Tools and Windows SDK.'}
$cmake=Join-Path $vs 'Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe'
$build=Join-Path $root 'build'
& $cmake -S $root -B $build -A x64
if($LASTEXITCODE){exit $LASTEXITCODE}
& $cmake --build $build --config Release --parallel 4
if($LASTEXITCODE){exit $LASTEXITCODE}
if($Action -eq 'readonly-test'){
    $env:TEMP=Join-Path $root 'work/temp';$env:TMP=$env:TEMP
    New-Item -ItemType Directory -Force $env:TEMP | Out-Null
    & (Join-Path $root 'toolchain/node.exe') --test --test-name-pattern='seven|invalid arguments|runtime integrity|Restart Manager' (Join-Path $root 'tests/addon.test.cjs')
    exit $LASTEXITCODE
}
if($Action -eq 'test'){
    & (Join-Path (Split-Path $cmake) 'ctest.exe') --test-dir $build -C Release --output-on-failure
    if($LASTEXITCODE){exit $LASTEXITCODE}
    $env:TEMP=Join-Path $root 'work/temp';$env:TMP=$env:TEMP
    New-Item -ItemType Directory -Force $env:TEMP | Out-Null
    & (Join-Path $root 'toolchain/node.exe') --test (Join-Path $root 'tests/addon.test.cjs')
    exit $LASTEXITCODE
}
