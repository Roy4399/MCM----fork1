"""同状态、同工时向量的首步配对回放。"""
from dataclasses import replace
from statistics import fmean
from math import inf
from model.replay.engine import apply, run
from model.evaluation.audit import audit


# 作用：比较首步动作并追完本批及传入的窗外承诺，仅返回本次要下达的指令。
# 参数：s 为可见现场，p 为策略（目标包/全批、样本数、可选风险阈值），options 为同一候选集。
# 返回：得分最优的 Order；若所有回放阻塞，使用同一优先级表中的首个动作。
def choose(s,p,options):
    if len(options)==1:
        return options[0]
    target = min((s.jobs[o.job] for o in options),key=lambda j:(j.due,j.key))
    worlds = [s.forecast.draw(811+i,s.now,s.starts,s.ends) for i in range(p.draws)]
    follow = replace(p,scope='rule')
    ranked, details = [], []
    for order in options:
        losses, moves, risks = [], [], []
        failures = 0
        for world in worlds:
            trial = s.fork()
            apply(trial,order,p)
            run(trial,world,follow)
            try:
                audit(trial)
            except ValueError:
                failures += 1
                continue
            keys = [target.key] if p.scope=='target' else list(trial.jobs)
            losses.append(sum(trial.pick[k]-trial.ready[k] for k in keys))
            finals = [k for k in trial.jobs if k not in trial.child and trial.jobs[k].cast_time]
            risks.extend(trial.drop[k]>trial.jobs[k].due+1e-8 for k in finals)
            moves.append(sum(b-a for _,_,_,a,b in trial.logs))
        risk = fmean(risks) if risks else 0.
        excess = max(0.,risk-p.risk) if p.risk is not None else 0.
        ranked.append((failures,excess,fmean(losses) if losses else inf,
                       fmean(moves) if moves else inf))
        details.append(dict(car=order.car+1,job=order.job,via=order.via,wake=order.wake,
                            failures=failures,risk=risk if risks else None,
                            expected_wait=fmean(losses) if losses else None,
                            expected_move=fmean(moves) if moves else None))
    ranking=sorted(range(len(options)),key=ranked.__getitem__)
    s.rankings.append(dict(time=s.now,target=target.key,draws=len(worlds),
                           candidates=[details[i] for i in ranking]))
    return options[ranking[0]]
