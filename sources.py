"""Read-only adapters. Neither indexing nor citation opening executes research code."""
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from db import APP, PORTABLE, ROOT, STATE, connect, digest, dump, get_setting, run_record, setting, store_source

HOME=Path.home()
DESKTOP=HOME/"Desktop"
EXTS=set(".docx .pdf .md .tex .txt .xlsx .csv .tsv .pptx .py .r .do .ado .ipynb .html .rtf .doc .xls .json .jl .m .qmd .rmd .bib".split())
SKIP=set("node_modules site-packages __pycache__ storage assets raw raw_data data_raw private backups backup env venv dist build cache caches downloads".split())
PRIVATE=re.compile(r"recovery.codes|password|passwd|credentials|\bsecrets?\b|api[_-]?keys?|traffic.accident|ssn|student.?grades|gradebook|工资单|身份证|成绩单|银行卡|密码|打分记录|学生名单|期中成绩",re.I)
COURSES=()
RESEARCH=()
MATH_NS="http://schemas.openxmlformats.org/officeDocument/2006/math"
GREEK={"ρ":r"\rho ","α":r"\alpha ","β":r"\beta ","γ":r"\gamma ","δ":r"\delta ","λ":r"\lambda ","π":r"\pi ","θ":r"\theta ","μ":r"\mu ","σ":r"\sigma ","ε":r"\epsilon ","τ":r"\tau ","Σ":r"\Sigma ","∞":r"\infty ","−":"-","×":r"\times ","≤":r"\le ","≥":r"\ge ","∈":r"\in ","→":r"\to ","∑":r"\sum "}
def local(tag): return tag.rsplit("}",1)[-1]
def starts_math(node): return node.tag.startswith("{"+MATH_NS+"}")

class Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out=[]
        self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ("script","style"): self.hidden+=1
        if tag in ("br","p","h1","h2","h3","li","tr","div","blockquote"): self.out.append("\n")
    def handle_endtag(self,tag):
        if tag in ("script","style"): self.hidden=max(0,self.hidden-1)
        if tag in ("p","div","li","blockquote"): self.out.append("\n")
    def handle_data(self,data):
        if not self.hidden: self.out.append(data)
def plain(s):
    p=Plain()
    p.feed(s)
    return "\n".join(x.strip() for x in "".join(p.out).splitlines() if x.strip())

def omml(node, unknown):
    tag=local(node.tag)
    children={local(c.tag):c for c in node}
    def part(name):
        return omml(children[name],unknown) if name in children else ""
    def join():
        return "".join(omml(x,unknown) for x in node if not local(x.tag).endswith("Pr"))
    def attribute(properties,name,default):
        x=children.get(properties)
        if x is not None:
            x=x.find(f"{{{MATH_NS}}}{name}")
        return x.get(f"{{{MATH_NS}}}val",default) if x is not None else default
    if tag=="t":
        return "".join(GREEK.get(c,c) for c in node.text or "")
    if tag=="f": return r"\frac{"+part("num")+"}{"+part("den")+"}"
    if tag in ("sSup","sSub","sSubSup"):
        return "{"+part("e")+"}"+("_{"+part("sub")+"}" if "sub" in children else "")+("^{"+part("sup")+"}" if "sup" in children else "")
    if tag=="rad":
        degree=part("deg")
        return r"\sqrt"+("["+degree+"]" if degree else "")+"{"+part("e")+"}"
    if tag in ("m","eqArr"):
        rows=[omml(x,unknown) for x in node if local(x.tag) in ("mr","e")]
        env="matrix" if tag=="m" else "aligned"
        return r"\begin{"+env+"}"+r"\\".join(rows)+r"\end{"+env+"}"
    if tag=="mr": return " & ".join(omml(x,unknown) for x in node if local(x.tag)=="e")
    if tag=="d":
        start=attribute("dPr","begChr","(") or "."
        end=attribute("dPr","endChr",")") or "."
        delimiters={"{":r"\{","}":r"\}","〈":r"\langle","〉":r"\rangle","‖":r"\|"}
        return r"\left"+delimiters.get(start,start)+join()+r"\right"+delimiters.get(end,end)
    if tag=="nary":
        operators={"∑":r"\sum","∏":r"\prod","∫":r"\int","∬":r"\iint","∮":r"\oint","⋃":r"\bigcup","⋂":r"\bigcap"}
        op=attribute("naryPr","chr","∫")
        if op not in operators: unknown.add(tag+":"+op)
        return operators.get(op,op)+"_{"+part("sub")+"}^{"+part("sup")+"} "+part("e")
    if tag in ("func","limLow","limUpp","acc","bar"):
        if tag=="func": return part("fName")+r"\,"+part("e")
        if tag=="bar": return r"\overline{"+part("e")+"}"
        if tag=="limLow": return part("e")+"_{"+part("lim")+"}"
        if tag=="limUpp": return part("e")+"^{"+part("lim")+"}"
        accents={"̂":r"\hat","̃":r"\tilde","̄":r"\bar","⃗":r"\vec","̇":r"\dot","̈":r"\ddot"}
        c=attribute("accPr","chr","̂")
        if c in accents: return accents[c]+"{"+part("e")+"}"
    if tag not in ("oMath","oMathPara","e","num","den","rad","sub","sup","deg","fName","lim","r","ctrlPr","argPr"):
        unknown.add(tag)
    return join()

def docx(path):
    out=[]
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name!="word/document.xml" and not re.fullmatch(r"word/(footnotes|endnotes|header\d+|footer\d+)\.xml",name):
                continue
            root=ET.fromstring(z.read(name))
            for i,p in enumerate((p for p in root.iter() if local(p.tag)=="p" and not starts_math(p)),1):
                text, formulas=[], []
                def walk(n):
                    if starts_math(n) and local(n.tag)=="oMath":
                        unknown=set()
                        formula=omml(n,unknown)
                        if unknown:
                            raw="".join(x.text or "" for x in n.iter() if local(x.tag)=="t")
                            text.append(f"[OMML 待核对：{raw}]")
                        elif formula.strip():
                            formulas.append(formula)
                            text.append("\\("+formula+"\\)")
                    elif local(n.tag)=="t":
                        text.append(n.text or "")
                    elif local(n.tag) in ("tab","br"):
                        text.append(" ")
                    else:
                        for c in n: walk(c)
                walk(p)
                if "".join(text).strip():
                    anchor=(f"段落 {i}" if name=="word/document.xml" else f"{name.split('/')[-1]} · 段落 {i}")
                    out.append((anchor,"文件原文","".join(text),formulas))
    return out

def xml_presentation(path):
    out=[]
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            n=re.fullmatch(r"ppt/slides/slide(\d+)\.xml",name)
            if n:
                xml=ET.fromstring(z.read(name))
                body="\n".join(t.text for t in xml.iter() if local(t.tag)=="t" and t.text)
                out.append((f"幻灯片 {n[1]}","文件原文",body,[]))
    return out

def spreadsheet(path):
    out=[]
    with zipfile.ZipFile(path) as z:
        names=z.namelist()
        shared=[]
        if "xl/sharedStrings.xml" in names:
            shared=["".join(t.text or "" for t in item.iter() if local(t.tag)=="t") for item in ET.fromstring(z.read("xl/sharedStrings.xml"))]
        if "xl/workbook.xml" in names:
            out.append(("工作簿","文件原文","工作表："+", ".join(e.get("name","") for e in ET.fromstring(z.read("xl/workbook.xml")).iter() if local(e.tag)=="sheet"),[]))
        for info in z.infolist():
            if not re.fullmatch(r"xl/worksheets/sheet\d+\.xml",info.filename):
                continue
            with z.open(info) as handle:
                parsed=ET.iterparse(handle,events=("end",))
                for _,row in parsed:
                    if local(row.tag)!="row":
                        continue
                    if int(row.get("r","0"))>12:
                        break
                    cells=[]
                    for c in row:
                        val=next((n.text for n in c if local(n.tag)=="v"),None)
                        formula=next((n.text for n in c if local(n.tag)=="f"),None)
                        if c.get("t")=="s" and val is not None: val=shared[int(val)]
                        if c.get("t")=="inlineStr": val="".join(t.text or "" for t in c.iter() if local(t.tag)=="t")
                        if val is not None: cells.append(f"{c.get('r','')} {str(val)[:180]}"+(f" (={formula})" if formula else ""))
                    if cells: out.append((f"{Path(info.filename).stem} · 行 {row.get('r')}", "表头及样本", " | ".join(cells[:40]),[]))
                    row.clear()
    return out

def get_file(path):
    ext=path.suffix.lower()
    if ext==".docx": return docx(path)
    if ext==".pptx": return xml_presentation(path)
    if ext==".xlsx": return spreadsheet(path)
    if ext==".pdf":
        completed=subprocess.run([str(APP/"pdftext"),str(path)],capture_output=True,text=True,timeout=35,check=True)
        pages=json.loads(completed.stdout)
        if not any(p.strip() for p in pages):
            raise ValueError("PDF 没有可搜索文字层，需 OCR")
        candidates=lambda page: [line.strip() for line in page.splitlines()
                                 if len(line)<=140 and re.search(r"[=≤≥]",line)
                                 and re.search(r"[ρλμ∑∫∞]|[A-Za-z]\s*\([^)]{1,30}\)",line)]
        return [(f"页 {i}","PDF 文字层 · 式子待核对",p,candidates(p)) for i,p in enumerate(pages,1) if p.strip()]
    if ext in (".doc",".rtf"):
        p=subprocess.run(["/usr/bin/textutil","-convert","txt","-stdout",str(path)],capture_output=True,text=True,timeout=15,check=True)
        return [("正文","文件原文",p.stdout,[])]
    if ext==".xls":
        return [("文件属性","文件元数据","旧版 Excel 工作簿。保留文件定位，表格正文待转换。",[])]
    with path.open("rb") as handle:
        raw=handle.read(1_500_000 if ext not in (".csv",".tsv") else 70_000)
    try: body=raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try: body=raw.decode("gb18030")
        except UnicodeDecodeError: body=raw.decode("utf-8",errors="replace")
    if ext==".ipynb":
        note=json.loads(body)
        return [(f"单元格 {i}","Notebook "+str(cell.get("cell_type","")),"".join(cell.get("source",[])),[])
                for i,cell in enumerate(note.get("cells",[]),1) if cell.get("cell_type") in ("code","markdown")]
    if ext in (".csv",".tsv"):
        reader=csv.reader(io.StringIO(body),delimiter="\t" if ext==".tsv" else ",")
        import itertools
        return [(f"行 {i}","表头及样本"," | ".join(cell[:180] for cell in row[:50]),[])
                for i,row in enumerate(itertools.islice(reader,10),1)]
    if ext==".html": body=plain(body)
    if ext==".json":
        return [("文件属性","文件元数据","结构化 JSON 文件。保留链接，避免将未经辨别的记录或整张数据表入库。",[])]
    out, blocks=[], []
    anchor=1
    for number,line in enumerate(body.splitlines(),1):
        if (line.startswith(("# ","## ","### ")) or sum(len(x) for x in blocks)>1000) and blocks:
            out.append((f"行 {anchor}–{number-1}","文件原文","\n".join(blocks),[]))
            blocks,anchor=[],number
        blocks.append(line)
    if blocks: out.append((f"行 {anchor}–{anchor+len(blocks)-1}","文件原文","\n".join(blocks),[]))
    return out

def allowed_folder(path):
    name=path.name
    return (not name.startswith(".") and name.lower() not in SKIP
            and not name.endswith((".app",".venv"))
            and name not in ("reconstruction_20261004","_历史过程归档_20261027")
            and not PRIVATE.search(name) and path not in (APP,PORTABLE)
            and not re.match(r"^(?:backup|cache|archive)[_-]",name,re.I)
            and not path.is_symlink())

def allowed_file(path):
    return (not path.name.startswith((".","~$")) and not PRIVATE.search(path.name)
            and not path.is_symlink() and path.suffix.lower() in EXTS)

def scan(roots):
    def fail(err): raise err
    for root in roots:
        if root.is_symlink() or not root.exists(): continue
        if root.is_file():
            yield root
            continue
        for current, folders, names in os.walk(root,onerror=fail):
            folders[:]=[n for n in folders if allowed_folder(Path(current)/n)]
            for name in sorted(names):
                path=Path(current)/name
                if allowed_file(path):
                    yield path

def event_scopes(roots, paths):
    scopes=set()
    for root in roots:
        real=root.resolve()
        for event in paths:
            if real.is_relative_to(event):
                scopes.add(root)
            elif event.is_relative_to(real):
                suffix=event.relative_to(real)
                parent=root
                for part in suffix.parts[:-1]:
                    parent=parent/part
                    if not allowed_folder(parent):
                        break
                else:
                    path=root/suffix
                    if path.is_symlink() or path.is_dir() and not allowed_folder(path):
                        continue
                    if path.is_file() and not allowed_file(path):
                        continue
                    scopes.add(path)
    return [p for p in sorted(scopes) if not any(parent in scopes for parent in p.parents)]

def sync_files(feed, roots, events=None):
    seen=set()
    scopes=roots if events is None else event_scopes(roots,events)
    if not scopes and events is not None:
        return
    missing_roots=[str(x) for x in roots if not x.exists()] if events is None else []
    with connect() as db:
        try:
            for path in scan(scopes):
                sid="file:"+str(path)
                if sid in seen: continue
                seen.add(sid)
                try:
                    stat=path.stat()
                except FileNotFoundError:
                    seen.remove(sid)
                    continue
                version="v5" if path.suffix.lower()==".pdf" else "v4"
                marker=f"{stat.st_mtime_ns}:{stat.st_size}:{version}"
                old=db.execute("SELECT state,meta,fingerprint FROM sources WHERE id=?",(sid,)).fetchone()
                if (old and old["state"]=="indexed" and json.loads(old["meta"]).get("file_marker")==marker and
                    not (json.loads(old["meta"]).get("cloud_placeholder") and not (stat.st_flags & 0x40000000))): continue
                if not stat.st_size:
                    fingerprint=digest(marker)
                    pieces=[("文件名","空文件标题",path.stem,[])]
                    state,error="indexed",None
                elif hasattr(stat,"st_flags") and stat.st_flags & 0x40000000:
                    fingerprint=digest(marker)
                    if (path.suffix.lower() in (".xlsx",".xls",".csv",".tsv") and
                        ("投入产出表" in path.parts or any(x.lower() in ("data","dataset","datasets") for x in path.parts))):
                        pieces=[("文件元数据","数据文件属性",f"{path.stem} · {stat.st_size:,} 字节 · OneDrive 占位，未读取数据行",[])]
                        state,error="indexed",None
                    else:
                        pieces=[]
                        state,error="failed","云端占位文件，待下载到 Mac"
                else:
                    try:
                        hv=hashlib.sha256()
                        with path.open("rb") as h:
                            if stat.st_size>64_000_000 and path.suffix.lower() in (".csv",".tsv",".xlsx",".xls"):
                                hv.update(h.read(1024*1024))
                                hv.update(marker.encode())
                            else:
                                for data in iter(lambda:h.read(1024*1024),b""): hv.update(data)
                        fingerprint=hv.hexdigest()+":"+version
                        pieces=get_file(path)
                        state,error="indexed",None
                    except Exception as e:
                        fingerprint=digest(marker)
                        pieces=[]
                        state,error="failed",str(e)[:260]
                modified=datetime.fromtimestamp(stat.st_mtime,timezone.utc).isoformat()
                store_source(db,sid,feed,path.suffix.lower()[1:],path.stem,path.as_uri(),modified,
                             fingerprint,pieces,meta={"size":stat.st_size,"file_marker":marker,
                             "created":datetime.fromtimestamp(stat.st_birthtime if hasattr(stat,"st_birthtime") else stat.st_ctime,timezone.utc).isoformat(),
                             "path":str(path),"cloud_placeholder":bool(getattr(stat,"st_flags",0)&0x40000000),
                             "coverage":"标题" if not stat.st_size else "元数据" if state=="indexed" and bool(getattr(stat,"st_flags",0)&0x40000000) else "文档"},state=state,error=error)
            if not missing_roots:
                active="SELECT id FROM sources WHERE feed=? AND state<>'missing'"
                if events is None:
                    existing=db.execute(active,(feed,)).fetchall()
                else:
                    existing=[]
                    for scope in scopes:
                        sid="file:"+str(scope)
                        existing.extend(db.execute(active+" AND (id=? OR (id>=? AND id<?))",
                                                   (feed,sid,sid+"/",sid+"0")).fetchall())
                db.executemany("UPDATE sources SET state='missing',error='源文件已移走或超出当前范围' WHERE id=?",
                               ((row[0],) for row in existing if row[0] not in seen))
            unread=db.execute("SELECT count(*) FROM sources WHERE feed=? AND state='failed'",(feed,)).fetchone()[0]
            metadata=db.execute("SELECT count(*) FROM sources WHERE feed=? AND state='indexed' AND json_extract(meta,'$.coverage') IN ('标题','元数据')",(feed,)).fetchone()[0]
            discovered=len(seen)
            if events is not None:
                discovered=db.execute("SELECT count(*) FROM sources WHERE feed=? AND state<>'missing'",(feed,)).fetchone()[0]
            detail="文件变更触发，已核对受影响路径。" if events is not None else "扫描配置内目录。"
            run_record(db,feed,"部分" if missing_roots or unread else "已同步",
                       detail+f"{metadata} 份只索引标题或文件元数据。表格保留表头和样本，PDF 保留可读文字层。"+(" 缺少目录："+", ".join(missing_roots) if missing_roots else ""),discovered)
        except OSError as e:
            run_record(db,feed,"失败",str(e),len(seen))

def zotero_get(path,params=None):
    query="?"+urlencode(params) if params else ""
    req=Request("http://127.0.0.1:23119/api/"+path+query,headers={"Zotero-API-Version":"3"})
    with urlopen(req,timeout=40) as r:
        return json.load(r),r.headers

def zotero_pages(path):
    start=0
    result=[]
    while True:
        rows,headers=zotero_get(path,{"limit":100,"start":start,"sort":"dateAdded","direction":"asc"})
        result.extend(x["data"] for x in rows)
        start+=len(rows)
        if not rows or 'rel="next"' not in headers.get("Link",""):
            return result

def zotero_link(prefix,key,kind="items"):
    target="library" if prefix=="users/0" else prefix
    return f"zotero://select/{target}/{kind}/{key}"

def sync_zotero():
    with connect() as db:
        try:
            groups,_=zotero_get("users/0/groups")
            libraries=["users/0"]+["groups/"+str(x["data"]["id"]) for x in groups]
        except Exception as e:
            run_record(db,"zotero","失败",f"Zotero 本地 API：{e}")
            return
    seen=set()
    errors=[]
    attachment_count=0
    for prefix in libraries:
        try:
            items=zotero_pages(prefix+"/items")
            collections={x["key"]:x for x in zotero_pages(prefix+"/collections")}
        except Exception as e:
            errors.append(f"{prefix}: {e}")
            continue
        by_key={x["key"]:x for x in items}
        def hierarchy(key):
            parts,visited=[],set()
            while key in collections and key not in visited:
                visited.add(key)
                c=collections[key]
                parts.append(c["name"])
                key=c.get("parentCollection")
            return " / ".join(reversed(parts))
        with connect() as db:
            for x in items:
                key,kind=x["key"],x["itemType"]
                sid=f"zotero:{prefix}:{key}"
                seen.add(sid)
                if kind=="attachment":
                    attachment_count+=1
                    continue
                parent=by_key.get(x.get("parentItem"),{})
                attachment=parent if kind=="annotation" else {}
                publication=by_key.get(parent.get("parentItem"),parent) if attachment else parent or x
                title=x.get("title") or publication.get("title") or ("独立笔记" if kind=="note" else "无标题条目")
                creators=[" ".join(filter(None,(a.get("firstName"),a.get("lastName")))) or a.get("name","")
                          for a in publication.get("creators",[])]
                author=", ".join(filter(None,creators))
                tags=list(dict.fromkeys(t.get("tag","") for item in (x,publication) for t in item.get("tags",[])))
                names=[hierarchy(k) for k in publication.get("collections",[])]
                page=x.get("annotationPageLabel","")
                meta={"item_key":publication.get("key",key),"key":key,"attachment_key":attachment.get("key",""),
                      "annotation_key":key if kind=="annotation" else "", "page":page,"position":x.get("annotationPosition"),
                      "date_added":x.get("dateAdded"),"doi":publication.get("DOI"),"url":publication.get("url"),
                      "tags":tags,"collections":names,"annotation_author":x.get("annotationAuthorName")}
                url=zotero_link(prefix,key)
                if kind=="annotation":
                    target="library" if prefix=="users/0" else prefix
                    url=f"zotero://open-pdf/{target}/items/{attachment.get('key','')}?annotation={key}"
                    pos=x.get("annotationPosition")
                    if pos:
                        try: url+="&page="+str(json.loads(pos).get("pageIndex",0)+1)
                        except (ValueError,TypeError,AttributeError): pass
                    place="页 "+str(page) if page else "附件批注"
                    passages=[(place,"文献高亮",x.get("annotationText",""),[]),(place,"Zotero 评论",x.get("annotationComment",""),[])]
                    if not any(p[2].strip() for p in passages):
                        passages=[(place,"批注位置",f"{x.get('annotationType','')} 批注（无可读文字或评论）",[])]
                elif kind=="note":
                    body=plain(x.get("note",""))
                    if title=="独立笔记" and body:
                        title=body.splitlines()[0][:130]
                    passages=[("笔记正文","Zotero 笔记 · 归属待确认",body,[])]
                else:
                    passages=[("文献元数据","文献元数据","\n".join(filter(None,[title,author,publication.get("date"),publication.get("DOI"),"；".join(names+tags)])),[]),
                              ("摘要","文献作者",plain(x.get("abstractNote") or ""),[])]
                store_source(db,sid,"zotero",kind,title,url,x.get("dateModified",""),
                             digest(dump([x,meta,title,author])+":v3"),passages,author=author,meta=meta)
    with connect() as db:
        if not errors:
            for row in db.execute("SELECT id FROM sources WHERE feed='zotero' AND state<>'missing'").fetchall():
                if row[0] not in seen:
                    db.execute("UPDATE sources SET state='missing' WHERE id=?",(row[0],))
        counts=db.execute("SELECT kind,count(*) FROM sources WHERE feed='zotero' AND state='indexed' GROUP BY kind").fetchall()
        detail="本地 API，只读。"+ "、".join(f"{k} {n}" for k,n in counts)+ f"。发现 {attachment_count} 个附件记录，附件全文与图像批注 OCR 未单独索引。"
        run_record(db,"zotero","部分" if errors else "已同步",detail+" ".join(errors),sum(n for _,n in counts) if not errors else None)
