# Dot-source AFTER Invoke-Expression (&starship init powershell).
# Target: PowerShell 7.6 on Windows.

# Clear legacy values when reloading a session after upgrading.
foreach ($name in @('BLUE', 'GREEN', 'YELLOW', 'PINK')) {
    Remove-Item -LiteralPath "Env:STARSHIP_WEATHER_$name" -ErrorAction Ignore
}

function Set-StarshipWeatherPayload {
    param([string]$Payload)
    $parts = $Payload -split "`t"
    if ($parts.Count -ne 2 -or $parts[0] -cnotmatch '^#[0-9a-fA-F]{6}$' -or
        $parts[1] -cnotmatch '^[0-9a-fA-F]{4,6}$') { return }
    $point = [Convert]::ToInt32($parts[1], 16)
    if (-not (($point -ge 0xe000 -and $point -le 0xf8ff) -or
              ($point -ge 0xf0000 -and $point -le 0xffffd) -or
              ($point -ge 0x100000 -and $point -le 0x10fffd))) { return }
    $glyph = [char]::ConvertFromUtf32($point)
    $red = [Convert]::ToInt32($parts[0].Substring(1, 2), 16)
    $green = [Convert]::ToInt32($parts[0].Substring(3, 2), 16)
    $blue = [Convert]::ToInt32($parts[0].Substring(5, 2), 16)
    $env:STARSHIP_WEATHER = "`e[1;38;2;$red;$green;${blue}m$glyph`e[0m"
}

if (-not (Get-Variable StarshipWeatherLastPoll -Scope Script -ErrorAction Ignore)) {
    $script:StarshipWeatherLastPoll = $null
}
if (-not (Get-Variable StarshipWeatherHookInstalled -Scope Script -ErrorAction Ignore)) {
    $script:StarshipWeatherHookInstalled = $false
}

function Update-StarshipWeather {
    # A missing optional tool must not replace the user's latest $Error entry:
    # Starship uses that entry to classify a failed PowerShell command.
    $uvCommand = Get-Command uv -CommandType Application -ErrorAction Ignore
    if (-not $uvCommand) {
        Remove-Item -LiteralPath Env:STARSHIP_WEATHER -ErrorAction Ignore
        return
    }
    $now = [DateTime]::UtcNow
    if ($script:StarshipWeatherLastPoll -and
        ($now - $script:StarshipWeatherLastPoll).TotalSeconds -lt 5) { return }
    $script:StarshipWeatherLastPoll = $now
    Remove-Item -LiteralPath Env:STARSHIP_WEATHER -ErrorAction Ignore
    $configPath = $env:STARSHIP_CONFIG
    if (-not $configPath) { $configPath = Join-Path $HOME '.config\starship\starship.toml' }
    $weatherScript = Join-Path (Split-Path $configPath -Parent) 'scripts\weather.py'
    if (-not (Test-Path -LiteralPath $weatherScript -PathType Leaf)) { return }

    # Explicit UTF-8 decoding also works with the legacy Windows console code page.
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $uvCommand.Source
    $start.Arguments = 'run --no-project --no-config --no-python-downloads --offline --python ">=3.10" "' + $weatherScript + '" prompt'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    $previousWeatherError = if ($global:Error.Count) { $global:Error[0] } else { $null }
    try {
        if (-not $process.Start()) { return }
        if (-not $process.WaitForExit(500)) {
            $process.Kill()
            return
        }
        if ($process.ExitCode -ne 0) { return }
        $payload = $process.StandardOutput.ReadToEnd().TrimEnd("`r", "`n")
        Set-StarshipWeatherPayload $payload
    } catch {
        # Method invocation can add wrapped and underlying records. Discard
        # only errors added by this weather call, keeping the user's latest.
        while ($global:Error.Count -and $global:Error[0] -ne $previousWeatherError) {
            $global:Error.RemoveAt(0)
        }
    } finally {
        $process.Dispose()
    }
}

if (-not $script:StarshipWeatherHookInstalled) {
    $previous = Get-Command Invoke-Starship-PreCommand -CommandType Function -ErrorAction Ignore
    $script:StarshipWeatherPreviousPreCommand = if ($previous) { $previous.ScriptBlock } else { $null }
    $script:StarshipWeatherHookInstalled = $true
}

function global:Invoke-Starship-PreCommand {
    $savedExitCode = $global:LASTEXITCODE
    try {
        if ($script:StarshipWeatherPreviousPreCommand) {
            & $script:StarshipWeatherPreviousPreCommand
        }
        Update-StarshipWeather
    } catch {
        # Keep the prompt usable if uv or a local configuration is unavailable.
    } finally {
        $global:LASTEXITCODE = $savedExitCode
    }
}
