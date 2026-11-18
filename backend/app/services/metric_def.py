"""指标定义业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "metric_def"
COMPARE_RESULT_MODULE = "segment_compare_result"  # 分群对比结果表
LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "基准取值", "精度位数", "口径版本"]
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
NEGATIVE_ACTIONS = []


class MetricDefService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("指标编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"指标定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标定义可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"指标定义已{action}"

    def validate_compare_results(self) -> dict[str, Any]:
        """跨模块联动：读取分群对比结果表的指标编号，按显著性字段校验每条任务结果。

        校验规则：
        - 指标编号必须在指标定义中已登记；
        - 显著性字段必须与复核值一致（p值 < 显著性水平 且 |差异| >= 差异阈值 → 显著）。
        """
        items: list[dict[str, Any]] = []
        for row in store.rows(COMPARE_RESULT_MODULE):
            problems: list[str] = []
            metric_no = str(row.get("指标编号", "") or "").strip()
            metric = store.find_by_field(MODULE, "指标编号", metric_no) if metric_no else None
            if not metric_no:
                problems.append("结果缺少指标编号")
            elif metric is None:
                problems.append(f"指标编号 {metric_no} 未在指标定义中登记")
            try:
                diff = abs(float(row.get("差异")))
                threshold = float(row.get("差异阈值"))
                p_value = float(row.get("p值"))
                alpha = float(row.get("显著性水平"))
            except (TypeError, ValueError):
                problems.append("差异或显著性相关字段不是有效数值")
            else:
                expected = "显著" if (p_value < alpha and diff >= threshold) else "不显著"
                if str(row.get("显著性", "")) != expected:
                    problems.append(f"显著性字段「{row.get('显著性')}」与复核值「{expected}」不一致")
            items.append({
                "批次号": row.get("批次号"),
                "任务编号": row.get("任务编号"),
                "指标编号": metric_no,
                "指标名称": metric.get("指标名称") if metric else None,
                "显著性": row.get("显著性"),
                "差异": row.get("差异"),
                "p值": row.get("p值"),
                "校验结果": "通过" if not problems else "未通过",
                "说明": "；".join(problems) if problems else "指标已登记，显著性字段复核一致",
            })
        passed = sum(1 for item in items if item["校验结果"] == "通过")
        return {"items": items, "total": len(items), "passed": passed,
                "failed": len(items) - passed}
