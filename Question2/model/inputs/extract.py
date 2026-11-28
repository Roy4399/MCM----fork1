"""从队内原始表生成脱离 Excel 依赖即可运行的样本。运行本模块才需要 pandas。"""
import argparse
import json
from collections import Counter
from pathlib import Path


# 作用：从两个明确对应的事件时间计算正工时。
# 参数：a/b 为开始和结束时间戳。
# 返回：以秒计的正持续时间；缺失或乱序抛出 ValueError。
def seconds(a,b):
    import pandas as pd
    if pd.isna(a) or pd.isna(b) or b<=a:
        raise ValueError("缺失或逆序工时")
    return (b-a).total_seconds()


# 作用：按炉号、有效计划版本和设备对齐转炉、精炼、连铸及计划记录。
# 参数：folder 为五张计划/工序/转炉表所在目录，out 为导出 JSON 路径。
# 返回：计数器，列出匹配、跳过和分组的炉次数。
def extract(folder,out):
    import pandas as pd
    folder = Path(folder)
    plan = pd.read_excel(folder/'出钢计划表单.xls')
    events = pd.read_excel(folder/'工序时间信息.xlsx')
    furnaces = {n:pd.read_excel(folder/f'{n}炉.xlsx') for n in 'ABC'}
    plan = plan[(plan['是否过期flag']==0)&(plan['熔炼号'].astype(str)!='-2')].copy()
    hist, cases, count = [], {}, Counter()
    joined = []
    for heat,g in plan.groupby('熔炼号'):
        if len(g)!=1:
            count['ambiguous_plan']+=1
            continue
        a=g.iloc[0]
        bof=a['转炉设备号']
        match=events[(events['熔炼号']==heat)&(events['转炉设备号']==bof)]
        f=furnaces[bof]
        f=f[f['熔炼号']==heat]
        if len(match)!=1 or len(f)!=1:
            count['ambiguous_or_missing_event']+=1
            continue
        e,f=match.iloc[0],f.iloc[0]
        route=['BOF']+['LF' if v=='L' else 'RH' for v in a['精炼路径'] if v in 'LR']
        stations=[bof+'炉']
        times=[(a['转炉开始时间'],a['转炉结束时间'])]
        actual=[(pd.to_datetime(f['转炉兑铁开始时间']),pd.to_datetime(f['出钢结束时间']))]
        try:
            planned_pairs=[(a[f'精炼{i}时间'],a[f'精炼{i+1}时间']) for i in (1,3,5,7)
                           if pd.notna(a[f'精炼{i}时间']) or pd.notna(a[f'精炼{i+1}时间'])]
            if len(planned_pairs)!=len(route)-1:
                raise ValueError("计划精炼段缺失")
            for i,kind in enumerate(route[1:]):
                stations.append(f"{int(e[kind+'设备号'])}#{kind}")
                times.append(planned_pairs[i])
                keys=('LF进处理位时间','LF出处理位时间') if kind=='LF' else ('RH处理开始时间','RH处理结束时间')
                actual.append((pd.to_datetime(e[keys[0]]),pd.to_datetime(e[keys[1]])))
            caster=int(e['连铸机设备号'])
            if caster!={'A':1,'B':2,'C':3}[a['连铸机号']]:
                raise ValueError("连铸设备不匹配")
            stations.append(f'{caster}#连铸')
            means=[seconds(*v) for v in times]
            measured=[seconds(*v) for v in actual]
            casting=seconds(a['浇铸开始时间'],a['浇铸结束时间'])
            if any(actual[i][1]>actual[i+1][0] for i in range(len(actual)-1)):
                raise ValueError("实际路线时序不符")
            if any(times[i][1]>times[i+1][0] for i in range(len(times)-1)):
                raise ValueError("计划时序不符")
        except (ValueError,TypeError):
            count['invalid_sequence_or_device']+=1
            continue
        date=str(times[0][0].date())
        count['usable_heats']+=1
        if '2026-09-25'<=date<='2026-10-04' and str(actual[-1][1].date())<='2026-10-04':
            hist.append({'heat':str(heat),'date':date,'plan':str(a['计划号']),
                         'ratios':{k:[s,v/m] for k,s,v,m in zip(route,stations,measured,means)}})
        joined.append(dict(heat=str(heat),version=str(a['计划号']),date=date,kinds=route,stations=stations,
                           start=str(times[0][0]),ends=[str(b) for _,b in times],means=means,
                           measured=measured,due=str(a['浇铸开始时间']),casting=casting,
                           cast=str(a['连连浇号']),sequence=int(a['浇次号'])))
    for date in sorted({r['date'] for r in joined if '2026-10-05'<=r['date']<='2026-10-11'}):
        rows=sorted((r for r in joined if r['date']==date),key=lambda r:r['start'])
        # 取第一个含同浇次相邻炉的五炉小窗；第一炉用于热身，第五炉用于检查出窗交付。
        for i in range(len(rows)-4):
            window=rows[i:i+5]
            if any(a['cast']==b['cast'] and a['sequence']+1==b['sequence']
                   for k,a in enumerate(window) for b in window[k+1:]):
                cases[date]=window
                break
    count['training_heat_blocks']=len(hist)
    count['date_windows']=len(cases)
    out.write_text(json.dumps(dict(training=hist,dates=cases,counts=count),ensure_ascii=False),
                   encoding='utf8')
    return count


# 作用：接收原始工作簿目录并生成训练块及验证、留出窗口。
# 参数：无；从命令行读取 folder 和 output。
# 返回：无；打印数据筛选计数。
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    print(extract(args.folder,args.output))


if __name__=='__main__':
    main()
