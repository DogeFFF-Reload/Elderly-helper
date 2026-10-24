# Voice pack generator -- Windows built-in SAPI (offline & free)
#   powershell -ExecutionPolicy Bypass -File gen_pack.ps1            # both speeds
#   powershell -ExecutionPolicy Bypass -File gen_pack.ps1 -OnlySlow  # slow speed only
# Output: wav/NN_id.wav and wav_slow/NN_id.wav (16kHz/16bit/mono)
# Script text: ALL literal strings are ASCII; Chinese lives only in lines.json (read as UTF8)
param([switch]$OnlySlow)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfg = Get-Content (Join-Path $here 'lines.json') -Encoding UTF8 -Raw | ConvertFrom-Json

$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $s.SelectVoice($cfg.voice) } catch { Write-Host ('!! voice not found: ' + $cfg.voice + ' -> default') }
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)

function GenDir($dir, $rate) {
    New-Item -ItemType Directory -Force -Path (Join-Path $here $dir) | Out-Null
    $i = 0
    foreach ($ln in $cfg.lines) {
        $i++
        $name = ('{0:D2}_{1}.wav' -f $i, $ln.id)
        $path = Join-Path (Join-Path $here $dir) $name
        $s.Rate = $rate
        $s.SetOutputToWaveFile($path, $fmt)
        $s.Speak($ln.text)
        $s.SetOutputToNull()
        Write-Host ($dir + '/' + $name)
    }
}

# probe: file must exist and be a sane size, else SAPI is silently broken
$probe = Join-Path $env:TEMP 'care_probe.wav'
$s.Rate = $cfg.rate_normal
$s.SetOutputToWaveFile($probe, $fmt)
$s.Speak([char]0x6D4B + [char]0x8BD5)   # "ce shi" in chars, avoids CN literals
$s.SetOutputToNull()
if ((Get-Item $probe).Length -lt 2000) { throw ('TTS dead (only ' + (Get-Item $probe).Length + ' bytes)') }

if (-not $OnlySlow) { GenDir 'wav' $cfg.rate_normal }
GenDir 'wav_slow' $cfg.rate_slow
Write-Host 'DONE voice_pack ready'
