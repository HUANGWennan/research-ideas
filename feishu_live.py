"""Read-only Feishu sync through an already authorized official CLI profile."""
import json
import os
import re
import subprocess
import tempfile
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from db import STATE, connect, digest, dump, now
from db import store_source
from external_sources import ACADEMIC, SNAPSHOTS, iso, mind_passages

CLI = Path(os.environ.get("RESEARCH_IDEAS_FEISHU_CLI", "/usr/local/bin/lark-cli")).expanduser()
PROFILE = os.environ.get("RESEARCH_IDEAS_FEISHU_PROFILE", "research-ideas")
HOST = os.environ.get("RESEARCH_IDEAS_FEISHU_HOST", "https://www.feishu.cn")
SELECTED_DRIVE = {}
EXPLICIT_DOCUMENTS = {}
TITLE_KEYWORDS = re.compile(
    r"课程|课堂|讲义|读书|读文献|研究|经济|数学|理论|方法论|期刊|论文|选题|假说|实证|"
    r"博士|资本|政治|历史唯物|社会科学|制度|文献|学术|投入产出|宏观|微观|经济史|"
    r"(?i:\b(?:ECON\s*\d*|PE\s*1|Nikaido|model|economics|capitalism|microeconomics|macroeconomics|math|syllabus|reading notes)\b)"
)
EXCLUDED = re.compile(r"身份|证件|护照|银行卡|密码|恢复码|财务报销|学生成绩|健康记录|私人日记|租房|求职|"
                      r"(?i:\b(?:passwords?|credentials?|gradebook|tax returns?|personal journal)\b)")
TYPE_ALIASES = {"mindnotes":"mindnote","slides":"slides","sheet":"sheet","sheets":"sheet","docs":"doc"}
PATH_KIND = {"docx":"docx","doc":"docs","mindnote":"mindnotes","sheet":"sheets","slides":"slides",
             "bitable":"base","file":"file","markdown":"file","wiki":"wiki"}
TOKEN = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
SEARCH_TERMS = ("", "课程", "学术", "研究", "论文", "讲义", "经济学", "政治经济学", "Nikaido")

class FeishuError(RuntimeError):
    pass

def read_cli(*args):
    cmd=[str(CLI),"--profile",PROFILE,*args,"--as","user"]
    try:
        p=subprocess.run(cmd,capture_output=True,text=True,timeout=75,check=False,env={**os.environ,"NO_COLOR":"1","PATH":"/usr/local/bin:/opt/homebrew/bin:"+os.environ.get("PATH","/usr/bin:/bin:/usr/sbin:/sbin")})
    except OSError as e:
        raise FeishuError(f"无法运行飞书 CLI：{e.strerror}") from e
    except subprocess.TimeoutExpired as e:
        raise FeishuError("飞书读取超时") from e
    result=None
    for raw in (p.stdout,p.stderr):
        try:
            result=json.loads(raw)
        except ValueError:
            continue
        if isinstance(result,dict):
            break
    if p.returncode or not isinstance(result,dict) or result.get("ok") is False or result.get("code",0)!=0:
        err=result.get("error",result) if isinstance(result,dict) else {}
        if isinstance(err,dict):
            parts=[str(err.get(k,"")) for k in ("type","subtype","code","message","msg")]
            msg=" ".join(x for x in parts if x).strip()
        else:
            msg=""
        # Report API error class, without echoing headers, credentials or raw response bodies.
        msg=re.sub(r"(?i)(bearer\s+)[\w.\-]+",r"\1[redacted]",msg)
        raise FeishuError(msg[:330] or f"飞书 CLI 返回 {p.returncode}，无可解析的 JSON")
    return result.get("data",result)

def pages(args,key,size):
    cursor=None
    seen=set()
    while True:
        params=("--page-token",cursor) if cursor else ()
        data=read_cli(*args,"--page-size",str(size),*params)
        if not isinstance(data,dict) or not isinstance(data.get(key),list):
            raise FeishuError(f"飞书列表缺少 {key} 字段")
        yield from data[key]
        if not data.get("has_more"):
            return
        cursor=data.get("next_page_token") or data.get("page_token")
        if not cursor or cursor in seen:
            raise FeishuError("飞书返回异常分页标记")
        seen.add(cursor)

def time_of(v):
    if isinstance(v,str) and v.isdigit():
        return iso(int(v))
    return iso(v)

def kind_of(value):
    k=str(value or "").lower()
    return TYPE_ALIASES.get(k,k)

def suitable(title):
    return bool(ACADEMIC.search(title) or TITLE_KEYWORDS.search(title))

def allowed_href(url):
    if not isinstance(url,str):
        return ""
    p=urlsplit(url)
    if p.scheme=="https" and p.hostname and p.hostname.endswith((".feishu.cn",".larksuite.com")) and not p.username:
        return f"{p.scheme}://{p.hostname}{p.path}"
    return ""

@dataclass
class Source:
    token: str
    title: str
    kind: str
    hierarchy: tuple
    url: str = ""
    modified: str = ""
    created: str = ""
    wiki_token: str = ""
    space_id: str = ""
    size: int = 0

    @property
    def sid(self):
        return "feishu:"+self.token

    def locator(self):
        domain=PATH_KIND.get(self.kind)
        return self.url or (f"{HOST}/wiki/{self.wiki_token}" if self.wiki_token else
               f"{HOST}/{domain}/{self.token}" if domain else "")

def build_inventory():
    documents={}
    errors=[]
    indexed_places=set()
    raw_count=0

    def add(doc):
        if not TOKEN.fullmatch(doc.token) or not doc.title or EXCLUDED.search(doc.title):
            return
        existing=documents.get(doc.token)
        if existing and existing.hierarchy and not doc.hierarchy:
            return
        if existing:
            doc.modified=max(doc.modified,existing.modified)
            doc.created=min(t for t in (doc.created,existing.created) if t) if doc.created or existing.created else ""
        documents[doc.token]=doc

    drive=deque([("",(),False)])
    drive.extend((token,label,True) for token,label in SELECTED_DRIVE.items())
    extra={}
    for term in SEARCH_TERMS:
        args=("drive","+search","--query",term)
        if term:
            args+=("--only-title",)
        try:
            for item in pages(args,"results",20):
                d=item.get("result_meta") or {}
                token=str(d.get("token") or "")
                title=unescape(re.sub(r"<[^>]*>","",str(item.get("title_highlighted") or ""))).strip()
                tag=kind_of(d.get("doc_types") or item.get("entity_type"))
                if not TOKEN.fullmatch(token) or not title or EXCLUDED.search(title):
                    continue
                if tag=="folder" and suitable(title):
                    drive.append((token,(title,),True))
                elif suitable(title):
                    extra[token]=Source(token,title,tag,(),allowed_href(d.get("url")),
                                        time_of(d.get("update_time_iso") or d.get("update_time")),
                                        time_of(d.get("create_time_iso") or d.get("create_time")))
        except FeishuError as e:
            errors.append(f"飞书搜索 {term or '可见文档'}：{e}")
    while drive:
        token,path,selected=drive.popleft()
        place=("drive",token)
        if place in indexed_places:
            continue
        indexed_places.add(place)
        args=("drive","files","list","--folder-token",token) if token else ("drive","files","list")
        try:
            for item in pages(args,"files",200):
                raw_count+=1
                title=str(item.get("name") or "").strip()
                f_token=str(item.get("token") or "")
                f_kind=kind_of(item.get("type"))
                if not title or not TOKEN.fullmatch(f_token) or EXCLUDED.search(title):
                    continue
                if f_kind=="shortcut":
                    shortcut=item.get("shortcut_info") or {}
                    f_token=str(shortcut.get("target_token") or f_token)
                    f_kind=kind_of(shortcut.get("target_type"))
                if not TOKEN.fullmatch(f_token):
                    continue
                if f_kind=="folder":
                    known=f_token in SELECTED_DRIVE
                    drive.append((f_token,path+(title,),selected or known or suitable(title)))
                elif selected or suitable(title):
                    add(Source(f_token,title,f_kind,path,allowed_href(item.get("url")),
                               time_of(item.get("modified_time")),time_of(item.get("created_time")),
                               size=int(item.get("size") or 0) if str(item.get("size") or "").isdigit() else 0))
        except FeishuError as e:
            errors.append(f"云盘 {' / '.join(path) or '根目录'}：{e}")

    wiki=deque()
    try:
        for space in pages(("wiki","spaces","list"),"items",50):
            sid=str(space.get("space_id") or "")
            label=str(space.get("name") or sid)
            if TOKEN.fullmatch(sid) and not EXCLUDED.search(label):
                wiki.append((sid,"",(label,),suitable(label)))
    except FeishuError as e:
        errors.append(f"知识库空间：{e}")
    wiki.append(("my_library","",("个人知识库",),False))
    while wiki:
        space_id,parent,path,selected=wiki.popleft()
        place=("wiki",space_id,parent)
        if place in indexed_places:
            continue
        indexed_places.add(place)
        args=("wiki","nodes","list","--space-id",space_id)
        if parent:
            args+=("--parent-node-token",parent)
        try:
            for node in pages(args,"items",50):
                raw_count+=1
                title=str(node.get("title") or "").strip()
                node_id=str(node.get("node_token") or "")
                subj=selected or suitable(title)
                if not title or EXCLUDED.search(title) or not TOKEN.fullmatch(node_id):
                    continue
                if node.get("has_child"):
                    wiki.append((space_id,node_id,path+(title,),subj))
                if subj:
                    token=str(node.get("obj_token") or node_id)
                    add(Source(token,title,kind_of(node.get("obj_type")),path,
                               allowed_href(node.get("url")),time_of(node.get("obj_edit_time")),
                               time_of(node.get("obj_create_time") or node.get("node_create_time")),
                               node_id,space_id))
        except FeishuError as e:
            errors.append(f"知识库 {' / '.join(path)}：{e}")

    for token,doc in extra.items():
        if token not in documents:
            add(doc)
    for _,title,url in SNAPSHOTS:
        token=url.rsplit("/",1)[-1]
        if token not in documents:
            add(Source(token,title,"mindnote" if "/mindnotes/" in url else "docx",(),url))
    for token,(title,kind) in EXPLICIT_DOCUMENTS.items():
        if token not in documents:
            add(Source(token,title,kind,()))
    return documents, errors, raw_count, len(indexed_places), len(extra)

class DocBlocks(HTMLParser):
    """Retain visible block text and formula source with stable block IDs when present."""
    BLOCKS={"title","p","h1","h2","h3","h4","h5","h6","h7","h8","h9","li","tr","quote","blockquote","pre",
            "code","callout","caption","slide","shape","text","heading","paragraph","text-box"}
    BREAKS={"br","td","th"}
    MATH={"equation","math","formula","latex"}
    def __init__(self, label):
        super().__init__(convert_charrefs=True)
        self.label=label
        self.stack=[]
        self.blocks=[]
        self.formula=None

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        bid=attrs.get("block-id") or attrs.get("id") or attrs.get("data-block-id") or attrs.get("slide-id")
        if tag in self.MATH:
            self.formula=[tag,[],attrs.get("latex") or attrs.get("content") or attrs.get("expression")]
            return
        if tag in self.BREAKS:
            if self.stack:
                self.stack[-1][2].append("\n" if tag=="br" else " | ")
            return
        if bid or tag in self.BLOCKS:
            self.stack.append((tag,bid,[],[]))

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self.formula:
            self.formula[1].append(data)
        elif self.stack:
            self.stack[-1][2].append(data)

    def handle_endtag(self, tag):
        if self.formula and tag==self.formula[0]:
            _,items,value=self.formula
            self.formula=None
            expression=(value or "".join(items)).strip()
            if expression and self.stack:
                self.stack[-1][2].append("\\("+expression+"\\)")
                self.stack[-1][3].append(expression)
            return
        if not self.stack or self.stack[-1][0]!=tag:
            return
        _,bid,fragments,formulas=self.stack.pop()
        body="".join(fragments).strip()
        if not bid and self.stack:
            self.stack[-1][2].append(body)
            self.stack[-1][3].extend(formulas)
            return
        if body:
            ref=f"{self.label} {bid or len(self.blocks)+1}"
            self.blocks.append((ref,"飞书正文 · 归属待确认",body,formulas))

def docs_pieces(content,label="文档块"):
    parser=DocBlocks(label)
    parser.feed(content)
    parser.close()
    return parser.blocks

def mind_elements(elements):
    words=[]
    for element in elements or []:
        if isinstance(element,str):
            words.append(element)
            continue
        typ=element.get("element_type")
        field={"user":"mention_user","doc":"mention_doc"}.get(typ,typ)
        obj=element.get(field) if field else None
        if isinstance(obj,str):
            words.append(obj)
        elif isinstance(obj,dict):
            visible=next((obj[key] for key in ("content","text","title","display_name","name","url","link") if isinstance(obj.get(key),str)),"")
            if visible:
                words.append(visible)
    return "".join(words)

def fetch(doc):
    kind=doc.kind
    token=doc.token
    title=doc.title
    meta={}
    if kind=="mindnote":
        data=read_cli("mindnotes","nodes","list","--mindnote-id",token)
        nodes=data.get("nodes")
        if not isinstance(nodes,list):
            raise FeishuError("思维笔记响应没有 nodes")
        nodes=[{"id":n.get("node_id"),"parent":n.get("parent_id"),
                "text":"\n".join(x for x in (mind_elements(n.get("texts")),mind_elements(n.get("notes"))) if x)}
               for n in nodes]
        pieces=mind_passages(nodes)
        meta["node_count"]=len(nodes)
    elif kind in ("docx","wiki"):
        data=read_cli("docs","+fetch","--doc",doc.locator() or token,"--detail","with-ids","--doc-format","xml")
        content=data.get("document") or data
        pieces=docs_pieces(content.get("content",""))
        meta["revision"]=content.get("revision_id")
        if not doc.hierarchy:
            title=next((p[2] for p in pieces[:1] if p[2] and len(p[2])<=120),title)
    elif kind=="doc":
        data=read_cli("api","GET",f"/open-apis/doc/v2/{token}/raw_content")
        content=data.get("content") or data.get("raw_content") or ""
        pieces=[("原文段落 "+str(i),"飞书正文 · 归属待确认",part,[])
                for i,part in enumerate(content.split("\n\n"),1) if part.strip()]
    elif kind=="slides":
        data=read_cli("slides","+xml-get","--presentation",doc.locator() or token)
        data=data.get("xml_presentation") or data
        pieces=docs_pieces(data.get("content",""),"演示文稿块")
        meta["revision"]=data.get("revision_id")
    elif kind=="markdown" or (kind=="file" and title.lower().endswith(".md")):
        data=read_cli("markdown","+fetch","--file-token",token)
        content=data.get("content") or data.get("markdown") or ""
        pieces=[("原文段落 "+str(i),"飞书正文 · 归属待确认",part,[])
                for i,part in enumerate(content.split("\n\n"),1) if part.strip()]
    elif kind=="sheet":
        data=read_cli("sheets","+workbook-info","--spreadsheet-token",token)
        sheets=data.get("sheets",data.get("sub_sheets",[]))
        if not isinstance(sheets,list) or not sheets:
            raise FeishuError("工作簿未返回工作表元数据")
        pieces=[]
        for sheet in sheets:
            label=str(sheet.get("title") or sheet.get("sheet_id") or "工作表")
            dimensions=sheet.get("grid_properties") or {}
            rows=sheet.get("row_count") or dimensions.get("row_count")
            cols=sheet.get("column_count") or dimensions.get("column_count")
            pieces.append((label,"飞书表格元数据",f"{label} · 行 {rows or '?'} · 列 {cols or '?'}",[]))
        meta["coverage"]="metadata"
    elif kind=="file":
        suffix=Path(title).suffix.lower()
        pieces=[]
        if suffix in (".docx",".doc",".pdf",".pptx",".md",".txt",".tex") and doc.size<=32_000_000:
            from sources import get_file
            try:
                with tempfile.TemporaryDirectory(prefix="research-feishu-") as tmp:
                    original=Path(tmp)/("document"+suffix)
                    read_cli("drive","+download","--file-token",token,"--output",str(original))
                    if original.stat().st_size<=32_000_000:
                        pieces=get_file(original)
            except (OSError,ValueError,FeishuError,subprocess.SubprocessError) as e:
                meta["body_note"]="附件正文未取得："+str(e)[:160]
        if not pieces:
            meta["coverage"]="metadata"
            pieces=[("附件登记","飞书文件元数据",title+" · "+(f"{doc.size:,} 字节" if doc.size else "大小未知")+"。"+meta.get("body_note","未提取正文。"),[])]
    else:
        raise FeishuError(f"{kind or '未知类型'} 正文暂未接入")
    if not pieces:
        meta["coverage"]="metadata"
        pieces=[("文档登记","飞书文件元数据",title+" · 接口未返回可检索文字，保留原件定位。",[])]
    return title,pieces,meta

def sync_feishu_live():
    """Synchronize authorized sources, retaining historical snapshots on access failures."""
    docs,errors,inventory,places,search_matches=build_inventory()
    copied,failed,partial=0,0,0
    coverage=Counter()
    with connect() as db:
        existing={row["id"]:row for row in db.execute("SELECT id,locator,title,meta,modified, state FROM sources WHERE feed='feishu'")}
    pending=[]
    for doc in docs.values():
        row=existing.get(doc.sid)
        old_meta=json.loads(row["meta"]) if row else {}
        if (row and row["state"]=="indexed" and old_meta.get("sync_mode")=="live-cli" and doc.modified
                and doc.modified==row["modified"] and doc.hierarchy==tuple(old_meta.get("hierarchy",[]))
                and doc.title==row["title"] and doc.locator()==row["locator"]):
            copied+=1
            partial+=int(old_meta.get("coverage")=="metadata")
            coverage[doc.kind]+=1
            continue
        pending.append(doc)

    def fetch_one(doc):
        try:
            return fetch(doc),None
        except (FeishuError, AttributeError, TypeError, ValueError, KeyError) as e:
            return None,e

    with ThreadPoolExecutor(max_workers=3) as worker:
        results=worker.map(fetch_one,pending)
        for doc,(result,error) in zip(pending,results):
            if error:
                failed+=1
                errors.append(f"{doc.title}：{error}")
                continue
            title,pieces,meta=result
            meta.update({"sync_mode":"live-cli","original_id":doc.token,"hierarchy":doc.hierarchy,
                         "source_modified_known":bool(doc.modified),"created":doc.created,"wiki_token":doc.wiki_token,
                         "space_id":doc.space_id,"block_count":len(pieces)})
            fingerprint=digest(dump([title,doc.modified,doc.locator(),pieces,meta]))
            with connect() as db:
                store_source(db,doc.sid,"feishu",doc.kind,title,doc.locator(),doc.modified,
                             fingerprint,pieces,meta=meta)
            copied+=1
            partial+=int(meta.get("coverage")=="metadata")
            coverage[doc.kind]+=1
    status="已同步" if copied and not failed and not errors and not partial else "部分" if copied else "待接入"
    counts="、".join(f"{name} {amount}" for name,amount in sorted(coverage.items()))
    detail=f"只读飞书用户 OAuth，遍历 {places} 个目录/节点入口，发现 {inventory} 个目录条目，搜索筛出 {search_matches} 份学术资料；合计选中 {len(docs)} 份。飞书搜索有结果上限，覆盖全部可见文档的比例未知。"
    if counts:
        detail+=" 正文或元数据："+counts+"。"
    if partial:
        detail+=f" 其中 {partial} 份仅有元数据。"
    if errors:
        detail+=" 差错："+ "；".join(errors[:12])
        if len(errors)>12:
            detail+=f"；另 {len(errors)-12} 项"
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
                   ("feishu-live",now(),len(docs),copied,failed,status,detail))
    folder=STATE/"reports"
    folder.mkdir(exist_ok=True,parents=True)
    report=folder/"feishu-sync.json"
    report_tmp=report.with_suffix(".tmp")
    report_tmp.write_text(json.dumps({"kind":dict(coverage),"errors":errors,"metadata_only":partial},ensure_ascii=False,indent=2),encoding="utf-8")
    report_tmp.replace(report)
    return {"discovered":len(docs),"indexed":copied,"failed":failed,"partial":partial,
            "inventory":inventory,"status":status,"by_type":dict(coverage),"errors":errors}
