# -*- coding: utf-8 -*-
"""_svc_boot.py — PC 冒烟共用:把 service_handboard 以 hand_controller 包形态导入。

板上部署形态 = /root/caremate/hand_controller/(python -m hand_controller.arm_service);
仓库里目录名叫 service_handboard,这里用 namespace 包注册对齐,两边运行形态一致。

serial / vosk 缺库自动打桩:PC 冒烟只碰纯函数与 HTTP,不触真串口/真模型;
pyserial 没装的机器也能跑(agent 冒烟、词表测试均不需要真 IO)。
"""
import importlib
import os
import sys
import types


def import_service():
    here = os.path.dirname(os.path.abspath(__file__))
    service_dir = os.path.normpath(os.path.join(here, "..", "..", "service_handboard"))
    if "hand_controller" not in sys.modules:
        pkg = types.ModuleType("hand_controller")
        pkg.__path__ = [service_dir]
        sys.modules["hand_controller"] = pkg
    for name in ("serial", "vosk"):
        try:
            __import__(name)
        except Exception:
            stub = types.ModuleType(name)
            if name == "serial":
                stub.Serial = object
                stub.SerialBase = object
            else:
                stub.Model = object
                stub.KaldiRecognizer = object
                stub.SetLogLevel = lambda *a: None
            sys.modules.setdefault(name, stub)
    return importlib.import_module("hand_controller.arm_service")