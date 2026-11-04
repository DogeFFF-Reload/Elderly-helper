"""RH56F2 寄存器映射 —— 现场唯一必改文件（对照官方手册 v1.09）。

⚠️ 下面的地址全部是**占位值**，`:TODO:` 标记的都是上真机前必须核对替换的。
只改这里，driver / service 不用动。

单位约定：本仓库内部统一用「开合度 0~100」（0=全握，100=全开），
driver 负责换算成寄存器原始值。
"""

SLAVE_ID_HAND1 = 1        # 手#1（在臂上）
SLAVE_ID_HAND2 = 2        # 手#2（床头旁）
BAUD = 115200
PARITY = "N"
STOPBITS = 1

# ---- :TODO: 现场核对的寄存器（占位地址，来源见 docs/10） ----
REG_TARGET_BASE = 0x0100        # 目标位置：6 个连续寄存器 (FC16 批量写)
REG_SPEED_BASE  = 0x0106        # 目标速度（可留默认）
REG_STATUS      = 0x0200        # 状态/错误码 (FC03 读)
REG_CONTROL     = 0x0210        # 使能/控制字（若需要）

SCALE = 100.0                   # 寄存器满量程 := 100 度的原始值 → :TODO: 核实
VALUE_MIN, VALUE_MAX = 0, 1000  # 原始值范围 :TODO: 核实（16bit？）

# 关节 → 寄存器偏移序列（6 自由度）：thumb, index, middle, ring, pinky, thumb_rot
JOINT_ORDER = ["thumb", "index", "middle", "ring", "pinky", "thumb_rot"]
#:TODO: 核对第 6 关节到底是拇指旋转还是别的
