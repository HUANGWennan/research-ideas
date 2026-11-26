"""Conservative local discovery from preserved user passages and recorded idea fields."""
import re
from db import digest, idea_save, idea_get

SIGNALS=re.compile(r"我认为|我的理解是|我以为|我的假设是|我的观点是|我想提出|我总结为|我理解为|我想知道|what if|my hypothesis|my interpretation",re.I)
QUESTION=re.compile(r"我想知道|[？?]|what if",re.I)
WORKFLOW=re.compile(r"额度|插件|APP|弹窗|工作区|终端|token|请继续|系统任务|授权|发送邮件|上传至",re.I)
TOPICS={
 "一般利润率":r"一般利润率|general rate of profit","利润率的概率分布":r"概率分布|profit rate distribution",
 "谱半径":r"谱半径|spectral radius","基本品与非基本品":r"基本品|non.basic|luxur(y|ies)",
 "绝对地租":r"绝对地租|absolute rent","级差地租":r"级差地租|differential rent","城市地租":r"城市地租|竞租|bid.rent",
 "劳动价值":r"劳动价值|labou?r value","生产价格":r"生产价格|prices? of production",
 "技术进步":r"技术进步|技术选择|置盐|okishio|technical change","资本主义":r"资本主义|capitalism",
 "联合生产":r"联合生产|joint production","经济危机":r"经济危机|capitalist crisis",
 "投入产出":r"投入产出|input.output","国家模型":r"国家理论|国家统一模型|社会分配",
}

def collect(db):
    rows=db.execute("""SELECT p.source,p.anchor,p.text,s.modified FROM passages p JOIN sources s ON s.id=p.source
        WHERE s.state='indexed' AND s.feed='ai' AND p.role='用户原话'
        ORDER BY s.modified DESC,p.id LIMIT 2000""").fetchall()
    existing=[r[0] for r in db.execute("SELECT body FROM ideas")]
    used=0
    for r in rows:
        text=r["text"].strip()
        match=SIGNALS.search(text)
        if not match or not 35<=len(text)<=1100 or WORKFLOW.search(text):
            continue
        topics=[name for name,pattern in TOPICS.items() if re.search(pattern,text,re.I)]
        if not topics:
            continue
        key="auto:"+digest(text)
        if any(text[:45] in body for body in existing) or db.execute("SELECT 1 FROM ideas WHERE dedup=?",(key,)).fetchone():
            continue
        sentence=re.split(r"[。？！!?\n]",text[match.start():],1)[0]
        title=sentence[:74].strip(" ，：:")
        if len(title)<12: continue
        idea_save(db,{"title":title,"full_content":text,"summary":"保留原话，待审核研究意义与前提。",
                      "idea_type":"未解决问题" if QUESTION.search(text) else "理论观点",
                      "author_type":"用户明确提出","research_status":"待研究","research_topics":topics,
                      "source_refs":[r["source"]],"source_excerpts":[text],
                      "first_proposed_at":r["modified"][:10],
                      "confidence":"来源为用户原话；自动分类与首提日期待审核",
                      "evidence":r["anchor"]},"自动发现含明确观点表述的用户原话，待审核",dedup=key)
        used+=1
        if used>=8: break
    return used

def maturity(idea):
    return {
        "研究问题": "有问题或摘要记录" if idea["open_questions"] or idea["summary"] else "待明确",
        "理论贡献": "已有阶段性差异核验" if idea["novelty_status"]=="已完成阶段性文献核验" else "待对照现有理论",
        "数学模型": "有推导及变量定义" if idea["derivation"] and idea["variable_definitions"] else "待完善或不适用",
        "可验证性": idea["verification_status"],
        "数据": "有证据记录，需核对数据可用性" if idea["evidence"] else "待登记或不适用",
        "发表准备": idea["research_status"],
    }

def proposals(db):
    entries=[idea_get(db,r[0]) for r in db.execute("SELECT id FROM ideas ORDER BY updated DESC LIMIT 400")]
    stored={tuple(sorted((r[0],r[1]))) for r in db.execute("SELECT a,b FROM links")}
    yielded=0
    for n,a in enumerate(entries):
        if a["research_status"]=="已否定": continue
        for b in entries[n+1:]:
            if b["research_status"]=="已否定" or tuple(sorted((a["idea_id"],b["idea_id"]))) in stored: continue
            topics=sorted(set(a["research_topics"]) & set(b["research_topics"]))
            if len(topics)<2: continue
            yield a["title"],b["title"],topics
            yielded+=1
            if yielded>=8: return
