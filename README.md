# 积分结转到期治理

本项目维护积分结转到期治理的领域约定、角色边界与样例数据，并提供完整的 Python 服务端实现。当前契约覆盖企业申报员、核算专员、交易运营员、监管审计员，并明确批次化余额、确定性扣减顺序、可注入到期时钟、消费分配追溯等关键约束。

多年度政策并行时，企业账户中同时存在不同来源和到期日的积分。服务端为每批积分记录来源、可用范围、生效期和结转规则；消费时按确定策略（先到期先扣）选择批次并保留分配明细；延期批准、冻结、解冻、退回和政策换版均不修改既有消费；定时到期使用可注入时钟且重复运行不重复失效。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/points_governance/`：服务端实现（领域模型、扣减策略、应用服务、HTTP API）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、策略、服务行为与 API 端到端测试。

## 服务端设计

### 领域模型（`src/points_governance/models.py`）

- `PolicyVersion`：政策版本。政策换版只新增版本，不影响既有批次与消费。
- `PointBatch`：积分批次，记录来源（`GrantSource`）、可用范围（`scopes`，空集为通用）、
  生效期（`effective_from` / `expires_at`）与结转规则快照（`CarryoverRule`）。
  延期通过 `extension_days` 累计表达，原始到期时刻永不改写。
- `Consumption` / `Refund`：消费与退回记录，创建后不可修改（frozen dataclass），
  批次分配明细（`Allocation`）随记录持久化。
- `LedgerEntry`：只增账本，全部余额变化的审计流。

### 确定性扣减（`src/points_governance/strategy.py`）

消费按 **先到期先扣（FEFO）** 分配：到期时刻升序 → 生效时刻升序 → 批次编号字典序。
排序键只取决于批次自身字段，与遍历顺序无关；未生效、已到期、范围不匹配、
冻结部分的额度均不参与扣减；额度不足时不产生任何部分扣减。

### 定时到期与幂等（`PointsService.run_expiration`）

批次状态单向流转 `ACTIVE → EXPIRED`，同一批次不会被重复失效、重复结转；
重复运行返回空报告。到期剩余额按批次结转规则快照生成结转批次
（上限千分比、结转后有效期），结转批次自任务运行时刻起算有效期，
任务迟到也不会"出生即过期"。所有时间读取经过注入的 `Clock`
（`SystemClock` / `MutableClock`），任务可用任意时刻重放。

### 不变量

- 延期批准、冻结、解冻、退回、政策换版都只追加新记录，既有消费永不修改；
- 退回按原消费分配比例分摊（向下取整、余数按原顺序补 1，结果确定），
  原批次已失效时为该部分生成补偿批次（`COMPENSATION`）；
- 消费接口支持 `request_id` 幂等键，重复提交不重复扣减。

## HTTP API

启动：`PYTHONPATH=src .venv/bin/python -m points_governance`（默认 0.0.0.0:8000）。

| 方法与路径 | 说明 |
| --- | --- |
| `POST /policies` | 发布政策版本（政策换版） |
| `GET /policies` | 政策版本列表 |
| `POST /accounts/{id}/grants` | 发放批次（来源/范围/生效期/结转规则） |
| `GET /accounts/{id}/batches` | 批次列表 |
| `POST /accounts/{id}/consumptions` | 消费（确定性分配，返回分配明细） |
| `GET /accounts/{id}/consumptions`、`GET /consumptions/{id}` | 消费追溯 |
| `POST /consumptions/{id}/refunds` | 退回（不修改原消费） |
| `POST /batches/{id}/freezes`、`/unfreezes` | 冻结 / 解冻 |
| `POST /batches/{id}/extensions` | 延期批准 |
| `POST /jobs/expire` | 运行到期任务（可传 `now` 注入时刻） |
| `GET /accounts/{id}/balance` | 余额及批次组成 |
| `GET /accounts/{id}/expiry-forecast` | 未来到期预测（按日分桶） |
| `GET /accounts/{id}/ledger` | 账本审计流 |

错误映射：404 对象不存在；409 积分不足 / 状态冲突；400 业务校验；422 请求体校验。

## 验证

依赖安装：`python3 -m venv .venv && .venv/bin/pip install fastapi "uvicorn[standard]" httpx`

测试命令：`.venv/bin/python -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
