#!/usr/bin/env python3
"""job_key.py — 职位去重（Python 标准库，零依赖）。

去重键 = 公司名归一化 + 职位名归一化 + 城市。
同一职位多个 HR 发：按最低薪资保留一条，其余 URL 并入 also_at。

用法：
  python job_key.py <postings.json> [--out 输出.json]

输入格式：{"postings": [ {...}, ... ]}，每条至少含 company/title/city，
薪资字段 salary（如 "15-25K·13薪"）可选。
输出格式：{"postings": [去重后的列表], "duplicates_removed": n}
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

COMPANY_SUFFIXES = [
    "股份有限公司", "有限责任公司", "有限公司", "集团公司", "集团",
    "（北京）", "(北京)", "（上海）", "(上海)", "（深圳）", "(深圳)",
    "（杭州）", "(杭州)", "（广州）", "(广州)",
]

# 职位名里可剥离的修饰词（剥掉后不影响「是不是同一个岗位」的判断）
TITLE_NOISE = [
    r"[（(【\[][^）)】\]]*[）)】\]]",   # 各类括号内容：急聘、双休、base地等
    r"急聘|急招|高薪|直招|最新|热招",
    r"\s+",
]


def norm_company(name: str) -> str:
    s = (name or "").strip().lower()
    for suf in COMPANY_SUFFIXES:
        s = s.replace(suf, "")
    s = re.sub(r"\s+", "", s)
    return s


def norm_title(title: str) -> str:
    s = (title or "").strip().lower()
    for pat in TITLE_NOISE:
        s = re.sub(pat, "", s)
    return s.strip()


def salary_min(salary: str):
    """'15-25K·13薪' -> 15；解析不了返回 None（排序时视为无穷大）。"""
    if not salary:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*[kK千]", str(salary))
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK千]", str(salary))
    if m:
        return float(m.group(1))
    return None


def job_key(posting: dict) -> str:
    company = norm_company(posting.get("company", ""))
    title = norm_title(posting.get("title", ""))
    city = re.sub(r"\s+", "", (posting.get("city") or "").strip().lower())
    raw = f"{company}|{title}|{city}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def dedupe(postings: list[dict]) -> tuple[list[dict], int]:
    best: dict[str, dict] = {}
    order: list[str] = []
    removed = 0

    for p in postings:
        key = job_key(p)
        if key not in best:
            p.setdefault("also_at", [])
            best[key] = p
            order.append(key)
            continue
        removed += 1
        kept = best[key]
        dup_min = salary_min(p.get("salary"))
        kept_min = salary_min(kept.get("salary"))
        # 按最低薪资保留；薪资都解析不了时保留先出现的那条
        if dup_min is not None and (kept_min is None or dup_min < kept_min):
            p.setdefault("also_at", [])
            p["also_at"] += kept.get("also_at", [])
            if kept.get("url"):
                p["also_at"].append(kept["url"])
            p["dedupe_note"] = (
                f"同键多条，保留最低薪资（{p.get('salary')} vs {kept.get('salary')}）"
            )
            best[key] = p
        else:
            if p.get("url"):
                kept.setdefault("also_at", []).append(p["url"])

    return [best[k] for k in order], removed


def main() -> int:
    ap = argparse.ArgumentParser(description="职位去重")
    ap.add_argument("postings", help="postings JSON 文件路径")
    ap.add_argument("--out", help="输出 JSON 路径（缺省打印到 stdout）")
    args = ap.parse_args()

    p = Path(args.postings)
    if not p.exists():
        print(json.dumps({"error": f"文件不存在：{p}"}, ensure_ascii=False))
        return 2
    data = json.loads(p.read_text(encoding="utf-8"))
    items = data.get("postings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        print(json.dumps(
            {"error": "输入需为 {\"postings\": [...]} 或数组"}, ensure_ascii=False))
        return 2

    kept, removed = dedupe(items)
    result = {"postings": kept, "duplicates_removed": removed}
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"已写入 {args.out}（{len(kept)} 条保留，{removed} 条去重）")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
