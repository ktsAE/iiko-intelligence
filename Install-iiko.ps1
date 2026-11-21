[CmdletBinding()]
param([string]$ParentDirectory = (Get-Location).Path)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
    throw 'Run the Windows x64 installer on a 64-bit Windows computer.'
}

$ownerRepo = 'ktsAE/iiko-intelligence'
$tag = 'v0.3.1'
$archiveName = 'iiko-intelligence-windows-x64.zip'
$parent = (Get-Item -LiteralPath $ParentDirectory).FullName
$destination = Join-Path $parent 'iiko-intelligence'
$marker = Join-Path $destination '.iiko-release'

if (Test-Path -LiteralPath $destination) {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf) -or
        [IO.File]::ReadAllText($marker).Trim() -ne "$ownerRepo $tag") {
        throw "The destination already exists: $destination. Choose another parent or follow WINDOWS.md to update an existing installation."
    }
} else {
    # Stage on the destination volume so concurrent or interrupted extracts cannot replace a manager's folder.
    $stage = Join-Path $parent ('.iiko-install-' + [Guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $stage
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$ownerRepo/releases/tags/$tag" -Headers @{
            'User-Agent' = 'iiko-intelligence-installer'
            'Accept' = 'application/vnd.github+json'
        }
        $assets = @($release.assets | Where-Object { $_.name -ceq $archiveName })
        if ($release.tag_name -cne $tag -or $assets.Count -ne 1 -or $assets[0].digest -notmatch '^sha256:[0-9a-f]{64}$') {
            throw 'GitHub did not return the expected release and SHA-256 digest.'
        }
        $archive = Join-Path $stage $archiveName
        $url = "https://github.com/$ownerRepo/releases/download/$tag/$archiveName"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive
        $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
        if ("sha256:$actual" -ne $assets[0].digest) { throw 'Downloaded archive failed SHA-256 verification.' }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $stage)
        $app = Join-Path $stage 'iiko-intelligence'
        if (-not (Test-Path -LiteralPath (Join-Path $app 'Setup-iiko.cmd') -PathType Leaf)) {
            throw 'The Windows setup command is missing from the archive.'
        }
        [IO.File]::WriteAllText((Join-Path $app '.iiko-release'), "$ownerRepo $tag")
        [IO.Directory]::Move($app, $destination)
    } finally {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}

Push-Location -LiteralPath $destination
try {
    & .\Setup-iiko.cmd /quiet
    if ($LASTEXITCODE -ne 0) { throw "Setup verification failed. Run Setup-iiko.cmd again in $destination." }
} finally {
    Pop-Location
}
Write-Output "Open this folder in local Codex and choose Trust: $destination"
