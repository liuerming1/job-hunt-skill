#!/usr/bin/env python3
"""BOSS 直聘搜索页抓取（2026-10-28 校准版）。

关键事实（真实登录态实测）：
- 搜索页 DOM：li.job-card-box，链接 a[href*="/job_detail/"]
- 金华 city code = 101210900（站点真实返回）
- 薪资等数字用 PUA 字符（E031-E03A）+ kanzhun-mix 字体混淆，
  canvas 像素比对实测映射：E031→0 … E03A→9
"""
import json
import random
import sys
import time
import urllib.parse

sys.path.insert(0, __file__.rsplit("/", 1)[0] if "/" in __file__ else ".")
from boss_cdp import CDP  # noqa: E402

PUA_MAP = {0xE031 + i: str(i) for i in range(10)}


def decode_pua(text: str) -> str:
    return "".join(PUA_MAP.get(ord(ch), ch) for ch in text)


EXTRACT_JS = """
(() => {
  const cards = [...document.querySelectorAll('li.job-card-box')];
  return cards.map(c => {
    const a = c.querySelector('a[href*="/job_detail/"]');
    const txt = sel => {
      const el = c.querySelector(sel);
      return el ? el.textContent.trim() : '';
    };
    const line = c.innerText.replace(/\\n+/g, '\\u0001');
    return {
      href: a ? a.getAttribute('href') : '',
      raw: line,
      salary: txt('.job-salary'),
      title: txt('.job-name') || txt('[class*="title"]')
    };
  });
})()
"""


def parse_card(card: dict) -> dict:
    """从 raw 行解析字段（title|salary|exp|edu|company|location 顺序实测成立）。"""
    parts = [p.strip() for p in card["raw"].split("\u0001") if p.strip()]
    salary = decode_pua(card.get("salary") or "")
    out = {
        "salary": salary,
        "url": "https://www.zhipin.com" + card["href"] if card["href"] else "",
    }
    # title: 第一段（有时含薪资混入，剔除已解出的 salary 段）
    non_salary = [p for p in parts if p and p != card.get("salary", "@none@")]
    out["title"] = decode_pua(non_salary[0]) if non_salary else ""
    # 从后往前：location, company, edu, exp
    if len(parts) >= 5:
        out["location"] = decode_pua(parts[-1])
        out["company"] = decode_pua(parts[-2])
        out["edu"] = decode_pua(parts[-3])
        out["exp"] = decode_pua(parts[-4])
        if not out["title"]:
            out["title"] = decode_pua(parts[0])
    elif parts:
        out["title"] = decode_pua(parts[0])
    return out


def extract_current(keyword: str, city_label: str = "金华") -> list:
    cdp = CDP()
    cards = cdp.evaluate(EXTRACT_JS, timeout=20)
    if not isinstance(cards, list):
        return []
    jobs = []
    for c in cards:
        job = parse_card(c)
        job["keyword"] = keyword
        job["city"] = city_label
        if job["url"]:
            jobs.append(job)
    return jobs


def goto_search(query: str, city: str = "101210900"):
    cdp = CDP()
    url = ("https://www.zhipin.com/web/geek/jobs?city=" + city
           + "&query=" + urllib.parse.quote(query))
    cdp.call("Page.navigate", {"url": url})
    time.sleep(random.uniform(4, 6))


def main():
    import json as _json
    # 目标城市前缀：从数据层 config.json 读，读不到默认金华
    global CITY_PREFIX
    try:
        _cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 os.pardir, "job-hunt", "config.json")
        _cfg = _json.load(open(os.path.normpath(_cfg_path), encoding="utf-8"))
        CITY_PREFIX = _cfg.get("目标城市", "金华") or "金华"
    except Exception:
        CITY_PREFIX = "金华"
    plan = [
        ("少儿编程", "D1"),
        ("编程教研", "D1"),
        ("课程研发", "D1"),
        ("编程讲师", "D2"),
        ("信奥", "D2"),
        ("课程运营", "D3"),
        ("教育运营", "D3"),
    ]
    all_jobs = []
    for i, (kw, direction) in enumerate(plan):
        if i == 0:
            jobs = extract_current(kw)  # 当前页已是该关键词结果
        else:
            goto_search(kw)
            jobs = extract_current(kw)
        # 列表页城市预筛：BOSS 本地供给不足时会混入外地相似职位（2026-10-28 实测），
        # 卡片地点字段非目标城市的直接丢弃，不进详情抓取
        dropped = [j for j in jobs if CITY_PREFIX not in j.get("location", "")]
        for j in dropped:
            print(f"  [城市预筛丢弃] {j['title'][:18]} @ {j.get('location', '?')}", flush=True)
        jobs = [j for j in jobs if CITY_PREFIX in j.get("location", "")]
        for j in jobs:
            j["direction"] = direction
        all_jobs.extend(jobs)
        print(f"{kw} [{direction}]: {len(jobs)} 条（预筛丢弃 {len(dropped)}）", flush=True)
        time.sleep(random.uniform(3, 5))

    out = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        os.pardir, "job-hunt", "data"))
    import os
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "postings_raw.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "postings": all_jobs}, f, ensure_ascii=False, indent=1)
    print("SAVED", path, "total", len(all_jobs))


if __name__ == "__main__":
    main()
