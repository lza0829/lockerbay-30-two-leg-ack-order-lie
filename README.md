# 格湾（LockerBay）

**骨架：** 入柜/取件事件装载 → 格口占用物化 → 超时回收 → 占用账对账

- 领域：数据工程（去皮=事件装载 + 占用状态物化 + 超时闸/回收 + 占用核证）
- 端口：前端 **6100**，接口 **11100**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/lockerbay-NN-*`
- 非仓库库存 CRUD、非运单轨迹轴、非营业日封账、非会话窗切分

本地冒烟：

```powershell
cd claude-workspace/lockerbay/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11100

cd ../frontend
npm install
npm run dev
```

## 双腿关闭栅栏（two-leg close fence）

占用标 `released` 之前必须同时具备**同一占用、同一格口、同一运单**的 `pickup` 与
`confirm` 两条事件；缺任一腿绝不关账。

- 落库形态：`close_legs` 边表（`occupancy_id + pickup_id + confirm_id`），边不可改删。
- 失败闭：另有一腿先到即进 `close_candidates` 候选区并写 `quarantine`（搁置）；
  配对成功在同一事务内落边、释放占用、腾格、收口搁置。
- 超时：`pair_timeout_minutes`（柜规页可改）到期后整组转 `expired`，搁置保持 open，
  **不放行**；此后到达的腿同样搁置，等人工复核。
- 错配：别的运单 / 别的格口的 `confirm` 不构成腿，进搁置（`two_leg:tracking_mismatch`
  / `two_leg:no_open_occupancy`）。
- 数据库兜底：触发器拒绝“无双腿边却 released”“边表被改删”“released 回滚”，
  任何旁路代码都绕不过；`/api/two-leg/audit` 做全库一致性审计。

只读核证接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/close-legs` | 双腿关闭边（含 pickup/confirm 事件时刻） |
| GET | `/api/close-candidates` | 候选区及各腿状态 |
| GET | `/api/two-leg/quarantine` | 双腿搁置明细 |
| GET | `/api/two-leg/verify/{occupancy_id}` | 单占用逐项核证 |
| GET | `/api/two-leg/audit` | 全库一致性审计 |
| POST | `/api/two-leg/sweep` | 超时扫描（只过期，不关账） |

夹具对照（`fixtures/`）：

- 单腿搁置：`TN1001` 的 `E3` pickup 缺 confirm → 占用保持 occupied、事件留搁置；
  同格 `E8` 用别的运单 `TN9999` 的 confirm 冒充 → 拒绝并搁置。
- 双腿释放：`TN1003` 的 `E6` pickup + `E7` confirm、`TN1004` 的 `E11` + `E12`
  → 原子关账并落边；`E9` 给 `TN1001` 补齐 confirm 后关账。

老库迁移：启动时一次性执行（`settings.legacy_reconcile_done` 记账）。历史“单腿即
released”的占用，若仍是本格口最新占用且格口空闲，会改回 occupied 并把那条 pickup
放回候选区等 confirm；有后继占用等无法安全重开的情况保留原样并写
`two_leg:legacy_released_unsafe` 搁置，交人工复核。

跑测试（SQLite，本地夹具）：

```bash
cd backend
python -m pytest -q tests
```
