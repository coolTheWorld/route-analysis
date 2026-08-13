# ADR 0002：使用 PySide6 与 Shapely 构建桌面分析工具

- 状态：已接受
- 日期：2026-08-13

## 背景

工具需要 Windows 原生桌面交互、复杂的米制二维画布、可拖动锚点与贝塞尔控制点、后台网络请求，以及车体矩形与多车道并集之间的包含/净距运算。手写多边形偏移、连接样式、并集和拓扑判断会显著增加几何错误风险。

## 决策

- 使用 Python 3.12 作为开发和 Windows x64 打包基线。
- 使用 PySide6 6.11 系列提供 Qt Widgets、Graphics View、撤销栈和线程池。
- 使用 Shapely 2.1 系列提供 GEOS 支持的缓冲、并集、覆盖和距离运算。
- 使用 Requests 2.34 系列实现边界清晰的同步 HTTP 客户端，由 Qt 线程池承载网络调用。
- 使用 PyInstaller 6.22 的文件夹模式交付便携 Windows x64 版本。

依赖选择依据：

- PySide6 是 Qt 官方 Python 模块，当前 Windows x86-64 轮子支持 Python 3.10 及以上：https://pypi.org/project/PySide6/
- Shapely 2.1 基于 GEOS，适合笛卡尔平面几何操作且支持 Python 3.10 及以上：https://pypi.org/project/shapely/
- Requests 当前版本支持 Python 3.10 及以上：https://pypi.org/project/requests/
- PyInstaller 6.22 提供 Windows x86-64 构建并支持 Python 3.8–3.15：https://pypi.org/project/pyinstaller/

## 后果

- 车道宽度、端部和连接样式由成熟几何内核实现，核心计算可在无 GUI 环境下单元测试。
- PySide6 发布物体积较大，但文件夹模式便于诊断和携带本地 `data/`。
- Shapely 的缓冲/并集是二维平面运算；本工具不处理坡度或三维车体。
- 连续扫掠仍采用可配置步长采样，不能表述为解析连续碰撞证明。
