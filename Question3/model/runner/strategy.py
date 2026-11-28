import json
from model.experiments.cases import repair_case
from model.experiments.report import record
from model.recovery.solver import solve


# 作用：比较原序、回填和有限换序。参数：无。返回：无，向标准输出写各情景的派工与指标。
def main():
    rows={}
    for name,case in [('short',repair_case()),('long',repair_case(15)),('short_slot',repair_case(short_slot=True)),('precedence',repair_case(dependent=True))]:
        rows[name]={policy:record(solve(case,policy,{'A','B'})) for policy in ('original','fill','cps')}
    from model.recovery.decision import common_dispatch
    name,start,_ = common_dispatch([repair_case(),repair_case(15)],{"A","B"})
    rows["shared_first_action"]={"task":name,"minute":start}
    print(json.dumps(rows,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
