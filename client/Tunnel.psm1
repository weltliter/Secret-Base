Set-StrictMode -Version Latest

function Get-VaultClientConfig {
    param(
        [Parameter(Mandatory)][string]$ProjectRoot,
        [ValidateRange(10000, 65535)][int]$LocalPort
    )
    $configPath = Join-Path $ProjectRoot '.local/console.psd1'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw 'SSH connection is not configured. Run .\console.ps1 -Action Configure, or copy client/console.example.psd1 to .local/console.psd1 and fill in your own server and key path.'
    }
    $config = Import-PowerShellDataFile -LiteralPath $configPath
    if ($PSBoundParameters.ContainsKey('LocalPort')) { $config.LocalPort = $LocalPort }
    if ($config.LocalPort -lt 10000 -or $config.LocalPort -gt 65535) {
        throw 'The local console port must be between 10000 and 65535.'
    }
    if ($config.RemotePort -lt 1 -or $config.RemotePort -gt 65535 -or
        $config.Server -notmatch '^[a-zA-Z0-9.-]+$' -or
        $config.UserName -notmatch '^[a-zA-Z0-9_-]+$') {
        throw 'Invalid SSH server, user or remote port in .local/console.psd1.'
    }
    if ([string]::IsNullOrWhiteSpace($config.KeyPath) -or $config.KeyPath -match '[\x00-\x1f"<>|]') { throw 'Invalid private key path.' }
    if (-not [IO.Path]::IsPathRooted($config.KeyPath)) {
        $config.KeyPath = Join-Path $ProjectRoot $config.KeyPath
    }
    return $config
}

function Set-VaultClientConfig {
    param(
        [Parameter(Mandatory)][string]$ProjectRoot,
        [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9.-]+$')][string]$Server,
        [Parameter(Mandatory)][ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$UserName,
        [Parameter(Mandatory)][string]$KeyPath,
        [ValidateRange(10000, 65535)][int]$LocalPort = 18080,
        [ValidateRange(1, 65535)][int]$RemotePort = 18080
    )
    if ([string]::IsNullOrWhiteSpace($KeyPath) -or $KeyPath -match '[\x00-\x1f"<>|]') { throw 'Invalid private key path.' }
    $absoluteKey = if ([IO.Path]::IsPathRooted($KeyPath)) { $KeyPath } else { Join-Path $ProjectRoot $KeyPath }
    if (-not (Test-Path -LiteralPath $absoluteKey -PathType Leaf)) { throw 'The specified SSH private key file does not exist.' }
    $directory = Join-Path $ProjectRoot '.local'
    [void](New-Item -ItemType Directory -Force -Path $directory)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $path = Join-Path $directory 'console.psd1'
    if (-not (Test-Path -LiteralPath $path)) { [void](New-Item -ItemType File -Path $path) }
    Protect-PrivatePath $path $false $identity
    # PSD1 string quoting, never executable PowerShell interpolation on reload.
    $quotedKey = $KeyPath.Replace("'", "''")
    $content = "@{`r`n    Server = '$Server'`r`n    UserName = '$UserName'`r`n    LocalPort = $LocalPort`r`n    RemotePort = $RemotePort`r`n    KeyPath = '$quotedKey'`r`n}`r`n"
    [IO.File]::WriteAllText($path, $content, [Text.UTF8Encoding]::new($false))
    Write-Output 'Connection configuration saved locally. The SSH private key is used only by the local tunnel launcher.'
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
