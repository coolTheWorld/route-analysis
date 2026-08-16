# 调度后端只读 API 契约

## 根地址和请求头

API 根地址由用户填写完整前缀，例如 `http://host/admin-api`。客户端会移除末尾斜杠，不允许根地址包含用户信息、查询参数或片段。

认证后的只读请求携带：

```text
Authorization: Bearer <accessToken>
tenant-id: <tenantId>
```

访问令牌不写入配置或车道文件；正常级别下仅存在于进程内存。按 [ADR 0004](adr/0004-record-sensitive-debug-logs.md)，启用 `DEBUG` 后会将令牌及完整请求/响应内容写入本地日志。

## 通用响应

后端标准包装为：

```json
{
  "code": 0,
  "data": {},
  "msg": ""
}
```

`code = 0` 表示成功。HTTP 401 或 `code = 401` 会触发一次重新登录和一次原 GET 重放；第二次失败直接返回认证错误。

分页数据位于 `data.list`，总数位于 `data.total`。

## 使用的接口

| 功能 | 方法与路径 | 参数 |
| --- | --- | --- |
| 按租户名取 ID | `GET /system/tenant/get-id-by-name` | `name` |
| 登录 | `POST /system/auth/login` | JSON：`username`、`password`；请求头 `tenant-id` |
| 订单分页 | `GET /scheduling/order/page` | `pageNo`、`pageSize`；可选重复参数 `orderIds` |
| 订单任务分页 | `GET /scheduling/order/detail` | `orderId`、`pageNo`、`pageSize` |
| 任务命令 | `GET /scheduling/order-task/work-flow` | `id`（任务 ID） |
| 下发路径 | `GET /scheduling/order-task/commandStr` | `id`（命令 ID） |
| 实际路径 | `GET /scheduling/order-task/actualPath` | `commandId`、`vin` |

除登录所需 POST 外，客户端公开接口不包含任何业务 POST、PUT、PATCH 或 DELETE 方法。

## 路径数据

下发和实际路径接口均返回包含 `commandStr` 的对象。`commandStr` 是 `AgvTaskCommand` JSON 字符串，路径位于：

```json
{
  "positionList": [
    {"x": 1.0, "y": 2.0, "yaw": 0.5, "gear": "D"}
  ]
}
```

- `x`、`y`：米制前轴中心坐标，必须为有限数字。
- `yaw`：弧度，可缺失；缺失时不使用 `roadYaw` 或相邻点推断。
- `gear`：保持后端原始值，不翻译或推断；缺失时界面显示“—”。
- 空 `commandStr` 解析为空路径；契约不合法会报告可恢复的数据错误。

客户端保留解析后的完整 `commandStr` 业务对象和 `positionList` 每个原始项。单个点位的 X/Y/Yaw 无效时，该行仍可在点位表格和 JSON 详情中检查；无效 X/Y 不进入画布或几何分析，有效 X/Y 但无效/缺失 yaw 只作为无朝向中心点进入画布。

注意：前端参考实现的调用处存在 `actualPath` 与 `actualSpeed` 互换现象。本工具依据后端控制器契约固定调用 `/actualPath` 获取实际轨迹。
