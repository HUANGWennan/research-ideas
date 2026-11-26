"""Persistent sources, citation passages, and versioned research ideas."""
import array
import fcntl
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

APP = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("RESEARCH_IDEAS_ROOT", Path.home() / "Research Ideas Sources")).expanduser()
STATE = Path(os.environ.get("RESEARCH_IDEAS_STATE", Path.home() / "Library/Application Support/Research Ideas/state"))
PORTABLE = Path(os.environ.get("RESEARCH_IDEAS_PORTABLE", ROOT / "Research Ideas")).expanduser()
MODEL = "bge-m3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, feed TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
 locator TEXT NOT NULL, author TEXT NOT NULL, modified TEXT NOT NULL,
 fingerprint TEXT NOT NULL, meta TEXT NOT NULL, state TEXT NOT NULL, error TEXT
);
CREATE INDEX IF NOT EXISTS sources_feed ON sources(feed,state);
CREATE TABLE IF NOT EXISTS passages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL REFERENCES sources(id),
 anchor TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS passages_source ON passages(source);
CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts5(title, text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS formulas (
 passage INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
 expression TEXT NOT NULL, normal TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS formulas_normal ON formulas(normal);
CREATE TABLE IF NOT EXISTS vectors (
 passage INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
 model TEXT NOT NULL, value BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS ideas (
 id TEXT PRIMARY KEY, current INTEGER NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
 body TEXT NOT NULL, dedup TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS revisions (
 idea TEXT NOT NULL REFERENCES ideas(id), version INTEGER NOT NULL,
 at TEXT NOT NULL, reason TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(idea,version)
);
CREATE TABLE IF NOT EXISTS links (
 a TEXT REFERENCES ideas(id), b TEXT REFERENCES ideas(id), kind TEXT,
 basis TEXT NOT NULL, confidence TEXT NOT NULL, PRIMARY KEY(a,b,kind), CHECK(a<>b)
);
CREATE TABLE IF NOT EXISTS runs (
 feed TEXT PRIMARY KEY, at TEXT, discovered INTEGER, indexed INTEGER,
 failed INTEGER, status TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""
TYPES = ("理论观点","理论批判","数学模型","数学证明","研究假说","实证构思","方法论","未解决问题","论文选题","搁置思路","AI 建议")
AUTHORS = ("用户明确提出","用户明确接受","AI 建议","外部文献观点","用户与 AI 共同发展","来源尚不明确")
STATUSES = ("初步灵感","待研究","建模中","推导中","待验证","已验证","论文构思","已纳入论文","暂时搁置","已否定")
VERIFICATIONS = ("尚未验证","初步推导","数学证明完成","数值验证完成","实证验证完成","存在反例","存在争议")
NOVELTY = ("尚未检索","已发现类似研究","可能存在新贡献","已完成阶段性文献核验")
RELATIONS = ("理论关联","数学关联","推导关系","扩展关系","支持关系","冲突关系","共同研究假设","潜在结合方向")
LONG_FIELDS = ("assumptions","variable_definitions","mathematical_formulas","derivation","conclusions","evidence","related_literature","open_questions","next_steps","novelty_log")
LIST_FIELDS = ("research_topics","keywords","source_refs","source_excerpts","related_ideas","contradictory_ideas")
CHOICES = {"idea_type": TYPES, "author_type": AUTHORS, "research_status": STATUSES,
           "verification_status": VERIFICATIONS, "novelty_status": NOVELTY}

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":"))

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

@contextmanager
def connect(path=None):
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path or STATE / "research.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        with db:
            yield db
    finally:
        db.close()

def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default

def setting(db, key, value):
    db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key,dump(value)))

def formula_normal(s):
    return re.sub(r"[\s{}]|\\(?:left|right|mathbf|mathrm)\b", "", s).replace("ρ", "\\rho")

def latex_in(text):
    pattern = r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|(?<![\w$])\$([^$\n]+)\$"
    return [next(p for p in match.groups() if p).strip() for match in re.finditer(pattern,text,re.S)
            if any(p and re.search(r"[A-Za-z_\\=^α-ωΑ-Ω]",p) for p in match.groups())]

def split_text(text, size=950):
    for para in re.split(r"\n\s*\n", text.strip()):
        if para:
            for start in range(0,len(para),size-110):
                yield para[start:start+size]
                if start+size >= len(para):
                    break

def store_source(db, sid, feed, kind, title, locator, modified, fingerprint, passages,
                 author="", meta=None, state="indexed", error=None):
    old = db.execute("SELECT fingerprint,state FROM sources WHERE id=?", (sid,)).fetchone()
    if old and old["fingerprint"] == fingerprint and old["state"] == state:
        return False
    db.execute("""INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET feed=excluded.feed,kind=excluded.kind,title=excluded.title,
      locator=excluded.locator,author=excluded.author,modified=excluded.modified,
      fingerprint=excluded.fingerprint,meta=excluded.meta,state=excluded.state,error=excluded.error""",
      (sid,feed,kind,title,locator,author,modified,fingerprint,dump(meta or {}),state,error))
    db.execute("DELETE FROM words WHERE rowid IN (SELECT id FROM passages WHERE source=?)", (sid,))
    db.execute("DELETE FROM passages WHERE source=?", (sid,))
    if state != "indexed":
        return True
    for anchor,role,body,formulas in passages:
        body = body.strip()
        if not body:
            continue
        chunks = [body] if len(body)<=1250 else list(split_text(body))
        for i,chunk in enumerate(chunks):
            pid = db.execute("INSERT INTO passages(source,anchor,role,text) VALUES (?,?,?,?)",
                             (sid,anchor if len(chunks)==1 else f"{anchor} · {i+1}",role,chunk)).lastrowid
            db.execute("INSERT INTO words(rowid,title,text) VALUES (?,?,?)",(pid,title,chunk))
            expressions = set(latex_in(chunk)) | (set(formulas) if i==0 else set())
            db.executemany("INSERT INTO formulas VALUES (?,?,?)",
                           ((pid,f,formula_normal(f)) for f in expressions if f))
    if not db.execute("SELECT 1 FROM passages WHERE source=?",(sid,)).fetchone():
        db.execute("INSERT INTO passages(source,anchor,role,text) VALUES (?,?,?,?)",(sid,"标题","未确认",title))
        pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO words(rowid,title,text) VALUES (?,?,?)",(pid,title,title))
    return True

def run_record(db, feed, status, detail, discovered=None):
    counts = dict(db.execute("SELECT state,count(*) FROM sources WHERE feed=? GROUP BY state",(feed,)).fetchall())
    if discovered is None:
        discovered = sum(v for k,v in counts.items() if k!="missing")
    db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
               (feed,now(),discovered,counts.get("indexed",0),counts.get("failed",0),status,detail))

def embed(texts):
    req = Request("http://127.0.0.1:11434/api/embed",
                  data=dump({"model":MODEL,"input":texts,"truncate":True,"keep_alive":"1m"}).encode(),
                  headers={"Content-Type":"application/json"})
    with urlopen(req, timeout=90) as r:
        return json.load(r)["embeddings"]

def embed_batches(db, max_passages, source_ids=None):
    total = 0
    while total < max_passages:
        selector = " AND p.source IN ("+",".join("?" for _ in source_ids)+")" if source_ids else ""
        rows = db.execute(f"""SELECT p.id,p.text,s.title FROM passages p JOIN sources s ON s.id=p.source
          WHERE s.state='indexed' AND COALESCE(json_extract(s.meta,'$.coverage'),'') NOT IN ('元数据','标题','metadata')
          {selector} AND NOT EXISTS(SELECT 1 FROM vectors v WHERE v.passage=p.id AND v.model=?)
          ORDER BY CASE s.feed WHEN 'idea' THEN 0 WHEN 'ai' THEN 1 WHEN 'course' THEN 2
          WHEN 'research' THEN 3 WHEN 'feishu' THEN 4 ELSE 5 END,
          CASE p.role WHEN '用户原话' THEN 0 WHEN 'Zotero 评论' THEN 0 WHEN '文献高亮' THEN 2 ELSE 1 END,p.id LIMIT ?""",
          (*list(source_ids or ()),MODEL,min(32,max_passages-total))).fetchall()
        if not rows:
            break
        values = embed([f"{r['title']}\n{r['text'][:1250]}" for r in rows])
        if len(values)!=len(rows):
            raise RuntimeError("Embedding response size differs from request")
        prepared=[(r["id"],MODEL,array.array("b",(round(max(-1,min(1,x))*127) for x in v)).tobytes(),r["id"])
                  for r,v in zip(rows,values)]
        db.execute("BEGIN IMMEDIATE")
        db.executemany("INSERT OR REPLACE INTO vectors SELECT ?,?,? WHERE EXISTS(SELECT 1 FROM passages WHERE id=?)",
                       prepared)
        db.commit()
        total += len(rows)
    return total

def tokenize(query):
    latin = re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}|[0-9]{3,}",query)
    cjk = re.findall(r"[\u3400-\u9fff]{3,}",query)
    grams = [g[i:i+3] for g in cjk for i in range(len(g)-2)]
    return list(dict.fromkeys(cjk+latin+grams))[:22]

RESULT = """SELECT p.id AS passage_id,p.anchor,p.role,p.text,s.id AS source_id,
                    s.title,s.feed,s.kind,s.author,s.modified,s.locator,s.meta
             FROM passages p JOIN sources s ON s.id=p.source"""
def search(db, query, feed="", kind="", author="", start="", end="", mode="hybrid", limit=24, tag=""):
    query = query.strip()[:300]
    limit = max(1,min(int(limit),100))
    checks, params = ["s.state='indexed'"], []
    for val,expr in ((feed,"s.feed=?"),(kind,"s.kind=?"),(author,"s.author LIKE ?"),
                     (start,"substr(s.modified,1,10)>=?"),(end,"substr(s.modified,1,10)<=?")):
        if val:
            checks.append(expr)
            params.append(f"%{val}%" if "LIKE" in expr else val)
    if tag:
        checks.append("EXISTS (SELECT 1 FROM json_each(s.meta, '$.tags') WHERE value LIKE ?) OR EXISTS (SELECT 1 FROM json_each(s.meta, '$.collections') WHERE value LIKE ?)")
        checks[-1]="("+checks[-1]+")"
        params.extend([f"%{tag}%",f"%{tag}%"])
    where = " AND ".join(checks)
    ranks, note = {}, ""
    def weight(ids, scale):
        for index,pid in enumerate(ids):
            ranks[pid] = ranks.get(pid,0)+scale/(30+index)
    terms = tokenize(query)
    s = query.replace('"','""')
    if mode in ("hybrid","keyword") and query:
        if len(query)>=3:
            exact = db.execute(f"""SELECT p.id FROM words JOIN passages p ON p.id=words.rowid
              JOIN sources s ON s.id=p.source WHERE words MATCH ? AND {where}
              ORDER BY bm25(words,4,1) LIMIT 100""", [f'"{s}"',*params])
            weight((r[0] for r in exact),2.5)
        if any(len(t)>=3 for t in terms):
            match = " OR ".join(f'"{t}"' for t in terms if len(t)>=3)
            lex = db.execute(f"""SELECT p.id FROM words JOIN passages p ON p.id=words.rowid
                JOIN sources s ON s.id=p.source WHERE words MATCH ? AND {where} ORDER BY bm25(words,4,1) LIMIT 160""",[match,*params])
            weight((r[0] for r in lex),1.2)
        if len(query)<3:
            rows = db.execute(f"{RESULT} WHERE {where} AND (instr(s.title,?)>0 OR instr(p.text,?)>0) LIMIT ?",[*params,query,query,limit])
            return [dict(r) for r in rows],note
    if mode=="formula" or (mode=="hybrid" and re.search(r"\\|[=ρ^_]",query)):
        rows = db.execute(f"""SELECT DISTINCT p.id FROM formulas f JOIN passages p ON p.id=f.passage
              JOIN sources s ON s.id=p.source WHERE {where} AND instr(f.normal,?)>0 LIMIT 120""",
              [*params,formula_normal(query)])
        weight((r[0] for r in rows),4)
    if query and mode in ("semantic","hybrid"):
        try:
            if not db.execute("SELECT 1 FROM vectors WHERE model=? LIMIT 1",(MODEL,)).fetchone():
                raise RuntimeError("向量索引尚未建立")
            vec=embed([query])[0]
            q=[round(max(-1,min(1,x))*127) for x in vec]
            qnorm=math.sqrt(sum(x*x for x in q))
            candidates=db.execute(f"""SELECT v.passage,v.value FROM vectors v JOIN passages p ON p.id=v.passage
              JOIN sources s ON s.id=p.source WHERE v.model=? AND {where}""",[MODEL,*params])
            def scores():
                for pid,blob in candidates:
                    a=memoryview(blob).cast("b")
                    norm=math.sqrt(sum(x*x for x in a))*qnorm
                    if norm and len(a)==len(q):
                        yield sum(x*y for x,y in zip(a,q))/norm,pid
            weight((pid for score,pid in heapq.nlargest(120,scores())),1.5)
        except (OSError,ValueError,RuntimeError,KeyError) as e:
            note=f"语义检索暂不可用：{e}"
    if not query:
        rows=db.execute(f"{RESULT} WHERE {where} ORDER BY s.modified DESC,p.id DESC LIMIT ?",[*params,limit])
        return [dict(r) for r in rows],note
    out, per_source, seen = [], {}, set()
    for pid in sorted(ranks,key=ranks.get,reverse=True):
        row=dict(db.execute(f"{RESULT} WHERE p.id=?",(pid,)).fetchone())
        sid=row["source_id"]
        signature=digest(row["text"])
        if per_source.get(sid,0)>=3 or signature in seen:
            continue
        seen.add(signature)
        per_source[sid]=per_source.get(sid,0)+1
        out.append(row)
        if len(out)>=limit:
            break
    return out,note

def idea_get(db, iid):
    row=db.execute("SELECT * FROM ideas WHERE id=?",(iid,)).fetchone()
    if not row:
        raise KeyError("找不到该思想")
    return {**json.loads(row["body"]),"idea_id":iid,"version":row["current"],
            "created_at":row["created"],"updated_at":row["updated"]}

def idea_save(db, value, reason, iid=None, expected=None, dedup=None):
    if not reason or not reason.strip():
        raise ValueError("请填写修改原因")
    when=now()
    old=idea_get(db,iid) if iid else None
    if old and expected!=old["version"]:
        raise ValueError("思想已更新，请重新打开最新版本")
    if not iid and dedup:
        row=db.execute("SELECT id FROM ideas WHERE dedup=?",(dedup,)).fetchone()
        if row:
            return row[0]
    base={**(old or {}),**value}
    clean={k:str(base.get(k) or "").strip() for k in ("title","summary","full_content","first_proposed_at","confidence",*LONG_FIELDS)}
    if not clean["title"] or not clean["full_content"]:
        raise ValueError("标题与完整内容为必填项")
    for k, choices in CHOICES.items():
        v=base.get(k) or ("来源尚不明确" if k=="author_type" else choices[0])
        if v not in choices:
            raise ValueError(f"无效的 {k}")
        clean[k]=v
    for k in LIST_FIELDS:
        v=base.get(k) or []
        clean[k]=[x.strip() for x in v.splitlines() if x.strip()] if isinstance(v,str) else list(v)
    if clean["author_type"] in AUTHORS[:2] and not (clean["source_refs"] and clean["source_excerpts"]):
        raise ValueError("标为用户观点需要原始来源和原话")
    clean["first_proposed_at"]=clean["first_proposed_at"] or when[:10]
    iid=iid or str(uuid.uuid4())
    version=old["version"]+1 if old else 1
    if old:
        if all(clean[k]==old.get(k) for k in clean):
            return iid
        n=db.execute("UPDATE ideas SET current=?,updated=?,body=? WHERE id=? AND current=?",
                     (version,when,dump(clean),iid,expected)).rowcount
        if not n:
            raise ValueError("思想已更新，请重新打开最新版本")
    else:
        db.execute("INSERT INTO ideas VALUES (?,?,?,?,?,?)",(iid,version,when,when,dump(clean),dedup))
    db.execute("INSERT INTO revisions VALUES (?,?,?,?,?)",(iid,version,when,reason,dump(clean)))
    return iid

def check_backup(path):
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
            raise RuntimeError("Backup integrity check failed")


@contextmanager
def backup_folder():
    dest=STATE/"backups"
    dest.mkdir(parents=True, exist_ok=True)
    with (dest/".rotation.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield dest


def rotate_backups(dest):
    backups=sorted(dest.glob("research-*.sqlite3"),reverse=True)
    if len(backups)<2:
        return 0
    from portable import archive_backup
    check_backup(backups[0])
    for old in backups[1:]:
        wal=old.with_name(old.name+"-wal")
        if wal.exists() and wal.stat().st_size:
            raise RuntimeError(f"Uncheckpointed backup preserved: {old.name}")
        archive_backup(old)
        old.unlink()
        wal.unlink(missing_ok=True)
        old.with_name(old.name+"-shm").unlink(missing_ok=True)
    return len(backups)-1


def backup():
    with backup_folder() as dest:
        output=dest/("research-"+datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")+".sqlite3")
        temp=output.with_suffix(".tmp")
        try:
            with closing(sqlite3.connect(STATE/"research.sqlite3", timeout=30)) as source:
                with closing(sqlite3.connect(temp)) as target:
                    source.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE")
            check_backup(temp)
            temp.replace(output)
        finally:
            temp.unlink(missing_ok=True)
        # An isolated runtime/test state must not publish its fixtures.
        if STATE.resolve()==(Path.home()/"Library/Application Support/Research Ideas/state").resolve():
            rotate_backups(dest)
        return output
