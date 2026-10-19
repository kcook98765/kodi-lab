# Run as the ordinary Windows user whose Ubuntu distribution owns the checkout.
[CmdletBinding()]
param(
    [ValidateSet('install', 'start', 'stop', 'status')]
    [string]$Action = 'install',
    [string]$Distribution = 'Ubuntu-24.04',
    [string]$InstallDirectory,
    [string]$CheckoutPath,
    [string]$Revision,
    [string]$RepositoryAddress,
    [string]$RepositoryBaseUrl,
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
function Read-WslText([string[]]$CommandArguments) {
    $Text = & wsl.exe --distribution $Distribution --exec @CommandArguments
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the selected Ubuntu distribution.' }
    return ($Text -join "`n").Trim()
}
if (-not $InstallDirectory) {
    $LinuxHome = Read-WslText -CommandArguments @('printenv', 'HOME')
    if (-not $LinuxHome.StartsWith('/') -or $LinuxHome -eq '/') {
        throw 'The selected Ubuntu distribution did not return a Linux home directory.'
    }
    $InstallDirectory = $LinuxHome.TrimEnd('/') + '/kodi-lab-install'
}
# Pass arguments directly to wsl.exe --exec, never through a shell expression.
# Absolute Linux paths avoid assumptions about Windows/WSL user names or cwd.
foreach ($PathValue in @($InstallDirectory, $CheckoutPath)) {
    if ($PathValue -and (-not $PathValue.StartsWith('/') -or $PathValue.StartsWith('/mnt/'))) {
        throw 'Use absolute Linux filesystem paths, such as /home/you/kodi-lab; not C:\ or /mnt/c.'
    }
}
if ($Revision -and $Revision -cnotmatch '^[0-9a-f]{40}$') {
    throw '-Revision must be a full lowercase Git commit ID.'
}
if ($Revision -and ($CheckoutPath -or $Action -ne 'install')) {
    throw '-Revision applies only to installation without -CheckoutPath.'
}
$TemporaryInstaller = $null
$LinuxAction = $Action
if ($Action -eq 'install' -and -not $CheckoutPath) {
    # Resolve once, then download and fetch exactly the same commit in WSL.
    if (-not $Revision) {
        $Remote = Read-WslText -CommandArguments @('git', 'ls-remote', 'https://github.com/kcook98765/kodi-lab.git', 'refs/heads/main')
        $Revision = ($Remote -split '\s+')[0]
        if ($Revision -cnotmatch '^[0-9a-f]{40}$') { throw 'Could not resolve the source commit.' }
    }
    $TemporaryInstaller = New-TemporaryFile
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/kcook98765/kodi-lab/$Revision/scripts/install.py" -OutFile $TemporaryInstaller.FullName
        $Installer = Read-WslText -CommandArguments @('wslpath', '-u', '--', $TemporaryInstaller.FullName)
    } catch {
        Remove-Item -LiteralPath $TemporaryInstaller.FullName -Force
        throw
    }
    $LinuxAction = 'bootstrap'
} elseif ($Action -eq 'install') {
    $Installer = $CheckoutPath.TrimEnd('/') + '/scripts/install.py'
} else {
    $Installer = $InstallDirectory.TrimEnd('/') + '/kodi-lab.py'
}
$WslArguments = @(
    '--distribution', $Distribution, '--exec', 'python3', $Installer,
    $LinuxAction, '--directory', $InstallDirectory
)
if ($Action -eq 'install') {
    if ($LinuxAction -eq 'bootstrap') { $WslArguments += @('--revision', $Revision) }
    $WslArguments += @(
        '--repository-port', [string]$RepositoryPort,
        '--repository-local-port', [string]$RepositoryLocalPort,
        '--web-port', [string]$WebPort
    )
    if ($RepositoryAddress) { $WslArguments += @('--repository-bind', $RepositoryAddress) }
    if ($RepositoryBaseUrl) { $WslArguments += @('--repository-base-url', $RepositoryBaseUrl) }
}
try {
    & wsl.exe @WslArguments
    if ($LASTEXITCODE -ne 0) { throw "Kodi Lab $Action failed (WSL exit $LASTEXITCODE)." }
} finally {
    if ($TemporaryInstaller) { Remove-Item -LiteralPath $TemporaryInstaller.FullName -Force }
}
