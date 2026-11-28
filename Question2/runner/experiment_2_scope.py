"""对照二：同一首步集合，只评目标包或追踪全批受影响任务。"""
from model.dispatch.policy import Policy
from model.evaluation.compare import experiment

if __name__=='__main__':
    experiment(__doc__,{'目标包':Policy(scope='target'),'全批':Policy(scope='all')})
