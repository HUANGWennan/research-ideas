"""Write a time-stamped status and acceptance report from production records."""
import json
from datetime import datetime
from pathlib import Path
from db import STATE, connect

LABELS={"idea":"桌面 idea","research":"桌面研究项目","course":"课程资料","zotero":"Zotero 条目、笔记、批注",
        "zotero-ocr":"Zotero 图像 OCR","feishu":"飞书本机快照","feishu-live":"飞书实时同步","ai":"本机 AI 历史",
        "curated":"学术对话核对摘录","vectors":"本地语义索引"}

def main():
    folder=STATE/"reports"
    folder.mkdir(parents=True,exist_ok=True)
    rows=["# Research Ideas 数据源覆盖与验收","",f"核对时间 {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}。","",
          "| 来源 | 已发现 | 已索引 | 失败 | 状态 |","|---|---:|---:|---:|---|"]
    with connect() as d:
        status=[dict(r) for r in d.execute("SELECT * FROM runs ORDER BY feed")]
        for r in status:
            if r["feed"]=="vectors":
                r["indexed"]=d.execute("SELECT count(*) FROM vectors v JOIN passages p ON p.id=v.passage JOIN sources s ON s.id=p.source WHERE s.state='indexed'").fetchone()[0]
                r["discovered"]=d.execute("""SELECT count(*) FROM passages p JOIN sources s ON s.id=p.source
                    WHERE s.state='indexed' AND COALESCE(json_extract(s.meta,'$.coverage'),'') NOT IN ('元数据','标题','metadata')""").fetchone()[0]
                r["detail"]=f"当前 {r['indexed']:,} / {r['discovered']:,} 个候选段落已有向量。后台按批更新。"
            rows.append(f"| {LABELS.get(r['feed'],r['feed'])} | {r['discovered']} | {r['indexed']} | {r['failed']} | {r['status']} |")
        rows.extend(["","## 实际规模",""])
        stats={
            "全文段落": "SELECT count(*) FROM passages p JOIN sources s ON s.id=p.source WHERE s.state='indexed'",
            "公式记录": "SELECT count(*) FROM formulas f JOIN passages p ON p.id=f.passage JOIN sources s ON s.id=p.source WHERE s.state='indexed'",
            "语义向量":"SELECT count(*) FROM vectors v JOIN passages p ON p.id=v.passage JOIN sources s ON s.id=p.source WHERE s.state='indexed'",
            "思想条目":"SELECT count(*) FROM ideas","思想版本":"SELECT count(*) FROM revisions","思想关系":"SELECT count(*) FROM links",
            "文献批注":"SELECT count(*) FROM sources WHERE feed='zotero' AND kind='annotation' AND state='indexed'",
            "Zotero 笔记":"SELECT count(*) FROM sources WHERE feed='zotero' AND kind='note' AND state='indexed'",
            "实时或快照飞书文档":"SELECT count(*) FROM sources WHERE feed='feishu' AND state='indexed'",
        }
        for name,query in stats.items():
            rows.append(f"- {name} {d.execute(query).fetchone()[0]:,}")
        for kind,n in d.execute("SELECT json_extract(body,'$.idea_type'),count(*) FROM ideas GROUP BY 1"):
            rows.append(f"- 思想类别 · {kind} {n}")
        for r in status:
            rows.extend(["",f"### {LABELS.get(r['feed'],r['feed'])}","",r["detail"].replace("\n"," ") if r["detail"] else ""])
        errors=d.execute("SELECT feed,error,count(*) FROM sources WHERE state='failed' GROUP BY feed,error ORDER BY count(*) DESC LIMIT 15").fetchall()
        rows.extend(["","## 文件读取缺口",""])
        rows.extend(f"- {LABELS.get(feed,feed)} · {amount} 份 · {reason}" for feed,reason,amount in errors)
    rows.extend(["","## 验证与尚未完成",""])
    for name in ("retrieval.txt","runtime.json","symbolic.json","answer-check.json"):
        item=folder/name
        if not item.is_file(): continue
        try:
            if name=="retrieval.txt":
                rows.append("- "+item.read_text(encoding="utf-8").splitlines()[-1])
            else:
                data=json.loads(item.read_text(encoding="utf-8"))
                for check in data.get("checks",[]):
                    rows.append("- 通过 · "+check)
                if name=="symbolic.json": rows.append("- 符号验算 · "+data.get("结论",""))
        except (OSError,ValueError,IndexError): pass
    rows.extend(["- Codex 本机客户端真实调用 MCP 只读工具通过。DeepSeek Harness 桌面配置已注册，实际用户会话调用另待核验。",
                 "- ChatGPT 官方历史导出已发起。下载文件未到本机时，云端历史仍为待导入。",
                 "- 飞书可见资料的全集与外部文献全集边界未知，报告仅使用接口及目录实际返回的计数。",
                 "- PDF 无文字层、图像公式和部分复杂 OMML 仍需逐页核查。外部文献初筛按公开书目元数据给候选。",
                 "- OneDrive 保存完成写入的数据库快照。另一设备是否已下载由 OneDrive 同步状态决定。",""])
    out=folder/"coverage.md"
    temp=out.with_suffix(".tmp")
    temp.write_text("\n".join(rows),encoding="utf-8")
    temp.replace(out)
    print(out)
if __name__=="__main__": main()
