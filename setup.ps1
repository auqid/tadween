# One-time setup for Tadween on Windows (x64): Python packages, ffmpeg, whisper.cpp and the models (~1 GB).
# Run it from PowerShell in the Tadween folder (safe to run again):
#     powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$release = Get-Content -Raw whisper\RELEASE | ConvertFrom-StringData  # TAG, and WINDOWS_BUILD: its Windows builds
$WhisperRelease = $release.WINDOWS_BUILD

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
# An NVIDIA GPU gets the CUDA build its driver supports. AMD and Intel graphics (or an NVIDIA driver too old for
# CUDA 11.8) get a Vulkan build: whisper.cpp publishes none for Windows, so this repository's GitHub Actions
# make it (.github/workflows/whisper-vulkan-windows.yml). Anything else, or no Vulkan build yet: the CPU build.
$cpuBuilds = "https://github.com/ggml-org/whisper.cpp/releases/download/$WhisperRelease"
$builds = $cpuBuilds
$asset = "whisper-bin-x64.zip"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    if ((& nvidia-smi | Out-String) -match "CUDA Version:\s*([\d.]+)") {
        $cuda = [version]$Matches[1]
        if ($cuda -ge [version]"12.4") { $asset = "whisper-bin-win-cuda-12.4.0-x64.zip" }
        elseif ($cuda -ge [version]"11.8") { $asset = "whisper-bin-win-cuda-11.8.0-x64.zip" }
    }
}
if ($asset -eq "whisper-bin-x64.zip" -and (Test-Path "$env:WINDIR\System32\vulkan-1.dll")) {  # a GPU driver with Vulkan
    $gpus = (Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
        ForEach-Object { "$($_.AdapterCompatibility) $($_.Name)" }) -join "; "
    if ($gpus -match "AMD|Advanced Micro Devices|Radeon|Intel|NVIDIA") {
        $asset = "whisper-bin-win-vulkan-x64.zip"
        $builds = "https://github.com/auqid/tadween/releases/download/whisper-$($release.TAG)"
        if ((curl.exe -sIL -o NUL -w "%{http_code}" "$builds/$asset") -ne "200") {
            Write-Host "    There's no Vulkan build for whisper.cpp $($release.TAG) yet, so Whisper will use the CPU."
            $asset = "whisper-bin-x64.zip"
            $builds = $cpuBuilds
        }
    }
}
$want = "$WhisperRelease $asset"
$have = if (Test-Path whisper\bin\VERSION) { (Get-Content -Raw whisper\bin\VERSION).Trim() } else { "" }
if ($have -ne $want -or -not (Test-Path whisper\bin\whisper-cli.exe)) {  # first time, new release, or new GPU
    Write-Host "    $asset"
    Fetch "$builds/$asset" "whisper\$asset"
    if (Test-Path whisper\bin) { Remove-Item -Recurse -Force whisper\bin }  # no DLLs left over from another build
    New-Item -ItemType Directory -Force whisper\bin | Out-Null
    Expand-Archive -Force "whisper\$asset" whisper\unpacked
    Copy-Item -Force (Get-ChildItem whisper\unpacked -Recurse -File).FullName whisper\bin\
    Remove-Item -Recurse -Force whisper\unpacked, "whisper\$asset"
    Set-Content whisper\bin\VERSION $want
}

Write-Host "==> Models (downloaded once)"
New-Item -ItemType Directory -Force models | Out-Null
Fetch "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin" "models\ggml-large-v3-turbo-q8_0.bin"
Fetch "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx" "models\silero_vad.onnx"
# (the release tag really is spelled "recongition")
Fetch "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx" "models\nemo_en_titanet_large.onnx"

# A weak integrated GPU can be slower than the CPU, and a card short of memory can fail: time them once.
Write-Host "==> Speed check: where Whisper runs fastest here (Settings can change it)"
& $venvPython -m tadween speed-check --if-needed

Write-Host ""
Write-Host "Done. Start Tadween with:  .\tadween.cmd"
