"""arm_backend_vendor.py —— 臂(Panthera-HT)真实驱动的【唯一接入点】。

arm_service 在 ARM_BACKEND=vendor_sdk 时优先调用本文件的 execute()。
契约（与 arm_execute 完全同签名，主脑/视觉零感知）：
    execute(waypoint_seq_name: str) -> dict   # {"ok": bool, "did": str, ...}

WAYPOINT 词表（arms_service.WAYPOINTS 里出现的语义动作，本文件负责映射到 SDK）：
    home / above_sos / press_sos / above_tray / pick_med / pick_item
    bedside / bedside_low

明天 SDK 拆盒后三步接线（对应 docs/22 §8/§9）：
    1) SDK 装进 X5（Linux aarch64），确认官方 import 名
    2) 把上面的 WAYPOINT 语义动作映射成 SDK 的关节/位姿调用
       （现场示教出的点写进 arm_service.WAYPOINTS 也行——两种路线选一）
    3) 想要"物理到位确认"：每个 move 后读手 REG_STATUS(FC03) 或臂 SDK 的到位接口

禁区：没有 SDK 资料就【不许瞎编调用】——返回 ok=False + 原因，永不伪装成功。
"""
from __future__ import annotations

BRAND_SDK_IMPORTED = False
try:
    import panthera_sdk as _brand   # ← 官方 SDK 到位后改成真实 import 名
    BRAND_SDK_IMPORTED = True
    _BRAND = _brand
except ImportError:
    pass


def execute(waypoint_seq_name: str) -> dict:
    if not BRAND_SDK_IMPORTED:
        return {"ok": False,
                "error": "panthera SDK not installed (明天拆盒) — "
                         "vendor backend 未接入, 拒绝伪装成功"}
    # —— SDK 装好后从这里开始填真实现（示教点 → SDK 调用）——
    raise NotImplementedError(
        "SDK 已可 import 但 WAYPOINT->SDK 映射未实现: "
        "按 docs/22 §9 填 execute() (seq=%s)" % waypoint_seq_name)