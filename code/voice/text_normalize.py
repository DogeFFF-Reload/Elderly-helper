"""把 ASR 输出整理成和手打 /mock/voice 一样的文本。

离线 ASR（如 Vosk 中文模型）常见输出："一 百 三十 八 比 八十 五"、"帮 我 打 幺 二 零"。
整理后："138比85"、"帮我打120"。函数幂等：已经是阿拉伯数字的文本原样返回。
"""
import re
import unicodedata

DIGITS = {"零": 0, "〇": 0, "一": 1, "幺": 1, "二": 2, "两": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
UNITS = {"十": 10, "百": 100, "千": 1000}
_NUM_RUN = re.compile("[%s]+" % "".join(list(DIGITS) + list(UNITS)))
_CJK = r"[㐀-鿿，。？！、：]"
# 只去掉挨着汉字的空格；"138 85" 这种数字之间的空格要留着（是两个数）
_SPACE_BETWEEN = re.compile(r"(?<=%s)\s+(?=%s|\d)|(?<=\d)\s+(?=%s)" % (_CJK, _CJK, _CJK))
# 单独一个"嗯/啊"多半是噪音或口头填充；"对"、"是"等短回答必须保留
FILLERS = {"嗯", "啊", "呃", "额", "哦", "唉", "哎"}


def chinese_to_int(run):
    """'一百三十八'→138，'一二零'→120，'一百五'→150，'十八'→18。无法解析返回 None。"""
    if not run:
        return None
    if all(ch in DIGITS for ch in run):
        return int("".join(str(DIGITS[ch]) for ch in run))
    total, current, last_unit = 0, None, None
    for ch in run:
        if ch in DIGITS:
            current = DIGITS[ch]
        elif ch in UNITS:
            unit = UNITS[ch]
            total += (1 if current is None else current) * unit
            current, last_unit = None, unit
        else:
            return None
    if current is not None:
        # "一百五" 口语里是 150；"一百零五" 中"零"已把 current 置 0 后再被 5 覆盖 → 105
        if last_unit and last_unit >= 100 and "零" not in run:
            current *= last_unit // 10
        total += current
    return total


def _convert_runs(text):
    def repl(m):
        run = m.group(0)
        # 单字（"一下"、"两杯"、"十分"）语义太多，保持原样
        if len(run) < 2:
            return run
        n = chinese_to_int(run)
        return run if n is None else str(n)

    return _NUM_RUN.sub(repl, text)


def join_cjk(text):
    """去掉汉字之间的空格（Vosk 中文按词输出带空格），不转数字。"""
    return _SPACE_BETWEEN.sub("", unicodedata.normalize("NFKC", text or "").strip())


def normalize(text):
    if not text:
        return ""
    return _convert_runs(join_cjk(text)).strip()


def is_filler(text):
    core = re.sub(r"[\s，。？！,.?!~～]", "", text or "")
    return core == "" or core in FILLERS
