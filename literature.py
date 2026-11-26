"""Record external bibliographic exploration using only reviewed public search terms."""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from db import STATE, connect

TOPICS={
 "一般利润率与基本品":[
    "basic and non-basic commodities rate of profit Sraffa",
    "joint production nonbasic commodities rates of profit Sraffa",
    "probabilistic political economy profit rate distribution Marxian",
 ],
 "地租分类与竞租":[
    "Marx absolute and differential rent land scarcity Basu",
    "urban bid rent location theory normal profit rate",
 ],
}

def work_search(query):
    params={"query.bibliographic":query,"rows":8,"select":"DOI,title,container-title,issued,author,URL"}
    request=Request("https://api.crossref.org/works?"+urlencode(params),headers={"User-Agent":"Research Ideas/1.0 (bibliographic catalog lookup)"})
    with urlopen(request,timeout=18) as result:
        return json.load(result)["message"]

def main():
    a=argparse.ArgumentParser()
    a.add_argument("topic",choices=tuple(TOPICS))
    args=a.parse_args()
    when=datetime.now().astimezone()
    folder=STATE/"reports"
    folder.mkdir(parents=True,exist_ok=True)
    out=folder/f"literature-{when:%Y-%m-%d}-{list(TOPICS).index(args.topic)+1}.md"
    rows=[f"# 外部文献初筛 · {args.topic}","",f"检索时间 {when:%Y-%m-%d %H:%M %Z}。",
          "使用 Crossref 公开书目元数据。发送预设通用词，按该接口的相关性排序。"
          "候选需核对正文、研究设定与论证，检索结果不能判定原创性。",""]
    with connect() as d:
        known={r[0].lower() for r in d.execute("SELECT json_extract(meta,'$.doi') FROM sources WHERE feed='zotero' AND json_extract(meta,'$.doi') IS NOT NULL")}
    for query in TOPICS[args.topic]:
        rows.extend([f"## {query}",""])
        try:
            result=work_search(query)
        except (OSError,ValueError,KeyError) as e:
            rows.extend([f"读取失败 · {type(e).__name__}",""])
            continue
        rows.extend([f"本次检视返回排名前 {len(result['items'])} 条；接口估计匹配数 {result.get('total-results','未知')}。",""])
        for hit in result["items"]:
            doi=hit.get("DOI","")
            title=next(iter(hit.get("title") or []),"(无标题)").replace("\n"," ")
            journal=next(iter(hit.get("container-title") or []),"")
            issued=(hit.get("issued") or {}).get("date-parts") or [[]]
            year=next(iter(issued[0]),"")
            zotero=" · 本地 Zotero 已有该 DOI" if doi.lower() in known else ""
            rows.append(f"- {title} · {journal} {year}{zotero}。 [DOI](https://doi.org/{doi})")
        rows.append("")
        time.sleep(.25)
    temp=out.with_suffix(".tmp")
    temp.write_text("\n".join(rows),encoding="utf-8")
    temp.replace(out)
    print(out)
if __name__=="__main__": main()
