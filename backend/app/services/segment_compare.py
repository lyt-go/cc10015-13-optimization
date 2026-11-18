"""分群对比业务规则：整批提交对比任务、稳定计算结果表、逐任务独立回执。

设计要点：
- 结果落库到分群对比结果表（segment_compare_result），列表页与详情页都读这份确认结果；
- 计算只依赖分群成员明细与 Welch t 检验，不含随机数，重算结果稳定；
- 批量结果与单任务完成回执在同一事务里落库，核对出残留即整体回滚；
- 批次中断后只补算未完成任务，已完成任务的差异与显著性不因重试而变化；
- 部分成功部分超时形成版本冲突时，支持「保留成功结果并补算」与「整批回滚重算」两种策略。
"""
from __future__ import annotations

import math
import time
from typing import Any

from app.services.sample_group import SampleGroupService
from app.store import TransactionResidueError, store

MODULE = "segment_compare"
RESULT_MODULE = "segment_compare_result"   # 分群对比结果表
RECEIPT_MODULE = "segment_compare_receipt"  # 单任务完成回执表
BATCH_MODULE = "segment_compare_batch"      # 批次表

LIST_FIELDS = ["任务编号", "分群A", "分群B", "对比指标", "时间范围", "差异阈值", "显著性水平", "比较方法"]
REQUIRED_FIELDS = ["任务编号", "分群A", "分群B"]
STATUS_ORDER = ["草稿", "计算中", "已完成", "已废弃"]
ACTION_RULES = {"开始计算": "计算中", "确认完成": "已完成", "废弃任务": "已废弃"}
NEGATIVE_ACTIONS = []

# 版本冲突（部分成功、部分超时）的两种处理策略
STRATEGY_KEEP_COMPLETED = "keep_completed"  # 结果表保留已完成任务并列出待补任务
STRATEGY_ROLLBACK_ALL = "rollback_all"      # 结果表回滚为空并从首个任务重新生成
STRATEGIES = [STRATEGY_KEEP_COMPLETED, STRATEGY_ROLLBACK_ALL]

RESULT_STATUS_DONE = "已完成"
RESULT_STATUS_PENDING = "待补算"


class TaskNotReadyError(RuntimeError):
    """单个任务暂不可算（如分群成员明细缺失），记入待补任务随后处理。"""


def _parse_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _welch_p_value(a: list[float], b: list[float]) -> float:
    """Welch 双样本 t 检验的近似 p 值（正态近似），纯确定性计算。"""
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return 1.0
    m1, m2 = sum(a) / n1, sum(b) / n2
    v1 = sum((x - m1) ** 2 for x in a) / (n1 - 1)
    v2 = sum((x - m2) ** 2 for x in b) / (n2 - 1)
    se = math.sqrt(v1 / n1 + v2 / n2)
    if se == 0:
        return 1.0 if m1 == m2 else 0.0
    t = abs(m1 - m2) / se
    return round(math.erfc(t / math.sqrt(2)), 6)


class SegmentCompareService:
    def __init__(self) -> None:
        self._sample_groups = SampleGroupService()

    # ------------------------------------------------------------------
    # 任务登记与状态流转（与其它模块同一套约定）
    # ------------------------------------------------------------------
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """任务列表：每行合并该任务的确认结果，列表页直接读这份结果。"""
        rows = [dict(row) for row in store.rows(MODULE)]
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("任务编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return [self._merge_result(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_task_detail(self, entry_id: int) -> dict[str, Any] | None:
        """详情页：任务明细 + 确认结果 + 完成回执，读的同一份落库数据。"""
        task = self.get_entry(entry_id)
        if task is None:
            return None
        detail = self._merge_result(dict(task))
        detail["确认结果"] = self._result_for_task(entry_id)
        detail["回执"] = self._receipt_for_task(entry_id)
        return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["created_at"] = time.time()
        store.rows(MODULE).append(entry)
        store.bump_version(MODULE)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"对比任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分群对比可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.bump_version(MODULE)
        return entry, f"对比任务已{action}"

    # ------------------------------------------------------------------
    # 结果表读取（消费端读全量确认结果）
    # ------------------------------------------------------------------
    def list_results(self) -> list[dict[str, Any]]:
        """分群对比结果表全量确认结果，按任务编号排序稳定输出。"""
        rows = [dict(row) for row in store.rows(RESULT_MODULE)]
        rows.sort(key=lambda row: (str(row.get("批次号", "")), int(row.get("task_id", 0))))
        return rows

    def list_batches(self) -> list[dict[str, Any]]:
        """批次列表：每批附上待补任务，页面刷新后也能恢复批次状态。"""
        rows: list[dict[str, Any]] = []
        for row in sorted(store.rows(BATCH_MODULE), key=lambda r: int(r.get("id", 0)), reverse=True):
            batch = dict(row)
            batch["pending_tasks"] = self._pending_tasks(batch)
            rows.append(batch)
        return rows

    # ------------------------------------------------------------------
    # 整批提交与续算
    # ------------------------------------------------------------------
    def submit_batch(
        self,
        task_ids: list[int] | None = None,
        *,
        strategy: str = STRATEGY_KEEP_COMPLETED,
        simulate_failures: list[int] | None = None,
    ) -> dict[str, Any]:
        """一次提交整组任务：逐条计算、逐条回执，未完成项随后处理。

        重复提交同一任务集合时复用已有批次按策略续算，已完成结果不重算。
        """
        tasks = self._select_tasks(task_ids)
        if not tasks:
            return {"ok": False, "message": "没有可提交的对比任务", "items": [], "pending": []}
        batch = self._find_active_batch([int(task["id"]) for task in tasks])
        if batch is None:
            batch = self._create_batch(tasks, strategy)
        return self._run_batch(batch, tasks, strategy=strategy, simulate_failures=simulate_failures)

    def resume_batch(
        self,
        batch_no: str,
        *,
        strategy: str = STRATEGY_KEEP_COMPLETED,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """批次中断后续算：默认只重算未完成任务，已完成结果保持不动。"""
        batch = self._find_batch(batch_no)
        if batch is None:
            return {"ok": False, "message": f"批次 {batch_no} 不存在", "items": [], "pending": []}
        if expected_version is not None and expected_version != int(batch.get("version", 1)):
            # 版本冲突：返回当前批次状态，由调用方选择策略后重试
            return {
                "ok": False,
                "conflict": True,
                "message": f"批次 {batch_no} 版本冲突：期望 v{expected_version}，当前 v{batch.get('version')}",
                "batch": dict(batch),
                "items": self._batch_items(batch),
                "pending": self._pending_tasks(batch),
            }
        tasks = [task for task in (store.find(MODULE, tid) for tid in batch.get("task_ids", [])) if task]
        return self._run_batch(batch, tasks, strategy=strategy)

    # ------------------------------------------------------------------
    # 批次执行内部逻辑
    # ------------------------------------------------------------------
    def _run_batch(
        self,
        batch: dict[str, Any],
        tasks: list[dict[str, Any]],
        *,
        strategy: str,
        simulate_failures: list[int] | None = None,
    ) -> dict[str, Any]:
        if strategy not in STRATEGIES:
            return {"ok": False, "message": f"未知策略「{strategy}」，可选：{'、'.join(STRATEGIES)}",
                    "items": [], "pending": []}
        batch_no = str(batch["批次号"])
        failures = {int(tid) for tid in (simulate_failures or [])}
        try:
            with store.transaction(MODULE, RESULT_MODULE, RECEIPT_MODULE, BATCH_MODULE):
                if strategy == STRATEGY_ROLLBACK_ALL:
                    self._rollback_batch_rows(batch_no)
                    todo = list(tasks)
                else:
                    done_ids = self._done_task_ids(batch_no)
                    todo = [task for task in tasks if int(task["id"]) not in done_ids]
                new_results: list[dict[str, Any]] = []
                new_receipts: list[dict[str, Any]] = []
                pending: list[dict[str, Any]] = []
                for task in todo:
                    task_id = int(task["id"])
                    if task_id in failures:
                        # 模拟超时：留在待补任务里随后处理
                        pending.append(self._pending_entry(task, "计算超时，等待补算"))
                        continue
                    try:
                        result = self._compute_task_result(task, batch_no)
                    except TaskNotReadyError as exc:
                        pending.append(self._pending_entry(task, str(exc)))
                        continue
                    result["id"] = store.next_id(RESULT_MODULE)
                    receipt = self._build_receipt(task, batch_no, result)
                    receipt["id"] = store.next_id(RECEIPT_MODULE)
                    new_results.append(result)
                    new_receipts.append(receipt)
                    # 结果确认后任务进入已完成
                    task["status"] = "已完成"
                    task["pending"] = False
                # 批量结果与单任务完成回执必须同一批次落库
                store.rows(RESULT_MODULE).extend(new_results)
                store.rows(RECEIPT_MODULE).extend(new_receipts)
                self._assert_no_residue(new_results, new_receipts)
                self._finalize_batch(batch, strategy)
                store.bump_version(MODULE)
                store.bump_version(RESULT_MODULE)
                store.bump_version(RECEIPT_MODULE)
                store.bump_version(BATCH_MODULE)
        except TransactionResidueError as exc:
            return {"ok": False, "message": f"事务残留，批次 {batch_no} 整体回滚：{exc}",
                    "items": [], "pending": []}
        conflict = int(batch.get("待补数", 0)) > 0
        return {
            "ok": True,
            "conflict": conflict,
            "message": self._batch_message(batch, conflict),
            "batch": dict(batch),
            "items": self._batch_items(batch),
            "pending": self._pending_tasks(batch, pending),
        }

    def _compute_task_result(self, task: dict[str, Any], batch_no: str) -> dict[str, Any]:
        """读取分群成员明细，稳定计算单任务对比结果（不含随机数）。"""
        members_a = self._resolve_members(task.get("分群A"))
        members_b = self._resolve_members(task.get("分群B"))
        if not members_a or not members_b:
            raise TaskNotReadyError(
                f"任务{task.get('任务编号')}的分群成员明细缺失（分群A/分群B未就绪）")
        values_a = [float(m["metric_value"]) for m in members_a]
        values_b = [float(m["metric_value"]) for m in members_b]
        mean_a = sum(values_a) / len(values_a)
        mean_b = sum(values_b) / len(values_b)
        diff = round(mean_a - mean_b, 4)
        p_value = _welch_p_value(values_a, values_b)
        threshold = _parse_float(task.get("差异阈值"), 0.0)
        alpha = _parse_float(task.get("显著性水平"), 0.05)
        significant = p_value < alpha and abs(diff) >= threshold
        return {
            "批次号": batch_no,
            "task_id": int(task["id"]),
            "任务编号": task.get("任务编号"),
            "分群A": task.get("分群A"),
            "分群B": task.get("分群B"),
            "对比指标": task.get("对比指标"),
            "指标编号": self._resolve_metric_no(task.get("对比指标")),
            "指标值": round(mean_a, 4),
            "对比基准": round(mean_b, 4),
            "差异": diff,
            "p值": p_value,
            "显著性": "显著" if significant else "不显著",
            "差异阈值": threshold,
            "显著性水平": alpha,
            "样本量A": len(values_a),
            "样本量B": len(values_b),
            "status": RESULT_STATUS_DONE,
            "completed_at": time.time(),
        }

    def _resolve_members(self, group_label: Any) -> list[dict[str, Any]]:
        """按分群 id / 分群编号 / 分群名称解析分群成员明细。"""
        label = str(group_label or "").strip()
        if not label:
            return []
        candidates: list[int] = []
        if label.isdigit():
            candidates.append(int(label))
        for row in store.rows("sample_group"):
            if label in (str(row.get("分群编号", "")), str(row.get("分群名称", ""))):
                candidates.append(int(row.get("id", 0)))
        for group_id in candidates:
            members = self._sample_groups.get_group_members(group_id)
            if members:
                return members
        return []

    def _resolve_metric_no(self, metric_label: Any) -> str:
        """对比指标解析为指标编号，供指标定义模块读取校验。"""
        label = str(metric_label or "").strip()
        for row in store.rows("metric_def"):
            if label and label in (str(row.get("指标编号", "")), str(row.get("指标名称", ""))):
                return str(row.get("指标编号", ""))
        return label

    # ------------------------------------------------------------------
    # 回执、批次与核对
    # ------------------------------------------------------------------
    def _build_receipt(self, task: dict[str, Any], batch_no: str, result: dict[str, Any]) -> dict[str, Any]:
        """每个任务一份独立回执，与批量结果同一事务落库。"""
        return {
            "回执编号": f"RCPT-{batch_no}-{task.get('任务编号')}",
            "批次号": batch_no,
            "task_id": int(task["id"]),
            "任务编号": task.get("任务编号"),
            "result_id": result["id"],
            "status": "已回执",
            "message": f"任务{task.get('任务编号')}对比结果已确认：差异{result['差异']}，{result['显著性']}",
            "completed_at": result["completed_at"],
        }

    def _assert_no_residue(
        self, new_results: list[dict[str, Any]], new_receipts: list[dict[str, Any]]
    ) -> None:
        """提交前核对：结果与回执一一对应，否则视为事务残留，整体回滚。"""
        result_ids = {int(row["id"]) for row in new_results}
        receipt_result_ids = {int(row["result_id"]) for row in new_receipts}
        if result_ids != receipt_result_ids:
            raise TransactionResidueError(
                f"结果{len(new_results)}条与回执{len(new_receipts)}条不一致")
        receipt_task_ids = {int(row["task_id"]) for row in store.rows(RECEIPT_MODULE)}
        orphan_results = [
            row for row in store.rows(RESULT_MODULE)
            if row.get("status") == RESULT_STATUS_DONE and int(row["task_id"]) not in receipt_task_ids
        ]
        if orphan_results:
            raise TransactionResidueError(
                f"结果表存在无回执的残留记录{len(orphan_results)}条")

    def _create_batch(self, tasks: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
        batch_id = store.next_id(BATCH_MODULE)
        batch_no = f"BATCH-{time.strftime('%Y%m%d')}-{batch_id:04d}"
        batch = {
            "id": batch_id,
            "批次号": batch_no,
            "task_ids": [int(task["id"]) for task in tasks],
            "任务数": len(tasks),
            "完成数": 0,
            "待补数": len(tasks),
            "status": "计算中",
            "strategy": strategy,
            "version": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        store.rows(BATCH_MODULE).append(batch)
        store.bump_version(BATCH_MODULE)
        return batch

    def _finalize_batch(self, batch: dict[str, Any], strategy: str) -> None:
        done = self._done_task_ids(str(batch["批次号"]))
        batch["完成数"] = len(done)
        batch["待补数"] = max(0, int(batch["任务数"]) - len(done))
        batch["status"] = "已完成" if batch["待补数"] == 0 else "部分完成"
        batch["strategy"] = strategy
        batch["version"] = int(batch.get("version", 0)) + 1
        batch["updated_at"] = time.time()

    def _rollback_batch_rows(self, batch_no: str) -> None:
        """整批回滚：结果表与回执表清空到本批提交前，从首个任务重新生成。"""
        store.rows(RESULT_MODULE)[:] = [
            row for row in store.rows(RESULT_MODULE) if row.get("批次号") != batch_no]
        store.rows(RECEIPT_MODULE)[:] = [
            row for row in store.rows(RECEIPT_MODULE) if row.get("批次号") != batch_no]

    def _done_task_ids(self, batch_no: str) -> set[int]:
        return {
            int(row["task_id"])
            for row in store.rows(RESULT_MODULE)
            if row.get("批次号") == batch_no and row.get("status") == RESULT_STATUS_DONE
        }

    def _select_tasks(self, task_ids: list[int] | None) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(MODULE) if row.get("status") != "已废弃"]
        if task_ids:
            wanted = {int(tid) for tid in task_ids}
            rows = [row for row in rows if int(row.get("id", 0)) in wanted]
        return rows

    def _find_batch(self, batch_no: str) -> dict[str, Any] | None:
        return store.find_by_field(BATCH_MODULE, "批次号", batch_no)

    def _find_active_batch(self, task_ids: list[int]) -> dict[str, Any] | None:
        wanted = sorted(task_ids)
        for batch in reversed(store.rows(BATCH_MODULE)):
            if sorted(int(tid) for tid in batch.get("task_ids", [])) == wanted:
                return batch
        return None

    def _result_for_task(self, task_id: int) -> dict[str, Any] | None:
        for row in reversed(store.rows(RESULT_MODULE)):
            if int(row.get("task_id", 0)) == task_id and row.get("status") == RESULT_STATUS_DONE:
                return dict(row)
        return None

    def _receipt_for_task(self, task_id: int) -> dict[str, Any] | None:
        for row in reversed(store.rows(RECEIPT_MODULE)):
            if int(row.get("task_id", 0)) == task_id:
                return dict(row)
        return None

    def _merge_result(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表行合并确认结果：没有结果的任务保持空值，由页面显示占位。"""
        result = self._result_for_task(int(row.get("id", 0)))
        for field in ("指标值", "对比基准", "差异", "显著性", "批次号"):
            row[field] = result.get(field) if result else None
        row["结果状态"] = RESULT_STATUS_DONE if result else RESULT_STATUS_PENDING
        return row

    def _batch_items(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """整组提交后每个任务的独立对比数据结果，逐条给出。"""
        batch_no = str(batch.get("批次号", ""))
        receipts = {
            int(row["task_id"]): row
            for row in store.rows(RECEIPT_MODULE)
            if row.get("批次号") == batch_no
        }
        items: list[dict[str, Any]] = []
        for row in store.rows(RESULT_MODULE):
            if row.get("批次号") != batch_no:
                continue
            receipt = receipts.get(int(row["task_id"]))
            items.append({
                "task_id": row["task_id"],
                "任务编号": row.get("任务编号"),
                "指标值": row.get("指标值"),
                "对比基准": row.get("对比基准"),
                "差异": row.get("差异"),
                "p值": row.get("p值"),
                "显著性": row.get("显著性"),
                "status": row.get("status"),
                "回执编号": receipt.get("回执编号") if receipt else None,
                "回执状态": receipt.get("status") if receipt else "缺回执",
            })
        items.sort(key=lambda item: int(item["task_id"]))
        return items

    def _pending_tasks(
        self, batch: dict[str, Any], pending: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        if pending is not None and pending:
            return pending
        done = self._done_task_ids(str(batch.get("批次号", "")))
        entries: list[dict[str, Any]] = []
        for task_id in batch.get("task_ids", []):
            if int(task_id) in done:
                continue
            task = store.find(MODULE, int(task_id))
            if task is not None:
                entries.append(self._pending_entry(task, "等待补算"))
        return entries

    def _pending_entry(self, task: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "task_id": int(task["id"]),
            "任务编号": task.get("任务编号"),
            "status": RESULT_STATUS_PENDING,
            "原因": reason,
        }

    def _batch_message(self, batch: dict[str, Any], conflict: bool) -> str:
        if conflict:
            return (f"批次{batch['批次号']}部分完成：{batch['完成数']}/{batch['任务数']}已确认，"
                    f"待补{batch['待补数']}个任务随后处理")
        return f"批次{batch['批次号']}整组完成：{batch['完成数']}个任务结果与回执已同批落库"
