param(
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$solution = Join-Path $projectRoot "RadialMenu.sln"
$appProject = Join-Path $projectRoot "src\RadialMenu.App\RadialMenu.App.csproj"
$testProject = Join-Path $projectRoot "tests\RadialMenu.Core.Tests\RadialMenu.Core.Tests.csproj"
$publishDirectory = Join-Path $projectRoot "artifacts\stage1-smoke"
$executable = Join-Path $publishDirectory "RadialMenu.exe"
$logPath = Join-Path $env:APPDATA ("RadialMenu\logs\{0}.log" -f (Get-Date -Format "yyyyMMdd"))

# Windows 上会实际执行 NativeLibrary 入口点检查；任一入口不存在都会让脚本失败。
dotnet test $testProject -c $Configuration
if ($LASTEXITCODE -ne 0) {
    throw "自动测试失败。"
}

dotnet publish $appProject -c $Configuration -r win-x64 --self-contained false -o $publishDirectory
if ($LASTEXITCODE -ne 0) {
    throw "Windows 测试程序发布失败。"
}

$oldLogLength = if (Test-Path $logPath) { (Get-Item $logPath).Length } else { 0 }
$process = Start-Process -FilePath $executable -ArgumentList "--smoke-test" -PassThru
if (-not $process.WaitForExit(15000)) {
    Stop-Process -Id $process.Id -Force
    throw "RadialMenu.exe 在 15 秒内没有完成启动冒烟测试。"
}

if ($process.ExitCode -ne 0) {
    throw "RadialMenu.exe 异常退出，退出码：$($process.ExitCode)"
}

if (-not (Test-Path $logPath)) {
    throw "没有生成启动日志：$logPath"
}

$stream = [System.IO.File]::Open($logPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
try {
    [void]$stream.Seek($oldLogLength, [System.IO.SeekOrigin]::Begin)
    $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8, $true)
    $newLog = $reader.ReadToEnd()
    $reader.Dispose()
}
finally {
    $stream.Dispose()
}

if ($newLog -notmatch "低级鼠标钩子安装成功") {
    throw "新日志中没有找到‘低级鼠标钩子安装成功’。"
}

if ($newLog -notmatch "低级鼠标钩子已卸载") {
    throw "新日志中没有找到‘低级鼠标钩子已卸载’。"
}

Write-Host "阶段 1 Windows 冒烟测试通过：入口点存在，程序成功启动，钩子安装并正常卸载。"
