[CmdletBinding()]
param(
    [string]$ParentDirectory = (Get-Location).Path,
    [string]$PackageDirectory,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'
if (-not $VerifyOnly -and ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem)) {
    throw 'Run the installer on 64-bit Windows.'
}
$ownerRepo = 'ktsAE/iiko-intelligence'
$tag = 'v0.5.1'
$archiveName = 'iiko-intelligence-windows-x64.zip'
$modulus = '2vMeeuov28EUNJvCPV61TzpEKP+8nfs/7xr1H+6vijf0itjas8y1cYY8TEEIKmpseCwfx2nruYScEc64zZG8XjvMHZUrPo9MX3XzN0gmOUkY3xvHOW1cbQeY3zOt94yXih1v0HTWH3SNv6xmnzqSE355ZBv3HTFM0zwIBrMmAFDS8J2p+TDUcWO9icCmDR/OFWBAX4al54cVwFyiwN9aXn/IrBQfEP6GbhTyc0HPigby3FS/DMjwEsOskhj4xyXV8T2KcpycXMv6hamwb2QcJdQC5JZaRDvVoPX8d4HipbWm4M5Mzugm1BWUMVe2MirdVjdlRD97kD/k09GLVYPPBe2PrBWHSVWpqkl80dY+xb2IH/8COrrHZT0+kTJl+SrzwIMa3miq2LZJbKAmyJ+hqttTUInwpoYa0CkiLWJUD9MVUGOjwZKDc29fsbNz+2bLiGCD0bf3W8anJ+gV0SI4Qy5cA6GlEf5Bn7MQuyL3XO/yWEpyMbkXCeQmPXGkQwJr'
$parent = (Get-Item -LiteralPath $ParentDirectory).FullName
$destination = Join-Path $parent 'iiko-intelligence'
$marker = Join-Path $destination '.iiko-release'

function Receive-Asset([string]$Name, [string]$Target, [long]$Limit) {
    if ($PackageDirectory) {
        $source = Get-Item -LiteralPath (Join-Path $PackageDirectory $Name)
        if ($source.Length -gt $Limit) { throw 'Unexpected release asset size.' }
        [IO.File]::Copy($source.FullName, $Target)
        return
    }
    $uri = [Uri]"https://github.com/$ownerRepo/releases/download/$tag/$Name"
    for ($i = 0; $i -lt 6; $i++) {
        if ($uri.Scheme -ne 'https' -or $uri.Port -ne 443 -or $uri.UserInfo -or
            $uri.Host -notin @('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')) {
            throw 'Unexpected release download location.'
        }
        $request = [Net.HttpWebRequest]::Create($uri)
        $request.AllowAutoRedirect = $false
        $request.Timeout = 45000
        $request.ReadWriteTimeout = 45000
        $request.UserAgent = 'iiko-intelligence-installer'
        $response = $request.GetResponse()
        try {
            if ([int]$response.StatusCode -in @(301,302,303,307,308)) {
                $uri = [Uri]::new($uri, $response.Headers['Location'])
                continue
            }
            if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -gt $Limit) {
                throw 'Unexpected release response.'
            }
            $stream = $response.GetResponseStream()
            $file = [IO.File]::Open($Target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                $buf = New-Object byte[] 65536
                while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) {
                    if ($file.Position + $n -gt $Limit) { throw 'Release exceeded expected size.' }
                    $file.Write($buf, 0, $n)
                }
            } finally { $file.Dispose(); $stream.Dispose() }
            return
        } finally { $response.Dispose() }
    }
    throw 'Too many release redirects.'
}

function Get-SignedRecord([string]$Path) {
    $raw = [IO.File]::ReadAllBytes($Path)
    if ($raw.Length -gt 1024) { throw 'Invalid signature asset.' }
    $lines = [Text.Encoding]::ASCII.GetString($raw).Split([char]10)
    if ($lines.Count -ne 9 -or $lines[8] -cne '' -or $lines[0] -cne 'iiko-manager-windows-release-v1' -or
        $lines[1] -cne $ownerRepo -or $lines[2] -cne $tag -or $lines[3] -cne $archiveName -or
        $lines[4] -cnotmatch '^[1-9][0-9]{0,8}$' -or [long]$lines[4] -gt 167772160 -or
        $lines[5] -cnotmatch '^[0-9a-f]{64}$' -or $lines[6] -cnotmatch '^[0-9a-f]{64}$' -or
        $lines[7] -cnotmatch '^[A-Za-z0-9+/]{512}$') { throw 'Unexpected signed release identity.' }
    $body = [Text.Encoding]::ASCII.GetBytes(($lines[0..6] -join [char]10) + [char]10)
    $rsa = [Security.Cryptography.RSA]::Create()
    try {
        $pub = New-Object Security.Cryptography.RSAParameters
        $pub.Modulus = [Convert]::FromBase64String($modulus)
        $pub.Exponent = [byte[]](1,0,1)
        $rsa.ImportParameters($pub)
        if (-not $rsa.VerifyData($body, [Convert]::FromBase64String($lines[7]),
            [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)) {
            throw 'Publisher signature verification failed.'
        }
    } finally { $rsa.Dispose() }
    return @{ Size = [long]$lines[4]; Digest = $lines[5] }
}

if (-not $VerifyOnly -and (Test-Path -LiteralPath $destination)) {
    if ((Test-Path -LiteralPath $marker -PathType Leaf) -and [IO.File]::ReadAllText($marker).Trim() -ceq "$ownerRepo $tag") {
        Write-Output "Existing installation marker found in $destination. No existing code was executed or verified."
        return
    }
    throw "Choose an empty parent directory for the first signed installation. See INSTALL.md for migrating older data."
}

$stage = Join-Path $parent ('.iiko-install-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $stage
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $signature = Join-Path $stage 'publisher.sig'
    Receive-Asset "$archiveName.sig" $signature 1024
    $record = Get-SignedRecord $signature
    $archive = Join-Path $stage $archiveName
    Receive-Asset $archiveName $archive $record.Size
    if ((Get-Item -LiteralPath $archive).Length -ne $record.Size -or
        (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $record.Digest) {
        throw 'Package differs from the signed release.'
    }
    if ($VerifyOnly) { Write-Output "Verified publisher signature and package for $tag."; return }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $stage)
    $app = Join-Path $stage 'iiko-intelligence'
    if (-not (Test-Path -LiteralPath (Join-Path $app 'Setup-iiko.cmd') -PathType Leaf)) { throw 'Missing setup command.' }
    [IO.File]::WriteAllText((Join-Path $app '.iiko-release'), "$ownerRepo $tag")
    [IO.Directory]::Move($app, $destination)
} finally { Remove-Item -LiteralPath $stage -Recurse -Force }
Push-Location -LiteralPath $destination
try {
    & .\Setup-iiko.cmd /quiet
    if ($LASTEXITCODE -ne 0) { throw "Setup verification failed. Run Setup-iiko.cmd again in $destination." }
} finally { Pop-Location }
Write-Output "Open this folder in local Codex and choose Trust: $destination"
