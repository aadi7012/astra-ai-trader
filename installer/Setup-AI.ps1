param(
    [string]$Model = "qwen2.5:3b"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$OllamaUri = "http://127.0.0.1:11434"
$InstallerUrl = "https://ollama.com/download/OllamaSetup.exe"
$InstallerPath = Join-Path $env:TEMP "AstraAITrader-OllamaSetup.exe"

function Test-OllamaApi {
    try {
        Invoke-RestMethod -Uri "$OllamaUri/api/tags" -TimeoutSec 3 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Find-OllamaExecutable {
    $command = Get-Command "ollama.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"),
        (Join-Path $env:ProgramFiles "Ollama\ollama.exe")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    return $null
}

function Install-Ollama {
    Write-Host "Downloading the official Ollama for Windows installer..."
    Invoke-WebRequest `
        -Uri $InstallerUrl `
        -OutFile $InstallerPath `
        -TimeoutSec 120 `
        -UseBasicParsing
    $signature = Get-AuthenticodeSignature -FilePath $InstallerPath
    if (
        $signature.Status -ne "Valid" -or
        $signature.SignerCertificate.Subject -notmatch "Ollama"
    ) {
        throw "The downloaded Ollama installer does not have a valid Ollama digital signature."
    }

    Write-Host "Starting the signed Ollama installer. Complete its installation prompts."
    $process = Start-Process -FilePath $InstallerPath -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Ollama setup exited with code $($process.ExitCode)."
    }
}

function Wait-OllamaApi {
    param([int]$TimeoutSeconds = 180)

    for ($attempt = 0; $attempt -lt $TimeoutSeconds; $attempt++) {
        if (Test-OllamaApi) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

try {
    Write-Host "Astra AI Trader first-time AI setup"
    Write-Host "The $Model model download is about 2 GB; keep this window open."
    $ollama = Find-OllamaExecutable

    if (-not $ollama -and -not (Test-OllamaApi)) {
        Install-Ollama
        $ollama = Find-OllamaExecutable
    }

    if ($ollama) {
        if (-not (Test-OllamaApi)) {
            Write-Host "Starting the local Ollama service..."
            Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden
        }
        if (-not (Wait-OllamaApi)) {
            throw "Ollama did not become ready at $OllamaUri. Restart Windows or start Ollama, then rerun AI setup."
        }

        & $ollama show $Model *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Model $Model is already installed."
        }
        else {
            Write-Host "Downloading $Model with Ollama. This can take a while..."
            & $ollama pull $Model
            if ($LASTEXITCODE -ne 0) {
                throw "Ollama failed to download $Model (exit code $LASTEXITCODE)."
            }
        }
    }
    else {
        Write-Host "Using the existing local Ollama API to download the model..."
        if (-not (Wait-OllamaApi -TimeoutSeconds 10)) {
            throw "Ollama did not become ready at $OllamaUri."
        }
        $tags = Invoke-RestMethod -Uri "$OllamaUri/api/tags" -TimeoutSec 10
        $installed = @($tags.models | Where-Object { $_.name -eq $Model -or $_.name -eq "$Model`:latest" })
        if ($installed.Count -gt 0) {
            Write-Host "Model $Model is already installed."
        }
        else {
            $body = @{ name = $Model; stream = $false } | ConvertTo-Json -Compress
            Write-Host "Downloading $Model through the existing local Ollama service..."
            Invoke-RestMethod `
                -Method Post `
                -Uri "$OllamaUri/api/pull" `
                -ContentType "application/json" `
                -Body $body `
                -TimeoutSec 7200 | Out-Null
        }
    }

    Write-Host "AI setup is complete. Astra AI Trader can now use $Model."
    exit 0
}
catch {
    Write-Host ""
    Write-Host "AI setup did not complete: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "You can retry by running Setup-AI.ps1 from the Astra AI Trader installation folder."
    exit 1
}
finally {
    if (Test-Path -LiteralPath $InstallerPath) {
        Remove-Item -LiteralPath $InstallerPath -Force -ErrorAction SilentlyContinue
    }
}
