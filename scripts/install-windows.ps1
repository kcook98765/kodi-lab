# Run as the ordinary Windows user whose Ubuntu distribution owns the checkout.
[CmdletBinding()]
param(
    [ValidateSet('install', 'start', 'stop', 'status')]
    [string]$Action = 'install',
    [string]$Distribution = 'Ubuntu-24.04',
    [Parameter(Mandatory = $true)]
    [string]$InstallDirectory,
    [string]$CheckoutPath,
    [string]$RepositoryAddress,
    [ValidateRange(1024, 65535)]
    [int]$RepositoryPort = 18120,
    [ValidateRange(1024, 65535)]
    [int]$RepositoryLocalPort = 18121,
    [ValidateRange(1024, 65535)]
    [int]$WebPort = 18100
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'Install WSL2 and Ubuntu 24.04 first; see docs/WINDOWS-WSL2-INSTALL.md.'
}
# Pass arguments directly to wsl.exe --exec, never through a shell expression.
# Absolute Linux paths avoid assumptions about Windows/WSL user names or cwd.
foreach ($PathValue in @($InstallDirectory, $CheckoutPath)) {
    if ($PathValue -and (-not $PathValue.StartsWith('/') -or $PathValue.StartsWith('/mnt/'))) {
        throw 'Use absolute Linux filesystem paths, such as /home/you/kodi-lab; not C:\ or /mnt/c.'
    }
}
if ($Action -eq 'install') {
    if (-not $CheckoutPath) { throw 'Installation requires -CheckoutPath in Ubuntu.' }
    $Installer = $CheckoutPath.TrimEnd('/') + '/scripts/install.py'
} else {
    $Installer = $InstallDirectory.TrimEnd('/') + '/kodi-lab.py'
}
$WslArguments = @(
    '--distribution', $Distribution, '--exec', 'python3', $Installer,
    $Action, '--directory', $InstallDirectory
)
if ($Action -eq 'install') {
    $WslArguments += @(
        '--repository-port', [string]$RepositoryPort,
        '--repository-local-port', [string]$RepositoryLocalPort,
        '--web-port', [string]$WebPort
    )
    if ($RepositoryAddress) { $WslArguments += @('--repository-bind', $RepositoryAddress) }
}
& wsl.exe @WslArguments
if ($LASTEXITCODE -ne 0) { throw "Kodi Lab $Action failed (WSL exit $LASTEXITCODE)." }
