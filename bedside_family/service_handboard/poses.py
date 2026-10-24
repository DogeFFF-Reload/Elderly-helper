"""姿态词汇表 —— 全系统唯一动作真源。

LLM / 手势识别 / 状态机 只许引用这里的名字；
想加新动作 = 在这里加一行 + 手动示教补偿一次。

值为 {关节: 开合度0~100}，0=全握 100=全开。
"""
from __future__ import annotations

# 简写 joint 名（与 register_map.JOINT_ORDER 对齐）
TH, IX, MI, RG, PK, TR = "thumb", "index", "middle", "ring", "pinky", "thumb_rot"

POSE_VOCAB: dict[str, dict[str, float]] = {
    # ---- 猜拳三兄弟 ----
    "rock":     {TH: 10, IX: 10, MI: 10, RG: 10, PK: 10, TR: 50},
    "paper":    {TH: 90, IX: 95, MI: 95, RG: 95, PK: 95, TR: 50},
    "scissors": {TH: 15, IX: 90, MI: 90, RG: 10, PK: 10, TR: 50},
    # ---- 破冰互动 ----
    "ok":        {TH: 45, IX: 45, MI: 90, RG: 90, PK: 90, TR: 50},
    "thumbs_up": {TH: 95, IX: 10, MI: 10, RG: 10, PK: 10, TR: 60},
    "fingers_1": {TH: 10, IX: 90, MI: 10, RG: 10, PK: 10, TR: 50},
    "fingers_2": {TH: 10, IX: 90, MI: 90, RG: 10, PK: 10, TR: 50},
    "fingers_3": {TH: 10, IX: 90, MI: 90, RG: 90, PK: 10, TR: 50},
    "fingers_4": {TH: 10, IX: 90, MI: 90, RG: 90, PK: 90, TR: 50},
    "fingers_5": {TH: 90, IX: 95, MI: 95, RG: 95, PK: 95, TR: 50},
    # ---- 任务姿态 ----
    "hold_card":   {TH: 40, IX: 40, MI: 10, RG: 10, PK: 10, TR: 70},  # 捏住卡片
    "release":     {TH: 90, IX: 95, MI: 95, RG: 95, PK: 95, TR: 50},  # 松手
    "grip_pillow": {TH: 30, IX: 30, MI: 30, RG: 30, PK: 30, TR: 55},  # 按键用的"按"

    # ---- 手指操（健康/康复向）微姿态：节奏要慢,宁可柔不要猛 ----
    "fist_soft":  {TH: 40, IX: 12, MI: 12, RG: 14, PK: 16, TR: 50},  # 松握拳(拇指不压死)
    "cup":        {TH: 55, IX: 60, MI: 55, RG: 55, PK: 60, TR: 50},  # 半握"捧杯"过渡位
    "curl_pk":    {TH: 55, IX: 85, MI: 80, RG: 70, PK: 18, TR: 50},  # 波浪:小指先卷
    "curl_pk_rg": {TH: 55, IX: 85, MI: 80, RG: 18, PK: 18, TR: 50},  # 波浪:+无名指
    "curl_mid":   {TH: 55, IX: 85, MI: 18, RG: 18, PK: 18, TR: 50},  # 波浪:+中指
    "curl_all":   {TH: 70, IX: 18, MI: 18, RG: 18, PK: 18, TR: 55},  # 波浪尾:四指全卷
    "pinch_1":    {TH: 65, IX: 20, MI: 85, RG: 85, PK: 85, TR: 55},  # 对指:拇↔食
    "pinch_2":    {TH: 25, IX: 85, MI: 20, RG: 85, PK: 85, TR: 55},  # 对指:拇↔中
    "pinch_3":    {TH: 30, IX: 85, MI: 85, RG: 20, PK: 85, TR: 55},  # 对指:拇↔无名
    "pinch_4":    {TH: 35, IX: 85, MI: 85, RG: 85, PK: 20, TR: 55},  # 对指:拇↔小指
}

# 挥手 = 姿态时间序列（由 arm_service 执行），不是单一姿态
SEQUENCES: dict[str, list[tuple[str, float]]] = {
    "wave": [("paper", 0.4), ("rock", 0.3), ("paper", 0.4), ("rock", 0.3), ("paper", 0.3)],
    "nod_like": [("thumbs_up", 0.5), ("release", 0.2), ("thumbs_up", 0.6)],
    # ---- 手指操带练（老人向陪玩：康复操节拍） ----
    "clench_open":   [("rock", 0.7), ("paper", 0.7)],          # 握拳→张开 一组
    "count_five":    [("fingers_1", 0.7), ("fingers_2", 0.7), ("fingers_3", 0.7),
                      ("fingers_4", 0.7), ("fingers_5", 0.8)],  # 数一到五
    # ---- 轻握安抚（手#2 专用：老人慌时的"拍拍"，慢速才像安抚） ----
    "comfort_rhythm": [("rock", 1.1), ("paper", 1.1), ("rock", 1.1),
                       ("paper", 1.1), ("fingers_5", 1.2)],
    # ---- 手指操全套（健康康复向 ~37s：热身→波浪卷指→对指→数数→收操） ----
    "hand_exercise": [
        # 第一节 热身：呼吸感开合 ×2
        ("cup", 0.9), ("fist_soft", 1.0), ("cup", 0.9), ("paper", 0.9),
        # 第二节 波浪卷指：小指→无名→中→食依次卷,整手展开,再来一轮(快些)
        ("curl_pk", 0.45), ("curl_pk_rg", 0.45), ("curl_mid", 0.45), ("curl_all", 0.5),
        ("paper", 0.6),
        ("curl_pk", 0.35), ("curl_pk_rg", 0.35), ("curl_mid", 0.4), ("curl_all", 0.5),
        ("paper", 0.6),
        # 第三节 对指循环：拇→食/中/无名/小,经典精细动作(捏合感)
        ("pinch_1", 0.55), ("pinch_2", 0.55), ("pinch_3", 0.55), ("pinch_4", 0.55),
        ("pinch_2", 0.45),
        # 第四节 数数：1→5 逐指展开定格
        ("fist_soft", 0.5), ("fingers_1", 0.65), ("fingers_2", 0.65), ("fingers_3", 0.65),
        ("fingers_4", 0.65), ("fingers_5", 0.9),
        # 收操：深握一拍,缓开展手
        ("fist_soft", 1.1), ("paper", 1.0),
    ],
    # ---- 猜拳互动（陪伴彩蛋：连续出拳像在"跟你玩"） ----
    "rps_game": [
        ("rock", 1.0), ("scissors", 0.9), ("paper", 1.0),
        ("rock", 0.7), ("scissors", 0.7), ("paper", 0.9), ("ok", 0.6),
    ],
}

ALL_ACTIONS = set(POSE_VOCAB) | set(SEQUENCES)
