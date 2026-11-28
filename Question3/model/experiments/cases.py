from dataclasses import replace
from model.domain.types import Case,Crane,Task


# 作用：构造文档争车与回填不足病例。参数：repair 距 10:10 的可吊分钟，short_slot 是否缩短 A 原预约，dependent 是否限定 A→B。返回：带冻结边界任务的三单快照。
def repair_case(repair=8,short_slot=False,dependent=False):
    a=Task('A',0,repair,40,'3',0,0,5 if short_slot else 10,('3',))
    b=Task('B',10,-2,10,'3',0,0,10,('3',),('A',) if dependent else ())
    c=Task('C',35,35,37,'3',0,0,2,('3',),locked=True)
    return Case((a,b,c),(Crane('3',0,450),),horizon=40)


# 作用：构造五车、五任务和邻车受阻实例。参数：无。返回：原序中 A 延误后与 B 的轨迹发生干涉的快照。
def rail_case():
    cranes=tuple(Crane(str(i),p,450) for i,p in enumerate((-20,0,5,20,30)))
    a=Task('A',0,4,15,'1',0,3,0,('1',))
    b=Task('B',4,4,13,'2',5,3,0,('2',))
    # A 必须返回原位，B 必须返回其初始位置。
    c=Task('C',5,5,18,'1',3,0,0,('1',),('A',))
    d=Task('D',8,8,18,'2',3,5,0,('2',),('B',))
    e=Task('E',14,14,17,'4',30,30,1,('4',),locked=True)
    return Case((a,b,c,d,e),cranes,horizon=19)
