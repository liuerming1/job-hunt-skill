#!/usr/bin/env python3
"""BOSS 详情页抓取（2026-10-28 校准版）。

- 正文：.job-detail-section 逐段 innerText（7 段含职位描述/亮点/工作地址等）
- 数字 PUA 混淆同搜索页，decode_pua 处理
- checkpoint：每抓 1 条即落盘，断点续跑
"""
import json
import os
import random
import re
import sys
import time
import urllib.parse

import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boss_cdp import CDP  # noqa: E402

PUA_MAP = {0xE031 + i: str(i) for i in range(10)}


def decode_pua(text: str) -> str:
    return "".join(PUA_MAP.get(ord(ch), ch) for ch in text)


DATA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(os.path.dirname(DATA), "job-hunt", "data")

IN_PATH = os.path.join(DATA, "postings_gated.json")
OUT_PATH = os.path.join(DATA, "postings_detail.json")

DETAIL_JS = """
(() => {
  const secs = [...document.querySelectorAll('.job-detail-section')]
    .map(e => e.innerText.trim()).filter(t => t.length > 4);
  const sals = [...document.querySelectorAll('.job-salary, [class*="salary"]')]
    .map(e => e.textContent.trim());
  // 结构化字段区（经验/学历等）
  const items = [...document.querySelectorAll('.job-detail-item, .detail-items')].map(e => e.innerText.trim());
  return {secs, sals, items, title: document.title, url: location.href.slice(0, 120),
          bodyLen: document.body ? document.body.innerText.length : 0};
})()
"""


def load_out() -> dict:
    if os.path.exists(OUT_PATH):
        return json.load(open(OUT_PATH, encoding="utf-8"))
    return {"details": {}}


def save_out(out: dict):
    tmp = OUT_PATH + ".tmp"
    json.dump(out, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, OUT_PATH)


def main():
    postings = json.load(open(IN_PATH, encoding="utf-8"))["postings"]
    out = load_out()
    done = out["details"]
    todo = [p for p in postings if p["url"] not in done]
    print(f"todo {len(todo)} / {len(postings)}", flush=True)

    cdp = CDP()
    for i, p in enumerate(todo):
        url = p["url"]
        try:
            cdp.call("Page.navigate", {"url": url})
        except Exception as e:
            print("nav err", e, flush=True)
            time.sleep(5)
            continue
        time.sleep(random.uniform(4.5, 7))
        try:
            d = cdp.evaluate(DETAIL_JS, timeout=25)
        except Exception as e:
            print("eval err", type(e).__name__, e, flush=True)
            continue
        if d.get("bodyLen", 0) < 300 or "security" in d.get("url", ""):
            print(f"[{i+1}/{len(todo)}] SECURITY/empty, backoff", flush=True)
            time.sleep(random.uniform(15, 25))
            continue
        rec = {
            "secs": [decode_pua(s) for s in d["secs"]],
            "salary_detail": decode_pua(" | ".join(d["sals"])[:200]),
            "items": [decode_pua(x) for x in d["items"]],
            "page_title": d["title"],
        }
        done[url] = rec
        save_out(out)
        jd_head = (rec["secs"][0] if rec["secs"] else "")[:50].replace("\n", " ")
        print(f"[{i+1}/{len(todo)}] ok {p['title'][:20]} :: {jd_head}", flush=True)

    print("DONE", len(done), flush=True)


if __name__ == "__main__":
    main()
