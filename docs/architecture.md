# 架构说明

## 设计目标

核心几何和存储不依赖 Qt，使车辆/车道算法能够在无窗口环境中验证。UI 只编排查询、选择、编辑和呈现；后端差异集中在一个只读客户端，避免 HTTP 字段假设散落于组件。

## 模块边界

```text
app / main_window
├── settings_dialog ─────────────┐
├── api_client ── parsing        │
├── canvas / control_panel       │
│   └── geometry                 │
├── analysis ─── geometry        │
├── storage ──── models          │
└── workers                      │
                                ▼
                           runtime data/
```

- `models.py`：不可变路径姿态、车辆参数、分析结果，以及可编辑车道对象。
- `geometry.py`：车辆矩形、yaw 最短角插值、贝塞尔离散、车道缓冲与多车道并集。
- `analysis.py`：对路径做位置/yaw 双约束采样并输出净距与越界结果。
- `api_client.py`：唯一 HTTP 边界，只暴露必要查询；负责认证、单次重登、分页和响应校验。
- `parsing.py`：解析 `AgvTaskCommand.positionList`，明确不以 `roadYaw` 补全 yaw。
- `storage.py`：版本化 JSON、服务器/mapId 隔离、原子替换、备份和导入校验。
- `canvas.py`：米制 QGraphicsView、路径/车体/车道绘制、方向变换、鼠标编辑和撤销栈。
- `control_panel.py`：图层开关、车道属性、锚点/贝塞尔数值编辑和结果摘要。
- `main_window.py`：层级导航、后台查询、车道上下文切换、脏状态协议和后台分析。
- `workers.py`：线程池适配器，只向 UI 传递用户可恢复错误，不泄露调用栈。

## 坐标变换

领域和存储始终使用后端原始坐标 `(x, y)`。显示方向 `θ` 仅在画布边界应用：

```text
x_display = x·cos(θ) - y·sin(θ)
y_display = x·sin(θ) + y·cos(θ)
```

鼠标编辑先用逆旋转还原原始坐标再写入模型。因此改变地图方向不会改写路径或车道 JSON。

## 车辆包络

路径点为前轴中心。车辆局部顶点为：

```text
(+中心前距, ±车宽/2)
(-中心后距, ±车宽/2)
```

顶点按 yaw 绕前轴中心旋转和平移。缺 yaw 不构造矩形。

## 车道区域

- 每段中心线独立产生半径为“总宽/2”的平头缓冲。
- 三次贝塞尔按容差自适应离散。
- 内部锚点按车道默认或锚点覆盖加入圆角/尖角连接区域。
- GEOS miter limit 截平过长尖角。
- 开放首尾不加端帽；闭合车道所有锚点均作为连接点。
- 所有启用车道区域使用并集，通行判断不绑定某一条路径或车道。

## 连续扫掠近似

每对 yaw 完整的相邻路径点按如下样本段数插值：

```text
max(ceil(位置距离 / 位置步长), ceil(|最短 yaw 差| / yaw 步长), 1)
```

每个姿态检查 `traversable_area.covers(vehicle_polygon)`。完全覆盖时净距为车体边界到区域边界的最小距离；不覆盖时报告负值并记录越界。缺 yaw 点及其相邻段跳过并标记数据不完整。

这是可配置分辨率的数值近似，不是解析连续碰撞证明。

## 并发模型

- HTTP 查询使用 2 线程 Qt 线程池。
- 几何分析使用独立单线程池，避免多次修改同时占用 CPU。
- 每次分析带代数编号，过期结果不覆盖新状态。
- UI 和 QGraphicsScene 只在主线程修改。

## 数据一致性

- 配置和车道先写同目录临时文件，再用 `os.replace` 原子替换。
- 覆盖保存前复制 `.bak`。
- 车道上下文键为规范化 API 根地址 SHA-256 前 16 位加 mapId。
- 导入文件必须声明 schema 1、米和弧度；导入布局只整体替换，不合并。
