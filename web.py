"""Private loopback library for research ideas and their citations."""
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
from collections import Counter
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from db import (APP, AUTHORS, CHOICES, LIST_FIELDS, LONG_FIELDS, RELATIONS, STATE,
                STATUSES, TYPES, MODEL, connect, idea_get, idea_save, search)

PORT = int(os.environ.get("RESEARCH_IDEAS_PORT", "8767"))
HOST = "127.0.0.1"
TOKEN = secrets.token_urlsafe(32)
NAV = (("/", "概览"), ("/search", "检索"), ("/ask", "问答"), ("/ideas", "研究思想"), ("/graph", "关系图"),
       ("/timeline", "演化记录"), ("/topics", "论文选题"), ("/sources", "数据源"), ("/reports", "研究报告"))
FEEDS = {"idea":"思想目录","research":"桌面研究项目","course":"课程资料","zotero":"Zotero","zotero-ocr":"Zotero 图像 OCR","feishu-live":"飞书实时同步","vectors":"语义索引",
         "feishu":"飞书","ai":"AI 学术记录","curated":"已核对摘录"}
SYNC_FEEDS = set(FEEDS) - {"curated"}
FIELDS = {
    "title":"标题","summary":"摘要","full_content":"完整内容","first_proposed_at":"首次提出日期","confidence":"可信程度",
    "idea_type":"类别","author_type":"归属","research_status":"研究状态","verification_status":"验证状态","novelty_status":"原创性核验",
    "assumptions":"假设","variable_definitions":"变量定义","mathematical_formulas":"数学公式","derivation":"推导",
    "conclusions":"结论","evidence":"证据","related_literature":"相关文献","open_questions":"待解决问题","next_steps":"下一步",
    "novelty_log":"文献核验记录","research_topics":"研究主题","keywords":"关键词","source_refs":"来源定位",
    "source_excerpts":"来源原话","related_ideas":"相关思想","contradictory_ideas":"冲突思想",
}
SYMBOLS = {"alpha":"α","beta":"β","gamma":"γ","delta":"δ","epsilon":"ε","varepsilon":"ε","eta":"η","theta":"θ",
           "lambda":"λ","mu":"μ","nu":"ν","rho":"ρ","sigma":"σ","tau":"τ","phi":"φ","varphi":"φ","chi":"χ","psi":"ψ",
           "omega":"ω","Gamma":"Γ","Delta":"Δ","Theta":"Θ","Lambda":"Λ","Sigma":"Σ","Phi":"Φ","Omega":"Ω",
           "pi":"π","ell":"ℓ","infty":"∞","vert":"|","lvert":"|","rvert":"|","cdot":"·","times":"×","pm":"±",
           "le":"≤","leq":"≤","ge":"≥","geq":"≥","neq":"≠","approx":"≈","equiv":"≡","subset":"⊂",
           "in":"∈","mapsto":"↦","rightarrow":"→","to":"→","partial":"∂","nabla":"∇","int":"∫","sum":"∑","prod":"∏"}
MATH_PATTERN = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|(?<![\w$])\$([^$\n]+)\$",re.S)
SYNC_LOCK = threading.Lock()
ACTIVE_SYNC = None
LOG = logging.getLogger("research-ideas")

def esc(value):
    return escape("" if value is None else str(value), quote=True)

def url(path, **query):
    return esc(path + ("?"+urlencode(query) if query else ""))

def stamp(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError,TypeError):
        return value or "—"

class MathML:
    """Render the common LaTeX operators emitted by the document adapter."""
    def __init__(self, expression):
        self.tokens = re.findall(r"\\[A-Za-z]+|\\.|[^\s]", expression)
        self.index = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def pop(self):
        token = self.peek()
        if token:
            self.index += 1
        return token

    def group(self, grouped=False):
        parts=[]
        while self.peek() and self.peek()!="}":
            base=self.atom()
            scripts={}
            while self.peek() in ("_","^"):
                op=self.pop()
                scripts[op]=self.atom()
            if len(scripts)==2:
                base=f"<msubsup>{base}{scripts['_']}{scripts['^']}</msubsup>"
            elif scripts:
                op,value=next(iter(scripts.items()))
                tag="msub" if op=="_" else "msup"
                base=f"<{tag}>{base}{value}</{tag}>"
            parts.append(base)
        if grouped and self.pop()!="}":
            raise ValueError("missing delimiter")
        return "<mrow>"+"".join(parts)+"</mrow>"

    def atom(self):
        token=self.pop()
        if token=="{":
            return self.group(True)
        if token==r"\frac":
            return "<mfrac>"+self.atom()+self.atom()+"</mfrac>"
        if token==r"\sqrt":
            return "<msqrt>"+self.atom()+"</msqrt>"
        variants={r"\mathbf":"bold",r"\boldsymbol":"bold",r"\mathrm":"normal",r"\mathit":"italic",r"\mathbb":"double-struck"}
        if token in variants:
            return f'<mstyle mathvariant="{variants[token]}">{self.atom()}</mstyle>'
        if token==r"\operatorname" or token==r"\text":
            if self.pop()!="{":
                raise ValueError("invalid text")
            text=[]
            while self.peek() and self.peek()!="}":
                text.append(self.pop())
            if self.pop()!="}":
                raise ValueError("invalid text")
            return f"<mtext>{esc(''.join(text))}</mtext>"
        if token in (r"\left",r"\right"):
            return self.atom()
        if token in (r"\,",r"\;",r"\quad",r"\qquad",r"\!"):
            return '<mspace width="0.25em"/>'
        if token.startswith("\\"):
            if token[1:] in SYMBOLS:
                token=SYMBOLS[token[1:]]
            elif token in (r"\{",r"\}",r"\|",r"\_",r"\%"):
                token=token[1:]
            elif token in (r"\ln",r"\log",r"\lim",r"\sup",r"\inf",r"\max",r"\min",r"\sin",r"\cos",r"\exp"):
                return f"<mi>{esc(token[1:])}</mi>"
            else:
                raise ValueError("unsupported notation")
        if not token:
            raise ValueError("incomplete notation")
        tag="mn" if token.isnumeric() else "mi" if token.isalpha() else "mo"
        return f"<{tag}>{esc(token)}</{tag}>"

def math_html(expression):
    if len(expression)>800:
        return f"<code>{esc(expression)}</code>"
    try:
        if not expression.strip():
            raise ValueError("empty notation")
        boxes=MathML(expression)
        inner=boxes.group()
        if boxes.peek():
            raise ValueError("incomplete notation")
        return f'<math xmlns="http://www.w3.org/1998/Math/MathML" aria-label="{esc(expression)}">{inner}</math>'
    except (ValueError,RecursionError):
        return f'<code title="LaTeX 原式">{esc(expression)}</code>'

def rich(value):
    text=str(value or "")
    parts=[]
    start=0
    for match in MATH_PATTERN.finditer(text):
        parts.extend((esc(text[start:match.start()]),math_html(next(x for x in match.groups() if x))))
        start=match.end()
    parts.append(esc(text[start:]))
    return "".join(parts)

def layout(path, title, main, action=""):
    nav="".join(
        f'<a href="{href}" class="{"active" if path==href else ""}" '
        f'{"aria-current=page" if path==href else ""}>{label}</a>' for href,label in NAV)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{esc(title)} · Research Ideas</title><link rel="stylesheet" href="/web.css">
    <script src="/web.js" defer></script></head><body><header class="sitebar">
    <a class="brand" href="/"><span aria-hidden="true">R<span>I</span></span>Research Ideas</a>
    <nav aria-label="主导航">{nav}</nav></header><main><div class="heading">
    <h1>{esc(title)}</h1>{action}</div>{main}</main></body></html>"""

def form_guard():
    return f'<input type="hidden" name="csrf" value="{TOKEN}">'

def hidden(name, value):
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'

def field(name, label, value="", kind="text", placeholder="", required=False):
    return (f'<label>{esc(label)}<input type="{kind}" name="{esc(name)}" value="{esc(value)}"'
            f' placeholder="{esc(placeholder)}"{" required" if required else ""}></label>')

def choose(name, label, options, selected="", any_label=None):
    options=list(options)
    if any_label:
        options=[("",any_label)]+options
    choices="".join(f'<option value="{esc(k)}"{" selected" if k==selected else ""}>{esc(v)}</option>' for k,v in options)
    return f'<label>{esc(label)}<select name="{esc(name)}">{choices}</select></label>'

def area(name, label, value="", rows=4, required=False, placeholder=""):
    return (f'<label>{esc(label)}<textarea name="{esc(name)}" rows="{rows}"'
            f' placeholder="{esc(placeholder)}"{" required" if required else ""}>{esc(value)}</textarea></label>')

def read_ideas(db):
    return [{**json.loads(r["body"]),"idea_id":r["id"],"version":r["current"],"updated_at":r["updated"],"created_at":r["created"]}
            for r in db.execute("SELECT * FROM ideas ORDER BY updated DESC,id")]

def chip(label):
    return f'<span class="chip">{esc(label)}</span>'

def prose(value):
    return f'<div class="prose">{rich(value)}</div>'

def empty(message, href=None, label=None):
    link=f' <a href="{esc(href)}">{esc(label)}</a>' if href else ""
    return f'<div class="empty">{esc(message)}{link}</div>'

def idea_card(idea):
    summary=idea.get("summary") or idea.get("full_content","")[:240]
    tags="".join(chip(x) for x in (idea.get("idea_type"),idea.get("author_type"),idea.get("research_status"),*idea.get("research_topics",[])[:3]) if x)
    return (f'<article class="item"><div class="tags">{tags}</div>'
            f'<h3><a href="{url("/idea",id=idea["idea_id"])}">{esc(idea.get("title"))}</a></h3>'
            f'{prose(summary)}<p class="muted">{esc(stamp(idea["updated_at"]))}</p></article>')

def idea_matches(ideas, query, kind="", topic="", status="", author=""):
    words=query.casefold().split()
    for idea in ideas:
        if kind and kind != idea.get("idea_type"):
            continue
        if topic and topic not in idea.get("research_topics",[]):
            continue
        if status and status != idea.get("research_status"):
            continue
        if author and author != idea.get("author_type"):
            continue
        haystack=json.dumps({k:v for k,v in idea.items() if k not in ("idea_id","created_at","updated_at")},ensure_ascii=False).casefold()
        if all(word in haystack for word in words):
            yield idea

def search_controls(args, include_mode=True):
    controls=(f'<form method="get" action="/search" class="searchform">'
              f'<label class="sr" for="query">检索内容</label><div class="searchline">'
              f'<input id="query" name="q" placeholder="用自然语言、关键词或公式检索" maxlength="300" value="{esc(args.get("q",""))}">'
              '<button type="submit">检索</button></div>')
    if include_mode:
        controls+=hidden("scope",args.get("scope","sources"))
        return controls
    return controls+'</form>'

def list_filters(args, ideas):
    topics=sorted({topic for item in ideas for topic in item.get("research_topics",[])})
    return (choose("idea_type","类别",[(x,x) for x in TYPES],args.get("idea_type"),"全部类别")
            +choose("topic","研究主题",[(x,x) for x in topics],args.get("topic"),"全部主题")
            +choose("research_status","研究状态",[(x,x) for x in STATUSES],args.get("research_status"),"全部状态")
            +choose("author_type","归属",[(x,x) for x in AUTHORS],args.get("author_type"),"全部归属"))

def status_table(db):
    records={r["feed"]:dict(r) for r in db.execute("SELECT * FROM runs ORDER BY feed")}
    for key in FEEDS:
        if key not in records:
            counts=dict(db.execute("SELECT state,count(*) FROM sources WHERE feed=? GROUP BY state",(key,)))
            if counts:
                records[key]={"feed":key,"status":"已有索引","at":None,"indexed":counts.get("indexed",0),"failed":counts.get("failed",0),
                              "discovered":counts.get("indexed",0)+counts.get("failed",0),"detail":""}
            else:
                records[key]={"feed":key,"status":"未接入","at":None,"indexed":0,"failed":0,"discovered":None,"detail":""}
    rows=[]
    for key,r in records.items():
        rows.append(f'<tr><th><a href="{url("/sources",feed=key)}">{esc(FEEDS.get(key,key))}</a></th>'
                    f'<td>{chip(r["status"])}</td><td>{esc(r["discovered"] if r["discovered"] is not None else "—")}</td>'
                    f'<td>{esc(r["indexed"])}</td><td>{esc(r["failed"])}</td><td>{esc(stamp(r["at"]))}</td><td>{esc(r["detail"])}</td></tr>')
    return ('<div class="table-wrap"><table><thead><tr><th>来源</th><th>接入状态</th><th>已发现</th><th>已索引</th>'
            '<th>失败</th><th>最近同步</th><th>覆盖说明</th></tr></thead><tbody>'+"".join(rows)+'</tbody></table></div>')

def dashboard(db):
    ideas=read_ideas(db)
    by_type=Counter(x.get("idea_type") for x in ideas)
    stats=[("研究思想",len(ideas),"/ideas"),("数学模型",by_type["数学模型"],url("/ideas",idea_type="数学模型")),
           ("待解决问题",sum(x.get("idea_type")=="未解决问题" and x.get("research_status") not in ("已验证","已否定") for x in ideas),url("/ideas",idea_type="未解决问题")),
           ("研究假说",by_type["研究假说"],url("/ideas",idea_type="研究假说"))]
    tiles="".join(f'<a class="metric" href="{link}"><span>{label}</span><strong>{value:,}</strong></a>' for label,value,link in stats)
    passages=db.execute("SELECT count(*) FROM passages").fetchone()[0]
    vectors=db.execute("SELECT count(*) FROM vectors WHERE model=?",(MODEL,)).fetchone()[0]
    coverage=f"全文索引 {passages:,} 段 · 语义索引 {vectors:,} 段"
    mod="".join(map(idea_card,[x for x in ideas if x["version"]>1][:5]))
    new=sorted(ideas,key=lambda x:x["created_at"],reverse=True)[:5]
    topics=Counter(topic for x in ideas for topic in x.get("research_topics",[]) if x.get("research_status") not in ("已否定","暂时搁置"))
    topic_links=" ".join(f'<a class="chip" href="{url("/ideas",topic=k)}">{esc(k)} · {n}</a>' for k,n in topics.most_common(12))
    return ('<section class="dashboard-search">'+search_controls({},False)+f'<p class="muted">{coverage}</p></section>'
            f'<div class="metrics">{tiles}</div><section><h2>研究方向</h2>{topic_links or empty("添加研究主题后可从这里查看相关思想。")}</section>'
            '<div class="columns"><section><h2>最近新增</h2>'+("".join(map(idea_card,new)) or empty("尚无研究思想。","/idea/new","添加思想"))
            +'</section><section><h2>最近修改</h2>'+(mod or empty("尚无已修改的思想。"))+'</section></div>'
            '<section><h2>数据源</h2>'+status_table(db)+'</section>')

def result_card(row):
    sid=row["source_id"]
    citation=" · ".join(x for x in (row["author"],row["anchor"],stamp(row["modified"])) if x and x!="—")
    return (f'<article class="item"><div class="tags">{chip(FEEDS.get(row["feed"],row["feed"]))}{chip(row["role"])}</div>'
            f'<h3><a href="{url("/source",id=sid,p=row["passage_id"])}">{esc(row["title"])}</a></h3>'
            f'<p class="muted">{esc(citation)}</p>{prose(row["text"])}'
            f'<div class="actions"><a href="{url("/source",id=sid,p=row["passage_id"])}">定位出处</a>'
            +open_action(sid,row["locator"],row.get("passage_id"),row["anchor"])+'</div></article>')

def render_search(db, args):
    query=args.get("q","").strip()[:300]
    scope=args.get("scope","sources")
    tabs='<div class="tabs">'+''.join(f'<a class="{"active" if scope==value else ""}" href="{url("/search",q=query,scope=value)}">{label}</a>'
                                    for value,label in (("sources","原始资料"),("ideas","研究思想")))+'</div>'
    html=tabs+search_controls({**args,"scope":scope})
    if scope=="ideas":
        ideas=read_ideas(db)
        matches=list(idea_matches(ideas,query,args.get("idea_type",""),args.get("topic",""),args.get("research_status",""),args.get("author_type","")))
        html+='<div class="filters">'+list_filters(args,ideas)+'</div></form>'
        return html+(f'<p class="muted result-count">{len(matches)} 条思想</p>'+"".join(map(idea_card,matches[:120])) if matches else empty("没有匹配的思想。"))
    mode=args.get("mode","hybrid")
    if mode not in {"hybrid","keyword","semantic","formula"}:
        mode="hybrid"
    kinds=db.execute("SELECT DISTINCT kind FROM sources WHERE state='indexed' ORDER BY kind").fetchall()
    kind_opts=sorted((r[0],r[0]) for r in kinds)
    facets=("feed","kind","author","tag","start","end")
    html+='<details class="filterbox"'+(" open" if any(args.get(k) for k in facets) or mode!="hybrid" else "")+'><summary>筛选条件</summary><div class="filters">'
    html+=choose("mode","检索方式",(("hybrid","混合"),("keyword","关键词"),("semantic","语义"),("formula","公式")),mode)
    html+=choose("feed","来源",FEEDS.items(),args.get("feed"),"全部来源")
    html+=choose("kind","文档类型",kind_opts,args.get("kind"),"全部类型")
    html+=field("author","作者",args.get("author",""))
    html+=field("tag","标签或分类",args.get("tag",""))
    html+=field("start","起始日期",args.get("start",""),"date")+field("end","结束日期",args.get("end",""),"date")
    html+='</div><button class="quiet" type="submit">应用筛选</button></details></form>'
    if not query and not any(args.get(k) for k in facets):
        return html
    rows,note=search(db,query,feed=args.get("feed",""),kind=args.get("kind",""),author=args.get("author",""),
                     start=args.get("start",""),end=args.get("end",""),mode=mode,tag=args.get("tag",""))
    if note:
        html+=f'<p class="notice">{esc(note)}</p>'
    if not rows:
        return html+empty("未找到匹配的资料。")
    return html+f'<p class="muted result-count">显示 {len(rows)} 个最相关片段</p>'+"".join(map(result_card,rows))


def answer_page(db,args):
    from answers import answer
    question=args.get("q","")[:280]
    feed=args.get("feed","")
    form=('<form method="get" action="/ask" class="filters">'
          +field("q","问题",question,placeholder="例如：我的资料如何讨论一般利润率？",required=True)
          +choose("feed","来源",FEEDS.items(),feed,"全部来源")
          +'<button class="primary" type="submit">生成回答</button></form>'
          '<p class="muted">回答在本机生成。引用编号对应下方原文，生成可能需要片刻。</p>')
    if not question:
        return form
    generated,rows,note=answer(db,question,feed)
    if note:
        form+=f'<p class="muted">{esc(note)}</p>'
    if generated:
        form+='<section class="item"><h2>基于原文的综合回答</h2>'+prose(generated)+'</section>'
    if rows:
        form+='<section><h2>引用原文</h2>'+''.join(f'<h3>[{i}]</h3>'+result_card(row) for i,row in enumerate(rows,1))+'</section>'
    return form

def external_original(split, anchor=""):
    if split.scheme != "https" or not split.hostname:
        return None
    if split.hostname.endswith(".feishu.cn") and split.path.startswith("/docx/"):
        block = re.match(r"文档块 ([A-Za-z0-9_-]{8,100})", anchor or "")
        if block:
            split = split._replace(fragment=block[1])
    return split.geturl()


def open_action(sid, locator, pid=None, anchor=""):
    if not locator:
        return ""
    target = external_original(urlsplit(locator), anchor)
    if target:
        return f'<a href="{esc(target)}" target="_blank" rel="noopener noreferrer">打开原件</a>'
    return (f'<form method="post" action="/open" target="_self">{form_guard()}{hidden("id",sid)}{hidden("p",pid) if pid else ""}'
            '<button class="clear" type="submit">打开原件</button></form>')

def source_page(db, args):
    sid=args.get("id","")
    row=db.execute("SELECT * FROM sources WHERE id=?",(sid,)).fetchone()
    if row is None:
        raise KeyError("找不到该来源")
    meta=json.loads(row["meta"])
    tags="".join(chip(v) for v in (FEEDS.get(row["feed"],row["feed"]),row["kind"],row["state"]))
    title=f'<div class="tags">{tags}</div>'
    actions=f'<div class="actions">{open_action(sid,row["locator"])}<a href="{url("/idea/new",source=sid,p=args.get("p",""))}">根据此来源记录想法</a></div>'
    info=(("作者",row["author"]),("最后修改",stamp(row["modified"])),("定位",row["locator"]),
          ("文献条目",meta.get("item_key")),("附件",meta.get("attachment_key")),("批注",meta.get("annotation_key")),
          ("页码",meta.get("page")),("DOI",meta.get("doi")),("分类"," · ".join(meta.get("collections") or ())),
          ("标签"," · ".join(meta.get("tags") or ())),("同步错误",row["error"]))
    details='<dl class="details">'+''.join(f'<dt>{k}</dt><dd>{esc(v)}</dd>' for k,v in info if v and v!="—")+'</dl>'
    focus=args.get("p","")
    after=args.get("after","")
    if not after.isdecimal():
        after="0"
    if focus and focus.isdecimal():
        position=db.execute("SELECT id FROM passages WHERE source=? AND id=?",(sid,focus)).fetchone()
        if position and after=="0":
            lead=db.execute("SELECT id FROM passages WHERE source=? AND id<? ORDER BY id DESC LIMIT 1 OFFSET 3",(sid,focus)).fetchone()
            after=str(lead[0]) if lead else "0"
    rows=db.execute("SELECT * FROM passages WHERE source=? AND id>? ORDER BY id LIMIT 61",(sid,after)).fetchall()
    parts=[]
    for p in rows[:60]:
        match=str(p["id"])==focus
        parts.append(f'<article id="p{p["id"]}" class="item{" focused" if match else ""}">'
                     f'<div class="tags">{chip(p["role"])}{chip(p["anchor"])}</div>{prose(p["text"])}</article>')
    more=f'<a class="secondary" href="{url("/source",id=sid,after=rows[59]["id"])}">下一页</a>' if len(rows)>60 else ""
    back=f'<a class="secondary" href="{url("/source",id=sid)}">回到开头</a>' if after!="0" else ""
    return row["title"],title+details+actions+'<section><h2>索引片段</h2>'+(''.join(parts) or empty("此来源暂无可检索片段。"))+f'<div class="actions">{back}{more}</div></section>'

def ideas_page(db, args):
    ideas=read_ideas(db)
    query=args.get("q","")
    matches=list(idea_matches(ideas,query,args.get("idea_type",""),args.get("topic",""),args.get("research_status",""),args.get("author_type","")))
    form=('<form class="searchform" action="/ideas" method="get"><div class="searchline">'
          f'<input aria-label="搜索思想" name="q" value="{esc(query)}" placeholder="搜索标题、正文和标签">'
          '<button type="submit">检索</button></div><div class="filters">'+list_filters(args,ideas)+'</div></form>')
    return form+f'<p class="muted result-count">{len(matches)} 条思想</p>'+("".join(map(idea_card,matches[:150])) or empty("没有匹配的思想。"))

def source_ref_html(db, ref):
    if isinstance(ref,dict):
        sid=ref.get("source_id") or ref.get("id") or ref.get("ref")
        target=ref.get("url") or ref.get("locator") or sid or json.dumps(ref,ensure_ascii=False)
        label=ref.get("title") or ref.get("anchor") or target
        passage=ref.get("passage_id","")
    else:
        sid=target=label=str(ref)
        passage=""
    if sid:
        row=db.execute("SELECT id,title FROM sources WHERE id=? OR locator=? LIMIT 1",(sid,sid)).fetchone()
        if row:
            return f'<a href="{url("/source",id=row["id"],p=passage)}">{esc(row["title"])}</a>'
    split=urlsplit(str(target))
    if split.scheme in ("https","zotero"):
        return f'<a rel="noreferrer" href="{esc(target)}">{esc(label)}</a>'
    return esc(label)

def idea_body(db, value):
    text=[]
    if value.get("summary"):
        text.append(f'<section><h2>摘要</h2>{prose(value["summary"])}</section>')
    text.append(f'<section><h2>正文</h2>{prose(value.get("full_content"))}</section>')
    refs=value.get("source_refs",[])
    excerpts=value.get("source_excerpts",[])
    if refs or excerpts:
        links="".join(f'<li>{source_ref_html(db,x)}</li>' for x in refs)
        quotes="".join(f'<blockquote>{rich(x if isinstance(x,str) else json.dumps(x,ensure_ascii=False))}</blockquote>' for x in excerpts)
        text.append(f'<section><h2>来源与原话</h2><ul>{links}</ul>{quotes}</section>')
    for k in LONG_FIELDS:
        if value.get(k):
            content=value[k]
            if k=="mathematical_formulas":
                lines=content.splitlines()
                markup="".join(f'<div class="formula">{rich(line) if MATH_PATTERN.search(line) else math_html(line)}</div>' for line in lines if line.strip())
            else:
                markup=prose(content)
            text.append(f'<section><h2>{FIELDS[k]}</h2>{markup}</section>')
    for k in ("related_ideas","contradictory_ideas"):
        if value.get(k):
            items=[]
            for ref in value[k]:
                if isinstance(ref,str):
                    try:
                        found=idea_get(db,ref)
                        rendered=f'<a href="{url("/idea",id=ref)}">{esc(found["title"])}</a>'
                    except KeyError:
                        rendered=esc(ref)
                else:
                    rendered=esc(json.dumps(ref,ensure_ascii=False))
                items.append(f"<li>{rendered}</li>")
            text.append(f'<section><h2>{FIELDS[k]}</h2><ul>{"".join(items)}</ul></section>')
    return "".join(text)

def edit_idea(value, error=""):
    iid=value.get("idea_id","")
    version=value.get("version","")
    notice=f'<p class="notice" role="alert">{esc(error)}</p>' if error else ""
    form=(f'<form class="editor" method="post" action="/idea/save">{form_guard()}{hidden("id",iid)}{hidden("version",version)}'
          +field("title",FIELDS["title"],value.get("title",""),required=True)
          +area("summary",FIELDS["summary"],value.get("summary",""),rows=3)
          +area("full_content",FIELDS["full_content"],value.get("full_content",""),rows=10,required=True)
          +'<div class="filters">')
    for k,choices in CHOICES.items():
        default="来源尚不明确" if k=="author_type" else choices[0]
        form+=choose(k,FIELDS[k],[(x,x) for x in choices],value.get(k,default))
    form+=field("first_proposed_at",FIELDS["first_proposed_at"],value.get("first_proposed_at",""),"date")
    form+=field("confidence",FIELDS["confidence"],value.get("confidence",""))+'</div>'
    for group, fields in (("主题与来源",LIST_FIELDS),("模型、证据与计划",LONG_FIELDS)):
        form+=f'<details class="filterbox" {"open" if group=="主题与来源" else ""}><summary>{group}</summary><div class="editor">'
        for k in fields:
            current=value.get(k,"")
            if isinstance(current,list):
                current="\n".join(x if isinstance(x,str) and "\n" not in x else json.dumps(x,ensure_ascii=False) for x in current)
            placeholder="每行一项，可用 JSON 字符串保留跨行引文" if k=="source_excerpts" else "每行一项" if k in LIST_FIELDS else ""
            form+=area(k,FIELDS[k],current,rows=3,placeholder=placeholder)
        form+='</div></details>'
    form+=field("reason","修改原因",value.get("reason","") or ("" if iid else "新建思想"),required=True)
    cancel=url("/idea",id=iid) if iid else "/ideas"
    return notice+form+f'<div class="actions"><button type="submit">保存版本</button><a href="{cancel}">取消</a></div></form>'

def idea_detail(db,args):
    iid=args.get("id","")
    current=idea_get(db,iid)
    value=current
    old=args.get("v","")
    if old and old.isdecimal() and int(old)!=current["version"]:
        row=db.execute("SELECT * FROM revisions WHERE idea=? AND version=?",(iid,int(old))).fetchone()
        if row is None:
            raise KeyError("找不到该版本")
        value={**json.loads(row["body"]),"version":row["version"]}
    title=value["title"]
    edit=f'<a class="secondary" href="{url("/idea/edit",id=iid)}">编辑思想</a>'
    state="".join(chip(value.get(k)) for k in CHOICES if value.get(k))
    topics=" ".join(f'<a class="chip" href="{url("/ideas",topic=x)}">{esc(x)}</a>' for x in value.get("research_topics",[]))
    keyword_chips=" ".join(chip(x) for x in value.get("keywords",[]))
    dates=f'<p class="muted">版本 {value["version"]} · 首次提出 {esc(value.get("first_proposed_at") or "日期待确认")}</p>'
    history=db.execute("SELECT version,at,reason FROM revisions WHERE idea=? ORDER BY version DESC",(iid,)).fetchall()
    versions="".join(f'<li><a href="{url("/idea",id=iid,v=r["version"])}">版本 {r["version"]}</a> '
                     f'<time>{esc(stamp(r["at"]))}</time> · {esc(r["reason"])}</li>' for r in history)
    content=f'<div class="tags">{state}{topics}{keyword_chips}</div>{dates}'
    if value["version"]!=current["version"]:
        content+=(f'<div class="notice">你正在查看历史版本。 <a href="{url("/idea",id=iid)}">查看当前版本</a>'
                  f'<form method="post" action="/idea/restore">{form_guard()}{hidden("id",iid)}'
                  f'{hidden("expected",current["version"])}{hidden("version",value["version"])}'
                  '<button class="quiet" type="submit">恢复为新的版本</button></form></div>')
    body=idea_body(db,value)
    relations=db.execute("SELECT * FROM links WHERE a=? OR b=? ORDER BY kind,a,b",(iid,iid)).fetchall()
    relation_html=[]
    for link in relations:
        other=link["b"] if iid==link["a"] else link["a"]
        other_title=idea_get(db,other)["title"]
        arrow="→" if iid==link["a"] else "←"
        relation_html.append(f'<li>{esc(link["kind"])} {arrow} <a href="{url("/idea",id=other)}">{esc(other_title)}</a> '
                             f'{chip(link["confidence"])}{prose(link["basis"])}</li>')
    linked='<ul>'+"".join(relation_html)+'</ul>' if relation_html else empty("还没有有据可查的关系。")
    candidates=[(i["idea_id"],i["title"]) for i in read_ideas(db) if i["idea_id"]!=iid]
    if candidates:
        linked+=(f'<details class="filterbox"><summary>记录关系</summary><form method="post" action="/idea/link" class="editor">'
                 f'{form_guard()}{hidden("a",iid)}'
                 +choose("b","相关思想",candidates)
                 +choose("kind","关系类型",[(x,x) for x in RELATIONS])
                 +choose("confidence","可信程度",((x,x) for x in ("待核对","有明确依据","仅为猜想")))
                 +area("basis","关系依据",required=True,rows=3)
                 +'<button type="submit">保存关联</button></form></details>')
    sidebar=f'<aside><section><h2>历史版本</h2><ul class="timeline-list">{versions}</ul></section><section><h2>思想关联</h2>{linked}</section></aside>'
    return title,content+f'<div class="detail-grid"><div>{body}</div>{sidebar}</div>',edit

def graph_page(db,args):
    ideas=read_ideas(db)
    lookup={x["idea_id"]:x for x in ideas}
    topics=sorted({t for x in ideas for t in x.get("research_topics",[])})
    topic=args.get("topic","")
    kind=args.get("kind","")
    allowed={x["idea_id"] for x in ideas if not topic or topic in x.get("research_topics",[])}
    links=[dict(r) for r in db.execute("SELECT * FROM links ORDER BY kind,a,b")
           if r["a"] in allowed and r["b"] in allowed and (not kind or r["kind"]==kind)]
    kinds=sorted({r["kind"] for r in links})
    form=('<form method="get" action="/graph" class="filters">'
          +choose("topic","主题",[(x,x) for x in topics],topic,"全部主题")
          +choose("kind","关系类型",[(x,x) for x in RELATIONS],kind,"全部关系")
          +'<button type="submit" class="quiet">筛选</button></form>')
    if not links:
        return form+empty("当前筛选范围内暂无已记录的关系。","/ideas","在思想详情页记录关系")
    connected=list(dict.fromkeys([r[key] for r in links for key in ("a","b")]))
    nodes=connected[:80]
    palette=("#548471","#af704f","#607597","#9c6a81","#6b7a48","#8d573b","#586d7d","#7f678e")
    color={k:palette[RELATIONS.index(k)] if k in RELATIONS else palette[0] for k in kinds}
    height=max(360, len(nodes)*12)
    width=max(620, len(nodes)*15)
    node_html=[]
    edge_html=[]
    link_list=[]
    for i,k in enumerate(nodes):
        angle=2*math.pi*i/len(nodes)
        radius=0.30 if len(nodes)<8 or i%2 else 0.43
        x=width*(0.5+radius*math.cos(angle))
        y=height*(0.5+radius*math.sin(angle))
        node_html.append(f'<a class="graph-node" data-key="{esc(k)}" data-x="{x:.2f}" data-y="{y:.2f}" '
                         f'href="{url("/idea",id=k)}" title="{esc(lookup[k]["title"])}">{esc(lookup[k]["title"])}</a>')
    for r in links:
        if r["a"] in nodes and r["b"] in nodes:
            edge_html.append(f'<i data-from="{esc(r["a"])}" data-to="{esc(r["b"])}" data-color="{color[r["kind"]]}"></i>')
        link_list.append(f'<li>{chip(r["kind"])} '
                         f'<a href="{url("/idea",id=r["a"])}">{esc(lookup[r["a"]]["title"])}</a> → '
                         f'<a href="{url("/idea",id=r["b"])}">{esc(lookup[r["b"]]["title"])}</a> '
                         f'{chip(r["confidence"])}{prose(r["basis"])}</li>')
    legend="".join(f'<span class="legend-key" data-color="{color[k]}">{esc(k)}</span>' for k in kinds)
    return (form+f'<div class="actions graph-tools"><label>缩放 <input id="graph-zoom" type="range" min="55" max="190" value="100"></label>{legend}</div>'
            f'<div class="graph-window"><div class="graph-stage" data-width="{width}" data-height="{height}">'
            f'<canvas aria-hidden="true"></canvas>{"".join(node_html)}<div class="graph-edges" hidden>{"".join(edge_html)}</div></div></div>'
            +('<p class="muted">关系图显示前 80 个节点。下方列出全部匹配依据。</p>' if len(connected)>80 else "")
            +f'<section><h2>关系依据</h2><ul class="timeline-list">{"".join(link_list)}</ul></section>')

def timeline_page(db,args):
    ideas=read_ideas(db)
    titles={x["idea_id"]:x["title"] for x in ideas}
    selected=args.get("id","")
    criteria=" WHERE idea=?" if selected else ""
    params=(selected,) if selected else ()
    rows=db.execute(f"SELECT idea,version,at,reason,body FROM revisions{criteria} ORDER BY at DESC,idea,version DESC LIMIT 200",params)
    form=('<form class="filters" action="/timeline">'
          +choose("id","思想",[(i["idea_id"],i["title"]) for i in ideas],selected,"全部思想")
          +'<button class="quiet" type="submit">筛选</button></form>')
    events=[]
    for row in rows:
        version=json.loads(row["body"])
        changes=version.get("research_status","")
        events.append(f'<li><time>{esc(stamp(row["at"]))}</time><h3><a href="{url("/idea",id=row["idea"],v=row["version"])}">'
                      f'{esc(version.get("title") or titles[row["idea"]])}</a></h3>{chip("版本 "+str(row["version"]))} {chip(changes)}'
                      f'<p>{esc(row["reason"])}</p></li>')
    return form+('<ol class="timeline-list">'+''.join(events)+'</ol>' if events else empty("暂无演化记录。"))

def topics_page(db):
    ideas=[x for x in read_ideas(db) if x.get("idea_type")=="论文选题" or x.get("research_status") in ("论文构思","已纳入论文")]
    if not ideas:
        return empty("已有选题会在这里显示。","/ideas","查看研究思想")
    return "".join(idea_card(x) for x in ideas)

def sources_page(db,args):
    feed=args.get("feed","")
    details=[]
    if feed:
        state=args.get("state","")
        page=max(1,min(10000,int(args.get("page","1")) if args.get("page","").isdecimal() else 1))
        cond=" WHERE feed=?"+(" AND state=?" if state else "")
        params=[feed]+([state] if state else [])
        rows=db.execute("SELECT id,title,kind,state,error,modified FROM sources"+cond+
                        " ORDER BY CASE state WHEN 'failed' THEN 0 WHEN 'missing' THEN 1 ELSE 2 END, modified DESC,id LIMIT 51 OFFSET ?",
                        [*params,(page-1)*50]).fetchall()
        table=[]
        for r in rows[:50]:
            table.append(f'<tr><th><a href="{url("/source",id=r["id"])}">{esc(r["title"])}</a></th><td>{esc(r["kind"])}</td>'
                         f'<td>{esc(r["state"])}</td><td>{esc(stamp(r["modified"]))}</td><td>{esc(r["error"])}</td></tr>')
        links="".join(f'<a class="chip" href="{url("/sources",feed=feed,state=value)}">{label}</a>' for value,label in
                      (("","全部"),("indexed","已索引"),("failed","失败"),("missing","源文件缺失")))
        pager=(f'<a href="{url("/sources",feed=feed,state=state,page=page-1)}">上一页</a>' if page>1 else "")
        if len(rows)>50:
            pager+=f'<a href="{url("/sources",feed=feed,state=state,page=page+1)}">下一页</a>'
        details.append(f'<section><h2>{esc(FEEDS.get(feed,feed))}</h2><div class="tags">{links}</div>')
        details.append('<div class="table-wrap"><table><thead><tr><th>原始资料</th><th>类型</th><th>状态</th><th>修改时间</th><th>错误</th></tr></thead>'
                       '<tbody>'+''.join(table)+'</tbody></table></div>' if table else empty("当前筛选范围内没有记录。"))
        details.append(f'<div class="actions">{pager}</div></section>')
    status=""
    with SYNC_LOCK:
        if ACTIVE_SYNC is not None and ACTIVE_SYNC.poll() is None:
            status='<p class="notice">同步正在后台执行。 <a href="/sources">刷新状态</a></p>'
        elif ACTIVE_SYNC is not None and ACTIVE_SYNC.returncode:
            status='<p class="notice">上次手动同步未正常结束。请查看下方数据源状态及同步日志。</p>'
    button=(f'<form method="post" action="/sync">{form_guard()}'
            +hidden("feed",feed if feed in SYNC_FEEDS else "")
            +f'<button type="submit">{"同步此数据源" if feed in SYNC_FEEDS else "手动同步"}</button></form>')
    return status+status_table(db)+"".join(details),button

def reports_page(args):
    directory=STATE/"reports"
    files=sorted(directory.glob("*.md"),key=lambda p:p.name,reverse=True) if directory.is_dir() else []
    files=[p for p in files if p.is_file() and not p.is_symlink()]
    if not files:
        return empty("暂无研究报告。")
    name=args.get("report","")
    current=next((p for p in files if p.name==name),files[0] if not name else None)
    if current is None:
        raise KeyError("找不到该报告")
    navigation="".join(f'<li><a href="{url("/reports",report=p.name)}">{esc(p.stem)}</a></li>' for p in files[:100])
    body=current.read_text(encoding="utf-8")[:250000]
    sections=[]
    for block in re.split(r"\n\s*\n",body):
        lines=block.splitlines()
        if len(lines)==1 and re.match(r"^#{1,6} ",block):
            sections.append(f'<h3>{rich(block.split(" ",1)[1])}</h3>')
        elif lines and all(line.startswith(("- ","* ")) for line in lines):
            entries="".join(f'<li>{rich(line[2:])}</li>' for line in lines)
            sections.append(f'<ul>{entries}</ul>')
        elif block:
            sections.append(prose(block))
    return (f'<div class="detail-grid"><section><h2>{esc(current.stem)}</h2>{"".join(sections)}</section>'
            f'<aside><h2>报告列表</h2><ul>{navigation}</ul></aside></div>')

def prefill(db,args):
    sid=args.get("source","")
    if not sid:
        return {}
    r=db.execute("SELECT * FROM sources WHERE id=?",(sid,)).fetchone()
    if r is None:
        raise KeyError("找不到该来源")
    p=args.get("p","")
    if p:
        quote=db.execute("SELECT text FROM passages WHERE source=? AND id=?",(sid,p)).fetchone()
    else:
        quote=None
    return {"source_refs":[sid],"source_excerpts":[quote["text"]] if quote else []}

def sync_start(feed):
    global ACTIVE_SYNC
    if feed and feed not in SYNC_FEEDS:
        raise ValueError("未知的数据源")
    with SYNC_LOCK:
        if ACTIVE_SYNC is not None and ACTIVE_SYNC.poll() is None:
            return
        log_dir=STATE/"logs"
        log_dir.mkdir(parents=True,exist_ok=True)
        with (log_dir/"web-sync.log").open("ab") as handle:
            cmd=[sys.executable,"-B",str(APP/"maintenance.py"),"sync"]
            if feed:
                cmd.append(feed)
            ACTIVE_SYNC=subprocess.Popen(cmd,stdout=handle,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,cwd=APP,start_new_session=True)

def open_original(db, sid, pid=None):
    row=db.execute("SELECT locator,state FROM sources WHERE id=?",(sid,)).fetchone()
    if row is None or not row["locator"]:
        raise KeyError("找不到原件")
    split=urlsplit(row["locator"])
    if split.scheme=="file" and split.netloc in ("","localhost"):
        path=Path(unquote(split.path)).resolve()
        home=Path.home().resolve()
        if not path.is_absolute() or home not in path.parents or not path.is_file():
            raise ValueError("本地原件已移动，或不在个人目录中。")
        allowed={".md",".txt",".rtf",".pdf",".doc",".docx",".odt",".ppt",".pptx",".xls",".xlsx",".csv",".tex",".py",".r",".do",".ipynb",".json",".jsonl",".html",".epub",".tsv"}
        if path.suffix.lower() not in allowed:
            raise ValueError("此文件类型请从记录的路径手动打开。")
        command=["/usr/bin/open",str(path)]
        if path.suffix.lower() in {".py",".r",".do",".json",".jsonl",".ipynb",".tex"}:
            command=["/usr/bin/open","-a","TextEdit",str(path)]
    elif split.scheme=="zotero" or (split.scheme=="https" and split.hostname):
        anchor=""
        if pid is not None and str(pid).isdigit() and split.path.startswith("/docx/") and (split.hostname or "").endswith(".feishu.cn"):
            passage=db.execute("SELECT anchor FROM passages WHERE source=? AND id=?",(sid,int(pid))).fetchone()
            anchor=passage[0] if passage else ""
        command=["/usr/bin/open",external_original(split,anchor) or row["locator"]]
    else:
        raise ValueError("此来源只有文本定位，不能直接打开。")
    subprocess.run(command,check=True,timeout=10,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

class Handler(BaseHTTPRequestHandler):
    def valid_host(self):
        return self.headers.get("Host","").lower() in {f"{HOST}:{PORT}",f"localhost:{PORT}"}

    def send_body(self, status, body, content_type="text/html; charset=utf-8"):
        data=body.encode() if isinstance(body,str) else body
        self.send_response(status)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Cross-Origin-Resource-Policy","same-origin")
        self.send_header("Referrer-Policy","same-origin")
        self.send_header("Content-Security-Policy","default-src 'none'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def page(self, path, title, content, action="", status=200):
        self.send_body(status,layout(path,title,content,action))

    def redirect(self, path):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location",path.replace("&amp;","&"))
        self.send_header("Cache-Control","no-store")
        self.end_headers()

    def fail(self, status, message):
        self.page("","操作未完成",empty(message,"/","返回概览"),status=status)

    def do_GET(self):
        if not self.valid_host():
            self.send_error(400)
            return
        split=urlsplit(self.path)
        route=split.path
        try:
            args={k:v[-1] for k,v in parse_qs(split.query,max_num_fields=30).items()}
            if route in ("/web.css","/web.js"):
                assets={"/web.css":"text/css; charset=utf-8","/web.js":"text/javascript; charset=utf-8"}
                self.send_body(200,(APP/route[1:]).read_bytes(),assets[route])
                return
            if route=="/favicon.ico":
                self.send_body(204,b"")
                return
            with connect() as db:
                if route=="/health":
                    db.execute("SELECT 1").fetchone()
                    self.send_body(200,'{"status":"ok"}',"application/json")
                elif route=="/":
                    action='<a class="primary" href="/idea/new">记录思想</a>'
                    self.page(route,"研究知识库",dashboard(db),action)
                elif route=="/search":
                    self.page(route,"检索",render_search(db,args))
                elif route=="/ask":
                    self.page(route,"本地资料问答",answer_page(db,args))
                elif route=="/ideas":
                    self.page(route,"研究思想",ideas_page(db,args),'<a class="primary" href="/idea/new">记录思想</a>')
                elif route=="/idea":
                    self.page("/ideas",*idea_detail(db,args))
                elif route=="/idea/new":
                    self.page("/ideas","记录思想",edit_idea(prefill(db,args)))
                elif route=="/idea/edit":
                    self.page("/ideas","编辑思想",edit_idea(idea_get(db,args.get("id",""))))
                elif route=="/source":
                    self.page("/sources",*source_page(db,args))
                elif route=="/sources":
                    self.page(route,"数据源",*sources_page(db,args))
                elif route=="/graph":
                    self.page(route,"关系图",graph_page(db,args))
                elif route=="/timeline":
                    self.page(route,"演化记录",timeline_page(db,args))
                elif route=="/topics":
                    self.page(route,"论文选题",topics_page(db))
                elif route=="/reports":
                    self.page(route,"研究报告",reports_page(args))
                else:
                    self.fail(404,"找不到页面。")
        except KeyError as error:
            self.fail(404,str(error).strip("'"))
        except (ValueError,sqlite3.Error) as error:
            LOG.error("GET %s: %s",route,type(error).__name__)
            self.fail(400,"请求无效，或索引暂时无法读取。")
        except (OSError,RuntimeError) as error:
            LOG.error("GET %s: %s",route,type(error).__name__)
            self.fail(503,"本地服务暂时无法完成请求。")

    def do_POST(self):
        if not self.valid_host() or self.headers.get("Sec-Fetch-Site") not in (None,"same-origin","none"):
            self.send_error(403)
            return
        origin=self.headers.get("Origin")
        if origin and origin!=f"http://{self.headers.get('Host')}":
            self.send_error(403)
            return
        if self.headers.get_content_type()!="application/x-www-form-urlencoded":
            self.send_error(415)
            return
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=1_000_000:
                self.send_error(413)
                return
            args={k:v[-1] for k,v in parse_qs(self.rfile.read(size).decode(),keep_blank_values=True,max_num_fields=70).items()}
            if not hmac.compare_digest(args.get("csrf","").encode(),TOKEN.encode()):
                self.fail(403,"页面的操作凭证已失效，请返回并刷新原页面后重试。")
                return
            route=urlsplit(self.path).path
            with connect() as db:
                if route=="/idea/save":
                    value={k:args[k] for k in FIELDS if k in args}
                    iid=args.get("id") or None
                    expected=int(args["version"]) if iid else None
                    try:
                        for k in LIST_FIELDS:
                            if k not in value:
                                continue
                            refs=[]
                            for line in value[k].splitlines():
                                item=line.strip()
                                if k in {"source_refs","source_excerpts","related_ideas","contradictory_ideas"} and item.startswith(("{",'"')):
                                    try:
                                        parsed=json.loads(item)
                                        if isinstance(parsed,(dict,str)):
                                            item=parsed
                                    except ValueError:
                                        pass
                                if item:
                                    refs.append(item)
                            value[k]=refs
                        iid=idea_save(db,value,args.get("reason",""),iid,expected)
                    except ValueError as error:
                        self.page("/ideas","编辑思想",edit_idea({**value,"idea_id":iid or "","version":expected or "",
                                                              "reason":args.get("reason","")},str(error)),status=409)
                        return
                    db.commit()
                    self.redirect(url("/idea",id=iid))
                elif route=="/idea/restore":
                    iid=args["id"]
                    version=int(args["version"])
                    row=db.execute("SELECT body FROM revisions WHERE idea=? AND version=?",(iid,version)).fetchone()
                    if row is None:
                        raise KeyError("找不到该版本")
                    idea_save(db,json.loads(row["body"]),f"恢复自版本 {version}",iid,int(args["expected"]))
                    db.commit()
                    self.redirect(url("/idea",id=iid))
                elif route=="/idea/link":
                    a,b,kind=args["a"],args["b"],args["kind"]
                    basis=args.get("basis","").strip()
                    confidence=args["confidence"]
                    if a==b or kind not in RELATIONS or not basis or confidence not in {"待核对","有明确依据","仅为猜想"}:
                        raise ValueError("关联需要两个思想和可核查的依据。")
                    idea_get(db,a)
                    idea_get(db,b)
                    db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?,?,?)",(a,b,kind,basis,confidence))
                    db.commit()
                    self.redirect(url("/idea",id=a))
                elif route=="/open":
                    open_original(db,args["id"],args.get("p"))
                    self.send_body(204,b"")
                elif route=="/sync":
                    feed=args.get("feed","")
                    sync_start(feed)
                    self.redirect(url("/sources",feed=feed) if feed else "/sources")
                else:
                    self.fail(404,"找不到页面。")
        except KeyError as error:
            self.fail(404,str(error).strip("'"))
        except (ValueError,UnicodeError) as error:
            self.fail(400,str(error))
        except (OSError,sqlite3.Error,subprocess.SubprocessError) as error:
            LOG.error("POST: %s",type(error).__name__)
            self.fail(503,"本地服务暂时无法完成请求。")

    def log_message(self, _format, *args):
        # Query text and document paths may include unpublished research.
        return

def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    with ThreadingHTTPServer((HOST,PORT),Handler) as server:
        LOG.info("Listening on http://%s:%s",HOST,PORT)
        server.serve_forever()

if __name__=="__main__":
    main()
