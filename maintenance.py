"""Background sync, off-line backups, and weekly/monthly research reports."""
import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

from db import APP, PORTABLE, ROOT, STATE, MODEL, backup, connect, dump, embed_batches, get_setting, idea_get, now, run_record, setting
from sources import HOME, DESKTOP, COURSES, RESEARCH, sync_files, sync_zotero
from discovery import collect, maturity, proposals
from portable import publish, publish_ideas, release_archived_backup_cache

def roots():
    return {
        "idea": [str(ROOT)],
        "research": [p for p in os.environ.get("RESEARCH_IDEAS_RESEARCH_PATHS", "").split(os.pathsep) if p],
        "course": [p for p in os.environ.get("RESEARCH_IDEAS_COURSE_PATHS", "").split(os.pathsep) if p],
    }

def watch_plan():
    watched={str(p.resolve()) for items in roots().values() for p in items}
    config=str((APP/"roots.json").resolve())
    return {"roots":sorted(watched), "config":config,
            "excluded":[str(PORTABLE.resolve()),str(APP.resolve())]}

def external(feed):
    try:
        import external_sources
        getattr(external_sources,"sync_"+feed)()
        if feed=="feishu":
            from feishu_live import sync_feishu_live
            result=sync_feishu_live()
            print(f"{now()} Feishu live: {result['indexed']}/{result['discovered']}, failures={result['failed']}",flush=True)
    except ModuleNotFoundError as e:
        with connect() as db: run_record(db,feed,"待接入",str(e),None)

def update(feed, auto=False):
    if feed in ("idea","research","course"):
        sync_files(feed,roots()[feed])
    elif feed=="zotero":
        p=HOME/"Zotero/zotero.sqlite"
        paths=(p,p.with_suffix(".sqlite-wal"))
        marker=[[(k.stat().st_mtime_ns),(k.stat().st_size)] if k.exists() else None for k in paths]
        with connect() as db:
            old=get_setting(db,"zotero_file_marker")
            last=db.execute("SELECT at,status FROM runs WHERE feed='zotero'").fetchone()
            if auto and old==marker and last and last["status"]=="已同步" and datetime.now(timezone.utc)-datetime.fromisoformat(last["at"])<timedelta(days=1):
                return
        sync_zotero()
        try:
            from annotation_ocr import sync_zotero_ocr
            sync_zotero_ocr()
        except (OSError, ModuleNotFoundError) as e:
            print(f"{now()} Zotero OCR pending: {type(e).__name__}",file=sys.stderr)
        with connect() as db:
            setting(db,"zotero_file_marker",marker)
    else:
        external(feed)

def report(period):
    days=7 if period=="week" else 31
    today=datetime.now().astimezone()
    start=(today-timedelta(days=days)).isoformat(timespec="seconds")[:10]
    folder=STATE/"reports"
    folder.mkdir(exist_ok=True)
    output=folder/(f"{today.isocalendar().year}-W{today.isocalendar().week:02d}.md" if period=="week" else today.strftime("%Y-%m.md"))
    headings={"week":"研究思想周报","month":"月度研究分析"}
    with connect() as db:
        ideas=[idea_get(db,r[0]) for r in db.execute("SELECT id FROM ideas ORDER BY updated DESC")]
        if period=="week":
            ideas=[i for i in ideas if i["updated_at"][:10]>=start]
        lines=[f"# {headings[period]}","",f"生成于 {today:%Y-%m-%d %H:%M}。"+(f"覆盖自 {start} 起的新增与修改。" if period=="week" else "依据当前思想库及其历史。"),""]
        for title,predicate in (
            ("用户明确提出或接受",lambda x:x["author_type"] in ("用户明确提出","用户明确接受")),
            ("模型及推导",lambda x:x["idea_type"] in ("数学模型","数学证明") or x["derivation"]),
            ("已解决问题",lambda x:x["research_status"] in ("已验证","已纳入论文")),
            ("待解决问题及待核验假说",lambda x:x["idea_type"] in ("未解决问题","研究假说") and x["research_status"] not in ("已验证","已否定")),
            ("潜在论文选题",lambda x:x["idea_type"]=="论文选题"),
            ("搁置及争议",lambda x:x["research_status"]=="暂时搁置" or x["verification_status"] in ("存在争议","存在反例")),
        ):
            lines.extend([f"## {title}",""])
            matches=[i for i in ideas if predicate(i)]
            lines.extend([f"- {i['title']} · {i['research_status']} · {i['verification_status']}"+(f"。下一步：{i['next_steps']}" if i["next_steps"] else "") for i in matches[:80]])
            if not matches: lines.append("本期无记录。")
            lines.append("")
        lines.extend(["## 历史变更",""])
        changes=db.execute("SELECT idea,version,at,reason FROM revisions WHERE substr(at,1,10)>=? ORDER BY at DESC",(start,)).fetchall()
        lines.extend(f"- {r['at'][:10]} · {idea_get(db,r['idea'])['title']} v{r['version']} · {r['reason']}" for r in changes[:100])
        lines.extend(["","## 待验证关联",""])
        for r in db.execute("SELECT * FROM links"):
            lines.append(f"- {idea_get(db,r['a'])['title']} ↔ {idea_get(db,r['b'])['title']} · {r['kind']} · {r['confidence']}。{r['basis']}")
        for a,b,matching in proposals(db):
            lines.append(f"- {a} ↔ {b}。待验证候选，共同主题：{'、'.join(matching)}。")
        if period=="month":
            lines.extend(["","## 选题成熟度记录",""])
            for i in ideas:
                if i["idea_type"] in ("论文选题","实证构思","研究假说"):
                    lines.append("- "+i["title"]+" · "+"；".join(f"{key} {value}" for key,value in maturity(i).items()))
        lines.extend(["","## 数据源覆盖",""])
        for r in db.execute("SELECT * FROM runs ORDER BY feed"):
            lines.append(f"- {r['feed']} · {r['status']} · 成功 {r['indexed']} · 失败 {r['failed']}。{r['detail']}")
    tmp=output.with_suffix(".tmp")
    tmp.write_text("\n".join(lines)+"\n",encoding="utf-8")
    tmp.replace(output)
    return output

def write_open_archive():
    with connect() as db:
        ids=[r[0] for r in db.execute("SELECT id FROM ideas")]
        data=[{"current":idea_get(db,i),
               "version_history":[{"version":r["version"],"at":r["at"],"reason":r["reason"],"body":json.loads(r["body"])}
                                   for r in db.execute("SELECT * FROM revisions WHERE idea=? ORDER BY version",(i,))]} for i in ids]
        links=[dict(r) for r in db.execute("SELECT * FROM links")]
    temp=STATE/"ideas-export.tmp"
    temp.write_text(json.dumps({"ideas":data,"links":links},ensure_ascii=False,indent=2),encoding="utf-8")
    temp.replace(STATE/"ideas-export.json")

def exists_and_recent(feed,hours):
    with connect() as db:
        row=db.execute("SELECT at,status FROM runs WHERE feed=?",(feed,)).fetchone()
        return row and row["status"] in ("已同步","部分") and datetime.now(timezone.utc)-datetime.fromisoformat(row["at"])<timedelta(hours=hours)

def sync(feed=None, auto=False):
    all_feeds=["idea","course","research","feishu","ai","zotero"]
    if feed=="files":
        feeds=all_feeds[:3]
    else:
        feeds=[feed] if feed else all_feeds[3:] if auto else all_feeds
    for name in feeds:
        if auto and name in ("zotero","feishu") and exists_and_recent(name,4 if name=="feishu" else .5):
            continue
        try:
            update(name,auto=auto)
        except Exception as e:
            with connect() as db: run_record(db,name,"失败",f"{type(e).__name__}: {e}"[:300])
            print(f"{now()} {name} failed: {type(e).__name__}: {e}", file=sys.stderr)
    with connect() as db:
        candidates=collect(db)
        if candidates: print(f"{now()} 自动提取待审核原话：{candidates} 条")
    write_open_archive()
    publish_ideas()

def vectors(count, priorities=False):
    with connect() as db:
        try:
            n=embed_batches(db,count)
            total=db.execute("SELECT count(*) FROM vectors WHERE model=?",(MODEL,)).fetchone()[0]
            todo=db.execute("SELECT count(*) FROM passages p JOIN sources s ON p.source=s.id WHERE s.state='indexed' AND COALESCE(json_extract(s.meta,'$.coverage'),'') NOT IN ('元数据','标题','metadata')").fetchone()[0]
            run_record(db,"vectors","已同步" if total>=todo else "部分",f"{MODEL} 向量 {total} / {todo}；本轮新增 {n}",todo)
            db.execute("UPDATE runs SET indexed=? WHERE feed='vectors'",(total,))
            print(f"{now()} vectors: new={n} total={total}/{todo}")
        except Exception as e:
            db.rollback()
            total=db.execute("SELECT count(*) FROM vectors WHERE model=?",(MODEL,)).fetchone()[0]
            run_record(db,"vectors","失败",f"{MODEL} 已存 {total}；{type(e).__name__}: {e}")
            print(f"{now()} vectors failed: {type(e).__name__}: {e}",file=sys.stderr)

def rotate():
    for name in ("worker.log","web.log","folders.log"):
        path=STATE/name
        if path.exists() and path.stat().st_size>1_500_000:
            with path.open("r+b") as f:
                f.seek(-300_000,2)
                tail=f.read().split(b"\n",1)[-1]
                f.seek(0)
                f.write(tail)
                f.truncate()

def daily():
    today=datetime.now().astimezone()
    day=today.strftime("%Y-%m-%d")
    week=f"{today.isocalendar().year}-{today.isocalendar().week}"
    month=today.strftime("%Y-%m")
    with connect() as db:
        last=get_setting(db,"scheduled_runs",{})
    actions={"backup":(day,backup),"week":(week,lambda:report("week")),"month":(month,lambda:report("month")),"onedrive":(day,publish)}
    for name,(key,action) in actions.items():
        if last.get(name)!=key:
            try:
                print(f"{now()} {name}: {action()}")
                last[name]=key
            except Exception as e:
                print(f"{now()} {name} failed: {e}",file=sys.stderr)
    with connect() as db: setting(db,"scheduled_runs",last)
    rotate()

def restore(source,destination):
    source=Path(source).expanduser().resolve(strict=True)
    destination=Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError("恢复目标必须是新的文件路径")
    with sqlite3.connect(source.as_uri()+"?mode=ro",uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise ValueError("备份未通过完整性检查")
        with sqlite3.connect(destination) as new:
            db.backup(new)
    return destination

def main():
    os.umask(0o077)
    STATE.mkdir(exist_ok=True,parents=True)
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("sync","files","file-events","watch-plan","vectors","run","backup","week","month","restore","status","publish"))
    parser.add_argument("parameters",nargs="*")
    args=parser.parse_args()
    if args.action=="watch-plan":
        print(dump(watch_plan()))
        return
    if args.action=="status":
        with connect() as db:
            print(json.dumps([dict(r) for r in db.execute("SELECT * FROM runs ORDER BY feed")],ensure_ascii=False,indent=2))
        return
    if args.action=="restore":
        if len(args.parameters)!=2: parser.error("restore BACKUP NEW_DB")
        print(restore(*args.parameters))
        return
    locks={"vectors":"embed.lock","files":"file-worker.lock"}.get(args.action,"worker.lock")
    if args.action=="file-events":
        locks="file-worker.lock"
    with (STATE/locks).open("a") as lock:
        try:
            flags=fcntl.LOCK_EX|fcntl.LOCK_NB if args.action in ("run","vectors") else fcntl.LOCK_EX
            fcntl.flock(lock,flags)
        except BlockingIOError:
            print("同步或维护任务已在运行")
            return
        if args.action=="sync": sync(args.parameters[0] if args.parameters else None)
        elif args.action=="file-events":
            changed=[Path(p) for p in args.parameters if Path(p).is_absolute()]
            if not changed or len(changed)!=len(args.parameters):
                parser.error("file-events requires absolute paths")
            print(now(),f"文件变更，检查 {len(changed)} 条路径",flush=True)
            for feed,paths in roots().items():
                sync_files(feed,paths,events=changed)
        elif args.action=="files":
            print(now(),"开始检查 Mac 文件",flush=True)
            sync("files")
            print(now(),"完成检查 Mac 文件",flush=True)
        elif args.action=="run":
            print(now(),"开始增量同步",flush=True)
            sync(auto=True)
            daily()
            released = release_archived_backup_cache()
            if released:
                print(now(), f"OneDrive 已释放 {released} 份历史备份的本机缓存", flush=True)
            print(now(),"完成增量同步",flush=True)
        elif args.action=="vectors": vectors(int(args.parameters[0]) if args.parameters else 320, "priorities" in args.parameters)
        elif args.action=="backup": print(backup())
        elif args.action=="publish": print(publish())
        else: print(report(args.action))
if __name__=="__main__":
    main()
