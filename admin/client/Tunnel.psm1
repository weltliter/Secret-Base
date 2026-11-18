Set-StrictMode -Version Latest

function Get-VaultClientConfig {
    param([Parameter(Mandatory)][string]$ProjectRoot)
    $configPath = Join-Path $ProjectRoot 'config.env'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw 'Create config.env from config.example.env, or run .\console.ps1 -Action Configure.'
    }
    $item = Get-Item -LiteralPath $configPath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -gt 65536) {
        throw 'config.env must be a regular file no larger than 64 KiB.'
    }
    $names = @('VAULT_SSH_HOST','VAULT_SSH_USER','VAULT_SSH_PORT','VAULT_SSH_KEY_FILE','VAULT_LOCAL_PORT','VAULT_REMOTE_PORT')
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($configPath, [Text.Encoding]::UTF8)) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith('#')) { continue }
        if ($text -cnotmatch '^([A-Z][A-Z0-9_]*)\s*=(.*)$') { throw 'Invalid config.env syntax; use KEY=VALUE.' }
        $name, $value = $Matches[1], $Matches[2].Trim()
        if ($name -cnotin $names -or $values.ContainsKey($name)) { throw 'Unknown or duplicate config.env key.' }
        if ($value.StartsWith('"') -or $value.StartsWith("'")) {
            if ($value.Length -lt 2 -or $value[$value.Length-1] -ne $value[0]) { throw 'Invalid config.env quoting.' }
            $value = $value.Substring(1, $value.Length-2)
        }
        if (-not $value -or $value -match '[\x00-\x1f$`]') { throw 'Invalid config.env value; interpolation is not supported.' }
        $values[$name] = $value
    }
    foreach ($name in $names) { if (-not $values.ContainsKey($name)) { throw "Missing config.env key: $name" } }
    foreach ($name in @('VAULT_SSH_PORT','VAULT_LOCAL_PORT','VAULT_REMOTE_PORT')) {
        $number = 0
        if ($values[$name] -notmatch '^[0-9]+$' -or -not [int]::TryParse($values[$name], [ref]$number)) { throw "Invalid port: $name" }
        $minimum = if ($name -eq 'VAULT_LOCAL_PORT') { 10000 } else { 1 }
        if ($number -lt $minimum -or $number -gt 65535) { throw "Invalid port: $name" }
        $values[$name] = $number
    }
    if ($values.VAULT_SSH_HOST -notmatch '^[a-zA-Z0-9.-]+$' -or $values.VAULT_SSH_USER -notmatch '^[a-zA-Z0-9_-]+$') {
        throw 'Invalid SSH host or username in config.env.'
    }
    $keyPath = $values.VAULT_SSH_KEY_FILE
    if ([string]::IsNullOrWhiteSpace($keyPath) -or $keyPath -match '[\x00-\x1f"<>|$`]') { throw 'Invalid private key path in config.env.' }
    if (-not [IO.Path]::IsPathRooted($keyPath)) { $keyPath = Join-Path $ProjectRoot $keyPath }
    return @{ Server=$values.VAULT_SSH_HOST; UserName=$values.VAULT_SSH_USER; SshPort=$values.VAULT_SSH_PORT;
              KeyPath=$keyPath; LocalPort=$values.VAULT_LOCAL_PORT; RemotePort=$values.VAULT_REMOTE_PORT }
}

function Set-VaultClientConfig {
    param(
        [Parameter(Mandatory)][string]$ProjectRoot,
        [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9.-]+$')][string]$Server,
        [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$UserName,
        [Parameter(Mandatory)][string]$KeyPath,
        [ValidateRange(1, 65535)][int]$SshPort = 22,
        [ValidateRange(10000, 65535)][int]$LocalPort = 18080,
        [ValidateRange(1, 65535)][int]$RemotePort = 18080
    )
    if ([string]::IsNullOrWhiteSpace($KeyPath) -or $KeyPath -match '[\x00-\x1f"<>|$`]') { throw 'Invalid private key path.' }
    $absoluteKey = if ([IO.Path]::IsPathRooted($KeyPath)) { $KeyPath } else { Join-Path $ProjectRoot $KeyPath }
    if (-not (Test-Path -LiteralPath $absoluteKey -PathType Leaf)) { throw 'The specified SSH private key file does not exist.' }
    [void](New-Item -ItemType Directory -Force -Path $ProjectRoot)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $path = Join-Path $ProjectRoot 'config.env'
    if (-not (Test-Path -LiteralPath $path)) { [void](New-Item -ItemType File -Path $path) }
    Protect-PrivatePath $path $false $identity
    $content = "VAULT_SSH_HOST=$Server`r`nVAULT_SSH_USER=$UserName`r`nVAULT_SSH_PORT=$SshPort`r`nVAULT_SSH_KEY_FILE=`"$KeyPath`"`r`nVAULT_LOCAL_PORT=$LocalPort`r`nVAULT_REMOTE_PORT=$RemotePort`r`n"
    [IO.File]::WriteAllText($path, $content, [Text.UTF8Encoding]::new($false))
    Write-Output 'Connection settings saved to private config.env. SSH key contents are never stored in configuration.'
}

function Test-VaultLocalPort {
    param([Parameter(Mandatory)][ValidateRange(10000, 65535)][int]$LocalPort)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $LocalPort)
    $listener.ExclusiveAddressUse = $true
    try {
        $listener.Start(1)
        return $true
    } catch [Net.Sockets.SocketException] {
        if ($_.Exception.SocketErrorCode -eq [Net.Sockets.SocketError]::AddressAlreadyInUse) { return $false }
        throw
    } finally { $listener.Stop() }
}

function Protect-PrivatePath {
    param([string]$LiteralPath, [bool]$Directory, [Security.Principal.SecurityIdentifier]$Identity)
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.PSIsContainer -ne $Directory) {
        throw 'The SSH client path must be a regular file or directory, not a link.'
    }
    # Preserve owner and audit sections; only restrict the DACL of the user's copy.
    $acl = Get-Acl -LiteralPath $LiteralPath
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($acl.AreAccessRulesProtected -and $rules.Count -eq 1 -and
        $rules[0].IdentityReference.Value -eq $Identity.Value -and
        $rules[0].AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        $rules[0].FileSystemRights -eq [Security.AccessControl.FileSystemRights]::FullControl) { return }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) {
        [void]$acl.RemoveAccessRuleSpecific($rule)
    }
    if ($Directory) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($Identity, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow')
    } else {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($Identity, 'FullControl', 'Allow')
    }
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $LiteralPath -AclObject $acl
}

function New-VaultSshSession {
    param([Parameter(Mandatory)][string]$ProjectRoot, [Parameter(Mandatory)][hashtable]$Config)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $directory = Join-Path $ProjectRoot ('.local/ssh/' + $identity.Value)
    [void](New-Item -ItemType Directory -Force -Path $directory)
    Protect-PrivatePath $directory $true $identity
    $privateKey = Join-Path $directory 'tunnel-key.pem'
    $knownHosts = Join-Path $directory 'known_hosts'
    if (-not (Test-Path -LiteralPath $privateKey)) { [void](New-Item -ItemType File -Path $privateKey) }
    Protect-PrivatePath $privateKey $false $identity
    if (-not (Test-Path -LiteralPath $knownHosts)) {
        $seed = Join-Path $ProjectRoot '.local/server_known_hosts'
        if (Test-Path -LiteralPath $seed -PathType Leaf) { Copy-Item -LiteralPath $seed -Destination $knownHosts }
        else { [void](New-Item -ItemType File -Path $knownHosts) }
    }
    Protect-PrivatePath $knownHosts $false $identity
    $keyBytes = [IO.File]::ReadAllBytes($Config.KeyPath)
    try { [IO.File]::WriteAllBytes($privateKey, $keyBytes) }
    finally { [Array]::Clear($keyBytes, 0, $keyBytes.Length) }
    [PSCustomObject]@{ KeyPath = $privateKey; KnownHostsPath = $knownHosts }
}

function Get-VaultConsoleStatus {
    param([Parameter(Mandatory)][ValidateRange(10000, 65535)][int]$LocalPort)
    Add-Type -AssemblyName System.Net.Http
    $url = 'http://127.0.0.1:{0}' -f $LocalPort
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(5)
    try {
        $response = $client.GetAsync("$url/api/status").GetAwaiter().GetResult()
        [void]$response.EnsureSuccessStatusCode()
        $status = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
        [PSCustomObject]@{
            ConsoleUrl = $url
            Initialized = [bool]$status.initialized
            QueryReady = [bool]$status.query_ready
            AutoResumeEnabled = [bool]$status.auto_restore_ready
            Model = $status.model_name
            WeChatBound = [bool]$status.wechat.bound
            WeChatConnected = [bool]$status.wechat.connected
            Management = 'Browser login required; locking the management console does not stop WeChat queries.'
        }
    } catch {
        throw "Cannot read the vault status at $url. Start console.ps1 first, or check the tunnel and service."
    } finally { $client.Dispose() }
}

Export-ModuleMember -Function Get-VaultClientConfig, Set-VaultClientConfig, Test-VaultLocalPort, New-VaultSshSession, Get-VaultConsoleStatus
