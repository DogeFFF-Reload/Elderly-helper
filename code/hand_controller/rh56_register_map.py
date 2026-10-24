"""RH56 灵巧手寄存器表 —— 摘自《因时机器人仿人五指灵巧手--RH56用户手册V1.09cn》（RS485）。

地址是手册 §2.4 的十进制地址，单位是字节：一个 short 占两个地址，低字节在前。
通信走手册 §2.2 的因时自有协议（包头 EB 90，0x11 读 / 0x12 写，见 rh56f2_driver）。
手册里的 Modbus RTU 没写清"Modbus 寄存器号和这张表的地址怎么对应"，所以不用它。

VERIFIED 改成 True 之前，真机模式只读不写。改之前要确认：
  1. 手上型号是 RH56F2-E2…（RS485 版）。E4… 是 CAN FD 版，帧格式完全不同（见《CAN增补协议V0.0.2cn》）
  2. 24V 上电后 `arm_service --probe` 读 ANGLE_ACT 能读到 6 个 0~1000 的数
F2 系列是否和这本 V1.09（RH56 通用手册）完全一致，以 F2 自己的资料为准，有出入改这里。
"""

VERIFIED = False

# 通信（手册 p7、p16）
SLAVE_ID_HAND1 = 1        # HAND_ID 出厂默认 1
SLAVE_ID_HAND2 = 2        # ⚠ 出厂也是 1：两只手接同一条总线前，先用官方上位机把手#2 改成 2；各接各的串口就不用改
BAUD = 115200             # REDU_RATIO 默认 0 = 115200
PARITY = "N"
STOPBITS = 1
TIMEOUT = 0.3             # 等回包的秒数

# 寄存器（手册 §2.4，十进制字节地址）
REG_ANGLE_SET = 1486      # 6 short：目标角度 -1、0~1000（1000 完全张开，0 握紧，-1 该指不动）
REG_FORCE_SET = 1498      # 6 short：力控阈值 0~1000，单位 g，手指受力到这个值就停
REG_SPEED_SET = 1522      # 6 short：速度 0~1000（1000 ≈ 空载全行程 800ms）
REG_ANGLE_ACT = 1546      # 6 short：实际角度 0~1000（只读）
REG_FORCE_ACT = 1582      # 6 short：实际受力 g（只读）
REG_ERROR = 1606          # 6 byte：故障位 bit0 堵转 / bit1 过温 / bit2 过流 / bit3 电机异常 / bit4 通讯（只读）
REG_STATUS = 1612         # 6 byte：状态（只读）
REG_TEMP = 1618           # 6 byte：温度 ℃（只读）

# 驱动只许写这几组（左闭右开的字节地址）。HAND_ID(1000)、REDU_RATIO(1002)、SAVE(1005)、
# RESET_PARA(1006) 这类会改 ID / 波特率 / 写 FLASH / 恢复出厂的，一律不许程序写，要改用官方上位机
WRITABLE = (
    (REG_ANGLE_SET, REG_ANGLE_SET + 12),
    (REG_FORCE_SET, REG_FORCE_SET + 12),
    (REG_SPEED_SET, REG_SPEED_SET + 12),
)

VALUE_MIN, VALUE_MAX = 0, 1000   # 角度：开合度 0（握紧）→ 0，100（张开）→ 1000
FORCE_MAX = 1000                 # g

# 6 个寄存器依次对应的关节（手册 p18-19）；thumb = 拇指弯曲，thumb_rot = 拇指旋转
JOINT_ORDER = ["pinky", "ring", "middle", "index", "thumb", "thumb_rot"]


# ---- 兼容 tools/hardware_preflight（队友的只读前置检查）----
PROVISIONAL = not VERIFIED      # 旧名：True = 寄存器表还没核对，真机只读


def confirmed_from_environment(value=None):
    """现场操作员的二次确认（环境变量 RH56_REGMAP_CONFIRMED=1）。只给前置检查报告用；
    驱动本身只认 VERIFIED。"""
    if value is not None:
        return bool(value)
    import os
    return os.environ.get("RH56_REGMAP_CONFIRMED", "0").strip().lower() in {"1", "true", "yes", "on"}
