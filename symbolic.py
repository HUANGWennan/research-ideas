"""Check scalar rational identities while preserving excluded denominators."""
import argparse
import ast
import json
import re

def compare(left,right,variables,nonzero=()):
    import sympy as s
    variables=list(dict.fromkeys(variables))
    if not 1<=len(variables)<=12 or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,24}",n) for n in variables):
        raise ValueError("请输入 1 到 12 个已定义的实标量变量")
    symbols={n:s.Symbol(n,real=True) for n in variables}
    conditions=set()
    def expr(text):
        if len(text)>240:
            raise ValueError("表达式过长")
        tree=ast.parse(text.replace("^","**").replace("−","-").strip(),mode="eval")
        if sum(1 for _ in ast.walk(tree))>85:
            raise ValueError("表达式节点过多")
        def node(n):
            if isinstance(n,ast.Constant) and type(n.value) in (int,float) and abs(n.value)<=10**10:
                return s.Rational(str(n.value))
            if isinstance(n,ast.Name) and n.id in symbols:
                return symbols[n.id]
            if isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.UAdd,ast.USub)):
                return node(n.operand) if isinstance(n.op,ast.UAdd) else -node(n.operand)
            if isinstance(n,ast.BinOp):
                a,b=node(n.left),node(n.right)
                if isinstance(n.op,ast.Add): return a+b
                if isinstance(n.op,ast.Sub): return a-b
                if isinstance(n.op,ast.Mult): return a*b
                if isinstance(n.op,ast.Div):
                    if b==0: raise ValueError("分母为零")
                    conditions.add(b)
                    return a/b
                if isinstance(n.op,ast.Pow) and b.is_Integer and abs(b)<=6:
                    if b<0:
                        if a==0: raise ValueError("分母为零")
                        conditions.add(a)
                    return a**b
            raise ValueError("只支持已声明的标量、实数和有界有理运算")
        return node(tree.body)

    def equation(e):
        if e.count("=")!=1:
            raise ValueError("两条公式各需一个等号")
        a,b=e.split("=")
        num,den=s.fraction(s.together(expr(a)-expr(b)))
        conditions.add(den)
        return s.Poly(num,*symbols.values())

    a,b=equation(left),equation(right)
    nonzero_set=set()
    for h in nonzero:
        for factor,_ in s.factor_list(s.together(expr(h)).as_numer_denom()[0],*symbols.values())[1]:
            nonzero_set.add(s.Poly(factor,*symbols.values()).monic().as_expr())
    needed=set()
    for c in conditions:
        top,bottom=s.together(c).as_numer_denom()
        for form in (top,bottom):
            for factor,_ in s.factor_list(form,*symbols.values())[1]:
                normalized=s.Poly(factor,*symbols.values()).monic().as_expr()
                if normalized not in nonzero_set:
                    needed.add(str(normalized)+" != 0")
    status="无法在该规则下确认"
    if a.is_zero and b.is_zero:
        status="共同定义域内恒等"
    elif not (a.is_zero or b.is_zero) and a.monic()==b.monic():
        status="共同定义域内等价"
    return {"结论":status,"变量":variables,"还需满足":sorted(needed),
            "化简后的两个方程左端":[str(a.as_expr()),str(b.as_expr())],
            "适用范围":"实标量有理方程；矩阵维度、谱半径与经济假设需单独核验"}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("left")
    ap.add_argument("right")
    ap.add_argument("--variables",required=True,help="逗号分隔的标量变量")
    ap.add_argument("--nonzero",action="append",default=[])
    a=ap.parse_args()
    print(json.dumps(compare(a.left,a.right,a.variables.split(","),a.nonzero),ensure_ascii=False,indent=2))
if __name__=="__main__": main()
