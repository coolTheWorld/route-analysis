# Suntae 路径通行分析

Windows x64 桌面工具。它以只读方式查询 Suntae 调度后端的订单、任务、命令、下发路径和实际执行路径，并用可编辑车道和叉车矩形包络分析直行、转弯时的通行情况。

## 主要能力

- 订单 ID 精确查询与服务端分页，逐级进入订单 → 任务 → 命令 → 路径。
- 同画布显示下发/实际路径，中心线、每点车辆矩形、越界标记可分别开关或隔离查看。
- 后端坐标按叉车前轴中心解释；车宽、中心前距、中心后距为米，yaw 和地图方向为弧度。
- 车辆尺寸支持全局默认与 VIN 覆盖；缺 yaw 点不猜测方向，只显示中心点并排除相关分析段。
- 鼠标绘制多锚点车道，或从下发/实际路径自动生成；中心线支持直线、真实圆弧和分段三次贝塞尔，开放平头或闭合环，每车道独立总宽。
- 新车道默认尖角；车道默认连接及单锚点覆盖支持尖角/圆角，尖角有 miter 上限。
- 自动车道提供半透明预览、最大拟合偏差、弯道模式和圆弧失败回退；确认后仅新增并作为一次撤销操作，圆弧半径可查看和修改。
- 多个启用车道区域取并集；按位置/yaw 步长连续插值车辆包络，分别给出绿/黄/红结果、最小净距与首次越界位置。
- 自动或手动选择每个转弯的入弯/出弯样本，以整弯等效旋转中心计算前轴中心、前外角、后外角、前内角、后内角五个固定半径；测量可命名、删除、离线保留并在画布定位。
- 车道编辑支持拖动锚点/控制点或整条车道、输入真实中心线总长和车道总宽、路径点吸附、撤销/重做、手动保存、前版备份、导入仅替换和导出。
- 本地便携存储和结构化轮转日志；打包版使用 EXE 同级 `data/` 与 `log/`。

完整操作见 [用户手册](docs/user-guide.md)，接口事实见 [API 契约](docs/api-contract.md)。

## 开发环境

开发与构建要求 Windows x64 和 Python 3.12 x64。当前验证依赖版本记录在 `requirements*.txt`。PySide6 是 Qt 官方 Python 绑定；几何计算使用 Shapely/GEOS。

首次准备：

```powershell
.\scripts\bootstrap.cmd
```

源码启动：

```powershell
.\scripts\run.cmd
```

也可直接运行：

```powershell
.\.venv\Scripts\python.exe -m route_analysis
```

## 测试与质量检查

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check route_analysis tests
.\.venv\Scripts\python.exe -m mypy route_analysis
.\.venv\Scripts\python.exe -m route_analysis --smoke-test
```

测试默认使用 Qt 离屏平台，不需要打开窗口或连接真实后端。

## Windows x64 打包

```powershell
.\scripts\build.cmd
```

发布目录为 `dist\RouteAnalysis\`，入口为 `RouteAnalysis.exe`。它是文件夹模式发布物，必须连同目录中其他文件一起复制。首次正常运行后会按需创建 `dist\RouteAnalysis\data\` 和 `dist\RouteAnalysis\log\`。

验证发布物：

```powershell
.\dist\RouteAnalysis\RouteAnalysis.exe --smoke-test
if ($LASTEXITCODE -ne 0) { throw "smoke test failed" }
```

## 运行期数据与安全说明

源码版数据位于仓库 `data/`，日志位于同级 `log/`；打包版位于 EXE 同级同名目录：

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
