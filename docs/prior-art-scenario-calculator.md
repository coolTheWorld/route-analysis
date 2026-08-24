# 场景速算的同类开源软件调研

## 结论

没有开源项目在做场景速算这件事。**扫掠路径分析**（swept path analysis）作为一个品类在开源侧确实存在，但可用的实现全部是**正向**的：给定道路几何与一条路径，画出扫掠包络、判定是否碰撞；没有一个能反过来"给定净距阈值，求道路的极限尺寸"。唯一做了反解的两个项目各只解**一个**尺寸，且都不是本页这种"多个耦合尺寸同时压到帕累托前沿上"的形态。

最接近的是两个：

- [FabienD/swept-path](https://github.com/FabienD/swept-path) —— 纯参数化、不吃地图、算最紧点净距，并且有一个 `min_road.rs` 对**车行道宽度**做二分反解。这是本次调研中唯一一个"反解道路尺寸"的**工程化**实现。但它只有一个场景（大门/门柱/人行道/车行道），只反解一个尺寸，且判据是"存不存在一条可行轨迹"，不是"最小净距 ≥ 阈值"。
- [CS-Twain/truck-turning-study](https://github.com/CS-Twain/truck-turning-study) —— 一份写死在 90° T 型路口上的一次性研究脚本，对 AASHTO 设计车辆求"转入道的最小宽度随转出道宽度变化"的边界，README 里直接用了 *Pareto frontier* 这个词。语义上与 ADR 0008 撞了个正着，但它是研究产物不是工具，没有 LICENSE 文件。

其余全部落在三档里：CAD/GIS 侧的正向扫掠工具（FreeCAD/QGIS 插件、AutoLISP 脚本）、机器人侧的非完整约束路径基元与自由空间规划器（Dubins/Reeds–Shepp、hybrid A\*、Apollo/Autoware/CommonRoad）、以及需要路网或地图才能跑的交通与驾驶仿真器（SUMO/esmini/CARLA）。第四类 —— 叉车直角堆垛通道宽、装卸月台进深、AGV 巷道净距这类**领域速算器** —— 在开源侧是空的，一个都没有。

本页真正独有的是三条叠在一起的性质：**判据是净距阈值而不是碰不碰**、**同时反解 2–3 个互相耦合的尺寸**、**机动是罐装的所以反解才算得动**。第三条既是能力也是代价，下文说清楚。

## 逐类核对

### 一、扫掠路径分析：品类存在，反解不存在

这一类的商业在位者是 Transoft AutoTURN 与 Autodesk Vehicle Tracking，都不开源，不在本文讨论范围。开源侧的完整清单如下。

**[FabienD/swept-path](https://github.com/FabienD/swept-path)**（Rust + WebAssembly + TypeScript）值得单独说。仓库建于 2026-08-08，84 次提交，最后推送 2026-08-16，2 star —— 很新，谈不上成熟，但工程质量是这批里最高的。它回答的问题是"这辆车能不能开进这个大门，要揉几把"。读 `crates/swept-core/src/scene/mod.rs` 的 `Scene` 结构：左右门柱各自独立放置、墙厚、人行道宽、路缘开口宽、**车行道宽**、路缘高度、平移门还是双开门（含门扇长/厚/铰链偏移/开启角）—— 全部是标量参数，不吃地图、不吃 CAD 图。这一点和本页一致。

它的反解在 `crates/swept-solver/src/min_road.rs`（已读源码）：对 `scene.road_width` 在 0.1 m–16 m 上做 12 次二分，谓词是"穷举搜索能否找到一次进门的轨迹"。注释里把上界和步数都标成 `ARBITRARY — carried over from the prototype`，这种把凑出来的常数明标出来的做法值得抄。但它与本页的反解有三处实质差别：只解一个尺寸，所以不存在帕累托前沿的歧义；谓词是"有没有解"而不是"最小净距够不够"，用户给不了阈值 —— 读 `crates/swept-solver/src/solve.rs` 可见净距（`min_clearance`）只用来在多个可行解之间**排序**（"最宽敞的那条"），从不作为可行性门槛；以及它**搜索轨迹**而本页**罐装机动**，见下。

车辆模型（读 `crates/swept-core/src/vehicle.rs`）以后轴中心为原点，含轴距、前后悬、车体宽、**后视镜宽**、**离地间隙**、后轴枢转半径，并附一个把厂商公布的转弯直径换算成后轴枢转半径的函数 `pivot_radius_from_curb`。碰撞判定（读 `crates/swept-core/src/clearance.rs`）分三条：车体轮廓对"过不去的"障碍、四个轮胎接地点对**全部**障碍（车身可以悬过路缘、轮胎不行）、以及障碍角点对车体矩形的反向测试 —— 最后这条与本项目 `scenario_solver._vertex_penetration` 解决的是同一个问题（四角都在内、车身却横跨凹口）。高度维度和后视镜是本页完全没有的。

许可证分两层，这一层很关键：仓库根 `LICENSE` 是 **AGPL-3.0-only**（已读文件首行），覆盖 `swept-solver`、`swept-wasm` 和 Web 界面；而 `crates/swept-core/Cargo.toml` 声明 `license = "MIT OR Apache-2.0"`，仓库里也确实并存 `LICENSE-MIT` 与 `LICENSE-APACHE`（已读）。也就是说几何与曲线闭式（Dubins、Reeds–Shepp、净距场）可以拿，求解器不能拿。

**[CS-Twain/truck-turning-study](https://github.com/CS-Twain/truck-turning-study)** 是一份 2026-06-05 建库当天推完的一次性研究，0 star，`GET /repos` 返回 `license: None`，仓库里也没有 LICENSE 文件（已核对文件树）。读 `truck_frontier.py`：前轴走直线—圆弧—直线，牵引轴与挂车轴各用一次离散**拽物线**（tractrix）跟随，车辆表里写死了 AASHTO 的 P / SU-30 / WB-40 / WB-50 / WB-62 / WB-67 六种设计车辆及其最小设计转弯半径；对每个 `W_stem` 求使车体全程留在砂石面上的最小 `W_thru`，并对转弯半径与行车线做优化。判据是**包含性**（车体角点在多边形内），没有净距阈值。它证明了"反解最小道宽"这个提法在公路侧不是新想法，但它交付的是一张表和一份 PDF，不是可复用的东西。

**[joelgraff/freecad.turns](https://github.com/joelgraff/freecad.turns)**，LGPL-2.1（已读 LICENSE 首行），FreeCAD 工作台，README 自述状态 Alpha 且"Documentation currently unavailable"，最后一次提交 2020-12-14（已核对提交列表）—— 事实上已停更五年多。文件树里有 `model/{vehicle,body,axis,wheel,path,path_segment,analyzer}.py` 和 `trackers/project/envelope_tracker.py`，形态是"在 FreeCAD 里画一条路径、拖车辆、看包络"，纯正向。依赖 FreeCAD 本体加 `pivy_trackers` 子模块。

**[lugafner/QgisSweptPath](https://github.com/lugafner/QgisSweptPath)**，GPL-3.0，QGIS 插件，建于 2024-12-13，最后推送 2026-06-30，1 star。README 明说定位是"粗略可行性研究，不追求专业 CAD 扫掠模拟软件的精度与分析深度"；已有功能是**手动操控车辆**、把驶过与扫掠区域作为 QGIS 图层（线）输出、内置一辆标准公交与一辆铰接公交；"沿事先画好的线自动行驶"仍在计划中。输入是 QGIS 工程与图层，即需要地图。README 也承认尚未发布到官方插件站。

顺带一条硬性的负面证据：把 QGIS 官方插件仓库的全量清单拉下来（`https://plugins.qgis.org/plugins/plugins.xml?qgis=3.40`，7.9 MB，3814 个 `<pyqgis_plugin>` 条目）后逐字检索，`swept` / `schlepp` / `turning path` / `turning circle` / `turning radius` **零命中**；仅有的 4 处 "vehicle track" 全部来自 GTFS 公交实时追踪与农机导航插件。QGIS 生态里没有扫掠路径插件。

**[RFLTools/QuickTurn2](https://github.com/RFLTools/QuickTurn2)**，AutoLISP，5 star，最后推送 2020-06-01，`license: None`。README 自述把路径切成短直线段、逐段按前一段的相对角推进车辆姿态，并按铰接点递推挂车 —— 正向轨迹生成，无净距、无区域。需要 AutoCAD。

**[brombirium/schlepp](https://github.com/brombirium/schlepp)**，MIT，Python 3.11+。TOML 进、SVG 出：`[vehicle]` 给轴距/前悬/后悬/车宽，可选 `[vehicle.trailer]` 给挂车与 `hitch_offset`，`[[path]]` 逐段给机动。参考点是后轴中心。纯正向渲染，不做碰撞也不做净距。这是这一批里唯一一个"轻量、Python、许可证干净"的，但它缺的正是本页的全部内容。

**[hojoonson/automatic_swept_path_analysis](https://github.com/hojoonson/automatic_swept_path_analysis)**，MIT，配套 IEEE TITS 论文（*Feasibility Evaluation of Oversize Load Transportation Using Conditional Rewarded Deep Q-Networks*）。用 DQN 自动打标 + CNN 做"这段路超限运输能否通过"的二分类。方向仍是正向判定，且需要 Docker 与训练流程，不是速算器。

其余命中的仓库均为 0–1 star 的个人一次性作品，无 LICENSE 文件，不构成可用前案，但其中两个的**问题设定**值得记一笔：
[oedipa-lunor/turning-envelope-simulator](https://github.com/oedipa-lunor/turning-envelope-simulator) 是一个日文的 T 字路旋回模拟器，读 `docs/spec.md` 可见它用前轮转向的运动学自行车模型、带一个"运転技能マージン"（相当于本页的净距阈值，做法是把车体外廓外扩后再判碰），求的是**可行起始位置的区域**（開始可能領域）—— 道宽 A、道宽 B 仍是输入。也就是说它把本页的"偏移优化"做成了主结果，而把道宽留在正向侧；
[YohShinK/truck-turn-volume](https://github.com/YohShinK/truck-turn-volume) 在三维里用街角三棱柱减去扫掠体求残余体积，方向也是正向。
[Holosim/SPA-CGAL](https://github.com/Holosim/SPA-CGAL)（GPL-3.0，C++/CGAL）README 为空，**能力无法从一手材料核实**，此处只记存在。

HawsEDC 的 TURN.LSP / TURNPLUS.LSP 常被列为 AutoCAD 上的自由软件转弯模板工具，但其官网 `hawsedc.com/gnu/turn.php` 在本次调研期间持续返回 HTTP 500，**一手来源不可达，未作核实**，本文不对其能力作任何陈述。

### 二、非完整约束基元与自由空间规划：全部正向，且解的是别的问题

这一族在开源侧极其成熟，但它们的输入输出方向与本页相反：给定**已知的**可行空间（栅格地图、多边形障碍、车道网络），求一条满足最小转弯半径与前进/倒车切换的轨迹。没有任何一个把"空间的尺寸"当作未知量。

[AtsushiSakai/PythonRobotics](https://github.com/AtsushiSakai/PythonRobotics)（MIT，已读 LICENSE 首行；文件树中确有 `PathPlanning/DubinsPath/`、`PathPlanning/ReedsSheppPath/`、`PathPlanning/HybridAStar/`）是最方便取用的参考实现，纯 Python，闭式部分可以直接读懂并移植。
[ApolloAuto/apollo](https://github.com/ApolloAuto/apollo)（Apache-2.0）的开放空间规划位于 `modules/planning/planning_open_space/{coarse_trajectory_generator, trajectory_smoother}`（已通过仓库内容 API 列目录核实），是 hybrid A\* 打粗解再做数值平滑的两段式。
[autowarefoundation/autoware_universe](https://github.com/autowarefoundation/autoware_universe)（Apache-2.0）对应 `planning/autoware_freespace_planner` 与 `planning/autoware_freespace_planning_algorithms`（同上已列目录核实）。
[CommonRoad/commonroad-drivability-checker](https://github.com/CommonRoad/commonroad-drivability-checker)（BSD-3-Clause）按 README 是"把碰撞规避、运动学可行性、道路合规三类校验统一起来"的工具箱，Python 3.10 + C++17，与 CommonRoad benchmark 场景文件配套；从源码构建需要 Eigen3、Boost、Box2D、FCL、libccd、GPC。它是**校验器**不是求解器，且需要场景文件作输入。

这一族里唯一与本页有真实交集的，是它们的**轨迹搜索**能力。本页的机动是罐装的：直线—圆弧—直线，或倒—进两段弧，横向偏移与起弯点是仅有的自由度。这是反解在算力上成立的前提（`solve_forward` 最坏情况已接近一万次求值），但也意味着本页**永远回答不了"揉两把能不能过"**。Reeds–Shepp 曲线族与 hybrid A\* 正是回答那个问题的工具，而 `swept-path` 已经把这两者与净距最大化拼在一起了。

### 三、交通与驾驶仿真器：需要路网，且不建模车体扫掠

[eclipse-sumo/sumo](https://github.com/eclipse-sumo/sumo)（EPL-2.0）按官方文档是"微观、连续的交通仿真包"，主输入是路网（可从 OSM/VISUM/Vissim/OpenDRIVE/MATsim 导入或用 netedit 建），文档中没有车体几何、扫掠路径或道路尺寸求解的内容，最接近的 Sublane Model 处理的是车道内横向定位而非车体几何（<https://sumo.dlr.de/docs/index.html>）。
[esmini/esmini](https://github.com/esmini/esmini)（MPL-2.0）README 自述是"基本的 OpenSCENARIO XML 播放器"，其 RoadManager 面向 OpenDRIVE 路网 —— 即场景与路网都必须先存在。
[carla-simulator/carla](https://github.com/carla-simulator/carla)（MIT）README 自述是自动驾驶研究用的仿真器，随附城市布局、建筑与车辆的数字资产。三者都是"路网在先、车辆在后"，与本页"路网尺寸就是未知量"正好反过来。

### 四、领域速算器：开源侧是空的

这一族本该是真正的同类 —— 叉车直角堆垛通道宽、装卸月台进深、AGV/AMR 巷道与转弯净距、仓库布局工具 —— 结果一个都没有。

GitHub 仓库检索 `forklift`（1969 命中）返回的全部是同名的运维/ETL/迁移工具，以及 ROS/Isaac Sim 里的叉车控制与强化学习仿真，没有一个做几何速算。`warehouse layout optimization`（53 命中）全部是货位分配、拣选路径、遗传算法/模拟退火的**运筹**类项目，处理的是"东西放哪儿"，不是"通道要多宽"。`aisle width warehouse`、`minimum aisle`、`required lane width`、`turning space required` 四条检索的有效命中为零；`minimum road width` 的唯一相关命中就是上面那份 truck-turning-study。

[open-rmf/rmf](https://github.com/open-rmf/rmf)（Apache-2.0）与 [open-rmf/rmf_traffic](https://github.com/open-rmf/rmf_traffic) 按 README 做的是"多智能体移动机器人交通的调度与协商"，需要建筑地图，解决的是多机冲突消解而非单机对墙的净距，不属于本族。

行业实践里，叉车直角堆垛通道宽是有闭式公式的（大意为最小转弯半径 + 前悬距 + 货叉/托盘长度 + 安全距离），其规范归属应在 VDI 2198 / ISO 3691 一系的整车参数表（`Ast` 字段）中。但本次检索中该公式只出现在二手中文行业文章与厂商页面上，**未能从任何一手规范文本核实**，因此本文只记录"存在这样一条经验闭式"，不引用其具体形式作为依据。需要时应查规范原文。这条闭式即便成立，也只覆盖直角堆垛一种工况，且不含本页的净距阈值与偏移自由度。

### 五、规范衍生几何：无开源实现

除 truck-turning-study 把 AASHTO 六种设计车辆的尺寸与最小设计转弯半径硬编码进脚本外（已读 `truck_frontier.py` 的 `VEHICLES` 字典），没有找到 AASHTO 转弯模板的开源实现。中文规范一侧更彻底：GitHub 上 `GB50647 厂矿道路`、`JGJ100 车库`、`车库 坡道 转弯 计算`、`转弯半径 计算` 四条检索**全部零命中**；放宽到单词 `转弯半径`、`扫掠`、`叉车`、`车库设计` 后返回的是 hybrid A\* 规划器、农机阿克曼底盘、CAD 扫掠体建模、以及大量无关仓库。GitLab 上检索 `swept path` / `Schleppkurve` / `turning envelope` 同样无有效项目。

## 对比表

| 项目 | 许可证 | 维护状态 | 形态 / 运行时 | 计算方向 | 输入 | 车辆模型 | 净距阈值 |
|---|---|---|---|---|---|---|---|
| **本页 场景速算** | Apache-2.0 | 在建 | PySide6 页面 + 纯几何模块 | **反解 2–3 个耦合尺寸至帕累托点** | 纯参数 | 矩形车体、前/后悬、档位对调悬距 | **用户给定，是可行性判据** |
| FabienD/swept-path | 应用 AGPL-3.0-only；`swept-core` MIT OR Apache-2.0 | 活跃（2026-08 新建） | Rust→WASM + TS 网页 | 正向为主，**单尺寸（车行道宽）二分反解** | 纯参数 | 自行车模型、前后悬、后视镜宽、离地间隙、轮胎接地点 | 无阈值；净距用于排序 |
| CS-Twain/truck-turning-study | **无 LICENSE 文件** | 一次性（2026-06 单日） | Python 脚本 + PDF | **单尺寸反解**（写死场景） | 纯参数（场景硬编码） | 拽物线牵引 + 挂车，AASHTO 设计车辆 | 无（判据为包含性） |
| joelgraff/freecad.turns | LGPL-2.1 | 停更（2020-12） | FreeCAD 工作台 | 正向 | FreeCAD 文档中的路径 | 车体/轴/轮分层模型 | 未见 |
| lugafner/QgisSweptPath | GPL-3.0 | 活跃（2026-06） | QGIS 插件 | 正向（手动驾驶） | QGIS 图层（地图） | 标准公交 / 铰接公交 | 无 |
| brombirium/schlepp | MIT | 活跃（2026-06） | Python CLI，SVG 出图 | 正向 | TOML 参数 | 牵引 + 挂车、前后悬 | 无（不做碰撞） |
| RFLTools/QuickTurn2 | 无 LICENSE 文件 | 停更（2020-06） | AutoLISP / AutoCAD | 正向 | CAD 图中的路径 | 分段递推 + 挂车 | 无 |
| hojoonson/automatic_swept_path_analysis | MIT | 停更（2025-03） | Docker + 训练脚本 | 正向（学习式二分类） | 生成的道路参数 | 超限运输车辆 | 无 |
| PythonRobotics | MIT | 活跃 | Python 库 | 正向（轨迹搜索） | 障碍/栅格 | Dubins / Reeds–Shepp / hybrid A\* | 无 |
| Apollo 开放空间规划 | Apache-2.0 | 活跃 | C++ / Cyber RT | 正向（轨迹搜索） | 地图 + 感知 | hybrid A\* + 数值平滑 | 无 |
| Autoware freespace planner | Apache-2.0 | 活跃 | C++ / ROS 2 | 正向（轨迹搜索） | 代价地图 | hybrid A\* / RRT\* | 无 |
| CommonRoad drivability checker | BSD-3-Clause | 活跃 | Python + C++17 | 正向（校验） | CommonRoad 场景文件 | 碰撞体 / 运动学可行性 | 有碰撞判定，无尺寸反解 |
| SUMO / esmini / CARLA | EPL-2.0 / MPL-2.0 / MIT | 活跃 | 仿真器 | 正向（仿真） | 路网（OSM/OpenDRIVE）+ 场景 | 交通流 / 场景执行 | 不建模车体扫掠净距 |

## 值得借鉴、值得跟踪，以及诚实的短板

**能直接拿的，只有 `swept-core` 一处，而且要付出代价。** 本仓库是 Apache-2.0（已读 LICENSE）。AGPL-3.0-only 的 `swept-solver` 与 Web 界面不能并入；GPL-3.0 的 QgisSweptPath 同理；LGPL-2.1 的 freecad.turns 理论上可动态链接，但它是 FreeCAD 工作台，形态上不可能嵌进来。剩下许可证干净的是 `swept-core`（MIT OR Apache-2.0）、PythonRobotics（MIT）、schlepp（MIT）、CommonRoad DC（BSD-3-Clause）。其中 `swept-core` 是 Rust，要用就得引入 cargo 工具链与 PyO3/WASM 边界，对一个靠 PyInstaller 打包、依赖只有 numpy/PySide6/requests/shapely 的桌面应用是净负担；CommonRoad DC 从源码构建要拖进 Eigen3/Boost/Box2D/FCL/libccd/GPC，更不必谈。**结论是不引入任何一个**，需要时读源码移植闭式公式即可 —— Dubins 与 Reeds–Shepp 的闭式在 PythonRobotics 里是纯 Python，移植成本几乎为零。

**值得抄的三条做法。** 其一是 `swept-path` 把"证明出来的"与"搜索没搜到"分开报告（`Confidence` 随每个结果一起返回，穷举失败意味着"这张网格上不存在"，启发式失败什么也不意味着）。本页 `ForwardSolution` 在无解时只给 `ceiling_clearance`，含义是"阈值要降到多少才有答案"，方向是对的，但没有把"粗扫没扫到"与"真的不可行"区分开 —— `_minimal_feasible` 的粗扫网格只有 11 个点，扫空返回 `None`，这时候报出去的"不可行"其实弱于它听起来的样子。其二是把凑出来的常数明标为 `ARBITRARY` 并写清出处；本项目的 `CAP_PAD`、`SCAN_SAMPLES`、`BISECTION_STEPS` 一类可以照办。其三是 `data/vehicles.json` 里逐字段记录数据来源（实测 / 推导 / 估计），理由是"三厘米的误差就会翻转结论"—— 这条对本页的默认车辆参数同样成立。

**值得跟踪的只有一个仓库。** `FabienD/swept-path` 是本领域里唯一一个把"参数化场景 + 净距 + 反解"三件事凑齐的活跃项目，虽然目前只有 2 star、建库两周。如果它把场景从"一个大门"泛化成"一族路口"，就会变成真正的同类。

**诚实地说，本页并不比它们强，只是更窄。** 反解之所以算得动，全靠机动是罐装的 —— 一旦机动本身要搜索，`solve_forward` 那近万次求值就会再乘一个规划器的开销。`swept-path` 的穷举 Dubins 扫描 + hybrid A\* 多次揉库、`schlepp` 与 truck-turning-study 的牵引车—挂车拽物线、`swept-core` 的高度维度与后视镜宽、CommonRoad 的运动学可行性校验，本页一样也没有。本页的车辆是一个刚性矩形，档位只是前后悬对调，没有任何动力学。这些取舍写在 ADR 0008 里是自洽的，但不该被读成"我们做得更全"。

## 查空了的检索

以下检索均已实际执行，列出来是为了让"没有同类"这个结论可复核。

- GitHub 仓库检索（`GET /search/repositories`）：`swept path`（23 命中，逐个核对完毕）、`vehicle swept path analysis`、`swept path vehicle turning`、`vehicle turning envelope`、`turning radius vehicle`、`turning circle calculator`、`turning template AASHTO design vehicle`（0）、`schlepp`/`Schleppkurve`（0）、`minimum road width`、`required lane width`、`minimum aisle`、`turning space required`、`aisle width warehouse`（0）、`forklift`、`loading dock`、`warehouse layout optimization`、`parking layout geometry`、`roundabout design open source`、`blender addon vehicle`。
- 中文检索：`转弯半径`、`扫掠`、`叉车`、`车库设计`、`GB50647 厂矿道路`（0）、`JGJ100 车库`（0）、`车库 坡道 转弯 计算`（0）、`转弯半径 计算`（0）。网页侧另检索了「叉车 直角堆垛通道宽度 计算 开源 程序」与「开源 扫掠路径 转弯包络 车辆 计算 github」，返回的全部是行业科普文章与 VRP 车辆路径规划项目，无一相关。
- GitLab 项目检索（`GET /api/v4/projects?search=`）：`swept path`（唯一命中是一个空的营销页仓库）、`Schleppkurve`（0）、`turning envelope`（0）。
- QGIS 官方插件仓库全量清单（3814 个插件）逐字检索 `swept` / `schlepp` / `turning path` / `turning circle` / `turning radius`：全部零命中。
- Blender 插件方向：`blender addon vehicle` 的 10 个命中全部是游戏资产、渲染与建模工具，无扫掠路径工具。

一手来源不可达、因而未作任何能力陈述的：HawsEDC 的 TURN.LSP / TURNPLUS.LSP（`hawsedc.com/gnu/turn.php` 持续 HTTP 500，Wayback 快照本环境无法抓取）。README 为空、能力无法核实的：Holosim/SPA-CGAL。
