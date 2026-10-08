# One-time setup for Tadween on Windows (x64): Python packages, ffmpeg, whisper.cpp and the models (~1 GB).
# Run it from PowerShell in the Tadween folder (safe to run again):
#     powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$WhisperRelease = "b5454"  # whisper.cpp v1.9.5 Windows builds

function Fetch($Url, $Dest) {
    if ((Test-Path $Dest) -and (Get-Item $Dest).Length -gt 0) { return }
    Write-Host "    $Dest"
    curl.exe -L --fail --progress-bar -o "$Dest.part" $Url
    if ($LASTEXITCODE -ne 0) { throw "Download failed: $Url" }
    Move-Item -Force "$Dest.part" $Dest
}

function Test-Python($exe) {
    # A broken or missing Python answers on stderr. Windows PowerShell 5.1 turns redirected stderr into
    # errors, so "Stop" would end setup here: in this function they are just a "no".
    $ErrorActionPreference = "Continue"
    & $exe -c "import sys" 2>$null | Out-Null  # not -c "": Windows PowerShell 5.1 drops empty arguments
    return $LASTEXITCODE -eq 0
}

function Find-Python {
    # The py launcher (python.org installs) or python on PATH; the Microsoft Store stub fails the check.
    $ErrorActionPreference = "Continue"  # as in Test-Python
    $probe = "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $path = & py -3 -c $probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $path) { return "$path".Trim() }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $path = & python -c $probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $path) { return "$path".Trim() }
    }
    return $null
}

Write-Host "==> Python packages"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not ((Test-Path $venvPython) -and (Test-Python $venvPython))) {  # missing, or its Python was uninstalled
    $python = Find-Python
    if (-not $python) {
        throw "Python 3.10 or newer is required. Install it with:  winget install Python.Python.3.12  then open a new PowerShell and run setup.ps1 again."
    }
    if (Test-Path .venv) { Remove-Item -Recurse -Force .venv }
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv" }
}
& $venvPython -m pip install -q --upgrade pip
& $venvPython -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Installing the Python packages failed." }

Write-Host "==> ffmpeg"
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue) -and -not (Test-Path tools\ffprobe.exe)) {
    New-Item -ItemType Directory -Force tools | Out-Null
    Fetch "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" "tools\ffmpeg.zip"
    Expand-Archive -Force tools\ffmpeg.zip tools\unpacked
    Copy-Item -Force (Get-ChildItem tools\unpacked -Recurse -Include ffmpeg.exe, ffprobe.exe).FullName tools\
    Remove-Item -Recurse -Force tools\unpacked, tools\ffmpeg.zip
}

Write-Host "==> whisper.cpp"
if (-not (Test-Path whisper\bin\whisper-cli.exe)) {
    $asset = "whisper-bin-x64.zip"  # CPU
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {  # an NVIDIA GPU: the CUDA build its driver supports
        if ((& nvidia-smi | Out-String) -match "CUDA Version:\s*([\d.]+)") {
            $cuda = [version]$Matches[1]
            if ($cuda -ge [version]"12.4") { $asset = "whisper-bin-win-cuda-12.4.0-x64.zip" }
            elseif ($cuda -ge [version]"11.8") { $asset = "whisper-bin-win-cuda-11.8.0-x64.zip" }
        }
    }
    Write-Host "    $asset"
    New-Item -ItemType Directory -Force whisper\bin | Out-Null
    Fetch "https://github.com/ggml-org/whisper.cpp/releases/download/$WhisperRelease/$asset" "whisper\$asset"
    Expand-Archive -Force "whisper\$asset" whisper\unpacked
    Copy-Item -Force (Get-ChildItem whisper\unpacked -Recurse -File).FullName whisper\bin\
    Remove-Item -Recurse -Force whisper\unpacked, "whisper\$asset"
    Set-Content whisper\bin\VERSION "$WhisperRelease $asset"
}

Write-Host "==> Models (downloaded once)"
New-Item -ItemType Directory -Force models | Out-Null
Fetch "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin" "models\ggml-large-v3-turbo-q8_0.bin"
Fetch "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx" "models\silero_vad.onnx"
# (the release tag really is spelled "recongition")
Fetch "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx" "models\nemo_en_titanet_large.onnx"

Write-Host ""
Write-Host "Done. Start Tadween with:  .\tadween.cmd"
