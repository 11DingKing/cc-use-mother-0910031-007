# 积分结转到期治理

多年度政策并行后，企业账户中同时存在**不同来源、不同可用范围、不同生效期、不同到期日**的积分。
人工扣减常常先用掉长期额度，临近到期的额度反而作废。本服务以**批次（Lot）**为最小账务单元，
按确定性的 **FEFO（First-Expire-First-Out，最早到期优先）** 策略选批扣减，并完整保留
每笔消费在各批次上的分配明细；延期批准、冻结/解冻、退回与政策换版均**只增事件、不改写历史**；
到期处理支持**注入时钟**且**重复运行幂等**。

纯 Python 标准库实现（SQLite 持久化、`http.server` 提供 JSON API），无第三方依赖。

## 领域模型

```
Policy（政策版本，可换版）
  └─ CarryoverRule（结转规则：EXPIRE 到期作废 / CARRYOVER 余额结转）
        │  发放时快照进批次（rule_id/kind/carry_days/carry_scope/max_carry_hops）
        ▼
Lot（批次）：账户 + 来源 + 可用范围 + 生效/到期时刻 + 政策版本 + 规则快照
  │  状态机：ACTIVE ⇄ FROZEN → EXPIRED（作废）/ CARRIED（已结转 → 新批次）
  ▼
Consumption（消费，只增） ── 1:N ── ConsumptionAllocation（按 FEFO 顺序的分配明细，只增）
  └── Refund（退回，只增）：额度回原开放批次；原批已终态则补发独立新批
```

关键规则：

- **确定性扣减顺序（FEFO）**：`expires_at ↑, effective_at ↑, created_at ↑, lot_id ↑`。
  未生效、已到期、冻结批次自动排除；可用范围不匹配的批次不参与。
- **可用范围（scope）**：`*` 为全场景通用；专属批次（如 `SVC_A`）只在对应场景可用，
  通用批可用于任何场景。
- **结转**：批次到期时若规则为 CARRYOVER 且仍有余额，原子生成结转新批（原批 → CARRIED）。
  `max_carry_hops` 控制最多连续结转次数（默认 1：结转一次后再到期作废），避免无限滚存。
- **不可变历史**：消费与分配记录写入后永不更新；延期只改批次当前到期时间、
  分配明细中的 `lot_expires_at_snapshot` / `lot_policy_version_snapshot` 永远是消费时的值。
- **政策换版**：发布新版本后旧版自动 SUPERSEDED，但旧批次继续携带旧规则快照运行，
  新规则只适用于换版后发放的批次。
- **定时到期**：判定时刻由时钟注入（`as_of` 或注入时钟当前值），
  幂等键默认 `JOB:{as_of}`（也可显式给 `job_key`）；单事务状态机 + 条件式更新，
  重复运行/多实例并发都不会重复失效或重复结转。

## 目录

- `domain/contract.json`：领域角色、状态、四大不变量与样例（需求契约）。
- `src/domain_contract/`：契约读取与确定性校验（既有）。
- `src/points_governance/`：服务端实现。
  - `models.py`：政策、结转规则、批次状态机与快照字段。
  - `clock.py`：`Clock` 协议、`SystemClock`、`FixedClock`（可注入、可推进）。
  - `storage.py`：SQLite schema、串行化事务；内存模式单连接加锁，文件模式 WAL + busy_timeout。
  - `repository.py`：行 ↔ 领域对象映射、批次/政策仓储。
  - `service.py`：核心领域服务（发放、FEFO 消费、退回、冻结/解冻、延期、换版、
    到期作业、余额组成、到期预测、事件审计）。
  - `api.py`：零依赖 JSON HTTP API 与完整路由。
  - `scheduler.py`：可选进程内定时调度器（用注入时钟、按日幂等键）。
  - `__main__.py`：命令行入口。
- `tools/check_contract.py`：契约摘要检查。
- `tests/`：45 项回归测试（领域 30+、HTTP API、并发幂等、调度器、契约）。

## 快速开始

```bash
# 启动（文件库，自动建表；--expire-interval 可选启用进程内调度器）
PYTHONPATH=src python3 -m points_governance --port 8080 --db ./data/points.db

# 1) 发布政策（第一条为发放时的默认规则）
curl -s -X POST localhost:8080/admin/policies -H 'Content-Type: application/json' -d '{
  "rules": [
    {"rule_id":"ANNUAL_EXPIRE","kind":"EXPIRE"},
    {"rule_id":"BRIEF_CARRY","kind":"CARRYOVER","carry_days":30}
  ]}'

# 2) 开户、发放两批不同来源/到期日/范围的积分
curl -s -X POST localhost:8080/admin/accounts -d '{"account_id":"ENT-1"}'
curl -s -X POST localhost:8080/accounts/ENT-1/lots -H 'Content-Type: application/json' -d '{
  "amount":100,"source":"2024年度申报返还","scope":"SVC_TAX",
  "effective_at":"2025-01-01T00:00:00Z","expires_at":"2026-03-01T00:00:00Z",
  "rule_id":"BRIEF_CARRY"}'
curl -s -X POST localhost:8080/accounts/ENT-1/lots -H 'Content-Type: application/json' -d '{
  "amount":500,"source":"2026年度活动奖励","expires_at":"2028-01-01T00:00:00Z"}'

# 3) 消费（FEFO 自动选批；先消耗最早到期批次）
curl -s -X POST localhost:8080/accounts/ENT-1/consumptions -d '{"amount":150}'

# 4) 定时到期（注入 as_of；重复调用幂等）
curl -s -X POST localhost:8080/admin/expiration/run -d '{"as_of":"2026-03-02T00:00:00Z"}'
curl -s -X POST localhost:8080/admin/expiration/run -d '{"as_of":"2026-03-02T00:00:00Z"}'

# 5) 余额批次组成 与 未来到期预测
curl -s localhost:8080/accounts/ENT-1/balance
curl -s 'localhost:8080/accounts/ENT-1/forecast?horizon_days=800'
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/accounts` | 开户 |
| POST | `/admin/policies` | 发布政策新版本（旧版自动 SUPERSEDED；可指定 `default_rule_id`） |
| GET  | `/admin/policies` | 政策版本列表 |
| POST | `/admin/expiration/run` | 到期作业（body：`as_of`、`job_key`、可选 `account_id`） |
| GET  | `/admin/expiration/jobs` | 到期作业台账（幂等留痕） |
| POST | `/accounts/{id}/lots` | 发放批次（来源/范围/生效期/到期日/规则） |
| GET  | `/accounts/{id}/lots` | 批次列表 |
| GET  | `/lots/{id}` / `/lots/{id}/extensions` | 批次详情 / 延期审批记录 |
| POST | `/lots/{id}/freeze` · `/unfreeze` · `/extend` | 冻结 / 解冻 / 延期批准 |
| POST | `/accounts/{id}/consume-preview` | 不落库预览 FEFO 分配 |
| POST | `/accounts/{id}/consumptions` | 消费并落分配明细 |
| GET  | `/accounts/{id}/consumptions` · `/consumptions/{id}` | 消费列表 / 详情（含快照、退回） |
| POST | `/consumptions/{id}/refund` | 退回（全额或部分；自动判定回原批/补发） |
| GET  | `/accounts/{id}/balance` | **余额的批次组成**（`?scope=` 按场景过滤） |
| GET  | `/accounts/{id}/forecast` | **未来到期预测**（`?horizon_days=&scope=`，模拟结转链） |
| GET  | `/events` | 追加式事件审计（`?account_id=&lot_id=`） |

## 测试与校验

```bash
python3 -m unittest discover -s tests -v     # 45 项：领域/API/并发/调度器/契约
python3 -m compileall -q src tools tests    # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## 设计备忘

- 金额一律为**整数最小单位**，杜绝浮点误差。
- 时间统一为带时区的 UTC ISO-8601（`Z`）；到期时刻语义为**左闭右开**
  （`expires_at` 时刻起不可消费、进入到期处理）。
- 到期边界场景：冻结批到期照常失效；零余额结转批直接作废；
  解冻时若已过到期时刻则拒绝（必须先走到期处理）。
- 退回语义：只恢复到仍开放的**原批次**；原批次已 EXPIRED/CARRIED 时
  补发一批沿用原范围/规则快照的新额度（默认 30 天有效），既有消费记录不变。
- 生产环境建议由外部调度（cron / CronJob）按日调用到期 API；
  进程内调度器适用于单体部署，幂等键为 `SCHED:{yyyy-mm-dd}`。
