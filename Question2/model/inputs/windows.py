"""读取可复现窗口与可替换厂房参数。"""
import json
from datetime import datetime
from pathlib import Path
from model.inputs.schema import Job, Plant, validate
from model.motion.track import Rail
from model.forecast.duration import Forecast

DATA=Path(__file__).resolve().parents[2]/'data'


# 作用：解析以秒、米表示的模型配置，生成窗口的任务及工艺边。
# 参数：rows 为按炉记录，cfg 为轨道/工位参数；hard_deadlines 可指定任务号到最晚交付秒数。
# 返回：经过校验的 Plant。原点取最早入选炉的计划开工时间。
def plant_for(rows,cfg,hard_deadlines=None):
    loc=cfg['locations']
    points=tuple(sorted(set([*loc.values(),*cfg['origins'],*cfg['parking'],*cfg['boundaries'],
                             *(t for a,b,_ in cfg['speed_zones']+cfg.get('gear_zones',[]) for t in (a,b))])))
    caps=tuple(min([max(v[0] for v in cfg['profiles'])]+
                   [v for a,b,v in cfg['speed_zones'] if a<=x and y<=b])
               for x,y in zip(points,points[1:]))
    gears=tuple(map(tuple,cfg.get('gears',())))
    edge_gears=tuple(min([len(gears[0])-1]+[v for a,b,v in cfg.get('gear_zones',())
                            if a<=x and y<=b]) for x,y in zip(points,points[1:])) if gears else ()
    rail=Rail(points,caps,tuple(map(tuple,cfg['profiles'])),tuple(cfg['parking']),cfg['gap'],
              gears,edge_gears)
    base=min(datetime.fromisoformat(r['start']) for r in rows)
    # 作用：把计划时间戳转换为从窗口原点起算的秒数。
    # 参数：value 为 ISO 日期时间字符串。
    # 返回：相对时间（秒）。
    def at(value):
        return (datetime.fromisoformat(value)-base).total_seconds()
    previous, order = {}, {}
    for r in sorted(rows,key=lambda v:v['due']):
        machine=r['stations'][-1]
        order[r['heat']]=previous.get(machine)
        previous[machine]=r
    jobs=[]
    for r in rows:
        prev=None
        for idx,(kind,mean) in enumerate(zip(r['kinds'],r['means'])):
            key=f"{r['heat']}-{idx+1}"
            last=idx==len(r['means'])-1
            before=order[r['heat']] if last else None
            # 顺序缺号时仍约束同机排序，但不可声称其为可核验的同浇次相邻交接。
            cast=r['cast']
            cast_prev=f"{before['heat']}-{len(before['means'])}" if before else None
            jobs.append(Job(key,r['heat'],kind,r['stations'][idx],r['stations'][idx+1],prev,
                        at(r['start']),mean,cfg['prep'][kind],at(r['ends'][idx])+cfg['prep'][kind],
                        at(r['due']) if last else at(r['ends'][idx+1])-r['means'][idx+1],
                        tuple(cfg['heavy_eligible']),cast_time=r['casting'] if last else 0.,
                        cast=cast,cast_prev=cast_prev,cast_sequence=r["sequence"],
                        commitment=(hard_deadlines or {}).get(key,float('inf'))))
            prev=key
    starts=sorted(rows,key=lambda r:r['start'])
    next_id=f"{starts[-1]['heat']}-1"
    p=Plant(tuple(jobs),rail,tuple(cfg['origins']),loc,
            {s:cfg.get('capacity',{}).get(s,cfg['capacity_default']) for s in loc},
            average_speed=cfg['average_speed'],heavy_eligible=tuple(cfg['heavy_eligible']),
            ferries=tuple(s for s in loc if s.endswith('渡车')),
            window_open=at(starts[1]['start']) if len(starts)>1 else 0.,
            window_close=at(starts[-1]['start']),next_commitment=next_id)
    validate(p)
    return p


# 作用：装载三组实验共用的输入、预测器与可选的实绩工时。
# 参数：name 为 five-heats 或日期，rail_path 为运动参数文件，dataset 为抽取数据文件。
# 返回：(厂房，工时预测器，实绩工时字典或 None)。
def load_case(name,rail_path=DATA/'rail.json',dataset=DATA/'plant_windows.json'):
    cfg=json.loads(Path(rail_path).read_text(encoding='utf8'))
    raw=json.loads(Path(dataset).read_text(encoding='utf8'))
    if name=='five-heats':
        rows=json.loads((DATA/'five_heats.json').read_text(encoding='utf8'))
        # 此样本早于训练期终点，验收时只用预设工时，避免未来信息泄漏。
        blocks=[{k:('*',z) for k in ('BOF','LF','RH')} for z in (.8,1.,1.,1.2)]
        measured=None
    else:
        rows=raw['dates'][name]
        blocks=[b['ratios'] for b in raw['training']]
        measured={f"{r['heat']}-{i}":v for r in rows for i,v in enumerate(r['measured'],1)}
    p=plant_for(rows,cfg)
    return p,Forecast(p.jobs,blocks),measured
