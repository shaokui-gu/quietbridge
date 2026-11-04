#!/usr/bin/env python3
"""Browse linux.do topics with a per-topic dwell time, in batches.

The window must be on screen (not minimised, not on another Space): a hidden
window commits navigation in the tab but never paints the new document, so the
AX tree keeps showing the previous page until it is restored.

Env:
  DWELL_TOPICS  topics to visit in this invocation (default 5)
  DWELL_BUDGET  wall-clock budget in seconds (default 420)
  DWELL_HEAVY   reply count treated as "many replies" (default 20)
  DWELL_SHORT   dwell for a normal topic (default 10)
  DWELL_LONG    dwell for a topic with many replies (default 30)
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as C  # noqa: E402

OUT = "/tmp/linuxdo-dwell.jsonl"
CATEGORIES = [
    "https://linux.do/c/develop/4",
    "https://linux.do/c/news/34",
    "https://linux.do/c/welfare/36",
    "https://linux.do/c/gossip/11",
    "https://linux.do/c/resource/14",
    "https://linux.do/c/reading/32",
    "https://linux.do/c/startup/46",
    "https://linux.do/c/job/27",
    "https://linux.do/c/square/110",
    "https://linux.do/c/feedback/2",
]

TOPICS = int(os.environ.get("DWELL_TOPICS", "5"))
BUDGET = float(os.environ.get("DWELL_BUDGET", "420"))
HEAVY = int(os.environ.get("DWELL_HEAVY", "20"))
SHORT = float(os.environ.get("DWELL_SHORT", "10"))
LONG = float(os.environ.get("DWELL_LONG", "30"))
CATEGORY = os.environ.get("DWELL_CATEGORY", "").strip()
PAGES = int(os.environ.get("DWELL_PAGES", "3"))


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def chrome_pid():
    out = subprocess.run(["pgrep", "-f", "Google Chrome.app/Contents/MacOS/Google Chrome"],
                         capture_output=True, text=True).stdout.split()
    return out[0] if out else None


class Page(object):
    def __init__(self):
        pid = chrome_pid()
        if not pid:
            raise SystemExit("Chrome 未运行")
        self.handle = C.call_helper("getApp", {"selector": pid})["appHandle"]
        self.win = self._pick_window()
        self.pid = pid

    def _pick_window(self):
        for w in C.call_helper("listWindows", {"appHandle": self.handle}):
            b = w.get("bounds") or {}
            if b.get("width", 0) < 900 or b.get("height", 0) < 500:
                continue
            r = C.call_helper("getState", {"appHandle": self.handle, "windowId": w["windowId"],
                                           "maxElements": 80, "maxDepth": 8})
            if "AXWebArea" in (r.get("tree") or "") or "LINUX DO" in (w.get("title") or ""):
                return w["windowId"]
        raise SystemExit("找不到浏览器窗口")

    def visible(self):
        for w in C.call_helper("listWindows", {"appHandle": self.handle}):
            if w["windowId"] == self.win:
                return w.get("isOnScreen"), w.get("onCurrentSpace")
        return None, None

    def read(self, max_elements=5000, max_depth=30):
        last = None
        for _ in range(4):
            try:
                r = C.call_helper("getState", {"appHandle": self.handle, "windowId": self.win,
                                               "maxElements": max_elements, "maxDepth": max_depth})
                return r, (r.get("tree") or "")
            except Exception as exc:
                last = exc
                if "STALE" in str(exc).upper():
                    time.sleep(0.4)
                    continue
                raise
        raise last

    @staticmethod
    def addr(tree):
        """Address bar value; Chrome updates it at navigation commit."""
        m = re.search(r'AXTextField[^\n]*= "([^"]+)"', tree)
        return m.group(1) if m else ""

    @staticmethod
    def topic_id(tree):
        m = re.search(r'/t/[^/"]+/(\d+)', Page.addr(tree))
        return int(m.group(1)) if m else None

    @staticmethod
    def reply_map(tree):
        out = {}
        for line in tree.splitlines():
            m = re.search(r'"(\d+) 条回复[^"]*"[^\n]*url="https://linux\.do/t/[^"/]+/(\d+)', line)
            if m:
                out[int(m.group(2))] = int(m.group(1))
        return out

    @staticmethod
    def topic_links(tree):
        out = []
        for line in tree.splitlines():
            m = re.search(r'\[(\d+)\][^\n]*AXLink[^\n]*?"([^"]{4,140})"[^\n]*'
                          r'url="https://linux\.do/t/([^"/]+)/(\d+)', line)
            if not m:
                continue
            idx, title, tid = int(m.group(1)), m.group(2), int(m.group(4))
            if re.search(r"条回复|跳转到|跳到主要|新话题|^\d+ 月|^昨天|^\d+ 分钟|^\d+ 小时", title):
                continue
            if len(title) < 5:
                continue
            out.append((idx, tid, title))
        seen, uniq = set(), []
        for item in out:
            if item[1] in seen:
                continue
            seen.add(item[1])
            uniq.append(item)
        return uniq

    @staticmethod
    def post_total(tree):
        m = re.search(r'AXStaticText[^\n]*= "\d+\s*/\s*(\d+)"', tree)
        return int(m.group(1)) if m else None

    def click(self, snapshot, index):
        return C.call_helper("click", {"appHandle": self.handle,
                                       "snapshotId": snapshot, "elementIndex": index})

    def back_index(self, tree):
        for line in tree.splitlines():
            if "AXButton" in line and "返回" in line and "disabled" not in line:
                m = re.search(r"\[(\d+)\]", line)
                if m:
                    return int(m.group(1))
        return None

    def cat_index(self, tree, url):
        for line in tree.splitlines():
            if "AXLink" in line and 'url="%s"' % url in line:
                m = re.search(r"\[(\d+)\]", line)
                if m:
                    return int(m.group(1))
        return None

    def wait_topic(self, tid, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.6)
            _r, tree = self.read(1200, 14)
            if self.topic_id(tree) == tid:
                return True, tree
        return False, tree

    @staticmethod
    def list_key(url):
        return url.split("//")[-1].rstrip("/")

    def wait_list(self, url, timeout=12.0):
        """Exact list URL: a prefix match would treat ?page=2 as page 1."""
        want = self.list_key(url)
        end = time.time() + timeout
        tree = ""
        while time.time() < end:
            time.sleep(0.6)
            _r, tree = self.read(1200, 14)
            got = self.addr(tree).rstrip("/")
            if got == want:
                return True, tree
        return False, tree

    def goto(self, url):
        """Click a category link that is present on the current page."""
        _r, tree = self.read(5000, 30)
        if url.split("//")[-1] in self.addr(tree) and "/t/" not in self.addr(tree):
            return True
        idx = self.cat_index(tree, url)
        if idx is None:
            back = self.back_index(tree)
            if back is None:
                return False
            idx = back
        try:
            self.click(_r["snapshotId"], idx)
        except Exception as exc:
            log("切换类别失败: %s" % exc)
            return False
        ok, _t = self.wait_list(url)
        return ok

    def navigate(self, url):
        """Address-bar navigation: works on a background window, unlike scrolling."""
        try:
            C.call_helper("pressKey", {"appHandle": self.handle, "key": "l", "modifiers": ["command"]})
            time.sleep(0.6)
            C.call_helper("typeText", {"appHandle": self.handle, "text": url})
            time.sleep(0.5)
            C.call_helper("pressKey", {"appHandle": self.handle, "key": "return"})
        except Exception as exc:
            log("导航失败: %s" % exc)
            return False
        ok, _t = self.wait_list(url, timeout=20.0)
        return ok


def visit_one(page, snap, idx, tid, title, count, category):
    """Click one topic, dwell, return its record; None when it could not be opened."""
    target = LONG if count >= HEAVY else SHORT
    t0 = time.time()
    ok, tree2 = False, ""
    for attempt in range(3):
        budget = 4.5 if attempt == 0 else 12.0
        try:
            page.click(snap, idx)
        except Exception as exc:
            log("点击失败 %s: %s" % (tid, exc))
            break
        ok, tree2 = page.wait_topic(tid, budget)
        if ok:
            break
        _r, tree = page.read(5000, 30)
        again = [x for x in page.topic_links(tree) if x[1] == tid]
        if not again:
            log("列表已无 %s，跳过" % tid)
            break
        idx = again[0][0]
        snap = _r["snapshotId"]
        log("重试 %s（第 %d 次）" % (tid, attempt + 1))
    if not ok:
        return None
    total = page.post_total(tree2)
    if total and total - 1 > count:
        count = total - 1
        target = LONG if count >= HEAVY else SHORT
    start = time.time()
    samples = 0
    stay_ok = True
    while time.time() - start < target:
        time.sleep(min(3.0, target - (time.time() - start)))
        samples += 1
        _rt, tt = page.read(1200, 14)
        stay_ok = stay_ok and page.topic_id(tt) == tid
    stayed = round(time.time() - start, 1)
    return {"tid": tid, "title": title, "replies": count, "total_posts": total,
            "dwell_target": target, "dwell_actual": stayed, "heavy": target == LONG,
            "samples": samples, "stay_ok": stay_ok, "category": category,
            "opened_at": time.strftime("%H:%M:%S", time.localtime(t0)),
            "window": page.win, "pid": page.pid}


def record(rec, visited):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    visited[rec["tid"]] = rec


def wait_back(page, cat):
    for _ in range(3):
        _rb, treeb = page.read(2000, 18)
        back = page.back_index(treeb)
        if not back:
            break
        try:
            page.click(_rb["snapshotId"], back)
        except Exception:
            pass
        ok, _tl = page.wait_list(cat)
        if ok:
            return True
    return False


def run_pinned(page, visited, deadline):
    """One category only: page through ?page=N and dwell on every topic."""
    done = failures = 0
    for page_no in range(1, PAGES + 1):
        if done >= TOPICS or time.time() >= deadline:
            break
        url = CATEGORY if page_no == 1 else "%s?page=%d" % (CATEGORY, page_no)
        if not page.navigate(url):
            log("第 %d 页打不开，停止" % page_no)
            break
        log("-- 第 %d 页 %s" % (page_no, url))
        while done < TOPICS and time.time() < deadline:
            _r, tree = page.read(6000, 32)
            if "/t/" in page.addr(tree):
                break
            replies = page.reply_map(tree)
            queue = [x for x in page.topic_links(tree)
                     if x[1] not in visited and not x[2].startswith("关于")]
            if not queue:
                log("   该页无新话题")
                break
            idx, tid, title = queue[0]
            rec = visit_one(page, _r["snapshotId"], idx, tid, title, replies.get(tid, 0), url)
            if not rec:
                failures += 1
                visited[tid] = {"tid": tid, "title": title, "failed": True}
                continue
            record(rec, visited)
            done += 1
            log("[%2d/%2d] %-9s 回复=%-4s 停留=%4.1fs/%2.0fs 稳定=%s 累计=%d | %s"
                % (done, TOPICS, tid, rec["replies"], rec["dwell_actual"], rec["dwell_target"],
                   rec["stay_ok"], len(visited), title[:26]))
            if not wait_back(page, url):
                log("返回失败，重新打开列表")
                if not page.navigate(url):
                    log("列表打不开，结束")
                    return done, failures
    return done, failures


def main():
    visited = {}
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            visited[rec["tid"]] = rec
    log("=== 本次目标 %d 个，预算 %.0fs，已有记录 %d ===" % (TOPICS, BUDGET, len(visited)))

    page = Page()
    onscreen, space = page.visible()
    log("window=%s onScreen=%s currentSpace=%s" % (page.win, onscreen, space))
    if not onscreen:
        log("警告：窗口不可见，导航可能不会提交渲染")

    deadline = time.time() + BUDGET
    done = failures = 0

    if CATEGORY:
        done, failures = run_pinned(page, visited, deadline)
        log("=== 本次新增 %d，累计 %d，失败 %d（类别 %s）===" % (done, len(visited), failures, CATEGORY))
        return

    for cat in CATEGORIES:
        if done >= TOPICS or time.time() >= deadline:
            break
        if not page.goto(cat):
            continue
        log("-- 类别 %s" % cat)

        while done < TOPICS and time.time() < deadline:
            _r, tree = page.read(5000, 30)
            if "/t/" in page.addr(tree):
                break
            replies = page.reply_map(tree)
            links = page.topic_links(tree)
            queue = [x for x in links if x[1] not in visited and not x[2].startswith("关于")]
            if not queue:
                log("   该页无新话题")
                break
            idx, tid, title = queue[0]
            count = replies.get(tid, 0)
            target = LONG if count >= HEAVY else SHORT
            t0 = time.time()
            ok = False
            for attempt in range(3):
                budget = 4.5 if attempt == 0 else 12.0
                try:
                    page.click(_r["snapshotId"], idx)
                except Exception as exc:
                    log("点击失败 %s: %s" % (tid, exc))
                    break
                ok, tree2 = page.wait_topic(tid, budget)
                if ok:
                    break
                _r, tree = page.read(5000, 30)
                again = [x for x in page.topic_links(tree) if x[1] == tid]
                if not again:
                    log("列表已无 %s，跳过" % tid)
                    break
                idx = again[0][0]
                log("重试 %s（第 %d 次）" % (tid, attempt + 1))
            if not ok:
                failures += 1
                visited[tid] = {"tid": tid, "title": title, "failed": True}
                continue

            total = page.post_total(tree2)
            if total and total - 1 > count:
                count = total - 1
                target = LONG if count >= HEAVY else SHORT
            start = time.time()
            samples = 0
            stay_ok = True
            while time.time() - start < target:
                time.sleep(min(3.0, target - (time.time() - start)))
                samples += 1
                _rt, tt = page.read(1200, 14)
                stay_ok = stay_ok and page.topic_id(tt) == tid
            stayed = round(time.time() - start, 1)
            rec = {"tid": tid, "title": title, "replies": count, "total_posts": total,
                   "dwell_target": target, "dwell_actual": stayed, "heavy": target == LONG,
                   "samples": samples, "stay_ok": stay_ok,
                   "opened_at": time.strftime("%H:%M:%S", time.localtime(t0)),
                   "window": page.win, "pid": page.pid}
            with open(OUT, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            visited[tid] = rec
            done += 1
            log("[%2d/%2d] %-9s 回复=%-4s 停留=%4.1fs/%2.0fs 稳定=%s 累计=%d | %s"
                % (done, TOPICS, tid, count, stayed, target, stay_ok, len(visited), title[:26]))

            ok2 = False
            for attempt in range(3):
                _rb, treeb = page.read(2000, 18)
                if not page.back_index(treeb):
                    ok2 = page.goto(cat)
                    break
                try:
                    page.click(_rb["snapshotId"], page.back_index(treeb))
                except Exception:
                    pass
                ok2, _tl = page.wait_list(cat)
                if ok2:
                    break
            if not ok2:
                log("返回列表失败，改为点击类别链接")
                ok2 = page.goto(cat)
            if not ok2:
                log("放弃该类别")
                break

    log("=== 本次新增 %d，累计 %d，失败 %d ===" % (done, len(visited), failures))


if __name__ == "__main__":
    main()
