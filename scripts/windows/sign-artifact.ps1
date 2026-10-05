param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ArtifactPath
)

$ErrorActionPreference = 'Stop'

if (-not $env:SIGNING_CERT_THUMBPRINT) {
    throw 'The production signing certificate is not installed in the runner certificate store.'
}

$signtool = Get-Command signtool.exe -ErrorAction Stop
$arguments = @(
    'sign',
    '/sha1', $env:SIGNING_CERT_THUMBPRINT,
    '/s', 'My',
    '/fd', 'sha256',
    '/tr', 'http://timestamp.digicert.com',
    '/td', 'sha256',
    '/v',
    (Resolve-Path -LiteralPath $ArtifactPath).Path
)

& $signtool.Source @arguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}