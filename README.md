# Suntae 路径通行分析

Windows x64 与 Linux x64 桌面工具。它以只读方式查询 Suntae 调度后端的订单、任务、命令、下发路径和实际执行路径，并用可编辑车道和叉车矩形包络分析直行、转弯时的通行情况。

## 主要能力

- 订单 ID 精确查询与服务端分页，逐级进入订单 → 任务 → 命令 → 路径。
- 命令下发/实际点位按原始顺序列表展示，保留 Gear、完整命令/点位 JSON，并与画布中心点及车辆框双向联动。
- 同画布显示下发/实际路径，中心线、每点车辆矩形（整车/仅前段/仅后段）、越界标记可分别开关或隔离查看；中心线同时给每个有坐标的位姿点标一个圆点，一个不少；圆点直径按拥挤程度自适应收缩，密集处变小但不消失，稀疏处保持满尺寸。
- 后端坐标按叉车前轴中心解释；车宽、中心前距、中心后距为米，yaw 和地图方向为弧度。
- 车辆尺寸支持全局默认与 VIN 覆盖；缺 yaw 点不猜测方向，只显示中心点并排除相关分析段。
- 鼠标逐点绘制多锚点车道，首次点击后实时预览吸附中心线和完整车道宽度，或从下发/实际路径自动生成；中心线支持直线、真实圆弧和分段三次贝塞尔，开放平头或闭合环，每车道独立总宽。
- 新车道默认尖角；车道默认连接及单锚点覆盖支持尖角/圆角，尖角有 miter 上限。
- 自动车道以当前路径上选定的两个点位为范围：点「按路径生成」后逐个确认两个点位，连接方式可选沿原路径或直线；车道两端各按该点位的车体外伸量自动延伸，保证两端车辆包络在长度方向上都被覆盖。提供半透明预览、最大拟合偏差、弯道模式和圆弧失败回退；确认后仅新增并作为一次撤销操作，圆弧半径可查看和修改。
- 车辆矩形图层为四态：整车、仅前段、仅后段、关，下发与实际各自独立；分界落在前轴中心点上，只影响显示，越界与净距永远按完整车辆包络计算。
- 多个启用车道区域取并集；按位置/yaw 步长连续插值车辆包络，分别给出绿/黄/红结果、最小净距与首次越界位置。
- 手动选择每个转弯的入弯/出弯样本，以整弯等效旋转中心计算前轴中心、前外角、后外角、前内角、后内角五个固定半径；选点时弹出候选列表，含最近样本、其前后各两个样本及命中的重合样本，逐项显示序号、坐标和 yaw；测量可命名、删除、离线保留并在画布定位。
- 画布区分“地图”“通行余量”“场景速算”三个标签页；通行余量针对下发路径给出净距剖面与逐段可行偏置带（共用同一条横轴）、瓶颈排行榜与内嵌的需求道宽三区标尺、优化建议，并可下钻到单个转角做入弯偏置/出弯偏置/起弯点三自由度求解，处理车道倒角半径与路径转弯半径不等时偏置沿弯变化的情况。只给建议，不修改下发路径也不修改车道。
- 通行余量可导出偏置表 CSV 与 A4 横向 PDF 报告；报告与界面共用同一套控件和绘制代码，页眉带齐车型、车道布局、步长与阈值等全部前提，页脚为免责声明。
- 场景速算不连接任何数据源，仅凭车辆参数离线速算典型路口与掉头工况的道路极限尺寸：直角转弯、直角 R 档直行转 D 档、直角 R 档转弯转 D 档、U 型转弯四个场景，各配单/双向与道路中心线/帕累托极限两种计算方向；帕累托极限下可固定任意道路尺寸或路径偏移，其余在其周围求极限，全部固定即为校核。结果是帕累托前沿上的一个点，界面写明解不唯一；未固定横向偏移时，帕累托极限的每一项尺寸都不会大于同参数下的中心线解。
- 场景速算俯视图可按整车／中心前距／中心后距分段查看扫掠包络，两段各有固定颜色并在图例中标明；路径画出行进方向与起点、终点，并可点「模拟运行」让车体包络自起点沿路径跑到终点。整页离线，不写回任何数据；当前变体可导出为一页 A4 横向 PDF 报告，页眉带齐车辆参数与全部固定项。
- 车道编辑支持拖动锚点/控制点或整条车道、输入真实中心线总长和车道总宽、路径点吸附、撤销/重做、手动保存、前版备份、导入仅替换和导出。
- 本地便携存储和结构化轮转日志；打包版使用入口可执行文件同级 `data/` 与 `log/`。

完整操作见 [用户手册](docs/user-guide.md)，接口事实见 [API 契约](docs/api-contract.md)。

## 开发环境

支持两个平台：Windows x64 配 Python 3.12 x64；Linux x86_64 配 Python 3.12–3.14，验证基线为 Ubuntu 26.04（glibc 2.43）。当前验证依赖版本记录在 `requirements*.txt`。PySide6 是 Qt 官方 Python 绑定；几何计算使用 Shapely/GEOS。

Windows 首次准备、源码启动与直接运行：

```powershell
.\scripts\bootstrap.cmd
.\scripts\run.cmd
.\.venv\Scripts\python.exe -m route_analysis
```

Linux 首次准备、源码启动与直接运行：

```bash
./scripts/bootstrap.sh
./scripts/run.sh
./.venv/bin/python -m route_analysis
```

两个平台共用同一个 `.venv/`。`bootstrap.sh` 按 `python3.12`、`python3.13`、`python3.14`、`python3` 顺序取第一个满足 `>=3.12,<3.15` 的解释器并打印实际版本。`run.sh` 转发全部命令行参数；当 `DISPLAY` 与 `WAYLAND_DISPLAY` 都未设置且未指定 `QT_QPA_PLATFORM` 时直接报错并给出提示，不把 Qt 平台插件错误留给使用者。

## 测试与质量检查

Windows：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check route_analysis tests
.\.venv\Scripts\python.exe -m mypy route_analysis
.\.venv\Scripts\python.exe -m route_analysis --smoke-test
```

Linux：

```bash
./.venv/bin/python -m pytest
./.venv/bin/python -m ruff check route_analysis tests
./.venv/bin/python -m mypy route_analysis
./.venv/bin/python -m route_analysis --smoke-test
```

测试默认使用 Qt 离屏平台，不需要打开窗口或连接真实后端。

## 打包

两个平台共用 `route-analysis.spec`，都产出文件夹模式发布物：入口可执行文件与依赖同目录，必须整个目录一起复制；首次正常运行后按需在入口同级创建 `data/` 与 `log/`。

Windows x64：

```powershell
.\scripts\build.cmd
```

发布目录为 `dist\RouteAnalysis\`，入口为 `RouteAnalysis.exe`。验证发布物：

```powershell
.\dist\RouteAnalysis\RouteAnalysis.exe --smoke-test
if ($LASTEXITCODE -ne 0) { throw "smoke test failed" }
```

Linux x64：

```bash
./scripts/build.sh
```

发布目录为 `dist/RouteAnalysis/`，入口为 `RouteAnalysis`。`build.sh` 依次执行架构与 glibc 基线检查、pytest、Ruff、mypy、PyInstaller 构建、ELF 产物校验，然后先用 `QT_QPA_PLATFORM=offscreen` 跑一次打包烟雾，再在检测到显示会话时用真实平台插件跑第二次；没有显示会话时跳过第二次并告警说明平台插件未经验证。最后打印产物文件数、总字节数、入口可执行文件的 SHA-256 和运行基线。

PyInstaller 不做交叉编译：Linux 发布物必须在 Linux 上构建，Windows 发布物必须在 Windows 上构建。本地之外也可在 GitHub Actions 上构建：`build` 工作流（手动触发或推送 `v*` 标签）在 ubuntu-24.04 与 windows-latest 上分别跑同一套构建脚本并上传产物，打标签时自动发布 Release；Actions 构建的 Linux 产物基线为 glibc 2.39（取 runner 的 glibc），兼容面比本地构建更大。Linux 产物动态链接构建机的 glibc，只能在同版本或更新的发行版上启动；当前基线为 x86_64 加 glibc 2.43。构建机没有安装 UPX 时 PyInstaller 会自动跳过压缩，产物体积因此大于 Windows 版。

单独验证 Linux 发布物：

```bash
./dist/RouteAnalysis/RouteAnalysis --smoke-test && echo "smoke test passed"
```

## 运行期数据与安全说明

源码版数据位于仓库 `data/`，日志位于同级 `log/`；打包版位于入口可执行文件同级同名目录：

```text
data/
├── config.json
├── vehicle-profiles.json
├── turn-radius-measurements.json
└── lanes/<server-id>/<mapId>.json
log/
├── route-analysis.log
└── route-analysis.<日期>.<序号>.log
```

根据用户明确选择，`config.json` 中密码明文保存。日志默认 INFO；切换到 DEBUG 后会原样记录密码、令牌、Authorization/tenant 请求头、完整配置、接口参数与响应、路径坐标和导入文件正文，不做脱敏、加密或额外权限控制。日志在每天零点或达到 20 MiB 时轮转，保留最近 30 个历史文件。`data/` 与 `log/` 均被 Git 忽略且不打入发布包。详见 [ADR 0001](docs/adr/0001-store-credentials-in-plaintext.md)、[ADR 0004](docs/adr/0004-record-sensitive-debug-logs.md) 与 [威胁模型](docs/threat-model.md)。

## 只读保证

认证除外，业务客户端只实现以下 GET：

- `/scheduling/order/page`
- `/scheduling/order/detail`
- `/scheduling/order-task/work-flow`
- `/scheduling/order-task/commandStr`
- `/scheduling/order-task/actualPath`

不存在下单、下发、完成、销毁、重传、修改或删除业务数据的方法。
