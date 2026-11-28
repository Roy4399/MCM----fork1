"""对照三：均速与分档估时，两组都通过真实分档轨迹执行器。"""
from model.dispatch.policy import Policy
from model.evaluation.compare import experiment

if __name__=='__main__':
    experiment(__doc__,{'均速估时':Policy(estimate='average'),'分档估时':Policy(estimate='geared')})
