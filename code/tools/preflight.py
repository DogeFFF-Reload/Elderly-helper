"""联调自检：一条命令看主脑、执行端、喇叭、麦克风、视觉、床头盒、LLM 是否就位，坏了告诉你去哪修。

  python -m tools.preflight                                     # 本机主脑
  python -m tools.preflight --server http://192.168.1.20:8000   # 在任意一台机器上查 X5-B
  python -m tools.preflight --wait 60                           # 等到没有 ❌ 或超时（开机后 / 开演前）

部署按两块 X5：X5-A 视觉；X5-B 主脑 + 三块屏 + 喇叭/麦克风 + 执行端（USB-RS485 连双手）。
❌ = 主线（康 / 递 / 救）会受影响；⚠️ = 有兜底，或只是提醒。没有 ❌ 退出码 0，有 ❌ 为 1，连不上主脑为 2。
数据来自主脑的 GET /api/nodes，自检本身不触发任何动作。
"""
import argparse
import json
import sys
import time
import unicodedata
import urllib.error
import urllib.request

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
FRESH = 5.0  # 订阅者多少秒内来拉过事件算在线（tts_player 每 0.3 秒拉一次）


def _fresh(clients, name):
    age = clients.get(name)
    return age is not None and age < FRESH


def check(n):
    """/api/nodes 的结果 → [(级别, 项目, 现状, 怎么修)]。纯函数，回归测试直接喂假数据。"""
    rows = []

    def add(level, name, text, fix=""):
        rows.append((level, name, text, fix))

    bus = n["bus"]
    if bus["state"] == "IDLE":
        add(OK, "状态机", "空闲%s" % ("（夜间模式）" if bus.get("night") else ""))
    else:
        add(WARN, "状态机", "正在 %s，上一段还没收尾" % bus["state"], "开演前在操作台点“重置”")

    if n.get("estop"):
        add(FAIL, "急停", "软件急停未解除：臂和双手不会动",
            "确认现场安全后在操作台点“解除急停”，或 POST /api/estop {\"on\": false}")

    tz = n.get("tz")
    if tz:
        offset, expected = tz["offset"], tz["expected"]
        if abs(offset - expected) >= 0.01:
            add(WARN, "时区", "本机 UTC%+g，应为 UTC%+g：夜间模式和健康日报的日期会错" % (offset, expected),
                "X5 上 sudo timedatectl set-timezone Asia/Shanghai 后重启主脑；"
                "演示时夜间段用操作台“夜间模式”开关，不依赖钟点")

    arm = n["arm"]
    if arm.get("online") is False:
        add(FAIL, "执行端", "连不上 %s" % arm.get("url", ""),
            "X5-B 上 bash deploy/start.sh arm（笔记本：python -m hand_controller.arm_service）；"
            "地址不对改 CAREMATE_ARM_URL。Plan B：手走 mock + 大屏动画")
    elif arm.get("online") is None:
        add(WARN, "执行端", "还没探测到（主脑刚启动？）", "过两秒再查一次")
    elif arm.get("failed") and "rejected" in (arm.get("last_error") or ""):
        add(WARN, "执行端", "在线，但有动作被拒绝：%s" % arm["last_error"],
            "动作名和 hand_controller/poses.py 对不上，找硬件同学")
    elif arm.get("mock"):
        add(WARN, "执行端", "在线，但是模拟（mock）：臂和手不会真动，大屏“床边执行端”照常演",
            "真手就绪设 MOCK_SERIAL=0、真臂就绪设 ARM_BACKEND=panthera 后重启 arm_service；演示时这就是 Plan B，台词别说“真机”")
    else:
        add(OK, "执行端", "在线 %s%s" % (arm.get("url", ""), "，已做 %d 个动作" % arm["done"] if arm.get("done") else ""))

    clients = n.get("clients", {})
    unnamed = [k for k, age in clients.items() if k.startswith("未标名@") and age < FRESH]
    if _fresh(clients, "tts_player"):
        add(OK, "喇叭", "tts_player 在接收台词")
    elif unnamed:
        add(WARN, "喇叭", "有 %d 个没报名字的台词订阅者，可能是旧版 tts_player" % len(unnamed),
            "更新 voice/bus_client.py 后重启 tts_player，自检就能认出来")
    else:
        add(FAIL, "喇叭", "没有 tts_player 在接收台词，AI 说的话没人念",
            "X5-B 上 bash deploy/start.sh tts（笔记本：python -m voice.tts_player）；"
            "兜底：大屏右上角“大屏播报”")

    heard = n.get("heard")
    if _fresh(clients, "asr_bridge"):
        add(OK, "麦克风", "asr_bridge 在线" + ("，刚听到“%s”" % heard["text"] if heard and heard["age"] < 60 else ""))
    elif heard:
        add(OK, "麦克风", "%g 秒前听到“%s”" % (heard["age"], heard["text"]))
    else:
        add(WARN, "麦克风", "没有 asr_bridge，也还没听到过老人说话",
            "X5-B 上 bash deploy/start.sh asr（笔记本：python -m voice.asr_bridge）；兜底：操作台“让老人说”输入框")

    vision = n["vision"]
    if vision["online"]:
        add(OK, "视觉", "骨架在推（%g 秒前）" % vision["age"])
    else:
        add(WARN, "视觉", "%g 秒没收到骨架" % vision["age"] if vision.get("age") is not None else "还没收到过骨架",
            "X5-A 上 bash deploy/start.sh vision；Plan B：fall_bridge --video 预录视频，或操作台“触发摔倒”")

    box = n["box"]
    if box["online"]:
        add(OK, "床头盒", "%s 心跳正常" % box["id"])
    else:
        add(WARN, "床头盒", "没有心跳" if box.get("age") is None else "%g 秒没心跳" % box["age"],
            "固件每 0.5 秒 GET /api/box?id=box1（README §10.6）；兜底：操作台“绿键”按钮")

    llm = n["llm"]
    if llm["mode"] == "scripted":
        add(OK, "LLM", "scripted（台词预编排，零网络）")
    elif not llm["enabled"]:
        add(WARN, "LLM", "llm 模式但接口没配全，台词全部走 scripted", "config.local.json 填 llm_base_url / llm_model")
    elif llm.get("cooldown_left") or llm.get("last_error"):
        add(WARN, "LLM", "最近出错：%s（已自动回落 scripted，不影响演示）" % llm.get("last_error"),
            "会场网络不稳就切回 scripted")
    else:
        add(OK, "LLM", "llm 通道正常（已润色 %d 句，回落 %d 句）" % (llm.get("llm", 0), llm.get("fallback", 0)))

    changed = n.get("params_changed", {})
    if changed:
        add(WARN, "演示参数", "和默认值不同：" + "，".join(
            "%s=%s（默认 %s）" % (k, _num(v), _num(d)) for k, (v, d) in sorted(changed.items())),
            "确认是有意改的；/api/params 改的重启主脑就恢复")
    else:
        add(OK, "演示参数", "全是默认值")
    return rows


def _num(v):
    return "%g" % v if isinstance(v, (int, float)) else v


def _pad(text, width):
    """按显示宽度补空格（中文占两格），让中英文项目名对齐。"""
    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - used)


def report(rows, server, out=print):
    out("CareMate 联调自检 · %s" % server)
    for level, name, text, fix in rows:
        out("  %s %s%s" % (ICON[level], _pad(name, 10), text))
        if fix and level != OK:
            out("     %s→ %s" % (" " * 10, fix))
    fails = [name for level, name, _, _ in rows if level == FAIL]
    warns = sum(1 for level, *_ in rows if level == WARN)
    out("")
    if fails:
        out("还不能开演：先修 %s" % "、".join(fails))
    else:
        out("可以开演" + ("（%d 项有兜底或提醒，见 ⚠️）" % warns if warns else ""))
    return 1 if fails else 0


def fetch(server, timeout=3.0):
    with urllib.request.urlopen(server.rstrip("/") + "/api/nodes", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main(argv=None, out=print, sleep=time.sleep):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="联调自检：各节点是否就位")
    ap.add_argument("--server", default="http://127.0.0.1:8000", help="主脑地址（X5-B）")
    ap.add_argument("--wait", type=float, default=0, help="最多等多少秒，直到没有 ❌")
    args = ap.parse_args(argv)

    deadline = time.monotonic() + args.wait
    while True:
        try:
            rows = check(fetch(args.server))
        except urllib.error.HTTPError as e:
            out("❌ 主脑在线，但没有 /api/nodes（HTTP %d）：X5-B 上的代码不是最新版，先 git pull 再重启主脑" % e.code)
            return 2
        except (urllib.error.URLError, OSError, ValueError) as e:
            rows = None
            why = getattr(e, "reason", e)
        fails = [r[1] for r in rows if r[0] == FAIL] if rows else ["主脑"]
        if not fails or time.monotonic() >= deadline:
            break
        out("… 还差 %s，继续等" % "、".join(fails))
        sleep(2)
    if rows is None:
        out("❌ 主脑连不上 %s（%s）" % (args.server, why))
        out("   → X5-B 上 bash deploy/start.sh master（笔记本：python -m master_service）；"
            "从别的机器查时，主脑要 --host 0.0.0.0 并放行 8000 端口")
        return 2
    return report(rows, args.server, out)


if __name__ == "__main__":
    sys.exit(main())
