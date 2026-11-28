"""公共实验协议：同窗、同场景配对比较，分日期重抽样。"""
import argparse
import json
from collections import defaultdict
from dataclasses import asdict, replace
from math import isfinite
from pathlib import Path
from random import Random
from statistics import fmean
from model.inputs.windows import load_case, DATA
from model.replay.state import State
from model.motion.track import state_at
from model.replay.engine import run
from model.dispatch.paired import choose
from model.evaluation.audit import metrics
from model.forecast.duration import quantile


# 作用：对各策略同场景的差值按日期整块重抽样。
# 参数：rows 为逐场景记录，labels 为策略名，seed 为种子，repeats 为重抽样次数。
# 返回：各策略相对首个策略的均值差与 95% 百分位区间；单日只报差值。
def paired_differences(rows,labels,seed,repeats=1000):
    tables=defaultdict(dict)
    for r in rows:
        if r['status']=='ok':
            tables[r['case'],r['world']][r['policy']]=r['metrics']
    output={}
    for label in labels[1:]:
        comparison={}
        for field in ('wait_total','wait_mean','wait_p95','target_wait','other_wait','late_rate',
                      'empty_seconds','move_seconds','repair_seconds','decision_seconds','modeled_breaks'):
            dates=defaultdict(list)
            for (date,_),pair in tables.items():
                if label in pair and labels[0] in pair:
                    a,b=pair[labels[0]][field],pair[label][field]
                    if a is not None and b is not None:
                        dates[date].append(b-a)
            delta=[fmean(v) for v in dates.values()]
            if not delta:
                continue
            ci=None
            if len(delta)>1:
                rng=Random(seed)
                boot=[fmean(rng.choices(delta,k=len(delta))) for _ in range(repeats)]
                ci=[quantile(boot,.025),quantile(boot,.975)]
            comparison[field]=dict(mean=fmean(delta),ci95=ci,blocks=len(delta),
                                   pairs=sum(map(len,dates.values())))
        output[label]=comparison
    return output


# 作用：执行一个实验的全部配对回放，失败保留在输出中。
# 参数：cases 为窗口名，policies 为策略字典，args 为命令行设置。
# 返回：含每次指标、起落明细、可选轨迹、分组差值及输入口径的报告字典。
def compare(cases,policies,args):
    rows=[]
    for name in cases:
        plant,f,measured=load_case(name,args.rail,args.dataset)
        worlds=[]
        if measured is not None and args.world in ('both','measured'):
            worlds.append(('measured',measured))
        if args.world in ('both','sampled'):
            worlds.extend((f'seed-{args.seed+i}',f.draw(args.seed+i))
                          for i in range(args.samples))
        if not worlds:
            raise ValueError('该窗口仅提供场景工时，请指定 --world sampled 或 both')
        for world_id,w in worlds:
            for label,policy in policies.items():
                state=run(State(plant,f),w,policy,choose if policy.scope!='rule' else None)
                record=dict(case=name,world=world_id,policy=label,status='ok')
                try:
                    record['metrics']=metrics(state,plant.jobs[0].key)
                except ValueError as exc:
                    record.update(status='failed',reason=str(exc),completed=len(state.drop))
                record['lifts']=[dict(job=k,car=state.owner[k]+1,process_start=state.starts[k],
                                      process_end=state.ends[k],ready=state.ready[k],arrive=state.arrival[k],
                                      pick=state.pick[k],drop=v,reference_due=state.jobs[k].due,
                                      cast_start=state.casts.get(k)) for k,v in state.drop.items()]
                if plant.next_commitment:
                    k=plant.next_commitment
                    cutoff=plant.window_close
                    record['window']=dict(start=plant.window_open,end=cutoff,
                        wip_at_start=[j.key for j in plant.jobs
                                      if state.starts.get(j.key,float('inf'))<plant.window_open
                                      and state.drop.get(j.key,float('inf'))>plant.window_open],
                        cranes_at_start=[state_at(x,tr,plant.window_open)[:2]
                                         for x,tr in zip(plant.origins,state.traces)],
                        first_outside_job=k,delivery=state.drop.get(k),reference_due=state.jobs[k].due,
                        met_reference=(state.drop[k]<=state.jobs[k].due+1e-8) if k in state.drop else None)
                if state.rankings:
                    record['decisions']=state.rankings
                if args.traces:
                    record['trajectories']=[[asdict(v) for v in t] for t in state.traces]
                    record['moves']=state.logs
                rows.append(record)
    return dict(units='seconds, metres',target='窗口内首个任务；首步回放选择中的局部目标为最早交接期限候选',
                settings={k:asdict(v) for k,v in policies.items()},seed=args.seed,rows=rows,
                differences=paired_differences(rows,list(policies),args.seed))


# 作用：给三个独立 runner 提供统一输入、输出及失败退出码。
# 参数：title 为实验名，policies 为有序对照策略映射。
# 返回：无；控制台打印结果，可保存 JSON；任何审计失败时退出码为 1。
def experiment(title,policies):
    ap=argparse.ArgumentParser(description=title)
    ap.add_argument('--case',default='five-heats',help='five-heats / validation / holdout / YYYY-MM-DD')
    ap.add_argument('--world',choices=['sampled','measured','both'],default='both')
    ap.add_argument('--samples',type=int,default=1)
    ap.add_argument('--seed',type=int,default=27)
    ap.add_argument('--draws',type=int,default=2,help='每个首步的共同预测样本数')
    ap.add_argument('--q',type=float,default=.25,help='车等包成本 / (包等车成本 + 车等包成本)')
    ap.add_argument('--advance',type=float,default=120.)
    ap.add_argument('--risk',type=float,help='备包交付迟到率阈值')
    ap.add_argument('--rail',type=Path,default=DATA/'rail.json')
    ap.add_argument('--dataset',type=Path,default=DATA/'plant_windows.json')
    ap.add_argument('--output',type=Path)
    ap.add_argument('--traces',action='store_true')
    args=ap.parse_args()
    if (min(args.samples,args.draws)<1 or not 0<=args.q<=1 or not isfinite(args.advance)
            or args.advance<0 or (args.risk is not None and not 0<=args.risk<=1)):
        ap.error('样本量必须为正，比例在 [0,1] 内，提前量须非负')
    cases=[args.case]
    if args.case in ('validation','holdout'):
        dates=sorted(json.loads(args.dataset.read_text(encoding='utf8'))['dates'])
        cases=[d for d in dates if (d<'2026-10-08')==(args.case=='validation')]
    policies={k:replace(p,draws=args.draws,q=args.q,advance=args.advance,risk=args.risk)
              for k,p in policies.items()}
    report=compare(cases,policies,args)
    print(title)
    print('窗口          场景       策略          段数   平均待吊(s)  P95(s)  备包迟到率   修复数')
    for row in report['rows']:
        if row['status']=='failed':
            print(row['case'],row['world'],row['policy'],'失败',row['reason'])
            continue
        m=row['metrics']
        print(f"{row['case']:12} {row['world']:10} {row['policy']:10} {m['tasks']:3} "
              f"{m['wait_mean']:10.2f} {m['wait_p95']:8.2f} {m['late_rate']:10.1%} {m['repair_count']:6}")
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
    if any(row['status']!='ok' for row in report['rows']):
        raise SystemExit(1)
