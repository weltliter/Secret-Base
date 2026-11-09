$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Import-Module (Join-Path $projectRoot 'client/Tunnel.psm1') -Force
$fixtureRoot = Join-Path $projectRoot ('.local/checks/client-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($fixtureRoot)
$fixtureKey = Join-Path $fixtureRoot 'fixture.pem'
[IO.File]::WriteAllText($fixtureKey, 'synthetic SSH fixture')
Set-VaultClientConfig -ProjectRoot $fixtureRoot -Server 'fixture.example.invalid' -UserName 'fixture' -KeyPath $fixtureKey | Out-Null

foreach ($port in @(9999, 0, 65536)) {
    $rejected = $false
    try { Get-VaultClientConfig -ProjectRoot $fixtureRoot -LocalPort $port | Out-Null }
    catch { $rejected = $true }
    if (-not $rejected) { throw "Invalid local port $port was accepted." }
}
$config = Get-VaultClientConfig -ProjectRoot $fixtureRoot -LocalPort 18081
if ($config.LocalPort -ne 18081 -or $config.RemotePort -ne 18080) {
    throw 'Changing the local port unexpectedly changed the remote port.'
}

$listener = $null
foreach ($candidate in 58100..58109) {
    $probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $candidate)
    $probe.ExclusiveAddressUse = $true
    try {
        $probe.Start()
        $listener = $probe
        $port = $candidate
        break
    } catch [Net.Sockets.SocketException] { $probe.Stop() }
}
if ($null -eq $listener) { throw 'No free five-digit port for the client check.' }
try {
    if (Test-VaultLocalPort -LocalPort $port) { throw 'An occupied local port was reported available.' }
    # The check must not stop or replace the process already holding this port.
    $connection = [Net.Sockets.TcpClient]::new()
    try {
        $connection.Connect('127.0.0.1', $port)
        if (-not $listener.Pending()) { throw 'Existing listener was disrupted.' }
        $accepted = $listener.AcceptTcpClient()
        $accepted.Dispose()
    } finally { $connection.Dispose() }
} finally { $listener.Stop() }
if (-not (Test-VaultLocalPort -LocalPort $port)) { throw 'Released local port is still reported occupied.' }
Write-Output 'Client checks passed: port range, independent remote port, conflict detection, existing listener preserved.'
