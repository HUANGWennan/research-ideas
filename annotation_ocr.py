"""Index reliably recognized text from existing local Zotero image-annotation caches.

The OCR passages are a separate, explicitly unverified source linked back to the
original annotation and page. Zotero's library and image cache are read only.
"""

import argparse
import hashlib
import json
import re
import select
import subprocess
import unicodedata
from collections import Counter
from contextlib import suppress
from pathlib import Path

from db import APP, connect, digest, run_record, store_source

FEED = "zotero-ocr"
CACHE = Path.home() / "Zotero/cache/library"
EXECUTABLE = APP / "vision-ocr"
VERSION = "vision-v1"
MIN_LINE_CONFIDENCE = 0.8
MAX_BYTES = 20_000_000
ANNOTATION_ID = re.compile(r"zotero:users/0:([A-Z0-9]{8})$")
WORDS = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


def comparable(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(character for character in normalized if character.isalnum())


class VisionWorker:
    def __init__(self, executable):
        self.executable = str(executable)
        self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.process is not None:
            process, self.process = self.process, None
            with suppress(OSError):
                process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            finally:
                process.stdout.close()

    def recognize(self, path):
        if self.process is None:
            self.process = subprocess.Popen(
                [self.executable, "--stdin"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        process = self.process
        try:
            process.stdin.write((json.dumps(str(path)) + "\n").encode())
            process.stdin.flush()
            if not select.select([process.stdout], [], [], 60)[0]:
                raise subprocess.TimeoutExpired(self.executable, 60)
            reply = json.loads(process.stdout.readline())
            if not isinstance(reply, dict):
                raise ValueError("Unexpected Vision response")
        except (OSError, ValueError, subprocess.SubprocessError):
            self.close()
            raise
        if reply.get("error"):
            raise ValueError(reply["error"])
        return reply["lines"]


def accept_lines(raw_lines, originals):
    accepted = []
    seen = set()
    for item in raw_lines:
        text = item["text"].strip()
        confidence = float(item["confidence"])
        canonical = comparable(text)
        if (
            not 0.0 <= confidence <= 1.0
            or confidence < MIN_LINE_CONFIDENCE
            or len(canonical) < 6
            or not WORDS.search(text)
            or canonical in seen
            or any(canonical in original for original in originals)
        ):
            continue
        accepted.append((text, round(confidence, 3)))
        seen.add(canonical)
    return accepted


def sync_zotero_ocr(cache=CACHE, executable=EXECUTABLE, database=None):
    """Run after Zotero metadata sync. Returns per-run counters for the caller."""
    cache, executable = Path(cache), Path(executable)
    stats = Counter()
    if not cache.is_dir() or not executable.is_file():
        reason = "未找到本地 Zotero 图片批注缓存。" if not cache.is_dir() else "Apple Vision 识别程序尚未安装。"
        with connect(database) as db:
            run_record(db, FEED, "待接入", reason)
        return dict(stats)

    with connect(database) as db:
        parents = {
            match[1]: dict(row)
            for row in db.execute(
                "SELECT * FROM sources WHERE feed='zotero' AND kind='annotation' AND state='indexed'"
            )
            if (match := ANNOTATION_ID.fullmatch(row["id"]))
        }
        previous = {
            r["id"]: (r["fingerprint"], r["state"])
            for r in db.execute("SELECT id,fingerprint,state FROM sources WHERE feed=?", (FEED,))
        }

    with VisionWorker(executable) as worker:
        for path in sorted(cache.glob("*.png")):
            parent = parents.get(path.stem)
            if parent is None or path.is_symlink() or not path.is_file():
                continue
            stats["matched"] += 1
            sid = FEED + ":" + path.stem
            meta = json.loads(parent["meta"])
            meta.update(source_ref=parent["id"], ocr_engine="Apple Vision", ocr_language_correction=False,
                        ocr_reviewed=False, ocr_line_threshold=MIN_LINE_CONFIDENCE)
            description = None
            passages = []
            fingerprint = digest(VERSION + parent["fingerprint"] + str(path))
            try:
                with path.open("rb") as image:
                    data = image.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError("图片超过大小限制")
                fingerprint = digest(VERSION + parent["fingerprint"] + hashlib.sha256(data).hexdigest())
                if previous.get(sid) in ((fingerprint, "indexed"), (fingerprint, "skipped")):
                    stats["unchanged"] += 1
                    continue
                raw_lines = worker.recognize(path)
                with connect(database) as db:
                    originals = [
                        comparable(p[0])
                        for p in db.execute("SELECT text FROM passages WHERE source=? AND role IN ('文献高亮','Zotero 评论')", (parent["id"],))
                    ]
                lines = accept_lines(raw_lines, originals)
                meta["ocr_confidence_by_line"] = [confidence for _, confidence in lines]
                if lines:
                    location = f"页 {meta['page']}" if meta.get("page") else "附件批注"
                    anchor = f"{location} · 图像识别 · 最低行置信度 {min(meta['ocr_confidence_by_line']):.0%}"
                    passages.append((anchor, "图像 OCR（待人工核对）", "\n".join(text for text, _ in lines), []))
                    state = "indexed"
                else:
                    state = "skipped"
                    description = "没有可独立索引的高置信文字"
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
                state = "failed"
                description = "图片批注 OCR 失败：" + type(error).__name__

            with connect(database) as db:
                store_source(db, sid, FEED, "annotation-ocr", parent["title"],
                             parent["locator"], parent["modified"], fingerprint, passages,
                             author=parent["author"], meta=meta, state=state, error=description)
            stats[state] += 1

    with connect(database) as db:
        db.execute(
            """UPDATE sources SET state='missing' WHERE feed=? AND state<>'missing' AND NOT EXISTS
               (SELECT 1 FROM sources original
               WHERE original.id=json_extract(sources.meta,'$.source_ref') AND original.state='indexed')""",
            (FEED,),
        )
        counts = dict(db.execute("SELECT state,count(*) FROM sources WHERE feed=? GROUP BY state", (FEED,)))
        note = (
            f"只读取个人 Zotero 库的本机已缓存图片。匹配 {stats['matched']} 个，当前可搜索 "
            f"{counts.get('indexed', 0)} 个，低置信或无新增文字 {counts.get('skipped', 0)} 个。"
            "识别文字需对照批注原图。"
        )
        run_record(db, FEED, "部分" if counts.get("failed") else "已扫描本机缓存", note,
                   discovered=max(stats["matched"], sum(count for state, count in counts.items() if state != "missing")))
    return dict(stats)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--executable", type=Path, default=EXECUTABLE)
    args = parser.parse_args()
    print(json.dumps(sync_zotero_ocr(args.cache, args.executable), ensure_ascii=False))


if __name__ == "__main__":
    main()
