# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 学院数据质量评分

校级管理员可按学院比较事件上报的**及时性、重复率、冲突率**。评分口径发布为不可变的规则版本，计算批次固定「观察窗口（按规则时区解释）+ 输入游标（事件 ID 与自增主键）+ 规则快照」，输出各学院的分项分数、证据明细与置信标记（`reliable` / `small_sample` / `empty_sample`）。

- 口径变化只能创建新规则版本，**不能重写已有批次的排名**；
- 迟到数据通过修订版体现：原批次标记为 `superseded` 但分数、排名、签名原样保留，修订版强制沿用原批次规则版本；
- 申诉只在引用的来源事件经核实（存在、归属该学院、落在窗口与游标内）后排除重算，查无实据的引用登记为 rejected；
- 签发对批次内容做 SHA-256 签名，并发签发只有一个事务成功；
- 每个学院结果是独立事务检查点，进程崩溃后重入同一批次会跳过已完成学院继续计算。

接口前缀 `/api/plans/{plan_version}/quality`：

| 类别 | 接口 |
| --- | --- |
| 配置 | `POST /rules`、`POST /rules/{rule_version}/publish`、`GET /rules` |
| 计算 | `POST /batches/{batch_id}/compute`、`GET /batches/{batch_id}`、`GET /batches` |
| 签发 | `POST /batches/{batch_id}/sign` |
| 申诉 | `POST /appeals`、`POST /appeals/{appeal_id}/resolve`、`GET /batches/{batch_id}/appeals` |
| 修订 | `POST /revisions/{new_batch_id}` |
| 趋势 | `GET /trends`（支持 `college_id`、`rule_version`、`include_unsigned` 过滤） |

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。
