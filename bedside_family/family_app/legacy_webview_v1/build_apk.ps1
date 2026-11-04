# Voice: rebuild CareMate_family.apk with a single command.
# Requires: JDK21 (javac), Android SDK at E:\agents\opencode\android_sdk
#           (cmdline-tools + platforms;android-34 + build-tools;34.0.0)
# Notes:
#   - ALL PS literals here keep ASCII-only. PS5.1 reads no-BOM files as ANSI
#     and shreds Chinese literals -- the same trap as gen_pack.ps1.
#   - No anonymous inner classes / no lambdas in java sources: build-tools
#     34.0.0's d8 is a dev build that NPEs on EnclosingMethod of $1 classes,
#     and android.jar stubs break lambda metafactory under -bootclasspath.
#     Top-level callback classes only (ConnectListener / MicChromeClient).
$ErrorActionPreference = "Stop"
$SDK  = "E:\agents\opencode\android_sdk"
$BT   = "$SDK\build-tools\34.0.0"
$PLAT = "$SDK\platforms\android-34\android.jar"
Set-Location (Join-Path $PSScriptRoot "..")   # .../code
$A = "family_apk"

Remove-Item "$A\build_cls", "$A\build_dex" -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "$A\build_cls", "$A\build_dex" | Out-Null

# 1 compile java (top-level classes only)
$src = Get-ChildItem "$A\java" -Recurse -Filter *.java | Select-Object -ExpandProperty FullName
javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options `
      -bootclasspath $PLAT -d "$A\build_cls" $src
if ($LASTEXITCODE -ne 0) { Write-Host "JAVAC FAIL"; exit 1 }

# 2 dex
$cls = Get-ChildItem "$A\build_cls" -Recurse -Filter *.class | Select-Object -ExpandProperty FullName
& cmd.exe /c "$BT\d8.bat --min-api 24 --output $((Resolve-Path $A).Path)\build_dex $($cls -join ' ')"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$A\build_dex\classes.dex")) {
    Write-Host "D8 FAIL"; exit 1 }

# 3 resources + manifest
& "$BT\aapt2.exe" compile --dir "$A\res" -o "$A\build_dex\res.zip"
& "$BT\aapt2.exe" link -o "$A\base.apk" -I $PLAT `
    --manifest "$A\AndroidManifest.xml" `
    --min-sdk-version 24 --target-sdk-version 34 `
    --version-code 1 --version-name "1.0" `
    "$A\build_dex\res.zip"
if ($LASTEXITCODE -ne 0) { Write-Host "AAPT2 FAIL"; exit 1 }

# 4 merge dex into apk (python zipfile, no extra tooling)
python -c "import zipfile;z=zipfile.ZipFile('family_apk/base.apk','a');z.write('family_apk/build_dex/classes.dex','classes.dex');z.close();print('dex merged')"

# 5 keystore (stable -- same key every rebuild, so Android updates over old installs)
if (-not (Test-Path "$A\caremate.jks")) {
    keytool -genkeypair -keystore "$A\caremate.jks" -alias caremate `
        -keyalg RSA -keysize 2048 -validity 10000 `
        -storepass android -keypass android `
        -dname "CN=CareMate,L=Shanghai,C=CN" | Out-Null
}

# 6 align + sign + verify
& "$BT\zipalign.exe" -f -p 4 "$A\base.apk" "$A\aligned.apk"
& "$BT\apksigner.bat" sign --ks "$A\caremate.jks" --ks-pass pass:android `
    --out "$A\CareMate_family.apk" "$A\aligned.apk"
& "$BT\apksigner.bat" verify "$A\CareMate_family.apk" | Out-Null
if ($LASTEXITCODE -eq 0) {
    $sz = (Get-Item "$A\CareMate_family.apk").Length
    Write-Host ("APK READY: code/family_apk/CareMate_family.apk  ({0:N0} bytes)  signature VERIFIED" -f $sz)
} else {
    Write-Host "SIGN FAIL"
    exit 1
}
