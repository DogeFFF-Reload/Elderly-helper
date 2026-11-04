"""真臂自检：一人守急停，一人跑这条命令。验的是执行端这条路，不是演示动作。

先在 X5-B 上用小角度点位文件起 arm_service：
  cd ~/Elderly-helper/code
  ARM_BACKEND=panthera ARM_EFFECTOR=gripper ARM_SKILLS_FILE=hand_controller/arm_selftest.json \\
      ~/panthera_venv/bin/python -m hand_controller.arm_service --host 0.0.0.0
再在笔记本（或 X5 另开一个终端）：
  python -m hand_controller.arm_selftest --server http://192.168.127.10:8013

每一步动臂之前等回车（守急停的人说"好"再按）；任何时候 Ctrl+C，脚本先发 /stop 再退出。
  1. 读状态：真臂、末端夹爪、自检点位文件、能动
  2. home：回 rest
  3. sign_family_received：J2、J3 各抬 20°，停住（看下垂多少）
  4. press_sos：走到左转那一段时脚本发 /stop，看臂当场停住保持
  5. home：从停住的地方回 rest（第一段慢速）
  6. deliver_medicine：夹爪开 → 抬 → 合 → 左右转 → 开 → 回 rest
最后汇总每步用时和各关节到位偏差；然后在 arm_service 窗口按 Ctrl+C，看它退出时回 rest、转阻尼。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from config import utf8_console


class SelftestError(Exception):
    pass


class Client:
    def __init__(self, server, poll=0.05):
        self.base = server.rstrip("/")
        self.poll = poll

    def _call(self, method, path):
        req = urllib.request.Request(self.base + path, data=b"" if method == "POST" else None, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def arm(self):
        code, body = self._call("GET", "/status")
        if code != 200:
            raise SelftestError("GET /status 回了 %d" % code)
        return body["arm"]

    def pose(self, name):
        code, body = self._call("POST", "/pose?name=%s&hand=1" % name)
        if code != 200:
            raise SelftestError("%s 被拒（%d）：%s" % (name, code, body.get("error")))

    def stop(self):
        return self._call("POST", "/stop")[0] == 200

    def wait(self, pred, timeout, what):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            st = self.arm()
            if pred(st):
                return st
            time.sleep(self.poll)
        raise SelftestError("%d 秒内没等到：%s（臂状态 %s）" % (timeout, what, self.arm()))

    def wait_idle(self, timeout):
        return self.wait(lambda s: not s["busy"] and not s["queued"], timeout, "臂做完、空下来")


def run(server, confirm=input, out=print, poll=0.05, allow_mock=False, timeout=90):
    """跑完返回 True；哪一步不对抛 SelftestError（调用方负责发 /stop）。"""
    c = Client(server, poll)
    report = []

    def ask(what):
        confirm("\n→ 下一步让臂：%s。守急停的人准备好了按回车（Ctrl+C 退出）" % what)

    def done(step, st, started):
        settle = st.get("settle") or {}
        report.append((step, time.monotonic() - started, settle.get("point"), settle.get("err_deg"), st.get("last_error")))
        out("  ✓ %s：%.1f 秒，停在 %s，各关节偏差 %s°" % (step, report[-1][1], st.get("at"), settle.get("err_deg")))

    st = c.arm()
    out("臂：%s，末端 %s，关节角 %s°" % (st["backend"], st.get("effector"), st.get("joints_deg")))
    if st["backend"] != "panthera" and not allow_mock:
        raise SelftestError("arm_service 没连真臂（ARM_BACKEND=panthera）")
    if st.get("effector") != "gripper":
        raise SelftestError("末端不是夹爪（ARM_EFFECTOR=gripper）")
    if st.get("untaught") != []:
        raise SelftestError("没用自检点位文件起服务（ARM_SKILLS_FILE=hand_controller/arm_selftest.json），没示教的点位：%s"
                            % st.get("untaught"))
    if st.get("error"):
        raise SelftestError("臂报错：%s" % st["error"])

    for step, pose, what, check in (
            ("回 rest", "home", "回到 rest（收拢）", lambda s: s["at"] == "rest"),
            ("抬起", "sign_family_received", "J2、J3 各抬 20°（小臂平着抬高约 9 cm）并停住", lambda s: s["at"] == "lift")):
        ask(what)
        t0 = time.monotonic()
        c.pose(pose)
        st = c.wait_idle(timeout)
        if not check(st):
            raise SelftestError("%s 没做完：%s" % (step, st.get("last_error")))
        done(step, st, t0)

    ask("抬着左转 15°；转到一半脚本发 /stop，臂应当场停住")
    t0 = time.monotonic()
    aborted = st["aborted"]
    c.pose("press_sos")
    c.wait(lambda s: "turn_l" in (s.get("step") or ""), timeout, "走到左转那一段")
    c.stop()
    st = c.wait_idle(10)
    if st["aborted"] != aborted + 1 or st["at"] is not None:
        raise SelftestError("/stop 之后臂没有停在半路（aborted %s → %s，at = %s）" % (aborted, st["aborted"], st["at"]))
    done("急停半路", st, t0)

    for step, pose, what in (("停住后回 rest", "home", "从停住的地方慢速回 rest"),
                             ("递药流程", "deliver_medicine", "夹爪开 → 抬 → 合 → 左右各转 15° → 开 → 回 rest")):
        ask(what)
        t0 = time.monotonic()
        c.pose(pose)
        st = c.wait_idle(timeout)
        if st["at"] != "rest":
            raise SelftestError("%s 没做完：%s" % (step, st.get("last_error")))
        done(step, st, t0)

    out("\n自检通过。各步到位偏差（°，看 J2/J3 下垂多少）：")
    for step, secs, point, err, _ in report:
        out("  %-8s %5.1f 秒  %-7s %s" % (step, secs, point, err))
    out("最后：在 arm_service 窗口按 Ctrl+C，看它退出时回 rest、转阻尼。")
    return True


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8013")
    args = ap.parse_args()
    c = Client(args.server)
    try:
        run(args.server)
        return
    except KeyboardInterrupt:
        why = "按了 Ctrl+C"
    except (SelftestError, urllib.error.URLError, OSError) as e:
        why = "自检没过：%s" % e
    try:
        sent = c.stop()
    except (urllib.error.URLError, OSError):
        sent = False
    print("\n%s。%s" % (why, "已发 /stop，臂停住保持。" if sent else "/stop 没发出去：用操作台 Esc 或物理急停。"))
    raise SystemExit(1)


if __name__ == "__main__":
    main()
