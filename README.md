# 快捷班列冷链衔接

客车化快捷班列把电商冷链生鲜按固定时刻送到站，当前序公路晚到、温控异常、临时换编
同时发生时，"上本班 / 转下一班 / 就地处置"不能靠电话拍板。本服务依据班次时刻、装卸
截止、货品耐受窗口、传感器质量、车厢能力和目的站接驳，在截关前给出**可执行装载清单**，
并在货损争议中给出**逐段时间线与贡献说明**。

## 决策规则

对每个候选批次，按 `as_of`（规划现场时刻）做点-in-time 快照，依次判定：

1. **隔离优先**：存在未解除隔离（`HARD_BREACH` / `EXPOSURE` / `GAP` /
   `SENSOR_UNVERIFIED`）→ `HOLD`。温度补报**永不自动覆盖**已触发隔离，只有
   `quarantine_dispose` 人工处置（放行 / 报废 / 退回，须责任人、原因、单据）能终结。
2. **到站与截关**：无 ETA/到站 → `NOT_ARRIVED`；到站 + 最短场内操作时间晚于装载截关
   → `LATE_FOR_CUTOFF`。
3. **温控可证实性**：校准失效/故障传感器读数不可采信；校准临期（SUSPECT）读数可触发
   拦截但不能证明合规；可采信非可疑读数阶梯积分计算超窗暴露，超过货品耐受预算
   → `WINDOW_EXCEEDED`；记录缺口超过传感器最大间隔 → 按不可证实拦截。
4. **目的站接驳**：到达 + 卸车准备时间晚于所有接驳交接截关 → `FEEDER_INFEASIBLE`。
5. **车厢能力与容量**：临时换编后无同温层车厢 → `CONSIST_CHANGED` +
   `NO_MATCHING_CAR`；有能力但容量被占 → `NO_CAPACITY`，排序依据
   `PRIORITY_RANK`（优先级 → 到站时刻 → 托运时刻）。
6. **转班**：本班不可行但下游班次（含接驳、耐受预算、容量）可行 → `ROLLOVER`，
   在目标班次建立转入预约；未证实/超窗/未放行隔离不得"转班了事"。
7. 以上都不可行 → `HOLD` 就地处置。

每个决定都带 `reason_codes`、量化 `basis`（晚到分钟、暴露分钟、容量余量、接驳时刻）、
责任人待办清单与风险提示；旧版决定标记 `superseded_by`，不被删除。

## 关键不变量

- **批次连续**：拆分/合并产生新批次，`origin_batch_ids` 与每托 `pallet_origins`
  可回溯到原始批次，转班不改批次。
- **隔离不可逆**：补报温度（`backfill=true`）只追加证据，不清除隔离；隔离状态、
  触发事件与处置记录只增不改。
- **优先级变更需依据**：必须携带 `business_reason` 和业务单据 `ref`，否则拒绝入账；
  历史链全程保留。
- **容量不超装**：所有预约在全局锁下原子记账（`HELD → COMMITTED / RELEASED`），
  已装车（COMMITTED）容量重规划不可挤占；8 线程并发规划测试验证不超装。
- **断网按现场时间**：扫描以设备时间入账；`clock_sync` 学到时钟偏移后，对该设备
  已入账扫描回溯校正（`corrected_at`），时间线按校正后现场时间排序。
- **点-in-time**：计划只采信 `as_of` 之前的读数、到站、编组与处置，后续事件不倒灌。
- **数据最小化**：
  - RAIL 铁路：完整计划、容量、隔离与依据；
  - SHIPPER 发货人：仅本发货人批次的决定/待办/自身时间线，不见竞争批次与容量余量；
  - FEEDER 接驳商：仅本承运商的交接（托数、温层、重量、就绪/交接时刻），不见
    发货人身份、货品、温度与隔离；
  - DEVICE 设备：仅可上报扫描/时钟/传感器事件。

## 目录

- `src/models.py` — 值对象与状态模型
- `src/ledger.py` — 事件入账、传感器采信、隔离触发、暴露积分
- `src/snapshot.py` — 点-in-time 事实快照（编组/到站/优先级/隔离）
- `src/engine.py` — 决策引擎、容量账本、转班、装车确认与截关
- `src/timeline.py` — 货损争议时间线与贡献说明
- `src/views.py` — 角色数据最小化视图
- `src/app.py` / `src/server.py` — 应用服务层与 HTTP 入口
- `src/replay.py` / `src/validation.py` — 夹具回放与契约校验
- `contracts/` — `event.envelope` / `plan` / `timeline` JSON 契约
- `fixtures/scenario.json` — 三异常并发场景（44 个事件）
- `tests/` — 22 个测试覆盖全部规则

## 场景夹具（2026-09-26，S100 合肥→上海 10:00 发）

- B-7001 冷鲜水产：公路晚到（09:40 到，晚 25 分钟）+ 09:35 读数 8.2°C 硬超限隔离 +
  09:50 合规补报（不解除隔离）+ 09:55 质检 qc-wang 凭单人工放行 → 转 S102；
- B-7003 P1 冻肉：08:55 冷藏车故障被摘下（临时换编），无 FROZEN 车厢 → HOLD；
- B-7005 凭时效赔付单据 P3→P1 抢占稀缺 CHILL 舱位，P2/P3 批次按优先级挤出转班；
- B-7006 传感器校准失效，读数"看起来正常"仍不可采信 → HOLD；
- D-55 手持机断网扫描，时钟同步后按现场时间回溯校正。

## 接口

鉴权头：`X-Role: RAIL|SHIPPER|FEEDER|DEVICE`、`X-Actor-Id: <主体标识>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/events` | 上报事件（类型见契约枚举） |
| POST | `/services/{id}/plan` | 生成截关前可执行清单，body `{"as_of": ISO}` |
| POST | `/reserves/{id}/commit` | 装车扫描完成确认，容量锁定 |
| POST | `/services/{id}/close` | 截关发车，释放未装占用 |
| GET  | `/batches/{id}/timeline` | 货损争议时间线 |
| GET  | `/health` `/context` | 健康检查与领域上下文 |

```bash
python3 -m unittest discover -s tests -v   # 全部规则测试
python3 -m src.server                      # 本地服务 127.0.0.1:8000
```

快速回放：

```python
from src.app import ServiceApp
from src.replay import replay
from src.views import RAIL
app = ServiceApp(replay())
plan = app.make_plan("S100", "2026-09-26T09:10:00+08:00", RAIL, "rail-ops")
print(plan["summary"])  # {'LOAD': 3, 'ROLLOVER': 2, 'HOLD': 2}
```
