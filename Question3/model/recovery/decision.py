from dataclasses import replace
from model.recovery.solver import solve


# 作用：在未知维修时长时选择共同的单车首单。参数：cases 同一时刻的维修情景，active 可调整任务，k 后续排序偏移界。返回：首单名称、时刻及各情景可行续排；无共同即刻动作时返回 None 表示等待新事件。
def common_dispatch(cases,active,k=1):
    if not cases or any(len(c.cranes)!=1 or c.now!=cases[0].now for c in cases):
        raise ValueError('需要同一决策时刻的单车情景')
    candidates=[]
    for t in cases[0].tasks:
        if t.locked or t.name not in active or t.parents:
            continue
        results=[]
        for case in cases:
            task=next(j for j in case.tasks if j.name==t.name)
            crane=case.cranes[0]
            if task.ready>case.now or task.origin!=crane.position or crane.name not in task.eligible or task.weight>crane.capacity:
                break
            # 先完成共同首单，再对剩余任务求解。首单整体按完整原计划校验。
            result=solve(case,'cps',active,k,first=(task.name,case.now))
            if result['score'] is None:
                break
            results.append(result)
        if len(results)==len(cases):
            candidates.append((max(r['score'] for r in results),t.name,results))
    if candidates:
        _,name,results=min(candidates,key=lambda item:item[:2])
        return name,cases[0].now,results
    return None
