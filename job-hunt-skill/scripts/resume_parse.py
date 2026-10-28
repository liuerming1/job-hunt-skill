#!/usr/bin/env python3
"""resume_parse.py — 简历转纯文本（Python 标准库，零依赖）。

支持：.txt / .md 直接读；.docx 解 zip 抽 word/document.xml；
.pdf 尽力提取（FlateDecode 流内 BT..ET 文本），失败时如实报告。

用法：
  python resume_parse.py <简历路径> [--out 输出.json]

输出 JSON：{"source", "method", "chars", "warnings", "text"}
PDF/docx 提取质量有限，text 过短时 warnings 会提示降级为用户粘贴。
"""

import argparse
import json
import re
import sys
import zipfile
import zlib
from pathlib import Path

MIN_USEFUL_CHARS = 200


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 清零宽字符与不可见控制符（保留换行制表）
    text = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()


def parse_txt(path: Path) -> tuple[str, str, list[str]]:
    warnings = []
    text = _decode_bytes(path.read_bytes())
    if "\ufffd" in text:
        warnings.append("文本中存在无法解码的字符，可能有乱码")
    return _clean(text), "text", warnings


def parse_docx(path: Path) -> tuple[str, str, list[str]]:
    warnings = []
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError) as e:
        return "", "docx", [f"docx 解析失败：{e}（请让用户粘贴文本）"]
    # 段落换行
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html
    text = html.unescape(text)
    return _clean(text), "docx", warnings


def parse_pdf(path: Path) -> tuple[str, str, list[str]]:
    """尽力而为：解压 FlateDecode 流，抽 Tj/TJ 括号文本。复杂 PDF（图片型、
    自定义编码字体）提不出来，此时如实告警，降级为用户粘贴。"""
    warnings = []
    chunks: list[str] = []
    raw = path.read_bytes()
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.DOTALL):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass  # 未压缩流直接用
        # BT..ET 文本块里的 (str) Tj 与 [..] TJ
        for block in re.finditer(rb"BT(.*?)ET", data, re.DOTALL):
            body = block.group(1)
            parts = re.findall(rb"\(((?:[^()\\]|\\.)*)\)", body)
            if parts:
                line = b"".join(parts)
                chunks.append(line)
    text = b" ".join(chunks)
    out = _decode_bytes(text)
    # PDF 字符串转义还原
    out = out.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
    out = _clean(out)
    if len(out) < MIN_USEFUL_CHARS:
        warnings.append(
            f"PDF 仅提取到 {len(out)} 字符（可能为图片型/嵌入字体 PDF），"
            "请降级：让用户直接粘贴简历文本"
        )
    return out, "pdf", warnings


PARSERS = {
    ".txt": parse_txt,
    ".md": parse_txt,
    ".markdown": parse_txt,
    ".docx": parse_docx,
    ".pdf": parse_pdf,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="简历转纯文本")
    ap.add_argument("resume", help="简历文件路径")
    ap.add_argument("--out", help="输出 JSON 路径（缺省打印到 stdout）")
    args = ap.parse_args()

    path = Path(args.resume)
    if not path.exists():
        print(json.dumps({"error": f"文件不存在：{path}"}, ensure_ascii=False))
        return 2

    ext = path.suffix.lower()
    parser = PARSERS.get(ext)
    if parser is None:
        print(json.dumps(
            {"error": f"不支持的格式 {ext}（支持 {', '.join(PARSERS)}）；"
                      "其他格式请让用户另存为 txt/md 或直接粘贴文本"},
            ensure_ascii=False))
        return 2

    text, method, warnings = parser(path)
    result = {
        "source": str(path),
        "method": method,
        "chars": len(text),
        "warnings": warnings,
        "text": text,
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"已写入 {args.out}（{method}，{len(text)} 字符，"
              f"{len(warnings)} 条警告）")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
