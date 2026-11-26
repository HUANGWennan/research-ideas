"""Capture one significant Chat turn, with a verbatim user excerpt and separate summary."""
import json
import re
import sys
import unicodedata
from urllib.parse import urlsplit

from db import connect, digest, dump, idea_save, now, store_source
from external_sources import PRIVATE, clean_turn

FIELDS={
    "title":{"type":"string","maxLength":160},
    "quote":{"type":"string","maxLength":6000,
             "description":"Verbatim excerpt of the user's message. Never use assistant text."},
    "summary":{"type":"string","maxLength":1200,"description":"Assistant-written synopsis, separately labeled."},
    "url":{"type":"string","description":"Observed original ChatGPT conversation URL, if available."},
    "idea_type":{"type":"string","enum":["理论观点","理论批判","数学模型","数学证明","研究假说","实证构思","方法论","未解决问题","论文选题"]},
}

def capture(db, args):
    def string(name, maximum, required=False):
        value=args.get(name,"")
        if not isinstance(value,str) or len(value)>maximum or (required and not value.strip()):
            raise ValueError(f"invalid {name}")
        value=value.strip()
        if PRIVATE.search(value):
            raise ValueError("内容可能含私人信息，未写入")
        return value

    title=string("title",160,True)
    quote=string("quote",6000,True)
    summary=string("summary",1200)
    source_url=string("url",300)
    if not clean_turn(quote) or len(quote)<8:
        raise ValueError("请提供可辨认的学术原话")
    if source_url:
        url=urlsplit(source_url)
        if (url.scheme,url.netloc)!=("https","chatgpt.com") or not re.fullmatch(r"(?:/g/[A-Za-z0-9_-]+)?/c/[A-Za-z0-9-]{12,80}/?",url.path):
            raise ValueError("只接收实际看到的 ChatGPT 对话链接")
        source_url="https://chatgpt.com"+url.path.rstrip("/")
    kind=args.get("idea_type","理论观点")
    if kind not in FIELDS["idea_type"]["enum"]:
        raise ValueError("无效的思想类型")
    key=digest(re.sub(r"\s+"," ",unicodedata.normalize("NFC",quote)))
    sid="ai:chat-capture:"+key
    db.execute("BEGIN IMMEDIATE")
    existing=db.execute("SELECT locator FROM sources WHERE id=?",(sid,)).fetchone()
    if not existing or (source_url and not existing["locator"]):
        stamp=now()
        store_source(db,sid,"ai","AI 对话","ChatGPT Chat · "+title,source_url,stamp,
                     digest(dump([title,quote,source_url])),[("ChatGPT 消息摘录","用户原话",quote,[])],
                     meta={"platform":"ChatGPT Chat","capture_mode":"live-user-quote",
                           "captured_at":stamp,"reference_type":"原会话" if source_url else "当轮摘录"})
    ref={"source_id":sid,"label":"当轮用户原话","url":source_url}
    iid=idea_save(db,{"title":title,"summary":"AI 摘要："+summary if summary else "",
                      "full_content":quote,"idea_type":kind,
                      "author_type":"用户明确提出","research_status":"待研究",
                      "confidence":"当轮原话摘录；分类及归属可复核",
                      "source_refs":[ref],"source_excerpts":[quote]},
                  "ChatGPT 当轮学术思想归档",dedup="chat-capture:"+key)
    return {"idea_id":iid,"source_id":sid,"status":"已入库","local_url":"http://127.0.0.1:8767/idea?id="+iid}

def main():
    try:
        raw=sys.stdin.read(40_001)
        if len(raw)>40_000: raise ValueError("内容超过单次归档上限")
        args=json.loads(raw)
        if not isinstance(args,dict): raise ValueError("需要 JSON 对象")
        with connect() as db:
            saved=capture(db,args)
        print(dump(saved))
    except (TypeError,ValueError) as exc:
        print(str(exc),file=sys.stderr)
        return 2
    return 0
if __name__=="__main__": raise SystemExit(main())
