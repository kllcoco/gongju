param([string]$BundlePath)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot
$required=@('toolchain/node.exe','toolchain/node-sdk/include/node/node_api.h','toolchain/node-sdk/Release/node.lib')
$missing=@($required | Where-Object {-not (Test-Path -LiteralPath (Join-Path $root $_))})
if(-not $missing.Count){return}
if(-not $BundlePath){$BundlePath=Join-Path (Split-Path $root) 'SnowLuma-v2.4.0-native-recovery.zip'}
if(-not (Test-Path -LiteralPath $BundlePath)){throw 'Missing bundled Node SDK. Extract the complete native-recovery ZIP, or place that ZIP and its .sha256 beside this source directory.'}
$checksumPath="$BundlePath.sha256"
if(-not (Test-Path -LiteralPath $checksumPath)){throw 'The bundle SHA-256 file is required.'}
$expected=((Get-Content -LiteralPath $checksumPath -Raw).Trim() -split '\s+')[0]
if($expected -notmatch '^[0-9a-fA-F]{64}$' -or (Get-FileHash -LiteralPath $BundlePath -Algorithm SHA256).Hash -ne $expected){throw 'Bundle SHA-256 mismatch.'}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive=[IO.Compression.ZipFile]::OpenRead((Resolve-Path -LiteralPath $BundlePath).Path)
$prefix='SnowLuma-v2.4.0-native-recovery/'
$destinationPrefix=[IO.Path]::GetFullPath((Join-Path $root 'toolchain'))+[IO.Path]::DirectorySeparatorChar
try {
    foreach($entry in $archive.Entries){
        if(-not $entry.FullName.StartsWith($prefix+'toolchain/',[StringComparison]::Ordinal) -or $entry.FullName.EndsWith('/')){continue}
        $relative=$entry.FullName.Substring($prefix.Length)
        $destination=[IO.Path]::GetFullPath((Join-Path $root $relative))
        if(-not $destination.StartsWith($destinationPrefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Invalid toolchain entry path.'}
        New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
        $inputStream=$entry.Open();$outputStream=[IO.File]::Create($destination)
        try{$inputStream.CopyTo($outputStream)}finally{$inputStream.Dispose();$outputStream.Dispose()}
    }
}finally{$archive.Dispose()}
foreach($file in $required){if(-not (Test-Path -LiteralPath (Join-Path $root $file))){throw "Bundle is missing $file"}}
