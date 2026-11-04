#!/usr/bin/env python3
"""Browse linux.do by clicking, in the background, without stealing focus.

Drives a Chrome window that lives on another Space via the CUA helper:
  - reads the page through the accessibility tree (AXEnhancedUserInterface makes
    Chromium expose the web tree even when the window is not on the active Space)
  - opens topics with real clicks on the title links
  - records each topic's post count from the Discourse timeline ("1 / N")
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as C  # noqa: E402

LOG = "/tmp/browse_linuxdo.log"
OUT = "/tmp/browse_linuxdo.json"
CATEGORY = "https://linux.do/c/develop/4"


def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def call(method, params=None):
    return C.call_helper(method, params)


class Chrome(object):
    def __init__(self, pid):
        self.pid = pid
        self.handle = call("getApp", {"selector": str(pid)})["appHandle"]
        self.win = self._pick_window()

    def _pick_window(self):
        best = None
        for w in call("listWindows", {"appHandle": self.handle}):
            b = w.get("bounds") or {}
            if b.get("width", 0) > 1200 and b.get("height", 0) > 600:
                if best is None or w["windowId"] > best:
                    best = w["windowId"]
        if not best:
            raise SystemExit("no browser window")
        return best

    def state(self, details=False, max_elements=4000, max_depth=30):
        params = {"appHandle": self.handle, "windowId": self.win,
                  "maxElements": max_elements, "maxDepth": max_depth}
        if details:
            params["details"] = True
        for attempt in range(4):
            try:
                r = call("getState", params)
                return r, (r.get("tree") or "")
            except Exception as exc:
                if "STALE_SNAPSHOT" in str(exc) and attempt < 3:
                    time.sleep(0.4)
                    continue
                raise

    def url(self, tree):
        m = re.search(r'AXWebArea[^\n]*url="([^"]+)"', tree)
        return m.group(1) if m else ""

    def title(self):
        for w in call("listWindows", {"appHandle": self.handle}):
            if w["windowId"] == self.win:
                return w.get("title") or ""
        return ""

    def click_index(self, snapshot, index):
        return call("click", {"appHandle": self.handle,
                              "snapshotId": snapshot, "elementIndex": index})

    def key(self, key, modifiers=None):
        params = {"appHandle": self.handle, "key": key}
        if modifiers:
            params["modifiers"] = modifiers
        return call("pressKey", params)

    def find_back(self, tree):
        for line in tree.splitlines():
            if "AXButton" in line and "返回" in line:
                m = re.search(r"\[(\d+)\]", line)
                if m:
                    return int(m.group(1))
        return None

    def topic_links(self, tree):
        """Title links only: /t/<slug>/<id> or /t/topic/<id>, no trailing post number."""
        out = []
        for line in tree.splitlines():
            m = re.search(r'\[(\d+)\][^\n]*AXLink[^\n]*?"([^"]{4,120})"[^\n]*'
                          r'url="https://linux\.do/t/([^"/]+)/(\d+)(?:/\d+)?"', line)
            if not m:
                continue
            idx, title, _slug, tid = int(m.group(1)), m.group(2), m.group(3), int(m.group(4))
            if title.strip().lower() in ("跳转到第一个帖子", "跳到主要内容"):
                continue
            out.append((idx, tid, title))
        seen, uniq = set(), []
        for item in out:
            if item[1] in seen:
                continue
            seen.add(item[1])
            uniq.append(item)
        return uniq

    def post_total(self, tree):
        m = re.search(r'AXStaticText[^\n]*= "\d+\s*/\s*(\d+)"', tree)
        return int(m.group(1)) if m else None

    def wait_for(self, pred, timeout=9, max_elements=3000, max_depth=30):
        end = time.time() + timeout
        last = (None, "")
        while time.time() < end:
            try:
                r, tree = self.state(max_elements=max_elements, max_depth=max_depth)
            except Exception:
                time.sleep(0.4)
                continue
            last = (r, tree)
            if pred(r, tree):
                return last
            time.sleep(0.4)
        return last

    def go_back(self):
        """Return to the category list by clicking its link, not by history back."""
        for attempt in range(4):
            r, tree = self.state(max_elements=3000, max_depth=30)
            if "/c/develop/4" in self.url(tree):
                return True
            idx = None
            for line in tree.splitlines():
                if 'url="https://linux.do/c/develop/4"' in line and "AXLink" in line:
                    m = re.search(r"\[(\d+)\]", line)
                    if m:
                        idx = int(m.group(1))
                        break
            if idx is None:
                idx = self.find_back(tree)
            if idx is None:
                return False
            try:
                self.click_index(r["snapshotId"], idx)
            except Exception as exc:
                log("返回点击失败: %s" % exc)
            for _ in range(8):
                time.sleep(0.55)
                r2, tree2 = self.state(max_elements=1200, max_depth=14)
                if "/c/develop/4" in self.url(tree2):
                    return True
        return False


def main():
    target_topics = int(os.environ.get("TARGET_TOPICS", "100"))
    target_posts = int(os.environ.get("TARGET_POSTS", "2000"))
    open(LOG, "w").close()
    ch = Chrome(80766)
    log("window=%s 起始标题=%s" % (ch.win, ch.title()[:50]))

    visited = {}
    if os.path.exists(OUT):
        try:
            visited = {int(k): v for k, v in json.load(open(OUT)).items()}
        except Exception:
            visited = {}

    if not ch.go_back():
        log("无法回到类别页")
        return

    rounds = 0
    while (len(visited) < target_topics or sum(visited.values()) < target_posts) and rounds < 60:
        rounds += 1
        r, tree = ch.state()
        if "/c/develop/4" not in ch.url(tree):
            if not ch.go_back():
                log("丢失类别页，停止")
                break
            continue
        links = [x for x in ch.topic_links(tree) if x[1] not in visited]
        if len(links) < 2:
            # list may still be rendering; give it a moment before scrolling
            r, tree = ch.wait_for(lambda rr, tt: len(ch.topic_links(tt)) >= 3, timeout=5)
            links = [x for x in ch.topic_links(tree) if x[1] not in visited]
        if not links:
            log("当前视口无新话题，向下翻页")
            for _ in range(3):
                ch.key("pagedown")
                time.sleep(0.5)
            time.sleep(0.6)
            r, tree = ch.state()
            links = [x for x in ch.topic_links(tree) if x[1] not in visited]
            if not links:
                log("翻页后仍无新话题，结束")
                break
        idx, tid, title = links[0]
        before_url = ch.url(tree)
        try:
            ch.click_index(r["snapshotId"], idx)
        except Exception as exc:
            log("click 失败 %s: %s" % (tid, exc))
            visited[tid] = visited.get(tid, 0)
            ch.go_back()
            continue

        def entered(rr, tt):
            u = ch.url(tt)
            return ("/t/" in u and u != before_url and str(tid) in u) or ("/t/" in u and u != before_url)

        def has_timeline(rr, tt):
            return ch.post_total(tt) is not None

        r2, tree = ch.wait_for(lambda rr, tt: entered(rr, tt), timeout=10, max_elements=600, max_depth=12)
        r3, tree = ch.wait_for(has_timeline, timeout=8, max_elements=3000, max_depth=30)
        total = ch.post_total(tree)
        browsed = min(total, 20) if total else 0
        visited[tid] = browsed
        json.dump({str(k): v for k, v in visited.items()}, open(OUT, "w"))
        log("topic %-8s posts=%-5s browsed=%-3s 累计话题=%d 累计帖子=%d | %s"
            % (tid, total, browsed, len(visited), sum(visited.values()), title[:34]))
        if not ch.go_back():
            log("返回失败，停止")
            break
        ch.wait_for(lambda rr, tt: len(ch.topic_links(tt)) >= 3, timeout=6)

    log("完成：话题 %d，帖子 %d" % (len(visited), sum(visited.values())))


if __name__ == "__main__":
    main()
