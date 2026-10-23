"""RH56F2 寄存器映射 —— 上真机前唯一必改的文件（对照官方手册）。来源：caremate_hand2_kit。

⚠️ 下面的地址、量程、关节顺序全部是**占位值**（:TODO:），手册到手前不要信。
核对完把 VERIFIED 改成 True，真机模式才会发动作；之前真机模式只读（rh56f2_driver 会拒绝写）。

还没定的大前提：手到底走 RS485 Modbus RTU（驱动现在就是这么写的），还是 CAN / CAN-FD
（随货的蓝盒丝印 COM-USB-CANFD-01）。如果是 CAN，rh56f2_driver 的帧格式要整个换，这个文件只是开始。

单位约定：仓库内部统一用「开合度 0~100」（0=全握，100=全开），driver 负责换算成寄存器原始值。
"""

VERIFIED = False          # :TODO: 按手册核对完下面每一项后改成 True

SLAVE_ID_HAND1 = 1        # 手#1（在臂上）
SLAVE_ID_HAND2 = 2        # 手#2（床头旁）
BAUD = 115200             # :TODO:
PARITY = "N"
STOPBITS = 1
TIMEOUT = 0.3             # 等回包的秒数

# ---- :TODO: 现场核对的寄存器（占位地址） ----
REG_TARGET_BASE = 0x0100        # 目标位置：6 个连续寄存器（FC16 批量写）
REG_SPEED_BASE = 0x0106         # 目标速度：6 个
REG_FORCE_BASE = 0x010C         # 力度上限：6 个（轻握安抚靠它限力，§5.2）
REG_STATUS = 0x0200             # 状态 / 错误码（FC03 读 2 个）

VALUE_MIN, VALUE_MAX = 0, 1000  # 原始值范围 :TODO: 核实；开合度 0 → VALUE_MIN，100 → VALUE_MAX

# 6 个寄存器依次对应的关节 :TODO: 核对顺序（不少灵巧手手册是小指在前、拇指旋转在最后），
# 以及第 6 个到底是不是拇指旋转
JOINT_ORDER = ["thumb", "index", "middle", "ring", "pinky", "thumb_rot"]
