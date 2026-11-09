[CmdletBinding()]
param(
    [ValidateSet('Connect', 'Open', 'Status', 'Check', 'Prepare', 'Configure')]
    [string]$Action = 'Connect',
    [ValidateRange(10000, 65535)]
    [int]$LocalPort,
    [string]$Server,
    [string]$UserName,
    [string]$KeyPath,
    [ValidateRange(1, 65535)][int]$RemotePort = 18080
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'client/Tunnel.psm1') -Force
if ($Action -eq 'Configure') {
    if (-not $Server) { $Server = Read-Host 'New ECS address or hostname' }
    if (-not $UserName) { $UserName = Read-Host 'SSH username' }
    if (-not $KeyPath) { $KeyPath = Read-Host 'Local path to the new ECS private key' }
    $configuration = @{ ProjectRoot = $PSScriptRoot; Server = $Server; UserName = $UserName; KeyPath = $KeyPath; RemotePort = $RemotePort }
    if ($PSBoundParameters.ContainsKey('LocalPort')) { $configuration.LocalPort = $LocalPort }
    Set-VaultClientConfig @configuration
    return
}
if ($PSBoundParameters.ContainsKey('Server') -or $PSBoundParameters.ContainsKey('UserName') -or $PSBoundParameters.ContainsKey('KeyPath') -or $PSBoundParameters.ContainsKey('RemotePort')) {
    throw 'Connection settings can only be changed with -Action Configure.'
}
$options = @{ ProjectRoot = $PSScriptRoot }
if ($PSBoundParameters.ContainsKey('LocalPort')) { $options.LocalPort = $LocalPort }
$config = Get-VaultClientConfig @options
$url = 'http://127.0.0.1:{0}' -f $config.LocalPort

switch ($Action) {
    'Check' {
        [PSCustomObject]@{
            ConsoleUrl = $url
            PortAvailable = Test-VaultLocalPort -LocalPort $config.LocalPort
            OpenSSHInstalled = [bool](Get-Command ssh -ErrorAction SilentlyContinue)
            KeyPresent = Test-Path -LiteralPath $config.KeyPath -PathType Leaf
            Server = $config.Server
        }
    }
    'Status' { Get-VaultConsoleStatus -LocalPort $config.LocalPort }
    'Prepare' { New-VaultSshSession -ProjectRoot $PSScriptRoot -Config $config }
    { $_ -in 'Connect', 'Open' } {
        if (-not (Test-VaultLocalPort -LocalPort $config.LocalPort)) {
            if ($Action -eq 'Open') {
                # Only reuse a listener that responds as the vault service.
                Get-VaultConsoleStatus -LocalPort $config.LocalPort | Out-Null
                Write-Host "Opening $url using the existing tunnel."
                Start-Process $url
                return
            }
            throw "Local port $($config.LocalPort) is already in use. Use -Action Status to check an existing tunnel, or choose -LocalPort 18081. No process was stopped."
        }
        $ssh = Get-Command ssh -ErrorAction Stop
        $session = New-VaultSshSession -ProjectRoot $PSScriptRoot -Config $config
        Write-Host "Console: $url. Keep this window open while using the page."
        Write-Host 'Press Ctrl+C to close this SSH tunnel.'
        $arguments = @(
            '-N', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3', '-o', 'StrictHostKeyChecking=accept-new',
            '-o', "UserKnownHostsFile=$($session.KnownHostsPath)",
            '-i', $session.KeyPath,
            '-L', "127.0.0.1:$($config.LocalPort):127.0.0.1:$($config.RemotePort)",
            "$($config.UserName)@$($config.Server)"
        )
        if ($Action -eq 'Connect') {
            & $ssh.Source @arguments
            if ($LASTEXITCODE -ne 0) { throw "SSH tunnel exited with code $LASTEXITCODE." }
            return
        }

        # Keep the launcher window as the owner; the SSH helper needs no window.
        $startInfo = [Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $ssh.Source
        # Windows PowerShell 5.1 has no ProcessStartInfo.ArgumentList.
        $startInfo.Arguments = ($arguments | ForEach-Object { '"{0}"' -f $_ }) -join ' '
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardError = $true
        $tunnel = [Diagnostics.Process]::Start($startInfo)
        $sshErrors = $tunnel.StandardError.ReadToEndAsync()
        try {
            Write-Host 'Connecting and waiting for the management page...'
            $deadline = [DateTime]::UtcNow.AddSeconds(30)
            $ready = $false
            while (-not $tunnel.HasExited -and [DateTime]::UtcNow -lt $deadline) {
                try {
                    Get-VaultConsoleStatus -LocalPort $config.LocalPort | Out-Null
                    $ready = $true
                    break
                } catch { Start-Sleep -Milliseconds 300 }
            }
            if ($tunnel.HasExited) {
                throw "SSH tunnel exited with code $($tunnel.ExitCode). $($sshErrors.GetAwaiter().GetResult())"
            }
            if (-not $ready) {
                throw "The management page did not respond within 30 seconds. Check the server and try again."
            }
            Start-Process $url
            Write-Host 'The management page is open. Press Ctrl+C here when finished.'
            while (-not $tunnel.WaitForExit(500)) { }
            if ($tunnel.ExitCode -ne 0) {
                throw "SSH tunnel exited with code $($tunnel.ExitCode). $($sshErrors.GetAwaiter().GetResult())"
            }
        } finally {
            if (-not $tunnel.HasExited) { $tunnel.Kill(); $tunnel.WaitForExit() }
            $tunnel.Dispose()
        }
    }
}
