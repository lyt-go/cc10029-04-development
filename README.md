# 跨系统数据交换与批量传输平台

面向异构系统间批量数据交换的字段契约、增量窗口抽取、暂存落地、校验和对账、断点续传与到达核对平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              19个业务模块配置（由 scripts/gen_modules.py 生成）
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 源端登记 | `source_register` | 源端系统 | 源端编号、系统名称、数据域 |
| 目标端登记 | `target_register` | 目标端点 | 目标编号、系统名称、接收格式 |
| 字段契约 | `field_contract` | 字段映射 | 契约编号、源字段、目标字段、适用源端、生效开始/结束、契约版本、撤回标记 |
| 可用字段契约清单 | `contract_catalog` | 窗口级冻结契约集合 | 清单编号、窗口编号、源端编号、生效时间、契约条目数、校验值 |
| 增量窗口 | `extract_window` | 抽取窗口 | 窗口编号、源端编号、增量字段 |
| 传输批次 | `transfer_batch` | 传输批次 | 批次编号、窗口编号、目标编号 |
| 暂存落地 | `staging_area` | 暂存分区 | 暂存编号、批次编号、落地路径 |
| 校验核对 | `checksum_verify` | 校验任务 | 校验编号、批次编号、源端校验值 |
| 到达对账 | `arrival_reconcile` | 对账单 | 对账编号、批次编号、应到条数 |
| 字段冲突 | `conflict_resolve` | 冲突记录 | 冲突编号、记录主键、冲突字段 |
| 断点续传 | `resume_point` | 续传断点 | 断点编号、批次编号、已完成分片 |
| 速率整形 | `speed_control` | 限速策略 | 策略编号、作用范围、每秒记录数 |
| 拒收回执 | `reject_record` | 拒收记录 | 拒收编号、批次编号、记录主键 |
| 格式适配 | `format_adapt` | 适配版本 | 适配编号、记录格式、旧版结构 |
| 传输血缘 | `transfer_lineage` | 血缘链路 | 链路编号、源端编号、抽取窗口 |
| 批次归档 | `history_archive` | 归档卷 | 归档编号、批次范围、归档路径 |
| 历史重放 | `exchange_replay` | 重放任务 | 重放编号、归档编号、重放范围 |
| 时效监控 | `sla_monitor` | 时效指标 | 指标编号、源端编号、约定到达时限 |
| 通道配置 | `channel_config` | 传输通道 | 通道编号、传输协议、并发连接数 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 可用字段契约清单（按窗口的规则归属与权限）

`contract_catalog` 解决字段契约缺少「按抽取窗口校验规则归属和权限」的问题，映射任务
不再直接读取全量契约，而是先拿到一份冻结、可回查的可用契约集合。

- **规则归属与有效期**：字段契约新增 `适用源端`、`生效开始/结束`、`撤回标记`。
  `适用源端=通用` 的规则可被两个源端共享；专属规则只有归属源端可用，权限分别核验，
  被拒绝的契约会进清单的 `denied` 列表并注明原因。
- **两版合法规则并存**：同一目标字段存在多版有效契约时，默认按版本号取最高（newest wins），
  清单保留 `候选版本` 可回查；`preferred_versions` 可在交接期钉住旧版（如 v1）。
- **冻结与复用**：`POST /api/contract-catalog/build` 按窗口冻结清单（含校验值，落盘
  `data/catalogs/`），同一窗口重复构建返回同一份；契约撤回不影响已冻结集合。
- **映射轮次**：`POST /api/contract-catalog/rounds/start` 基于冻结清单转换，结果逐行保留
  实际采用的源字段、目标字段、契约编号与版本（lineage），并写入轮次校验值。轮次只访问
  清单内的源字段，因此 A 轮次不会读取 B 的专属列。
- **暂停/恢复**：`pause` / `resume` 保留处理水位并复用同一冻结清单，恢复时校验清单
  校验值，类型规则不会 v1/v2 混用。
- **重算评估**：`POST /api/contract-catalog/evaluate-recompute` 按当前时刻重新组装临时
  清单，与历史轮次采用版本逐字段对比，输出 `need_recompute` 与差异字段（如交接期结束后
  v1 → v2）。

领域：数据工程
子方向：数仓与数据湖
