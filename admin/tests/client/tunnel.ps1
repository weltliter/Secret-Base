$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Import-Module (Join-Path $projectRoot 'client/Tunnel.psm1') -Force
$fixtureRoot = Join-Path $projectRoot ('.local/checks/client-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($fixtureRoot)
$firstKey = Join-Path $fixtureRoot 'first.pem'
$secondKey = Join-Path $fixtureRoot 'second.pem'
[IO.File]::WriteAllText($firstKey, 'synthetic SSH fixture one; not a real key')
[IO.File]::WriteAllText($secondKey, 'synthetic SSH fixture two; not a real key')
Set-VaultClientConfig -ProjectRoot $fixtureRoot -Server 'ecs-one.example.invalid' -UserName 'root' -KeyPath $firstKey | Out-Null

foreach ($port in @(9999, 0, 65536)) {
    $rejected = $false
    try { Set-VaultClientConfig -ProjectRoot $fixtureRoot -Server fixture.example.invalid -UserName root -KeyPath $firstKey -LocalPort $port | Out-Null }
    catch { $rejected = $true }
    if (-not $rejected) { throw "Invalid local port $port was accepted." }
}
Set-VaultClientConfig -ProjectRoot $fixtureRoot -Server fixture.example.invalid -UserName root -KeyPath $firstKey -LocalPort 18081 -SshPort 2222 | Out-Null
$config = Get-VaultClientConfig -ProjectRoot $fixtureRoot
if ($config.LocalPort -ne 18081 -or $config.RemotePort -ne 18080 -or $config.SshPort -ne 2222) {
    throw 'Changing the local port unexpectedly changed the remote port.'
}
Set-VaultClientConfig -ProjectRoot $fixtureRoot -Server 'ecs-two.example.invalid' -UserName 'ubuntu' -KeyPath $secondKey | Out-Null
$replacement = Get-VaultClientConfig -ProjectRoot $fixtureRoot
if ($replacement.Server -ne 'ecs-two.example.invalid' -or $replacement.KeyPath -ne $secondKey) {
    throw 'New ECS connection did not replace only the local connection settings.'
}
if (Test-Path -LiteralPath (Join-Path $fixtureRoot 'data')) { throw 'SSH configuration unexpectedly created application state.' }

$configFile = Join-Path $fixtureRoot 'config.env'
$validText = [IO.File]::ReadAllText($configFile)
$originalEnv = [Environment]::GetEnvironmentVariable('VAULT_SSH_HOST', 'Process')
try {
    [Environment]::SetEnvironmentVariable('VAULT_SSH_HOST', 'inherited.invalid', 'Process')
    if ((Get-VaultClientConfig -ProjectRoot $fixtureRoot).Server -ne 'ecs-two.example.invalid') { throw 'Inherited environment overrode config.env.' }
} finally { [Environment]::SetEnvironmentVariable('VAULT_SSH_HOST', $originalEnv, 'Process') }
$invalidTexts = @(
    $validText + "VAULT_SSH_HOST=duplicate.invalid`n",
    $validText + "UNKNOWN_KEY=fixture-secret-not-echoed`n",
    $validText.Replace('VAULT_SSH_PORT=22', 'VAULT_SSH_PORT=0'),
    $validText.Replace('VAULT_SSH_PORT=22', 'VAULT_SSH_PORT=fixture-secret-not-echoed'),
    $validText.Replace('VAULT_SSH_HOST=ecs-two.example.invalid', 'VAULT_SSH_HOST=$(fixture-secret-not-echoed)'),
    $validText.Replace('VAULT_SSH_USER=ubuntu', 'VAULT_SSH_USER='),
    ($validText -replace '(?m)^VAULT_REMOTE_PORT=.*\r?\n', '')
)
foreach ($invalidText in $invalidTexts) {
    [IO.File]::WriteAllText($configFile, $invalidText)
    $rejected = $false
    try { Get-VaultClientConfig -ProjectRoot $fixtureRoot | Out-Null }
    catch {
        $rejected = $true
        if ($_.Exception.Message.Contains('fixture-secret-not-echoed')) { throw 'Config diagnostic exposed a value.' }
    }
    if (-not $rejected) { throw 'Invalid configuration was accepted.' }
}
[IO.File]::WriteAllText($configFile, $validText)
if (Test-Path -LiteralPath (Join-Path $fixtureRoot '.local/console.psd1')) { throw 'Legacy second config created.' }

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
Write-Output 'Client checks passed: single config, strict parsing, private diagnostics, SSH/forward ports, environment isolation and listener preservation.'
