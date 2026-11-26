"""Line-delimited JSON-RPC MCP transport, with separate read and write processes."""
import argparse
import json
import re
import sys

from capture_chat import FIELDS as CHAT_CAPTURE_FIELDS, capture
from db import RESULT, RELATIONS, connect, digest, dump, idea_get, idea_save, search

LIST_ID={"idea_id": {"type":"string","description":"Stable UUID returned from the idea index"}}
QUERY={"query":{"type":"string"},"mode":{"type":"string","enum":["hybrid","keyword","semantic","formula"]},
       "feed":{"type":"string"},"kind":{"type":"string"},"author":{"type":"string"},
       "tag":{"type":"string"},"start":{"type":"string"},"end":{"type":"string"},"limit":{"type":"integer","maximum":100}}
READ={
 "search_knowledge":("Search local bilingual passages, with exact source locators.",QUERY,["query"]),
 "search_formulas":("Find normalized formula fragments in source passages.",QUERY,["query"]),
 "search_annotations":("Find Zotero annotation text or comments with page and attachment IDs.",QUERY,["query"]),
 "search_ideas":("Find research ideas, keeping their authorship and verification labels.",{"query":{"type":"string"}},[]),
 "search_models":("Find research models and proofs.",{"query":{"type":"string"}},[]),
 "search_questions":("Find open research questions.",{"query":{"type":"string"}},[]),
 "list_open_questions":("List open research questions.",{},[]),
 "read_source":("Read one page of passages from an original source record.",
                {"source_id":{"type":"string"},"offset":{"type":"integer","minimum":0}},["source_id"]),
 "get_idea":("Read an idea by stable ID, including source references.",LIST_ID,["idea_id"]),
 "get_idea_history":("Read the original and each saved revision.",LIST_ID,["idea_id"]),
 "find_related_ideas":("Read recorded relationships and their basis.",LIST_ID,["idea_id"]),
 "find_conflicting_ideas":("Read recorded conflicts and their basis.",LIST_ID,["idea_id"]),
 "get_research_summary":("Summarize real corpus size and original idea status counts.",{},[]),
 "get_sync_status":("Show actual last run and coverage per source.",{},[]),
}
WRITE={
 "capture_chat":("Save one significant ChatGPT user idea with exact quote, optionally a verified conversation URL.",
                 CHAT_CAPTURE_FIELDS,["title","quote"]),
 "create_idea":("Create one idea. Provide the exact original source and quoted evidence for user attribution.",
                {"body":{"type":"object"},"reason":{"type":"string"},"dedup":{"type":"string"}},["body","reason"]),
 "update_idea":("Update one idea, appending a revision. Requires current version.",
                {**LIST_ID,"body":{"type":"object"},"reason":{"type":"string"},"version":{"type":"integer"}},["idea_id","body","reason","version"]),
 "link_ideas":("Record one relationship with an evidence basis. Use 待验证 for suggestions.",
               {"a":{"type":"string"},"b":{"type":"string"},"kind":{"type":"string","enum":list(RELATIONS)},
                "basis":{"type":"string"},"confidence":{"type":"string"}},["a","b","kind","basis"]),
}

def call(name, args):
    with connect() as db:
        if name in ("search_knowledge","search_formulas","search_annotations"):
            opts={k:v for k,v in args.items() if k in QUERY}
            if name=="search_formulas": opts["mode"]="formula"
            if name=="search_annotations": opts.update(feed="zotero",kind="annotation")
            opts.setdefault("limit",15)
            rows,note=search(db,**opts)
            return {"matches":rows,"status":note}
        if name in ("search_ideas","search_models","search_questions","list_open_questions"):
            terms=str(args.get("query","")).lower().split()
            matches=[]
            for row in db.execute("SELECT id,body FROM ideas ORDER BY updated DESC"):
                i=idea_get(db,row["id"])
                if name=="search_models" and i["idea_type"] not in ("数学模型","数学证明"): continue
                if name in ("search_questions","list_open_questions") and (i["idea_type"]!="未解决问题" or i["research_status"] in ("已验证","已否定")): continue
                if all(t in row["body"].lower() for t in terms):
                    matches.append({k:i[k] for k in ("idea_id","title","summary","idea_type","author_type","research_status","source_refs")})
                if len(matches)>=50: break
            return matches
        if name=="read_source":
            sid=args["source_id"]
            row=db.execute("SELECT * FROM sources WHERE id=?",(sid,)).fetchone()
            if not row: raise KeyError("source_id 不存在")
            offset=max(0,int(args.get("offset",0)))
            rows=db.execute(f"{RESULT} WHERE p.source=? ORDER BY p.id LIMIT 17 OFFSET ?",(sid,offset)).fetchall()
            return {"source":dict(row),"passages":[dict(r) for r in rows[:16]],"next_offset":offset+16 if len(rows)>16 else None}
        if name=="get_idea": return idea_get(db,args["idea_id"])
        if name=="get_idea_history":
            return [{"version":r["version"],"at":r["at"],"reason":r["reason"],"body":json.loads(r["body"])}
                    for r in db.execute("SELECT * FROM revisions WHERE idea=? ORDER BY version",(args["idea_id"],))]
        if name in ("find_related_ideas","find_conflicting_ideas"):
            rows=db.execute("SELECT * FROM links WHERE a=? OR b=?",(args["idea_id"],args["idea_id"]))
            return [dict(r) for r in rows if name=="find_related_ideas" or r["kind"]=="冲突关系"]
        if name=="get_sync_status": return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY feed")]
        if name=="get_research_summary":
            return {"ideas":dict(db.execute("SELECT json_extract(body,'$.idea_type'),count(*) FROM ideas GROUP BY 1")),
                    "sources":dict(db.execute("SELECT feed,count(*) FROM sources WHERE state='indexed' GROUP BY feed")),
                    "vectors":db.execute("SELECT count(*) FROM vectors").fetchone()[0]}
        if name=="capture_chat":
            return capture(db,args)
        if name=="create_idea":
            body=args["body"]
            key=args.get("dedup") or digest(dump({"sources":body.get("source_refs",[]),"quotes":body.get("source_excerpts",[]),
                                                "title":body.get("title"),"content":body.get("full_content")}))
            return idea_get(db,idea_save(db,body,args["reason"],dedup="mcp:"+key))
        if name=="update_idea":
            return idea_get(db,idea_save(db,args["body"],args["reason"],args["idea_id"],args["version"]))
        if name=="link_ideas":
            kind=args["kind"]
            if kind not in RELATIONS or not args["basis"].strip(): raise ValueError("需填写受支持的关系及证据")
            db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?,?,?)",
                       (args["a"],args["b"],kind,args["basis"],args.get("confidence","待验证")))
            return {"saved":True}
        raise ValueError("Unknown tool")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--write",action="store_true")
    ap.add_argument("--call")
    ap.add_argument("--args",default="{}")
    cli=ap.parse_args()
    tools=WRITE if cli.write else READ
    if cli.call:
        if cli.call not in tools: ap.error("This process does not provide that tool")
        print(dump(call(cli.call,json.loads(cli.args))))
        return
    for line in sys.stdin:
        msg={}
        try:
            msg=json.loads(line)
            method=msg["method"]
            if "id" not in msg: continue
            params=msg.get("params") or {}
            if method=="initialize":
                version=params.get("protocolVersion")
                supported=("2024-11-05","2025-03-26","2025-06-18","2025-11-25")
                result={"protocolVersion":version if version in supported else supported[-1],
                        "capabilities":{"tools":{}},"serverInfo":{"name":"Research Ideas "+("Write" if cli.write else "Read"),"version":"1.0.0"}}
            elif method=="ping": result={}
            elif method=="tools/list":
                result={"tools":[{"name":n,"description":d,"inputSchema":{"type":"object","properties":p,
                                  "required":r,"additionalProperties":False},"annotations":{"readOnlyHint":not cli.write,
                                  "destructiveHint":False,"openWorldHint":False}} for n,(d,p,r) in tools.items()]}
            elif method=="tools/call":
                name=params["name"]
                try:
                    if name not in tools: raise ValueError("Tool is not available in this process")
                    val=call(name,params.get("arguments") or {})
                    result={"content":[{"type":"text","text":dump(val)}]}
                except Exception as e:
                    result={"isError":True,"content":[{"type":"text","text":str(e)}]}
            else:
                print(dump({"jsonrpc":"2.0","id":msg["id"],"error":{"code":-32601,"message":"Method not found"}}),flush=True)
                continue
            print(dump({"jsonrpc":"2.0","id":msg["id"],"result":result}),flush=True)
        except (KeyError,ValueError,TypeError) as e:
            print(dump({"jsonrpc":"2.0","id":msg.get("id"),"error":{"code":-32600,"message":str(e)}}),flush=True)
if __name__=="__main__": main()
