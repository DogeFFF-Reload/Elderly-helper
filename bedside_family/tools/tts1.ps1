param([string]$WavPath, [string]$Text)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = 0
$zh = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like "zh*" } | Select-Object -First 1
if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) } else { Write-Output "WARN: no zh voice, default used" }
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($WavPath, $fmt)
$s.Speak($Text)
$s.Dispose()
Write-Output ("VOICE=" + $(if ($zh) { $zh.VoiceInfo.Name } else { "(default)" }))