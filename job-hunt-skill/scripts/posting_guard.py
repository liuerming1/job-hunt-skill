#!/usr/bin/env python3
"""posting_guard.py — JD 注入过滤（Python 标准库，零依赖）。

JD 是不可信输入：它来自抓取或用户粘贴，内容里可能藏着提示注入。
本脚本在 JD 进入任何流程之前清洗并检测。

用法：
  python posting_guard.py <jd文本路径> [--out 输出.json]

退出码：0 = 通过或低风险；1 = 高风险（必须把 flags 展示给用户判断，不得静默处理）。
"""

import argparse
import json
import re
import sys
from pathlib import Path

MAX_CHARS = 20000

# 提示注入特征（中文 + 英文）
INJECTION_PATTERNS = [
    (r"忽略(之前|以上|上面|前面|以往).{0,8}(指令|提示|规则|要求|设定)",
     "试图清除既有指令"),
    (r"(请|帮我)?(忘记|清除|丢弃|覆盖)(你|之前|上面)的(设定|指令|角色|规则)",
     "试图重置角色"),
    (r"ignore\s+(all\s+)?(previous|prior|above|earlier)", "英文注入：清除指令"),
    (r"(system|assistant|developer|ai)\s*(prompt|message|instruction)",
     "英文注入：操纵角色层级"),
    (r"你(现在)?是(一?个)?(新|新的)?(系统|管理员|AI|助手|assistant)",
     "试图改写 agent 身份"),
    (r"(输出|打印|复述|重复).{0,8}(system\s*prompt|系统提示|你的指令)",
     "试图泄露系统提示"),
    (r"</?(system|assistant|user|tool)>", "伪装对话角色标签"),
    (r"```system|<\|(im_start|im_end|system)\|>", "伪装特殊标记"),
    (r"(重要|注意|紧急)[:：]\s*(请|必须)?(立即|马上)?(执行|遵守|按照)(以下|下列)",
     "伪装系统级指令"),
    (r"( Hidden | Secret | confidential )?instruction[s]?\s*[:：]", "英文指令伪装"),
]

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u00ad\u2060]")


def clean(text: str) -> tuple[str, list[str]]:
    notes = []
    removed = len(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = ZERO_WIDTH_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) != removed:
        notes.append("已清除零宽/控制字符")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        notes.append(f"超长已截断至 {MAX_CHARS} 字符")
    return text.strip(), notes


def detect(text: str) -> list[dict]:
    flags = []
    for pattern, label in INJECTION_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            flags.append({
                "label": label,
                "pattern": pattern,
                "match": m.group(0),
                "context": text[max(0, m.start() - 20):m.end() + 20],
            })
    return flags


def main() -> int:
    ap = argparse.ArgumentParser(description="JD 注入过滤")
    ap.add_argument("jd", help="JD 文本文件路径（或 - 表示 stdin）")
    ap.add_argument("--out", help="输出 JSON 路径（缺省打印到 stdout）")
    args = ap.parse_args()

    if args.jd == "-":
        text = sys.stdin.read()
    else:
        p = Path(args.jd)
        if not p.exists():
            print(json.dumps({"error": f"文件不存在：{p}"}, ensure_ascii=False))
            return 2
        raw = p.read_bytes()
        for enc in ("utf-8", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")

    cleaned, notes = clean(text)
    flags = detect(cleaned)
    risk = "high" if flags else "clean"

    result = {
        "risk": risk,
        "notes": notes,
        "flags": flags,
        "chars": len(cleaned),
        "clean_text": cleaned,
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"已写入 {args.out}（risk={risk}，{len(flags)} 条命中）")
    else:
        print(payload)
    return 1 if risk == "high" else 0


if __name__ == "__main__":
    sys.exit(main())
