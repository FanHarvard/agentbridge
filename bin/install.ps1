# Install the LAN server/client for the current Windows user (Python 3 required).
param(
    [string]$ProgramDirectory = (Join-Path $env:LOCALAPPDATA 'AgentBridge'),
    [string]$Workspace = (Join-Path $env:LOCALAPPDATA 'AgentBridgeData')
)
$ErrorActionPreference = 'Stop'
$SourceDirectory = Split-Path $PSScriptRoot -Parent
$ProgramDirectory = [System.IO.Path]::GetFullPath($ProgramDirectory)
$Workspace = [System.IO.Path]::GetFullPath($Workspace)
if ($ProgramDirectory -eq $Workspace -or
    $Workspace.StartsWith($ProgramDirectory.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase) -or
    $ProgramDirectory.StartsWith($Workspace.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Use separate program and workspace directories.'
}
foreach ($Directory in @($ProgramDirectory, $Workspace)) {
    if ((Test-Path -LiteralPath $Directory) -and
        ((Get-Item -LiteralPath $Directory).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "Install targets must not be symbolic links or junctions: $Directory"
    }
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
}
foreach ($Name in @('agent_hub.py', 'agent_lan.py', 'opencode_watch.py', 'notification_preview.py')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $Name) -Destination $ProgramDirectory -Force
}
Copy-Item -LiteralPath (Join-Path $SourceDirectory 'doc\lan.html') -Destination $ProgramDirectory -Force
Copy-Item -LiteralPath (Join-Path $SourceDirectory 'doc\opencode.html') -Destination $ProgramDirectory -Force
Copy-Item -LiteralPath (Join-Path $SourceDirectory 'templates\LAN_snippet.md') -Destination $ProgramDirectory -Force
py -3 (Join-Path $ProgramDirectory 'agent_hub.py') --workspace $Workspace init
if ($LASTEXITCODE -ne 0) { throw 'Workspace initialization failed.' }
Write-Output "Program: $ProgramDirectory"
Write-Output "Workspace: $Workspace"
