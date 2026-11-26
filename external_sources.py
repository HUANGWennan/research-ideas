"""Local Feishu exports and academic AI history. No browser credentials are read."""
import json
import re
import shutil
import subprocess
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from db import STATE, connect, digest, dump, run_record, store_source

HOME = Path.home()
ACADEMIC = re.compile(
    r"地租|置盐|基本品|利润率|利润-工资|生产价格|劳动价值|剩余价值|资本主义|原始积累|"
    r"生产方式|政治经济学|资本有机构成|二重生产|联合生产|投入产出|数字人民币|谱半径|"
    r"比较静态|微分方程|拉格朗日|二阶条件|拟凸|准凸|特征值|边际替代率|因果识别|"
    r"国家理论|国家统一模型|社会分配|阶级斗争|Solow|Sraffa|Nikaido|Wallerstein|Moraitis|Shaikh|"
    r"Marx(?:ian|ist)?\b|economic history|rate of profit|labou?r value|differential rent|"
    r"absolute rent|spectral radius|input.output model|HonestDiD|Okishio|Hessian|"
    r"means of production|capital accumulation|political economy",
    re.I,
)
PRIVATE = re.compile(
    r"(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[\w-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"AKIA[A-Z0-9]{16}|\b\d{3}-\d{2}-\d{4}\b|\b(?:bearer|password|transfer pin|recovery code|"
    r"银行卡号|社会安全号码|信用卡号|恢复代码|手机号|身份证号|我的税表|账号密码)\s*[:：=]|"
    r"工资单|租房申请|身体不适|我的护照|女朋友|我女友|我的银行|我的 SSN|"
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|[\w-]{20,}\.[\w-]{20,}\.[\w-]{20,})"
)
NON_CHAT = re.compile(
    r"^\s*(?:#\s*AGENTS\b|<|## Referenced ChatGPT conversation|\[Current Zotero HTML snapshot\]|"
    r"Developer message|Message Type:|background job |Team message from |# Files mentioned by the user:|"
    r"# [^\n]{0,100}任务交接|# 个人科研知识与研究思想管理系统)", re.I
)
SNAPSHOTS = []

def iso(value):
    if isinstance(value, (int,float)):
        try:
            seconds = value / 1000 if value > 99_999_999_999 else value
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    if isinstance(value,str) and re.match(r"^\d{4}-\d{2}-\d{2}",value):
        return value
    return ""

def file_date(path):
    return iso(path.stat().st_mtime)

def file_signature(path):
    st=path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"

def json_file(path):
    if path.is_symlink() or path.stat().st_size > 180_000_000:
        raise ValueError("导出文件不是本地普通文件，或超过 180 MB")
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)

def feishu_url(value):
    target=urlsplit(value)
    allowed=(target.hostname or "").endswith((".feishu.cn",".larksuite.com"))
    if target.scheme!="https" or not allowed or not re.fullmatch(r"/(?:mindnotes|docx|docs|wiki|sheets|file)/[A-Za-z0-9]+/?", target.path):
        raise ValueError("飞书快照缺少有效原件链接")
    return f"{target.scheme}://{target.hostname}{target.path.rstrip('/')}"

def attributed_text(fragment):
    props=fragment.get("apool", {}).get("numToAttrib",{})
    initial=fragment.get("initialAttributedTexts",{})
    out, formulas=[], []
    for key,text in initial.get("text",{}).items():
        cursor, attrs, pieces=0, {}, []
        for match in re.finditer(r"\*([a-z0-9]+)|\+([a-z0-9]+)",initial.get("attribs",{}).get(key,"")):
            attribute,length=match.groups()
            if attribute:
                pair=props.get(str(int(attribute,36)))
                if isinstance(pair,list) and len(pair)==2:
                    attrs[pair[0]]=pair[1]
                continue
            n=int(length,36)
            formula=attrs.get("equation")
            if formula:
                formulas.append(formula)
                pieces.append("\\("+formula+"\\)")
            else:
                pieces.append(text[cursor:cursor+n])
            cursor+=n
            attrs={}
        if cursor==len(text) and pieces:
            out.append("".join(pieces))
        else:
            out.append(text)
    return "\n".join(out),formulas

def mind_passages(nodes):
    labels={str(n.get("id")):str(n.get("text","")) for n in nodes if isinstance(n,dict)}
    passages=[]
    for i,entry in enumerate(nodes,1):
        if not isinstance(entry,dict):
            continue
        text=str(entry.get("text","")).strip()
        if not text:
            continue
        parent=labels.get(str(entry.get("parent")),"")
        locator=f"导图节点 {entry.get('id') or i}"
        if parent:
            locator+=" · "+parent[:75]
        passages.append((locator,"飞书原文 · 归属待确认",text,[]))
    return passages

def doc_passages(record):
    records=record["recordMap"]
    block_guid=dict(zip(record.get("recordIds",[]),record.get("blockIds",[])))
    passages=[]
    for rid in record.get("recordIds",[]):
        snap=records.get(rid,{}).get("snapshot",{})
        fragment=snap.get("text")
        if not isinstance(fragment,dict):
            continue
        text, formulas=attributed_text(fragment)
        if text.strip():
            role="飞书原文 · 归属待确认"
            passages.append((f"文档块 {block_guid.get(rid,rid)}",role,text,formulas))
    return passages

def sync_feishu():
    captures=STATE/"imports/feishu"
    supplied=list(sorted(captures.glob("*.json"))) if captures.is_dir() else []
    failures=0
    sources_to_read=[*SNAPSHOTS,*[(path,None,None) for path in supplied]]
    with connect() as db:
        for path,title,url in sources_to_read:
            try:
                payload=json_file(path)
                if title is None:
                    if not isinstance(payload,dict):
                        raise ValueError("快照必须提供 url、title 和 blocks 或 nodes")
                    title=str(payload.get("title","")).strip()
                    url=payload.get("url","")
                    if not title:
                        raise ValueError("快照缺少标题")
                url=feishu_url(url)
                if isinstance(payload,list):
                    pieces=mind_passages(payload)
                elif isinstance(payload.get("recordMap"),dict):
                    pieces=doc_passages(payload)
                elif isinstance(payload.get("nodes"),list):
                    pieces=mind_passages(payload["nodes"])
                elif isinstance(payload.get("blocks"),list):
                    pieces=mind_passages(payload["blocks"])
                else:
                    raise ValueError("快照没有可用文本节点")
                if not pieces:
                    raise ValueError("快照无可读段落")
                doc_id=url.rsplit("/",1)[-1]
                modified=iso(payload.get("modified_at")) if isinstance(payload,dict) else ""
                stamp=modified or file_date(path)
                sid="feishu:"+doc_id
                previous=db.execute("SELECT modified,meta FROM sources WHERE id=?",(sid,)).fetchone()
                if previous and (previous[0]>stamp or json.loads(previous[1]).get("sync_mode")=="live-cli"):
                    continue
                store_source(db,sid,"feishu",urlsplit(url).path.split("/")[1],title,url,stamp,
                             digest(dump([title,url,stamp,pieces])),pieces,
                             meta={"snapshot":str(path),"snapshot_blocks":len(pieces),
                                   "source_modified_known":bool(modified),"sync_mode":"verified-local-snapshot" if path not in supplied else "local-export"})
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                failures+=1
        count=db.execute("SELECT count(*) FROM sources WHERE feed='feishu' AND state='indexed' AND json_extract(meta,'$.sync_mode')<>'live-cli'").fetchone()[0]
        detail=f"{count} 份本地快照仍作为当前记录。已由实时同步更新的文档见飞书实时同步。"
        if count==0 and sources_to_read:
            detail="已知本机快照已有在线记录，原文件留存。"
        if failures:
            detail+=f" {failures} 份预期快照缺失或格式有误。"
        run_record(db,"feishu","部分" if count else "已被实时同步覆盖" if sources_to_read and not failures else "待接入",detail,len(sources_to_read))
        db.execute("UPDATE runs SET failed=?,indexed=? WHERE feed='feishu'",(failures,count))

def clean_turn(value):
    if not isinstance(value,str):
        return ""
    text=value.strip()
    if not text or NON_CHAT.match(text) or PRIVATE.search(text):
        return ""
    if len(text)>28_000 or "# AGENTS.md instructions" in text or "<environment_context>" in text:
        return ""
    return text

def academic_turn(text):
    return bool(text and len(text)>=15 and ACADEMIC.search(text)
                and not re.search(r"余额下次|额度查询|帮我把这些文献的PDF附件都加上|飞书讲义项目的数据队友",text))

def select_turns(messages):
    pieces=[]
    user_matches=False
    seen=set()
    for role,ref,body in messages:
        body=clean_turn(body)
        if role=="user":
            user_matches=academic_turn(body)
            if not user_matches:
                continue
            label="用户原话"
        elif role=="assistant" and user_matches and academic_turn(body):
            label="AI 回复"
        else:
            continue
        key=(role,digest(body))
        if key not in seen:
            seen.add(key)
            pieces.append((ref,label,body,[]))
    return pieces

def work_history(path):
    messages=[]
    sid=""
    last=""
    with path.open(encoding="utf-8") as f:
        for number,line in enumerate(f,1):
            try:
                event=json.loads(line)
            except ValueError:
                continue
            data=event.get("payload",{})
            if event.get("type")=="session_meta":
                sid=data.get("id","")
                source=data.get("source")
                if isinstance(source,dict) and "subagent" in source:
                    return "","",[]
            if event.get("type")!="response_item" or data.get("type")!="message":
                continue
            role=data.get("role")
            if role not in ("user","assistant") or data.get("phase")=="commentary":
                continue
            allowed={"input_text"} if role=="user" else {"output_text"}
            text="\n".join(p["text"] for p in data.get("content",[])
                if isinstance(p,dict) and p.get("type") in allowed and isinstance(p.get("text"),str))
            messages.append((role,f"JSONL 行 {number}",text))
            last=iso(event.get("timestamp")) or last
    return sid,last,messages

# Harness appends multiple independent frames. Node's sync operation exposes consumed input.
NODE_HARNESS = r"""
const fs=require('node:fs');
const z=require('node:zlib');
const raw=fs.readFileSync(process.argv[1]);
if(raw.length>96*1024*1024) throw Error('Oversized transcript');
let offset=0, total=0;
while(offset<raw.length) {
  const d=z.zstdDecompressSync(raw.subarray(offset),{info:true,maxOutputLength:24*1024*1024});
  const length=d.engine.bytesWritten;
  if(length<1) throw Error('Invalid compressed frame');
  offset+=length;
  for(const line of d.buffer.toString('utf8').split('\n')) {
    if(!line.trim()) continue;
    const e=JSON.parse(line);
    if(e.type==='session') {
      if (e.delegationDepth>0) process.exit(0);
      console.log(JSON.stringify({id:e.id}));
      continue;
    }
    const role=e.type==='user/message'?'user':e.type==='assistant/message'?'assistant':'';
    if(!role) continue;
    const m=role==='user'?e.data:e.data?.message;
    if(m?.role!==role || (role==='user' && m.source?.kind!=='user')) continue;
    const text=(Array.isArray(m.content)?m.content:[])
      .filter(p=>p.type==='text'&&typeof p.text==='string').map(p=>p.text).join('\n');
    total+=text.length;
    if(total>24*1024*1024) throw Error('Oversized text output');
    console.log(JSON.stringify({role,seq:e.seq,time:e.time,text}));
  }
}
"""

def harness_history(path):
    node=shutil.which("node") or "/usr/local/bin/node"
    result=subprocess.run([node,"-e",NODE_HARNESS,str(path)],capture_output=True,text=True,check=True,timeout=40)
    messages=[]
    sid=""
    last=""
    for line in result.stdout.splitlines():
        row=json.loads(line)
        if "id" in row:
            sid=row["id"]
            continue
        messages.append((row["role"],f"Harness 事件 {row.get('seq')}",row["text"]))
        last=iso(row.get("time")) or last
    return sid,last,messages

def chat_export(path):
    if path.suffix.lower()==".zip":
        if path.is_symlink(): raise ValueError("不读取符号链接")
        with zipfile.ZipFile(path) as archive:
            members=[item for item in archive.infolist() if re.fullmatch(r"conversations(?:-\d+)?\.json",Path(item.filename).name)]
            if not members: raise ValueError("ZIP 不含 conversations.json")
            for member in members:
                if member.file_size>600_000_000: raise ValueError("单份对话归档超过可直接导入大小")
                with archive.open(member) as f:
                    yield from chat_conversations(json.load(f))
    else:
        yield from chat_conversations(json_file(path))

def chat_conversations(raw):
    if not isinstance(raw,list):
        raise ValueError("不是 ChatGPT 官方 conversations.json")
    for convo in raw:
        if not isinstance(convo,dict):
            continue
        mapping=convo.get("mapping",{})
        cursor=convo.get("current_node")
        if not isinstance(mapping,dict) or cursor not in mapping:
            continue
        sid=convo.get("conversation_id") or convo.get("id","")
        if not isinstance(sid,str) or not re.fullmatch(r"[A-Za-z0-9-]{12,80}",sid):
            continue
        seen=set()
        turns=[]
        while cursor in mapping and cursor not in seen:
            seen.add(cursor)
            node=mapping[cursor]
            m=node.get("message") or {}
            cursor=node.get("parent")
            if (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
                continue
            author=m.get("author") or {}
            role=author.get("role")
            content=m.get("content") or {}
            if role not in ("user","assistant") or author.get("name") or m.get("recipient") not in (None,"all"):
                continue
            body="\n".join(x for x in content.get("parts",[]) if isinstance(x,str))
            turns.append((role,f"消息 {m.get('id','')}",body))
        yield sid,iso(convo.get("update_time")),str(convo.get("title","")),list(reversed(turns))

def save_dialogue(db,platform,path,source_id,last,messages,title="",signature=None):
    if not source_id:
        return False
    sid="ai:"+platform+":"+source_id
    pieces=select_turns(messages)
    if not pieces:
        return False
    # Export titles sometimes quote unrelated topics; fall back to the first qualifying message.
    topic=clean_turn(title) if academic_turn(clean_turn(title)) else re.sub(r"\s+"," ",pieces[0][2])[:68]
    stamp=last or file_date(path)
    locator=("https://chatgpt.com/c/"+source_id) if platform=="ChatGPT Chat" else path.as_uri()
    meta={"platform":platform,"local_history_path":str(path),"session_id":source_id,"file_signature":signature}
    store_source(db,sid,"ai","AI 对话",f"{platform} · {topic}",locator,stamp,
                 digest(dump([platform,source_id,stamp,pieces])),pieces,meta=meta)
    return True

def sync_ai():
    work_root=STATE/"imports"/"codex"
    harness_root=STATE/"imports"/"dsh"
    export_root=STATE/"imports/chatgpt"
    roots=[("ChatGPT Work",work_root,"*.jsonl",work_history),
           ("DeepSeek Harness",harness_root,"session.v4.jsonl.zstd",harness_history)]
    discovered, failures=0, 0
    with connect() as db:
        existing={}
        for row in db.execute("SELECT meta FROM sources WHERE feed='ai' AND state='indexed'"):
            m=json.loads(row[0])
            if m.get("file_signature"):
                existing[m.get("local_history_path")]=m["file_signature"]
        for platform,root,glob,reader in roots:
            for path in sorted(root.rglob(glob)):
                if path.is_symlink() or "agents" in path.relative_to(root).parts:
                    continue
                # Harness mirrors some Work sessions. Prefer the Work original.
                if platform=="DeepSeek Harness" and path.parent.name.startswith("import-"):
                    continue
                discovered+=1
                try:
                    signature="academic-v2:"+file_signature(path)
                    if existing.get(str(path))==signature:
                        continue
                    if path.stat().st_size>110_000_000:
                        raise ValueError("会话文件过大")
                    sid,last,messages=reader(path)
                    if not save_dialogue(db,platform,path,sid,last,messages,signature=signature):
                        db.execute("""UPDATE sources SET state='missing',error='按照现有作者和学术范围规则无可入库回合'
                                      WHERE feed='ai' AND json_extract(meta,'$.local_history_path')=?""",(str(path),))
                except (OSError,ValueError,KeyError,TypeError,AttributeError,subprocess.SubprocessError):
                    failures+=1
        exports=[] if not export_root.is_dir() else sorted([*export_root.glob("conversations*.json"),*export_root.glob("*.zip")])
        for path in exports:
            try:
                for sid,last,title,messages in chat_export(path):
                    discovered+=1
                    save_dialogue(db,"ChatGPT Chat",path,sid,last,messages,title)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                failures+=1
        platforms=Counter()
        for row in db.execute("SELECT meta FROM sources WHERE feed='ai' AND state='indexed'"):
            platform=json.loads(row[0]).get("platform")
            if platform:
                platforms[platform]+=1
        detail="仅保留本机历史中的学术回合。已入库："+("、".join(f"{name} {n}" for name,n in platforms.items()) or "0")
        detail+="。ChatGPT 云端全历史与 DeepSeek 网页历史没有可用连续读取接口。"
        if not exports:
            detail+=" 尚未发现 ChatGPT 官方导出的 conversations.json。"
        if failures:
            detail+=f" {failures} 个历史文件读取失败。"
        run_record(db,"ai","部分",detail,discovered)
        db.execute("UPDATE runs SET failed=? WHERE feed='ai'",(failures,))
