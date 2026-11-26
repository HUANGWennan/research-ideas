"""Publish finished snapshots to the user's OneDrive-synced project folder."""
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import uuid
from pathlib import Path
from db import APP, PORTABLE, STATE, check_backup

MARKER=".research-ideas-managed"
FILES=tuple(sorted(p.name for p in APP.iterdir() if p.is_file() and p.suffix in {".py", ".md", ".css", ".js", ".swift"}))
BACKUP_NAME = re.compile(r"research-[0-9]{8}-[0-9]{6}-[0-9]{6}\.sqlite3")
ONEDRIVE = Path("/Applications/OneDrive.app/Contents/MacOS/OneDrive")

def prepare():
    if PORTABLE.is_symlink():
        raise ValueError("OneDrive 项目目录不可为符号链接")
    if PORTABLE.exists() and not (PORTABLE/MARKER).is_file():
        raise RuntimeError("OneDrive 项目目录已存在，未覆盖")
    PORTABLE.mkdir(exist_ok=True,mode=0o700)
    marker=PORTABLE/MARKER
    if not marker.exists():
        with marker.open("x",encoding="utf-8") as f: f.write("Research Ideas managed portable workspace\n")

def copy_atomic(source,target):
    target.parent.mkdir(exist_ok=True,parents=True)
    if target.is_symlink():
        raise ValueError("发布目标不可为符号链接")
    if target.is_file() and source.suffix!=".sqlite3" and source.read_bytes()==target.read_bytes():
        return
    temp=target.parent/("."+target.name+"-"+uuid.uuid4().hex+".tmp")
    try:
        with source.open("rb") as src, temp.open("xb") as dst:
            shutil.copyfileobj(src,dst)
            dst.flush()
            os.fsync(dst.fileno())
        temp.replace(target)
    finally:
        if temp.exists(): temp.unlink()

def archive_backup(source):
    prepare()
    folder=PORTABLE/"data"/"backup-history"
    if folder.is_symlink() or folder.parent.is_symlink():
        raise ValueError("OneDrive 历史备份目录不可为符号链接")
    folder.mkdir(exist_ok=True,parents=True)
    target=folder/source.name
    if target.is_symlink():
        raise ValueError("OneDrive 备份目标不可为符号链接")

    def backup_hash(path):
        sha=hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda:file.read(1024*1024),b""):
                sha.update(chunk)
        return sha.digest()

    expected=backup_hash(source)
    if not target.exists():
        stage=folder/("."+source.name+"-"+uuid.uuid4().hex+".tmp")
        try:
            with source.open("rb") as src, stage.open("xb") as dst:
                shutil.copyfileobj(src,dst)
                dst.flush()
                os.fsync(dst.fileno())
            check_backup(stage)
            if backup_hash(stage)!=expected:
                raise RuntimeError("OneDrive 归档副本校验失败")
            stage.rename(target)
        finally:
            stage.unlink(missing_ok=True)
    elif backup_hash(target)!=expected:
        raise RuntimeError(f"OneDrive 已有不同内容的备份，保留本机原件：{source.name}")
    return target


def uploaded_archive(path):
    result = subprocess.run(
        ["/usr/bin/fileproviderctl", "evaluate", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, errors="replace", timeout=8,
    )
    required = {
        "isUploaded": "1",
        "isUploading": "0",
        "isKeepDownloaded": "0",
        "isExcludedFromSync": "0",
        "isSyncPaused": "0",
        "hasUnresolvedConflicts": "0",
    }
    return result.returncode == 0 and all(
        re.findall(rf"(?m)^\s*{key} = ([01]);", result.stdout) == [value]
        for key, value in required.items()
    )


def release_archived_backup_cache():
    folder = PORTABLE / "data" / "backup-history"
    if folder.is_symlink() or folder.parent.is_symlink() or PORTABLE.is_symlink():
        return 0
    try:
        folder = folder.resolve(strict=True)
        storage = (Path.home() / "Library" / "CloudStorage").resolve(strict=True)
        relative = folder.relative_to(storage)
        if not relative.parts or not relative.parts[0].startswith("OneDrive-"):
            return 0
        paths = list(folder.iterdir())
    except (OSError, ValueError):
        return 0

    released = 0
    for path in paths:
        if not BACKUP_NAME.fullmatch(path.name):
            continue
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_size == 0 or before.st_blocks == 0:
                continue
            if not uploaded_archive(path):
                continue
            current = path.lstat()
            if not os.path.samestat(current, before) or current.st_mtime_ns != before.st_mtime_ns:
                continue
            # OneDrive can return a nonzero code even after releasing the data.
            subprocess.run(
                [str(ONEDRIVE), "/unpin", str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8,
            )
            after = path.lstat()
            if stat.S_ISREG(after.st_mode) and after.st_size == before.st_size and after.st_blocks == 0:
                released += 1
        except (OSError, subprocess.TimeoutExpired):
            continue
    return released


def publish_ideas():
    prepare()
    source=STATE/"ideas-export.json"
    if source.is_file():
        copy_atomic(source,PORTABLE/"ideas"/"ideas-export.json")

def publish():
    prepare()
    for name in FILES:
        source=APP/name
        if source.is_file():
            copy_atomic(source,PORTABLE/"app"/name)
    readme=APP/"README.md"
    if readme.is_file(): copy_atomic(readme,PORTABLE/"README.md")
    for source in (STATE/"reports").glob("*"):
        if source.is_file() and source.suffix in (".md",".txt",".json"):
            copy_atomic(source,PORTABLE/"reports"/source.name)
    publish_ideas()
    snapshot=STATE/("portable-"+uuid.uuid4().hex+".sqlite3")
    target=PORTABLE/"data"/"research-snapshot.sqlite3"
    try:
        with sqlite3.connect(STATE/"research.sqlite3", timeout=40) as src, sqlite3.connect(snapshot) as dst:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            if dst.execute("PRAGMA quick_check").fetchone()[0]!="ok": raise RuntimeError("数据库快照校验失败")
        copy_atomic(snapshot,target)
    finally:
        if snapshot.exists(): snapshot.unlink()
    return target
