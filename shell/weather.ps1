# Dot-source AFTER Invoke-Expression (&starship init powershell).
# Target: PowerShell 7.6 on Windows.

if (-not (Get-Variable StarshipWeatherLastPoll -Scope Script -ErrorAction Ignore)) {
    $script:StarshipWeatherLastPoll = $null
}
if (-not (Get-Variable StarshipWeatherHookInstalled -Scope Script -ErrorAction Ignore)) {
    $script:StarshipWeatherHookInstalled = $false
}

function Update-StarshipWeather {
    $weatherNames = @('BLUE', 'GREEN', 'YELLOW', 'PINK')
    # A missing optional tool must not replace the user's latest $Error entry:
    # Starship uses that entry to classify a failed PowerShell command.
    $uvCommand = Get-Command uv -CommandType Application -ErrorAction Ignore
    if (-not $uvCommand) {
        foreach ($name in $weatherNames) {
            Remove-Item -LiteralPath "Env:STARSHIP_WEATHER_$name" -ErrorAction Ignore
        }
        return
    }
    $now = [DateTime]::UtcNow
    if ($script:StarshipWeatherLastPoll -and
        ($now - $script:StarshipWeatherLastPoll).TotalSeconds -lt 5) { return }
    $script:StarshipWeatherLastPoll = $now
    foreach ($name in $weatherNames) {
        Remove-Item -LiteralPath "Env:STARSHIP_WEATHER_$name" -ErrorAction Ignore
    }
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
        $payload = $process.StandardOutput.ReadToEnd().Trim()
        $parts = $payload -split "`t", 2
        if ($parts.Count -eq 2 -and $weatherNames -contains $parts[0].ToUpperInvariant()) {
            [Environment]::SetEnvironmentVariable(
                ('STARSHIP_WEATHER_' + $parts[0].ToUpperInvariant()), $parts[1], 'Process')
        }
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
