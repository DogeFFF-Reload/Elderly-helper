# -*- coding: utf-8 -*-
"""build_app.py — 手工工具链打包原生安卓 APK(aapt2 → javac → d8 → zip → zipalign → apksigner)
所有中间产物放 temp\\apkbuild(路径无空格,绕开 .bat 转义坑)
"""
import os, shutil, subprocess, zipfile, sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SRC = r"D:\文档\Default Project\caremate_code\code\family_app\android"
BT  = r"D:\android-sdk\build-tools\35.0.0"
PLAT = r"D:\android-sdk\platforms\android-34\android.jar"
JDK = r"D:\Java\jdk-25\bin"
B   = r"C:\Users\36197\AppData\Local\Temp\opencode\apkbuild"
OUT_APK = os.path.join(B, "family_app_v1.apk")

def run(cmd, label, use_shell=False):
    print("== " + label, flush=True)
    if use_shell:                     # .bat 脚本走 shell(含空格路径手工加引号)
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           errors="replace", cwd=B)
    else:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", cwd=B)
    if r.returncode != 0:
        print(r.stdout[-4000:]); print(r.stderr[-4000:])
        raise SystemExit("FAILED at: " + label)
    if r.stdout.strip(): print(r.stdout.strip()[-1500:])
    return r

# 0) 干净构建目录
if os.path.exists(B): shutil.rmtree(B)
os.makedirs(B)
shutil.copy2(os.path.join(SRC, "AndroidManifest.xml"), os.path.join(B, "AndroidManifest.xml"))
shutil.copytree(os.path.join(SRC, "res"), os.path.join(B, "res"))
shutil.copytree(os.path.join(SRC, "java"), os.path.join(B, "java"))
print("staged")
os.chdir(B)   # 之后脚本内所有相对路径都基于构建目录

# 1) aapt2 compile 资源
run([os.path.join(BT, "aapt2.exe"), "compile", "--dir", "res", "-o", "res.zip"],
    "aapt2 compile")

# 2) aapt2 link → base.apk + R.java
run([os.path.join(BT, "aapt2.exe"), "link", "-o", "base.apk", "-I", PLAT,
     "--manifest", "AndroidManifest.xml", "--java", "gen", "-R", "res.zip",
     "--min-sdk-version", "24", "--target-sdk-version", "34"],
    "aapt2 link")

# 3) javac(JDK25,--release 11,d8 可消)
jfiles = []
for root, _, fs in os.walk(os.path.join(B, "java")):
    for f in fs:
        if f.endswith(".java"): jfiles.append(os.path.join(root, f))
jfiles.append(os.path.join(B, "gen", "com", "mocen", "caremate", "family", "R.java"))
os.makedirs(os.path.join(B, "classes"))
run([os.path.join(JDK, "javac.exe"), "--release", "11", "-encoding", "UTF-8",
     "-classpath", PLAT, "-d", "classes"] + jfiles, "javac")

# 4) jar → d8 → classes.dex
run([os.path.join(JDK, "jar.exe"), "cf", "classes.jar", "-C", "classes", "."], "jar")
os.makedirs(os.path.join(B, "dexout"))
run('"%s" --release --lib "%s" --min-api 24 --output dexout classes.jar'
    % (os.path.join(BT, "d8.bat"), PLAT), "d8", use_shell=True)
assert os.path.isfile(os.path.join(B, "dexout", "classes.dex")), "dex missing"

# 5) classes.dex 塞进 base.apk(逐条复制,保持每条原压缩方式)
with zipfile.ZipFile("base.apk") as zi, zipfile.ZipFile("unsigned.apk", "w") as zo:
    for it in zi.infolist():
        zo.writestr(it.filename, zi.read(it.filename), it.compress_type)
    zo.writestr("classes.dex", open(os.path.join(B, "dexout", "classes.dex"), "rb").read(),
                zipfile.ZIP_DEFLATED)
print("== dex packed")

# 6) zipalign
run([os.path.join(BT, "zipalign.exe"), "-f", "4", "unsigned.apk", "aligned.apk"], "zipalign")

# 7) keystore:固定用稳定密钥(临时目录外),保证各版本签名一致可升级安装
KS = os.path.join(B, "caremate.ks")
KS_STABLE = r"C:\Users\36197\AppData\Local\Temp\opencode\caremate_stable.ks"
if os.path.isfile(KS_STABLE):
    shutil.copy2(KS_STABLE, KS)
elif os.path.isfile(KS):
    shutil.copy2(KS, KS_STABLE)
else:
    run([os.path.join(JDK, "keytool.exe"), "-genkeypair", "-keystore", "caremate.ks",
         "-alias", "caremate", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
         "-storepass", "caremate2026", "-keypass", "caremate2026",
         "-dname", "CN=CareMate, OU=Mocen, O=Mocen, C=CN"], "keytool")
    shutil.copy2(KS, KS_STABLE)

# 8) apksigner 签名
run('"%s" sign --ks caremate.ks --ks-key-alias caremate '
    '--ks-pass pass:caremate2026 --key-pass pass:caremate2026 '
    '--out family_app.apk aligned.apk' % os.path.join(BT, "apksigner.bat"),
    "apksigner sign")
run('"%s" verify --print-certs family_app.apk' % os.path.join(BT, "apksigner.bat"),
    "apksigner verify")

# 9) 交付:项目目录 + 桌面(v10 = v1.9 预下载缓存+秒播版)
OUT_APK = os.path.join(B, "family_app.apk")
dst1 = r"D:\文档\Default Project\caremate_code\family_app_v1.9.apk"
dst2 = os.path.join(os.path.expanduser("~"), "Desktop", "家人留言v10.apk")
shutil.copy2(OUT_APK, dst1)
if os.path.isdir(os.path.dirname(dst2)): shutil.copy2(OUT_APK, dst2)
print("== APK OK")
print(dst1)
print(dst2, "(%.2f MB)" % (os.path.getsize(OUT_APK) / 1048576))