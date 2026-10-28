#!/usr/bin/env python3
"""boss_cdp.py — BOSS 直聘 CDP 抓取（Python 标准库，零依赖）。

通道 A 的实现：接管一个已登录 BOSS 的 Chrome（--remote-debugging-port=9222），
通过 CDP 的 Runtime.evaluate 在真实页面里执行 JS 提取。不装 playwright、
不装 websocket-client——WebSocket 握手与帧编解码在本文件内手写实现。

用法：
  python boss_cdp.py check                        验证 CDP 连接，列出可用标签页
  python boss_cdp.py search "<搜索页URL>" <out.json>   抓搜索结果列表（≤10 条）
  python boss_cdp.py detail "<职位详情URL>" <out.json>  抓单个职位详情页全文

环境变量：CDP_PORT（默认 9222）、CDP_HOST（默认 127.0.0.1）。

注意：内置页面选择器按 2026-10 的页面结构编写，BOSS 改版会失效。
字段抓不全时脚本自动降级为整页 innerText，由 agent 负责从文本结构化。
"""

import base64
import hashlib
import json
import os
import random
import socket
import struct
import sys
import time
import urllib.request

CDP_HOST = os.environ.get("CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 每方向硬上限（与 SKILL.md / 08-boss-portal.md 一致）
SEARCH_LIMIT = 10
# 请求间随机间隔（秒）——低频，模拟人的浏览
DELAY_RANGE = (3.0, 8.0)

# 搜索列表卡片选择器：按命中优先级排列，BOSS 改版时改这里
SEARCH_CARD_SELECTORS = [
    ".job-card-wrapper",
    ".job-card-box",
    "li[ka^='search_list']",
]

# 详情页 JD 正文选择器：逐个尝试，全失则整页 innerText 兜底
DETAIL_BODY_SELECTORS = [
    ".job-sec-text",
    ".job-detail-section",
    ".job-detail",
    ".detail-content",
]


# ---------------------------------------------------------------- WebSocket

class WS:
    """最小 WebSocket 客户端：握手、文本帧收发、ping/pong。够 CDP 用。"""

    def __init__(self, host: str, port: int, path: str):
        self.sock = socket.create_connection((host, port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手被对端关闭")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        status = head.decode("latin-1").split("\r\n")[0]
        if " 101 " not in f"{status} ":
            raise ConnectionError(f"WebSocket 握手失败：{status}")
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if accept not in head.decode("latin-1"):
            raise ConnectionError("Sec-WebSocket-Accept 校验失败")
        self.buf = rest
        self._id = 0

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("连接被对端关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, s: str) -> None:
        payload = s.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_msg(self, timeout: float = 30.0) -> str:
        self.sock.settimeout(timeout)
        while True:
            two = self._read(2)
            first, second = two[0], two[1]
            opcode = first & 0x0F
            ln = second & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else None
            data = self._read(ln)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, data)
                continue
            if opcode == 0x8:
                raise ConnectionError("对端关闭了连接")
            if opcode in (0x1, 0x2):
                return data.decode("utf-8", errors="replace")
            # 0x0 continuation：本场景消息不分片，忽略

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)


# ---------------------------------------------------------------- CDP 调用

class CDP:
    def __init__(self, target_url_fragment: str = "zhipin.com"):
        targets = self._http_targets()
        page = next((t for t in targets
                     if t.get("type") == "page"
                     and target_url_fragment in (t.get("url") or "")), None)
        if page is None:
            page = next((t for t in targets if t.get("type") == "page"), None)
            if page:
                print("[warn] 未找到 zhipin.com 标签页，使用第一个标签页："
                      f"{page.get('url')}", file=sys.stderr)
        if page is None:
            raise ConnectionError("没有可用的浏览器标签页。请确认 Chrome 已带 "
                                  "--remote-debugging-port=9222 启动且有打开的页面")
        self.target = page
        self._id = 0
        ws_url = page["webSocketDebuggerUrl"]
        assert ws_url.startswith("ws://")
        rest = ws_url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.ws = WS(host or CDP_HOST, int(port or CDP_PORT), "/" + path)

    @staticmethod
    def _http_targets() -> list[dict]:
        url = f"http://{CDP_HOST}:{CDP_PORT}/json"
        # 强制直连：系统代理会把 127.0.0.1 的请求也截走，Chrome 挂掉时报 502 掩盖真实错误
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def call(self, method: str, params: dict | None = None,
             timeout: float = 30.0) -> dict:
        self._id += 1
        # 注意：CDP 不是 JSON-RPC——消息必须平铺 {id, method, params}，
        # 带 "jsonrpc" 键 Chrome 会静默丢弃请求（实测 2026-10-28）
        msg = {"id": self._id, "method": method, "params": params or {}}
        self.ws.send_text(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = json.loads(self.ws.recv_msg(timeout=max(1, deadline - time.time())))
            if resp.get("id") == self._id:
                if "error" in resp:
                    raise RuntimeError(f"CDP 错误：{resp['error']}")
                return resp.get("result", {})
        raise TimeoutError(f"CDP 调用超时：{method}")

    def evaluate(self, expr: str, timeout: float = 30.0) -> object:
        result = self.call("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True,
                            "awaitPromise": True}, timeout=timeout)
        value = result.get("result", {})
        if value.get("subtype") == "error":
            raise RuntimeError(f"页面执行出错：{value.get('description')}")
        return value.get("value")

    def navigate_and_settle(self, url: str, wait_seconds: float = 8.0,
                            poll_expr: str | None = None) -> None:
        """导航并等待渲染：先固定等待，再轮询 poll_expr 直到非空或超时。"""
        self.call("Page.navigate", {"url": url})
        time.sleep(wait_seconds)
        if poll_expr:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    if self.evaluate(poll_expr):
                        return
                except RuntimeError:
                    pass
                time.sleep(1.5)


# ---------------------------------------------------------------- 提取 JS

# 整页兜底提取（选择器全失效时用，agent 负责从文本结构化）
JS_INNERTEXT = "document.body ? document.body.innerText : ''"

JS_SEARCH = """
() => {
  const sels = %s;
  let cards = [];
  for (const s of sels) {
    cards = [...document.querySelectorAll(s)];
    if (cards.length) break;
  }
  if (!cards.length) return {cards: [], fallback: document.body ? document.body.innerText.slice(0, 8000) : ''};
  return {
    cards: cards.slice(0, %d).map(c => {
      const t = sel => { const el = c.querySelector(sel); return el ? el.innerText.trim() : null; };
      const a = c.querySelector('a[href]');
      return {
        title: t('.job-name') || t('.job-title'),
        salary: t('.salary'),
        company: t('.company-name') || t('.name'),
        tags: t('.job-info') || t('.filter-labels'),
        company_tags: t('.company-tag-list'),
        hr: t('.info-pub') || t('.job-info-footer'),
        url: a ? a.href : null,
        raw: c.innerText.replace(/\\n+/g, ' | ').slice(0, 500),
      };
    }),
    fallback: null,
  };
}
"""

JS_DETAIL = """
() => {
  const sels = %s;
  let body = null;
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && el.innerText.trim().length > 100) { body = el.innerText.trim(); break; }
  }
  return {
    body,
    fallback: body ? null : (document.body ? document.body.innerText : ''),
    title: document.querySelector('.job-banner .name, .name, h1') ?
           document.querySelector('.job-banner .name, .name, h1').innerText.trim() : null,
    url: location.href,
  };
}
"""


def human_delay() -> None:
    d = random.uniform(*DELAY_RANGE)
    print(f"[wait] 随机间隔 {d:.1f}s（低频抓取）", file=sys.stderr)
    time.sleep(d)


def cmd_check() -> int:
    try:
        targets = CDP._http_targets()
    except Exception as e:
        print(json.dumps({"ok": False,
                          "error": f"连不上 CDP（{CDP_HOST}:{CDP_PORT}）：{e}。"
                                   "请确认 Chrome 已带 --remote-debugging-port=9222 启动"},
                         ensure_ascii=False))
        return 1
    pages = [{"title": t.get("title"), "url": t.get("url")}
             for t in targets if t.get("type") == "page"]
    print(json.dumps({"ok": True, "pages": pages}, ensure_ascii=False, indent=2))
    return 0


def cmd_search(url: str, out: str) -> int:
    cdp = CDP()
    cdp.call("Page.enable")
    poll = "!!document.querySelector('%s')" % SEARCH_CARD_SELECTORS[0]
    cdp.navigate_and_settle(url, wait_seconds=6.0, poll_expr=poll)
    js = JS_SEARCH % (json.dumps(SEARCH_CARD_SELECTORS), SEARCH_LIMIT)
    data = cdp.evaluate(js)
    if not data.get("cards"):
        print("[warn] 卡片选择器零命中，已用整页文本兜底，请由 agent 结构化",
              file=sys.stderr)
    result = {
        "source_url": url,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "count": len(data.get("cards") or []),
        "cards": data.get("cards") or [],
        "fallback_text": data.get("fallback"),
        "selectors_version": "2026-10-21（未在真实登录态验证，首次使用需核对字段完整率）",
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"已写入 {out}（{result['count']} 张卡片，"
          f"{'兜底文本 ' + str(len(result['fallback_text'] or '')) + ' 字符' if result['fallback_text'] else '结构化命中'}）")
    return 0


def cmd_detail(url: str, out: str) -> int:
    cdp = CDP()
    cdp.call("Page.enable")
    poll = "!!document.querySelector('%s')" % DETAIL_BODY_SELECTORS[0]
    cdp.navigate_and_settle(url, wait_seconds=5.0, poll_expr=poll)
    js = JS_DETAIL % json.dumps(DETAIL_BODY_SELECTORS)
    data = cdp.evaluate(js)
    if not data.get("body"):
        print("[warn] 正文选择器零命中，已用整页文本兜底", file=sys.stderr)
    result = {
        "source_url": url,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "title": data.get("title"),
        "body": data.get("body"),
        "fallback_text": data.get("fallback"),
        "selectors_version": "2026-10-21（未在真实登录态验证，首次使用需核对）",
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"已写入 {out}（{'正文命中' if data.get('body') else '兜底文本'}，"
          f"{len(data.get('body') or data.get('fallback') or '')} 字符）")
    return 0


from pathlib import Path  # noqa: E402  (置于底部避免顶部噪音)


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "check":
        return cmd_check()
    if cmd == "search" and len(argv) == 3:
        human_delay()
        return cmd_search(argv[1], argv[2])
    if cmd == "detail" and len(argv) == 3:
        human_delay()
        return cmd_detail(argv[1], argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
