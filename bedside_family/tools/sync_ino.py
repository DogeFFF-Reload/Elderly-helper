# -*- coding: utf-8 -*-
# 固件编译正本 -> 仓库版同步脚本:整文件拷贝 + 把真 WiFi 密码换回占位符
# 红线:真密码绝不入库——本脚本自身也不出现真密码,密码由运行环境变量注入,
#      部署者本机运行前先设置: set CAREMATE_WIFI_PASS=你的真实密码
import io
import os

REAL_PASS = os.environ["CAREMATE_WIFI_PASS"]  # 仅运行期注入,不落盘不入库
PLACEHOLDER = "YOUR_WIFI_PASSWORD"

master = r"D:\文档\Default Project\caremate_code\code\bedside_s3_arm_kiosk\bedside_s3_arm_kiosk.ino"
repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
repo = os.path.join(repo_dir, "firmware_s3", "bedside_s3_arm_kiosk.ino")

src = io.open(master, encoding="utf-8").read()
out = src.replace(REAL_PASS, PLACEHOLDER)
assert REAL_PASS not in out, "密码没换干净,终止写入!"
assert PLACEHOLDER in out, "占位符缺失,检查正本是否已按占位符策略维护"

io.open(repo, "w", encoding="utf-8", newline="\n").write(out)

# 写后复核:仓库版绝不允许出现真密码
chk = io.open(repo, encoding="utf-8").read()
assert REAL_PASS not in chk, "复核失败:仓库版仍含真密码,立即检查!"
print("repo 行数:", len(chk.splitlines()), "| 占位符行:",
      [i + 1 for i, ln in enumerate(chk.splitlines()) if "WIFI_PASS" in ln])
print("master 行数:", len(src.splitlines()))
