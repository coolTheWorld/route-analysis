# Suntae 路径通行分析

Windows x64 桌面工具。它以只读方式查询 Suntae 调度后端的订单、任务、命令、下发路径和实际执行路径，并用可编辑车道和叉车矩形包络分析直行、转弯时的通行情况。

## 主要能力

- 订单 ID 精确查询与服务端分页，逐级进入订单 → 任务 → 命令 → 路径。
- 同画布显示下发/实际路径，中心线、每点车辆矩形、越界标记可分别开关或隔离查看。
- 后端坐标按叉车前轴中心解释；车宽、中心前距、中心后距为米，yaw 和地图方向为弧度。
- 车辆尺寸支持全局默认与 VIN 覆盖；缺 yaw 点不猜测方向，只显示中心点并排除相关分析段。
- 鼠标绘制多锚点车道；每段支持直线或三次贝塞尔，开放平头或闭合环，每车道独立总宽。
- 新车道默认尖角；车道默认连接及单锚点覆盖支持尖角/圆角，尖角有 miter 上限。
- 多个启用车道区域取并集；按位置/yaw 步长连续插值车辆包络，分别给出绿/黄/红结果、最小净距与首次越界位置。
- 车道编辑支持拖动、原始坐标数值输入、路径点吸附、撤销/重做、手动保存、前版备份、导入仅替换和导出。
- 本地便携存储；打包版使用 EXE 同级 `data/`。

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

发布目录为 `dist\RouteAnalysis\`，入口为 `RouteAnalysis.exe`。它是文件夹模式发布物，必须连同目录中其他文件一起复制。首次保存后会自动创建 `dist\RouteAnalysis\data\`。

验证发布物：

```powershell
.\dist\RouteAnalysis\RouteAnalysis.exe --smoke-test
if ($LASTEXITCODE -ne 0) { throw "smoke test failed" }
```

## 运行期数据与安全说明

源码版数据位于仓库 `data/`；打包版位于 EXE 同级 `data/`：

```text
data/
├── config.json
├── vehicle-profiles.json
└── lanes/<server-id>/<mapId>.json
```

根据用户明确选择，`config.json` 中密码明文保存。`data/` 已被 Git 忽略，应用不记录密码、Authorization 或访问令牌。请仅在受信任的 Windows 账号和设备上使用，并限制目录访问权限。详见 [ADR 0001](docs/adr/0001-store-credentials-in-plaintext.md) 与 [威胁模型](docs/threat-model.md)。

## 只读保证

认证除外，业务客户端只实现以下 GET：

- `/scheduling/order/page`
- `/scheduling/order/detail`
- `/scheduling/order-task/work-flow`
- `/scheduling/order-task/commandStr`
- `/scheduling/order-task/actualPath`

不存在下单、下发、完成、销毁、重传、修改或删除业务数据的方法。
