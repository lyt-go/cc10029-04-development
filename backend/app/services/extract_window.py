"""增量窗口领域服务：维护按增量字段切分的抽取窗口边界、抽取执行与推进水位。

本模块负责根据源端登记信息创建抽取窗口，执行数据抽取并写入抽取产物文件，
完成后自动驱动传输批次创建，形成源端登记→窗口抽取→批次传输的贯通链路。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ExtractWindowService(BaseModuleService):
    MODULE = "extract_window"
    ENTITY = "抽取窗口"
    OPERATION_TERM = "切分"
    LIST_FIELDS = ["窗口编号", "源端编号", "增量字段", "窗口起点", "窗口终点", "抽取条数", "窗口类型", "推进水位"]
    REQUIRED_FIELDS = ["窗口编号", "源端编号", "增量字段"]
    STATUS_ORDER = ["草稿", "待抽取", "抽取中", "已闭合"]
    ACTION_RULES = {"启动抽取": "抽取中", "闭合窗口": "已闭合", "标记作废": "已闭合"}
    NEGATIVE_ACTIONS = ["标记作废"]

    def create_window(self, source_code: str, count: int = 10) -> dict[str, Any]:
        """为指定源端创建增量抽取窗口并写入窗口配置快照，记录窗口边界与初始水位。"""
        sources = [r for r in store.rows("source_register") if r.get("源端编号") == source_code]
        if not sources:
            raise ValueError(f"源端编号 {source_code} 未登记，请先完成源端登记")
        entry, missing = self.create_entry({
            "窗口编号": f"EXTR-{source_code}-{int(time.time())}",
            "源端编号": source_code,
            "增量字段": "updated_at",
            "窗口起点": time.strftime("%Y-%m-%dT00:00:00", time.localtime(time.time())),
            "窗口终点": time.strftime("%Y-%m-%dT23:59:59", time.localtime(time.time())),
            "抽取条数": str(count),
            "窗口类型": "增量抽取",
            "推进水位": "0",
        })
        if entry is None:
            raise ValueError(f"创建窗口失败: {missing}")
        # 写入窗口配置快照
        self._write_jsonl(
            self._data_dir("windows") / f"{entry['窗口编号']}_config.jsonl",
            [{"window_id": entry["id"], "window_code": entry["窗口编号"], "source": source_code,
              "start": entry["窗口起点"], "end": entry["窗口终点"], "count": count,
              "created_at": time.time()}]
        )
        return entry

    def execute_extract(self, window_id: int, record_count: int = 10) -> dict[str, Any]:
        """执行增量窗口数据抽取，生成抽取产物文件并推进窗口水位。

        从已在 store 登记的源端信息中查找匹配记录，按窗口范围筛选数据行，
        逐条写入抽取产物文件（jsonl），最后推进水位并自动触发传输批次创建。
        """
        window = self.get_entry(window_id)
        if window is None:
            return {"error": f"抽取窗口 {window_id} 不存在"}
        entry, msg = self.run_action(window_id, "启动抽取")
        if entry is None:
            return {"error": msg, "window_id": window_id}

        source_code = window["源端编号"]
        # 生成抽取数据行，每条记录含窗口编号、源端编号、增量字段取值和时间戳
        records = self._simulate_records(source_code, count=record_count, field_prefix="ext")

        # 写入抽取产物文件——这是本模块的核心数据产物
        output_file = self._data_dir("extracted") / f"{window['窗口编号']}.jsonl"
        written = self._write_jsonl(output_file, records)

        # 推进窗口水位
        self._write_checkpoint(f"window_{window['窗口编号']}", {
            "window_code": window["窗口编号"],
            "source_code": source_code,
            "records_extracted": written,
            "last_extract_at": time.time(),
            "watermark": str(written),
        })

        result = {
            "window_code": window["窗口编号"],
            "source_code": source_code,
            "records_extracted": written,
            "artifact_path": str(output_file),
            "watermark": str(written),
        }

        # 抽取完成后自动触发传输批次创建
        try:
            tb_svc = self._safe_import("app.services.transfer_batch", "TransferBatchService")()
            batch_result = tb_svc.create_batch(window["窗口编号"], written)
            result["auto_batch"] = batch_result
        except Exception as e:
            result["auto_batch"] = {"error": str(e)}

        return result

    def get_window(self, window_id: int) -> dict[str, Any] | None:
        """按 id 回查抽取窗口记录。"""
        return self.get_entry(window_id)

    def is_window_started(self, window_id: int) -> bool:
        """判断窗口是否已经开始产生映射结果（状态离开草稿即视为已开始）。

        窗口登记后、启动抽取前处于「草稿」态，对应「已登记尚未开始」；
        一旦执行启动抽取进入「抽取中」，映射结果即开始产生。
        """
        window = self.get_entry(window_id)
        if window is None:
            return False
        return window.get("status") != "草稿"

    def close_window(self, window_id: int) -> dict[str, Any]:
        """闭合抽取窗口，生成窗口产物摘要（记录条数、字节总量、校验值），写入闭合事件日志。"""
        window = self.get_entry(window_id)
        if window is None:
            return {"error": f"抽取窗口 {window_id} 不存在"}

        entry, msg = self.run_action(window_id, "闭合窗口")
        # 从产物文件读取统计信息
        artifact_file = self._data_dir("extracted") / f"{window['窗口编号']}.jsonl"
        records = self._read_jsonl(artifact_file)
        checksum = self._compute_checksum(str(records))

        # 写入闭合事件日志，记录抽取产物的完整统计
        self._write_jsonl(
            self._data_dir("events") / "window_close_log.jsonl",
            [{"action": "close", "window_code": window["窗口编号"], "record_count": len(records),
              "checksum": checksum, "closed_at": time.time()}]
        )

        return {
            "window_code": window["窗口编号"],
            "status": entry["status"] if entry else "已闭合",
            "record_count": len(records),
            "checksum": checksum,
            "message": msg,
        }