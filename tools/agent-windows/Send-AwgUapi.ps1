param(
    [Parameter(Mandatory = $true)]
    [string]$Command,
    [string]$TunnelName = "AmneziaVPN",
    [string]$PipeName = "",
    [int]$TimeoutMs = 5000
)

$ErrorActionPreference = "Stop"

if (-not $PipeName) {
    $PipeName = "ProtectedPrefix\Administrators\AmneziaWG\$TunnelName"
}

$client = [System.IO.Pipes.NamedPipeClientStream]::new(
    ".",
    $PipeName,
    [System.IO.Pipes.PipeDirection]::InOut,
    [System.IO.Pipes.PipeOptions]::None,
    [System.Security.Principal.TokenImpersonationLevel]::Impersonation
)

try {
    $client.Connect($TimeoutMs)
    $client.ReadMode = [System.IO.Pipes.PipeTransmissionMode]::Byte

    while (-not $Command.EndsWith("`n`n")) {
        $Command += "`n"
    }

    $bytes = [System.Text.Encoding]::ASCII.GetBytes($Command)
    $client.Write($bytes, 0, $bytes.Length)
    $client.Flush()

    $buffer = New-Object byte[] 4096
    $reply = New-Object System.Text.StringBuilder
    while ($true) {
        $read = $client.Read($buffer, 0, $buffer.Length)
        if ($read -le 0) {
            break
        }
        $chunk = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $read)
        [void]$reply.Append($chunk)
        if ($reply.ToString().Contains("`n`n")) {
            break
        }
    }

    $reply.ToString().Trim()
} finally {
    $client.Dispose()
}
