""" 台词预生成（edge-tts）→ tts_cache/*.mp3

    pip install edge-tts
    python -m tools.gen_tts         # 生成全部
    python -m tools.gen_tts --only fall_ask
现场播放： mpg123 tts_cache/<name>.mp3  或 X5 直接 aplay。
写死演员名单：所有语音都出自这里，断网 = 还有全部本地 MP3。
"""
from __future__ import annotations
import argparse
import asyncio
import os

import edge_tts   # 若 ImportError: pip install edge-tts

VOICE = os.environ.get("TTS_VOICE", "zh-CN-XiaoyiNeural")   # 女声温柔
RATE = os.environ.get("TTS_RATE", "-10%")                    # 慢速版默认
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "tts_cache")

LINES = {
    "greet":      "见到你真开心！",
    "rps_intro":  "来！石头，剪刀，布！",
    "rps_win":    "这局让您了，让您啦。",
    "rps_lose":   "体力活交给手，脑子才是我的！",
    "rps_draw":   "心有灵犀，再来一局！",
    "med_note":   "药给您带过来了，记得按时吃。",
    "phone_note": "手机在这儿，给你放床头。",
    "msg_relay":  "女儿留言：今晚晚点回，不用等她吃饭！",
    "got_ack":    "好的，家人知道你收到啦！",
    "checkin":    "孩子到家喽，放心吧！",
    "fall_ask":   "奶奶,您还好吗？能说话吗？",
    "fall_ask2":  "奶奶,听得见我吗？不舒服要告诉我哦。",
    "fall_reassure": "别怕,女儿已经在路上了,您先别起身哦。",
    "fall_care":  "不要起身,保持呼吸,我在这里陪您。",
    "cancel":     "好,已经取消了。需要我告诉家人吗？",
    "family_coming": "女儿说,她在路上,马上到！",
    "daily_report": "今天按时吃了药,孩子来过电话,整体都很平稳哦。",
    "introduce_self": "我是Mocen_CareMate,床边的一双手。会打招呼、猜拳、递药,摔了帮您按救命键。",
    "boot_check": "自检完成,所有手指都听指挥,今天也要元气满满哦！",
}


async def gen(name: str, text: str, rate: str):
    mp3 = os.path.join(OUT, "%s_%s.mp3" % (name, rate_remove(rate)))
    tts = edge_tts.Communicate(text, VOICE, rate=rate)
    await tts.save(mp3)
    print("✓", mp3)


def rate_remove(r: str) -> str:
    return ("fast" if "+" in r else "slow")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--both", action="store_true", help="同时生成慢速+快速版本")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    if a.only:
        if a.only not in LINES:
            raise SystemExit("no line: %s" % a.only)
        await gen(a.only, LINES[a.only], RATE)
        return
    names = LINES if not a.both else {k: v for k in LINES for v in (k,)}
    for n, t in LINES.items():
        await gen(n, t, RATE)
    if a.both:
        FAST = os.environ.get("TTS_RATE_FAST", "+10%")
        for n, t in LINES.items():
            await gen(n, t, FAST)


if __name__ == "__main__":
    asyncio.run(main())
