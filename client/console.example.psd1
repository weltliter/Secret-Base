# Copy to .local/console.psd1, or run console.ps1 -Action Configure.
# This file is a template. It is never used automatically for a connection.
@{
    Server = 'your-ecs.example.com'
    UserName = 'root'
    LocalPort = 18080
    RemotePort = 18080
    KeyPath = '<本机私钥路径>'
}
