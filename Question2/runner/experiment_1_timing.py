"""对照一：沿用原时刻、统一提前、条件分位数提前。"""
from model.dispatch.policy import Policy
from model.evaluation.compare import experiment

if __name__=='__main__':
    experiment(__doc__,{'原时刻':Policy(timing='plan'),'统一提前':Policy(timing='fixed'),
                        '分位数提前':Policy(timing='quantile')})
