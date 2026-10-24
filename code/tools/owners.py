"""模块归属（v2.5 §7.3）：run_all 和 merge_gate 共用这一张表，改分工只改这里。"""

ROLES = {"software": "软件", "hardware": "硬件", "demo": "演示", "captain": "队长"}

# 路径（相对仓库根）→ 负责角色；按最长前缀匹配
PATHS = {
    "code/master_service.py": "software",
    "code/state_machine/": "software",
    "code/agent/supervisor.py": "software",
    "code/agent/skills.json": "software",
    "code/agent/llm_client.py": "software",
    "code/agent/intent.py": "software",
    "code/config.py": "software",
    "code/demo_driver.py": "software",
    "code/tests/": "software",
    "code/tools/": "software",
    "code/hand_controller/": "hardware",
    "firmware/": "hardware",
    "code/requirements-hardware.txt": "hardware",
    "code/vision/": "demo",
    "code/camera/": "demo",
    "code/gesture/": "demo",
    "code/requirements-vision.txt": "demo",
    "code/requirements-phonecam.txt": "demo",
    "deploy/": "demo",
    "code/ui/": "captain",
    "code/voice/": "captain",
    "code/requirements-voice.txt": "captain",
    "code/agent/health_tracker.py": "captain",
    "code/agent/voice_profile.py": "captain",
    "code/agent/guides.json": "captain",
    "code/agent/device_helper.py": "captain",
    "code/agent/lines.json": "captain",
}

# 只由软件改的"交通枢纽"（§7.3 协作规则 2）：别人的分支动了它，merge_gate 直接拦
HUB = ("code/master_service.py",)

# 回归测试套 → 负责角色（§7.3"负责的测试"）。新加测试记得登记，run_all 会提示
TESTS = {
    "test_fsm_escalation": ("software",),
    "test_agent_supervisor": ("software",),
    "test_help_chain": ("software",),
    "test_bus_http": ("software",),
    "test_llm_fallback": ("software",),
    "test_config_params": ("software",),
    "test_demo_driver": ("software",),
    "test_merge_gate": ("software",),
    "test_arm_link": ("software",),
    "test_estop": ("software",),
    "test_preflight": ("software",),
    "test_handover_chain": ("hardware",),
    "test_exercise": ("hardware",),
    "test_hand_driver": ("hardware",),
    "test_rps_engine": ("demo",),
    "test_fall_detector": ("demo",),
    "test_phone_fall_watch": ("demo",),
    "test_phone_calibrate": ("demo",),
    "test_phone_calibration_report": ("demo",),
    "test_phone_pose": ("demo",),
    "test_rps_hook": ("software",),
    "test_vision_teach_report": ("demo", "captain"),  # 跌倒/夜间归演示，教学/日报/健康归队长
    "test_voice_profile": ("captain",),
    "test_asr_chain": ("captain",),
    "test_health_readback": ("captain",),
    "test_hardware_preflight": ("hardware",),
}


def owner_of(path):
    """返回负责角色；测试文件按 TESTS 归属；不在表里返回 None（README、docs 等公共文件）。"""
    path = path.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if path.startswith("code/tests/test_") and name.endswith(".py"):
        roles = TESTS.get(name[:-3])
        return roles[0] if roles else None
    best = None
    for prefix, role in PATHS.items():
        if (path == prefix or (prefix.endswith("/") and path.startswith(prefix))) and \
                (best is None or len(prefix) > len(best[0])):
            best = (prefix, role)
    return best[1] if best else None


def role_of_branch(branch):
    """分支按 角色/主题 命名：captain/interaction、demo/vision、software/core、hardware/rh56。
    远程分支（origin/demo/vision）先去掉远程名再认。"""
    parts = branch.split("/")
    if len(parts) > 2 and parts[0] not in ROLES and parts[1] in ROLES:
        parts = parts[1:]
    return parts[0] if parts[0] in ROLES else None


def names(roles):
    return "、".join(ROLES.get(r, r) for r in roles)
