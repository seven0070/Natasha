param(
    [switch]$VerifyOnly,
    [string]$ArtifactsDirectory
)

$ErrorActionPreference = 'Stop'

$buildArgs = @('build', '-b', 'nsis', '-b', 'msi')
$isRelease = $VerifyOnly -or $env:GITHUB_REF -like 'refs/tags/v*'

if (-not $isRelease -and -not $VerifyOnly) {
    $buildArgs += '--no-sign'
    & npx --yes @tauri-apps/cli @buildArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    exit 0
}

if (-not $env:WINDOWS_SIGNING_CERTIFICATE -or -not $env:WINDOWS_SIGNING_PASSWORD) {
    throw 'Production Authenticode certificate unavailable. Release signing cannot be finalized until a trusted certificate is provided.'
}
if (-not $env:WINDOWS_SIGNING_PUBLISHER) {
    throw 'WINDOWS_SIGNING_PUBLISHER must be configured as a repository variable.'
}

$certificatePath = Join-Path $env:RUNNER_TEMP 'natasha-windows-signing.pfx'
$certificateThumbprint = $null
$certificate = $null

function Assert-TrustedChain {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate,
        [Parameter(Mandatory = $true)]
        [string]$Purpose
    )

    $chain = [System.Security.Cryptography.X509Certificates.X509Chain]::new()
    $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::Online
    $chain.ChainPolicy.RevocationFlag = [System.Security.Cryptography.X509Certificates.X509RevocationFlag]::EntireChain
    $chain.ChainPolicy.UrlRetrievalTimeout = [TimeSpan]::FromSeconds(15)
    try {
        if (-not $chain.Build($Certificate)) {
            $statuses = ($chain.ChainStatus | ForEach-Object { $_.Status.ToString() }) -join ', '
            throw "$Purpose certificate chain is not trusted: $statuses"
        }
        if ($chain.ChainElements.Count -lt 2) {
            throw "$Purpose certificate chain does not terminate at a trusted issuing CA."
        }
    }
    finally {
        $chain.Dispose()
    }
}

function Assert-AuthenticodeArtifact {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedThumbprint,
        [switch]$AllowSigning
    )

    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -eq [System.Management.Automation.SignatureStatus]::NotSigned -and $AllowSigning) {
        & (Join-Path $env:GITHUB_WORKSPACE 'scripts\windows\sign-artifact.ps1') $Path
        if ($LASTEXITCODE -ne 0) {
            throw "Signing failed for $([IO.Path]::GetFileName($Path))."
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $Path
    }

    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Authenticode verification failed for $([IO.Path]::GetFileName($Path)): $($signature.Status)"
    }
    if ($signature.SignerCertificate.Thumbprint -ne $ExpectedThumbprint) {
        throw "Unexpected Authenticode signer for $([IO.Path]::GetFileName($Path))."
    }
    if (-not $signature.TimeStamperCertificate) {
        throw "No trusted timestamp was found for $([IO.Path]::GetFileName($Path))."
    }

    & signtool.exe verify /pa /all /v $Path
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool verification failed for $([IO.Path]::GetFileName($Path))."
    }
    Assert-TrustedChain -Certificate $signature.SignerCertificate -Purpose 'Signer'
    Assert-TrustedChain -Certificate $signature.TimeStamperCertificate -Purpose 'Timestamp'
    Write-Host "Verified Authenticode signature: $([IO.Path]::GetFileName($Path))"
}

try {
    $certificateBytes = [Convert]::FromBase64String($env:WINDOWS_SIGNING_CERTIFICATE)
    [IO.File]::WriteAllBytes($certificatePath, $certificateBytes)
    $securePassword = ConvertTo-SecureString $env:WINDOWS_SIGNING_PASSWORD -AsPlainText -Force
    $certificate = Import-PfxCertificate -FilePath $certificatePath `
        -CertStoreLocation 'Cert:\CurrentUser\My' -Password $securePassword
    if (-not $certificate -or -not $certificate.HasPrivateKey) {
        throw 'The configured signing certificate does not include an importable private key.'
    }
    $certificateThumbprint = $certificate.Thumbprint
    $env:SIGNING_CERT_THUMBPRINT = $certificateThumbprint

    if ($certificate.Subject -eq $certificate.Issuer) {
        throw 'Self-signed certificates cannot be used for production release signing.'
    }
    if ($certificate.Subject -cne $env:WINDOWS_SIGNING_PUBLISHER) {
        throw 'The certificate subject does not match WINDOWS_SIGNING_PUBLISHER.'
    }
    $now = [DateTime]::UtcNow
    if ($now -lt $certificate.NotBefore.ToUniversalTime() -or $now -gt $certificate.NotAfter.ToUniversalTime()) {
        throw 'The production signing certificate is not currently valid.'
    }
    $codeSigningEku = $certificate.Extensions |
        Where-Object { $_.Oid.Value -eq '2.5.29.37' } |
        ForEach-Object { $_.EnhancedKeyUsages } |
        Where-Object { $_.Value -eq '1.3.6.1.5.5.7.3.3' }
    if (-not $codeSigningEku) {
        throw 'The certificate does not include the Code Signing EKU.'
    }
    Assert-TrustedChain -Certificate $certificate -Purpose 'Production signing'

    $windowsKits = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits\10\bin'
    $signtoolPath = Get-ChildItem -LiteralPath $windowsKits -Directory -ErrorAction Stop |
        Sort-Object { [version]$_.Name } -Descending |
        ForEach-Object { Join-Path $_.FullName 'x64\signtool.exe' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if (-not $signtoolPath) {
        throw 'Windows SDK x64 signtool.exe was not found on the GitHub runner.'
    }
    $signtoolDirectory = Split-Path -Parent $signtoolPath
    $env:PATH = "$signtoolDirectory;$env:PATH"
    Add-Content -LiteralPath $env:GITHUB_PATH -Value $signtoolDirectory
    & npx --yes @tauri-apps/cli @buildArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri Windows build failed with exit code $LASTEXITCODE."
    }

    if ($VerifyOnly) {
        if (-not $ArtifactsDirectory) {
            throw 'ArtifactsDirectory is required when VerifyOnly is specified.'
        }
        $artifactRoot = (Resolve-Path -LiteralPath $ArtifactsDirectory).Path
        $artifacts = @(
            (Join-Path $artifactRoot 'natasha.exe'),
            (Join-Path $artifactRoot 'Natasha_0.9.1_x64-setup.exe'),
            (Join-Path $artifactRoot 'Natasha_0.9.1_x64_en-US.msi')
        )
    }
    else {
        $artifacts = @(
            (Join-Path $env:GITHUB_WORKSPACE 'src-tauri\target\release\natasha.exe'),
            (Join-Path $env:GITHUB_WORKSPACE 'src-tauri\target\release\bundle\nsis\Natasha_0.9.1_x64-setup.exe'),
            (Join-Path $env:GITHUB_WORKSPACE 'src-tauri\target\release\bundle\msi\Natasha_0.9.1_x64_en-US.msi')
        )
    }
    foreach ($artifact in $artifacts) {
        if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
            throw "Expected release artifact was not produced: $([IO.Path]::GetFileName($artifact))"
        }
        Assert-AuthenticodeArtifact -Path $artifact -ExpectedThumbprint $certificateThumbprint -AllowSigning:(-not $VerifyOnly)
    }

    $installDirectory = Join-Path $env:RUNNER_TEMP 'NatashaInstalled'
    $installer = Start-Process -FilePath $artifacts[1] -ArgumentList @('/S', "/D=$installDirectory") -Wait -PassThru
    if ($installer.ExitCode -ne 0) {
        throw "NSIS installation failed with exit code $($installer.ExitCode)."
    }
    $installedExecutable = Get-ChildItem -LiteralPath $installDirectory -Filter 'natasha.exe' -File -Recurse |
        Select-Object -First 1
    if (-not $installedExecutable) {
        throw 'The NSIS installer did not install natasha.exe at the expected location.'
    }
    Assert-AuthenticodeArtifact -Path $installedExecutable.FullName -ExpectedThumbprint $certificateThumbprint
}
finally {
    $env:WINDOWS_SIGNING_PASSWORD = $null
    $env:WINDOWS_SIGNING_CERTIFICATE = $null
    $env:SIGNING_CERT_THUMBPRINT = $null
    if ($certificate) {
        $certificate.Dispose()
    }
    if ($certificateThumbprint) {
        Remove-Item -LiteralPath "Cert:\CurrentUser\My\$certificateThumbprint" -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $certificatePath) {
        Remove-Item -LiteralPath $certificatePath -Force
    }
}