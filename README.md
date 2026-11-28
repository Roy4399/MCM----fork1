# 问题一 · 钢包吊运与共轨行车调度

解压后进入 ladle 目录，使用 Python 3.10+。所有命令从该目录执行。

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m runner.experiment_1_baseline
python -m runner.experiment_2_joint
python -m runner.experiment_3_local
```

## 从哪里读代码

| 路径 | 职责与主要接口 |
|---|---|
| model/windows/ | Task 保存计划窗口、取放位置、车辆资格、最晚起吊、工位和释放滞后。chain(legs,lags) 串联工艺段。cast_bookings(casts,tasks) 按固定开浇时间核对同浇次接续，返回处理位预约。 |
| model/map/ | Track 定义每车有向移动边、初位、停车点和相邻间距。fits 检查半开区间容量，safe_trace 检查折点。joint.joint(p,seconds) 在时空图上联合求解。 |
| model/dp/ | solve 可对小实例精确枚举并合并等价状态。local(p,base,car,begin,end,seconds) 固定邻车全程、所有窗外轨迹及任务承诺，重排目标车完全落入窗口的任务。 |
| model/problem.py | Problem 组合任务、轨道、时域、容量及已有预约。Lift 和 Result 记录车辆、起落时刻、轨迹和目标值。 |
| model/baseline.py | baseline(p,seconds) 按最小剩余裕量插单，同裕量优先新增空驶较少者。锁定既有起落时间与车辆，允许重新安排空驶和避让。 |
| model/audit.py | audit(p,result) 从完整任务表和轨迹复算硬约束及指标，失败抛出 ValueError。 |
| model/example.py | 三个 runner 共享的合成输入。 |
| runner/ | 三个独立入口依次为规则基线、联合优化、局部 DP 对照。 |
| tests/test_model.py | 文档手算、非法输入、轨道与容量边界、多车同时分配和算法交叉核验。 |

## 接口与口径

单位由调用方统一。示例一拍为一分钟，每车每拍移动一格。车辆编号从 0 开始。每个任务给定固定取放点与允许车号，可用不同边集限定车辆作业区。不同工位可以共享沿轨坐标，使用不同的资源名区分。窗前占位、固定工艺处理和连铸换中包均可作为 Problem.bookings 输入。处理位与接收位应分别命名，cast_bookings 的返回值须合入 bookings。固定 LF/RH 路线由调用方按工艺顺序拆段。渡车编号及对应坐标由场景给定。示例未读取生产表。

任务就绪为 upstream + alpha，最迟落包为 downstream − beta。source 从 upstream 占用到实际起吊加 release_lag；receiver 从落包占用到 downstream。若下一包已有到位安排，使用同一资源名或已有预约。单独使用 latest_pick 时须预先扣除释放滞后。接收位内可等待时长为最迟落包减实际落包。P95 使用最近秩定义。轨迹包括空车、静止车及避让移动，各拍内匀速，检查所有同步折点即可覆盖拍内的最小间距。

完整模型用时空弧和“任务–车–起吊拍”二元变量建立整数线性约束，以 SciPy/HiGHS 两次求解。先最小化所有任务总待吊，再固定已证最优待吊并最小化移动。若第一阶段限时，只以其待吊为上界，最终保留字典序较好的可行解。局部 DP 在相同拍、位置、任务及起落记录下，仅保留移动较少的状态。比较口径为相同输入、初位、预约和每次 10 秒预算。

optimal 表示该搜索范围内两个目标都已证最优，feasible 表示有完整可行解，infeasible 表示该离散模型已证无解，unknown 表示限时未判定。greedy_blocked 表示基线既有承诺阻止继续插单。异常输入给出 ValueError。部分插单的任务表仅用于定位失败，没有完整轨迹时不计算收益。

## 合成验收

五台行车中前两台执行三项任务，后三台驻车。基线总待吊 6、空驶 3；联合解总待吊 2、空驶 1。局部 DP 固定原方案出窗位置，得到总待吊 2、空驶 3。三者带包均为 5，最小间距均为 2。联合解落包后等待从 8 升至 12，不能据此推断总生产周期缩短。运行入口可看到每项起落点时刻、释放时刻、接收位预约和全部车位轨迹。

此版本面向小窗口与算法核验。轨道为一维有向单位格，重包走直达路径，车辆速度一致。连续时间、车辆异速、大规模生产时窗的性能和实际收益尚未验证。

