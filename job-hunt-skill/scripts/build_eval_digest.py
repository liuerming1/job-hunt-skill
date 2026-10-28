#!/usr/bin/env python3
"""把 postings_detail.json 压缩成打分摘要（工时/学历信号 + JD 全文归档）。"""
import json
import os
import re

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    os.pardir, "job-hunt", "data")
DATA = os.path.normpath(DATA)

detail = json.load(open(os.path.join(DATA, "postings_detail.json"), encoding="utf-8"))["details"]
gated = json.load(open(os.path.join(DATA, "postings_gated.json"), encoding="utf-8"))["postings"]

WORK_PAT = re.compile(r"双休|单休|大小周|周末双休|月休(\d)|做六休一|做五休二|上六休一|周末上班|排课|加班")
EDU_PAT = re.compile(r"(统招|全日制|非全日制|本科及以上|大专及以上|硕士|研究生|学历不限|中专|高中)")

out = []
for p in gated:
    d = detail.get(p["url"])
    if not d:
        p["detail_missing"] = True
        out.append(p)
        continue
    jd_text = "\n".join(d["secs"])
    p["jd_text"] = jd_text
    work_hits = sorted(set(WORK_PAT.findall(jd_text)) | set(WORK_PAT.findall(p.get("title", ""))))
    # 直接找工时相关句子
    work_snips = []
    for m in re.finditer(r"[^。;；\n]{0,18}(双休|单休|大小周|月休|做六休一|做五休二|排课|加班)[^。;；\n]{0,18}", jd_text):
        s = m.group(0).strip()
        if s not in work_snips:
            work_snips.append(s)
    p["work_signals"] = work_snips[:6]
    p["detail_items"] = d["items"][:6]
    out.append(p)

json.dump({"postings": out}, open(os.path.join(DATA, "postings_for_eval.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

have_jd = sum(1 for p in out if p.get("jd_text"))
print(f"total {len(out)}, with jd {have_jd}")

# 工时信号分布
from collections import Counter
sig = Counter()
for p in out:
    ws = " ".join(p.get("work_signals", []))
    if "双休" in ws or "做五休二" in ws or re.search(r"月休[6-9]|月休8", ws):
        sig["rest_ok"] += 1
    elif "单休" in ws or "大小周" in ws or "做六休一" in ws or "上六休一" in ws:
        sig["rest_bad"] += 1
    else:
        sig["unknown"] += 1
print(sig)
