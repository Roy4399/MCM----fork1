import json
from model.experiments.report import window_comparison


# 作用：在同一多车阻挡实例中比较 graph 与 no_neighbor。参数：无。返回：无，输出窗口规模、扩窗次数、耗时及排程。
def main():
    print(json.dumps(window_comparison(('graph','no_neighbor')),indent=2))


if __name__=='__main__':
    main()
