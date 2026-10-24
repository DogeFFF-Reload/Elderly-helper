"""公网链接：用 Cloudflare 免费隧道把主脑的“家属端”开到公网，家属手机 4G 也能打开、床头盒在别的网也能推拉留言。

  python -m tools.public_link [--port 8000] [--cloudflared D:\\Elderly-tools\\cloudflared\\cloudflared.exe]

做的事：
  1. 公网钥匙：config 里没有 public_token 就生成一把，写进 code/config.local.json（不进仓库），提示重启主脑
  2. 起 cloudflared 免费隧道（不用账号），拿到 https://xxx.trycloudflare.com
  3. 用钥匙从公网地址拉一次 /api/poll，确认整条路通、主脑认这把钥匙
  4. 打印家属端 App 链接（每位家属一条）和床头盒要配的 FAMILY_PUSH_URL / FAMILY_PULL_URL / FAMILY_PULL_TOKEN，
     同时写进 code/data/public_link.txt
公网能碰的只有家属端页面和家属端接口（见 master_service.PUBLIC_*），动臂、操作台、大屏只在局域网。
免费隧道每次启动地址都会变：重启后把新链接再发给家属、改一下床头盒配置。Ctrl+C 关闭隧道。
"""
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import config

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def ensure_token():
    """返回 (钥匙, 是否新生成)。新生成的写进 config.local.json，主脑要重启才认。"""
    cfg = config.load()
    if cfg["public_token"]:
        return cfg["public_token"], False
    token = secrets.token_urlsafe(12)
    data = {}
    if config.LOCAL_PATH.exists():
        data = json.loads(config.LOCAL_PATH.read_text(encoding="utf-8"))
    data["public_token"] = token
    config.LOCAL_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return token, True


def find_cloudflared(arg):
    for cand in (arg, os.environ.get("CLOUDFLARED"), shutil.which("cloudflared"),
                 r"D:\Elderly-tools\cloudflared\cloudflared.exe"):
        if cand and os.path.isfile(cand):
            return cand
    raise SystemExit("找不到 cloudflared：从 https://github.com/cloudflare/cloudflared/releases 下载 "
                     "cloudflared-windows-amd64.exe（X5 用 linux-arm64），用 --cloudflared 指定路径")


def check(url, token):
    req = urllib.request.Request(url + "/api/poll?since=0", headers={"X-CareMate-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, "ok"
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8")).get("error", "")
        except ValueError:
            return e.code, ""
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def main():
    config.utf8_console()
    ap = argparse.ArgumentParser(description="主脑家属端开到公网（Cloudflare 免费隧道）")
    ap.add_argument("--port", type=int, default=8000, help="本机主脑端口")
    ap.add_argument("--cloudflared", help="cloudflared 可执行文件路径")
    args = ap.parse_args()

    token, fresh = ensure_token()
    if fresh:
        print("· 已生成公网钥匙，写进 %s。**请重启主脑**让它认这把钥匙，再看下面的连通检查。" % config.LOCAL_PATH, flush=True)
    exe = find_cloudflared(args.cloudflared)
    # 默认的 QUIC 走 UDP 7844，校园网 / 热点常被挡；http2 走 TCP 443，哪儿都通
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", "http://127.0.0.1:%d" % args.port],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    print("· 隧道启动中（cloudflared）…", flush=True)
    found = {}

    def pump():
        for line in proc.stdout:
            m = URL_RE.search(line)
            if m and "url" not in found:
                found["url"] = m.group(0)
            if "ERR" in line and "url" in found:
                print("  cloudflared: " + line.strip(), flush=True)
    threading.Thread(target=pump, daemon=True).start()

    deadline = time.time() + 60
    while "url" not in found and time.time() < deadline and proc.poll() is None:
        time.sleep(0.3)
    if "url" not in found:
        proc.kill()
        raise SystemExit("× 60 秒没拿到公网地址（网络连不上 Cloudflare？）")
    url = found["url"]

    status = 0
    for _ in range(20):   # 新地址的 DNS 要几秒才生效
        status, why = check(url, token)
        if status in (200, 401, 403):
            break
        time.sleep(1.5)
    if status == 200:
        print("✓ 公网连通：%s（钥匙已被主脑认可）" % url, flush=True)
    elif status in (401, 403):
        print("! 公网通了，但主脑不认这把钥匙（%s）：重启主脑后再试" % why, flush=True)
    else:
        print("! 公网暂时连不上（%s），隧道还开着，稍后家属端会自动连上" % why, flush=True)

    cfg = config.load()
    lines = ["家属端 App 链接（手机浏览器打开 → 菜单“添加到主屏幕”，之后像 App 一样点开就用）："]
    for c in cfg["contacts"]:
        lines.append("  %s：%s/ui/family/?as=%s&key=%s" % (c["name"], url, c["id"], token))
    lines += ["", "床头盒（手板）配置：",
              "  FAMILY_PUSH_URL=%s/api/voice_msg" % url,
              "  FAMILY_PULL_URL=%s/api/family_replies" % url,
              "  FAMILY_PULL_TOKEN=%s" % token,
              "", "（免费隧道地址每次启动都会变；钥匙不变。Ctrl+C 关闭隧道）"]
    text = "\n".join(lines)
    print("\n" + text, flush=True)
    out = config.CODE_DIR / "data" / "public_link.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + text + "\n", encoding="utf-8")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
