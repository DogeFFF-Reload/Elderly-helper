"""拖动示教：X5 上把臂切到重力补偿（kp = 0，只抵消重力），用手把臂摆到位，敲点位名回车就记进 arm_skills.json。

先停 arm_service（SDK 有单实例锁，两个程序不能同时控臂）：Ctrl+C，或 sudo systemctl stop caremate-arm
  ~/panthera_venv/bin/python -m hand_controller.arm_teach [--effector gripper|hand1]

命令（敲完回车）：
  <点位名>   记下当前关节角（taught = true）。先查限位、禁区、桌面，不过会提示，但照样记（禁区可能还没量）
  where      当前关节角和末端位置
  list       所有点位和示教状态
  q          退出：先在当前位置保持住，再问要不要慢慢回 rest
记完把 X5 上的 code/hand_controller/arm_skills.json 拷回笔记本提交，跑一遍 python tests/run_all.py arm。

安全：两人操作，一人扶臂一人敲键盘，手边就是物理急停。X5 上固件失联保护是关着的（motor_timeout_ms
必须为 0），程序卡死时电机会一直顶着最后一份重力补偿力矩，换了姿态就不再平衡：扶住臂，拍急停。
正常退出（q、Ctrl+C）时 SDK 让电机阻尼，不在 rest 的话臂会慢慢下沉。
"""
import argparse
import json
import queue
import sys
import threading
import time

from config import utf8_console
from hand_controller import arm_safety as S
from hand_controller.arm_backend import REST, ArmError, PantheraArm
from hand_controller.arm_kinematics import tool_position

TAU_LIMIT = (15.0, 30.0, 30.0, 15.0, 5.0, 5.0)   # 重力补偿力矩限幅，照 SDK 的 2_gravity_compensation_control.py


def dump_skills(raw):
    """arm_skills.json 的固定格式：第二层每项一行。示教只改动被记的那一行，git diff 好看。"""
    def one(v):
        return json.dumps(v, ensure_ascii=False)

    lines = ["{"]
    items = list(raw.items())
    for i, (k, v) in enumerate(items):
        tail = "," if i < len(items) - 1 else ""
        if isinstance(v, dict):
            inner = ["    %s: %s" % (one(ik), one(iv)) for ik, iv in v.items()]
            lines.append("  %s: {\n%s\n  }%s" % (one(k), ",\n".join(inner), tail))
        elif isinstance(v, list):
            inner = ["    %s" % one(iv) for iv in v]
            lines.append("  %s: [\n%s\n  ]%s" % (one(k), ",\n".join(inner), tail))
        else:
            lines.append("  %s: %s%s" % (one(k), one(v), tail))
    lines.append("}")
    return "\n".join(lines) + "\n"


def save_point(name, q, path=None):
    """把 q 记成点位 name（新点位就新建），返回更新后的点位。点位上的 note、near_person 等原样保留。"""
    path = path or S.SKILLS_FILE
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    p = raw["points"].setdefault(name, {})
    p["q"] = [round(float(x), 4) for x in q]
    p["taught"] = True
    p["taught_at"] = time.strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_skills(raw))
    return p


def check_report(q, cfg):
    """记点之前查一遍：返回问题列表（空 = 没问题）。"""
    problems = []
    try:
        S.check_limits(q)
    except S.SafetyError as e:
        problems.append(str(e))
    try:
        S.check_pose(q, cfg)
    except S.SafetyError as e:
        problems.append(str(e))
    return problems


def _reader(lines):
    for line in iter(sys.stdin.readline, ""):
        lines.put(line.strip())
    lines.put("q")


def main():
    utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--effector", choices=("gripper", "hand1"), help="末端（决定末端位置怎么算）；不设：装了夹爪就是 gripper")
    args = ap.parse_args()
    import numpy as np   # 只在 X5 上跑，SDK 环境里有

    arm = PantheraArm()
    ok, why = arm.connect()
    if not ok:
        raise SystemExit(why + "（arm_service 还在跑的话先停掉它）")
    r = arm.robot
    cfg = S.load_config(args.effector or ("gripper" if arm.has_gripper else "hand1"))
    print(__doc__.split("\n\n")[2])
    input("扶住臂，回车开始重力补偿（松手前确认臂能自己停住）……")
    lines = queue.Queue()
    threading.Thread(target=_reader, args=(lines,), daemon=True).start()
    zero = [0.0] * r.motor_count
    limit = np.array(TAU_LIMIT)
    try:
        while True:
            tor = np.clip(r.get_Gravity(), -limit, limit)
            r.pos_vel_tqe_kp_kd(zero, zero, tor, zero, zero)
            try:
                cmd = lines.get_nowait()
            except queue.Empty:
                time.sleep(0.002)
                continue
            q = [float(x) for x in r.get_current_pos()]
            if not cmd:
                continue
            if cmd == "q":
                break
            if cmd == "where":
                print("关节角 %s°，末端 %s m" % (S.deg(q), [round(c, 3) for c in tool_position(q, cfg["tool_length"])]))
            elif cmd == "list":
                for n, p in S.load_config(cfg["effector"])["points"].items():
                    print("  %-12s %s  %s" % (n, "已示教" if p.get("taught") else "占位  ", p.get("note", "")))
            else:
                for problem in check_report(q, cfg):
                    print("  ⚠ %s" % problem)
                save_point(cmd, q)
                print("记下 %s = %s°" % (cmd, S.deg(q)))
    except KeyboardInterrupt:
        pass
    try:
        arm.hold()
    except ArmError as e:
        print("没能在当前位置保持：%s。扶住臂，拍急停" % e)
        return
    print("已在当前位置保持。慢慢回 rest？[y/N] ", end="", flush=True)
    try:
        back = lines.get(timeout=60).lower() == "y"   # stdin 归读线程，这里不能再 input()
    except (queue.Empty, KeyboardInterrupt):
        back = False
    if back:
        q = arm.joints()
        try:
            S.check_segment(q, REST, cfg, "rest")
            arm.move(REST, S.duration(q, REST, cfg, slow=True), threading.Event())
        except Exception as e:  # noqa: BLE001
            print("没回 rest：%s。扶住臂再退出" % e)
    print("退出后电机进入阻尼：不在 rest 的话臂会慢慢下沉，扶一下。")


if __name__ == "__main__":
    main()
