"""分群对比整批提交链路测试：稳定计算、独立回执、事务残留回滚、版本冲突策略。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import TransactionResidueError, store
from app.services.segment_compare import (
    BATCH_MODULE,
    RECEIPT_MODULE,
    RESULT_MODULE,
    MODULE,
    SegmentCompareService,
)
from app.services.metric_def import MetricDefService


def _clear_all():
    for k in list(store._tables.keys()):
        store._tables[k] = []
    store._sequences.clear()


def _seed_base(task_count=3):
    """造好分群、指标与对比任务，返回服务实例。"""
    _clear_all()
    for gid, gtype in ((1, "control"), (2, "experiment"), (3, "control")):
        store.rows("sample_group").append({
            "id": gid, "分群编号": f"SAMP-{gid:04d}", "分群名称": f"分群{gid}",
            "样本基数": "300", "group_type": gtype, "status": "已就绪",
        })
    for mid in (1, 2, 3):
        store.rows("metric_def").append({
            "id": mid, "指标编号": f"METR-{mid:04d}", "指标名称": f"指标{mid}", "status": "已生效",
        })
    for tid in range(1, task_count + 1):
        store.rows(MODULE).append({
            "id": tid, "任务编号": f"SEGM-{tid:04d}",
            "分群A": "分群1", "分群B": f"分群{2 if tid % 2 else 3}",
            "对比指标": f"指标{tid}", "时间范围": "最近7天",
            "差异阈值": "0.05", "显著性水平": "0.05", "比较方法": "环比",
            "status": "草稿", "pending": True, "abnormal": False,
        })
    return SegmentCompareService()


def test_batch_submit_persists_results_and_receipts():
    """整批提交：每个任务都有独立结果与回执，且同一批次落库。"""
    svc = _seed_base()
    resp = svc.submit_batch()
    assert resp["ok"], resp
    batch = resp["batch"]
    assert batch["status"] == "已完成"
    assert batch["完成数"] == 3 and batch["待补数"] == 0
    results = store.rows(RESULT_MODULE)
    receipts = store.rows(RECEIPT_MODULE)
    assert len(results) == 3 and len(receipts) == 3
    # 结果与回执一一对应（同一批次落库，无残留）
    assert {r["id"] for r in results} == {r["result_id"] for r in receipts}
    # 每个任务独立回执
    assert len({r["回执编号"] for r in receipts}) == 3
    # 逐条给出结果
    assert len(resp["items"]) == 3
    assert all(item["回执状态"] == "已回执" for item in resp["items"])
    # 任务状态联动为已完成
    assert all(t["status"] == "已完成" for t in store.rows(MODULE))


def test_batch_results_are_stable_and_idempotent():
    """重复提交不改变已完成任务的差异与显著性，也不产生重复结果。"""
    svc = _seed_base()
    first = svc.submit_batch()
    assert first["ok"]
    snapshot = [(r["task_id"], r["差异"], r["显著性"], r["id"]) for r in store.rows(RESULT_MODULE)]
    second = svc.submit_batch()
    assert second["ok"]
    again = [(r["task_id"], r["差异"], r["显著性"], r["id"]) for r in store.rows(RESULT_MODULE)]
    assert snapshot == again, "重试后已完成结果的差异与显著性不能变化"
    assert len(store.rows(RESULT_MODULE)) == 3
    assert len(store.rows(RECEIPT_MODULE)) == 3


def test_partial_failure_then_resume_keep_completed():
    """部分任务超时形成版本冲突：保留成功结果，只补算失败任务。"""
    svc = _seed_base()
    resp = svc.submit_batch(simulate_failures=[2])
    assert resp["ok"] and resp["conflict"]
    assert resp["batch"]["status"] == "部分完成"
    assert resp["batch"]["完成数"] == 2 and resp["batch"]["待补数"] == 1
    assert [p["task_id"] for p in resp["pending"]] == [2]
    done_before = {r["task_id"]: dict(r) for r in store.rows(RESULT_MODULE)}
    batch_no = resp["batch"]["批次号"]
    version = resp["batch"]["version"]

    resumed = svc.resume_batch(batch_no, strategy="keep_completed", expected_version=version)
    assert resumed["ok"], resumed
    assert resumed["batch"]["status"] == "已完成"
    after = {r["task_id"]: dict(r) for r in store.rows(RESULT_MODULE)}
    assert set(after) == {1, 2, 3}
    for tid in (1, 3):
        # 已完成结果不因补算而变化
        assert after[tid]["id"] == done_before[tid]["id"]
        assert after[tid]["差异"] == done_before[tid]["差异"]
        assert after[tid]["显著性"] == done_before[tid]["显著性"]
        assert after[tid]["completed_at"] == done_before[tid]["completed_at"]
    assert len(store.rows(RECEIPT_MODULE)) == 3


def test_rollback_all_strategy_regenerates_from_first_task():
    """整批回滚：结果表回滚为空，从首个任务重新生成。"""
    svc = _seed_base()
    resp = svc.submit_batch(simulate_failures=[2, 3])
    assert resp["batch"]["完成数"] == 1
    batch_no = resp["batch"]["批次号"]
    old_ids = [r["id"] for r in store.rows(RESULT_MODULE)]

    resumed = svc.resume_batch(batch_no, strategy="rollback_all")
    assert resumed["ok"], resumed
    assert resumed["batch"]["status"] == "已完成"
    results = store.rows(RESULT_MODULE)
    assert len(results) == 3
    # 旧结果已被回滚清空，全部重新生成
    assert all(r["id"] not in old_ids for r in results)
    assert [r["task_id"] for r in sorted(results, key=lambda x: x["id"])] == [1, 2, 3]
    assert len(store.rows(RECEIPT_MODULE)) == 3


def test_version_conflict_detected_on_stale_version():
    """续算携带过期版本号时返回冲突，由调用方选策略重试。"""
    svc = _seed_base()
    resp = svc.submit_batch(simulate_failures=[3])
    batch_no = resp["batch"]["批次号"]
    conflict = svc.resume_batch(batch_no, expected_version=999)
    assert not conflict["ok"] and conflict["conflict"]
    assert "版本冲突" in conflict["message"]
    # 冲突不写入任何新结果
    assert len(store.rows(RESULT_MODULE)) == 2


def test_transaction_residue_rolls_back_whole_batch():
    """结果与回执不一致视为事务残留：整批回滚，页面不会看到半吊子数据。"""
    svc = _seed_base()
    original = svc._build_receipt

    def broken_receipt(task, batch_no, result):
        receipt = original(task, batch_no, result)
        receipt["result_id"] = result["id"] + 1000  # 制造结果/回执不一致
        return receipt

    svc._build_receipt = broken_receipt
    resp = svc.submit_batch()
    assert not resp["ok"]
    assert "残留" in resp["message"]
    # 整体回滚：结果表、回执表、任务状态都不留痕迹
    assert store.rows(RESULT_MODULE) == []
    assert store.rows(RECEIPT_MODULE) == []
    assert all(t["status"] == "草稿" for t in store.rows(MODULE))


def test_nested_transaction_is_rejected_as_residue():
    """事务未清理时再开事务直接判残留。"""
    _clear_all()
    with store.transaction(RESULT_MODULE):
        try:
            with store.transaction(RECEIPT_MODULE):
                pass
            raise AssertionError("应抛出事务残留")
        except TransactionResidueError:
            pass


def test_metric_def_validates_compare_results():
    """跨模块联动：指标定义读取结果表指标编号，按显著性字段校验每条结果。"""
    svc = _seed_base()
    resp = svc.submit_batch()
    assert resp["ok"]
    md = MetricDefService()
    report = md.validate_compare_results()
    assert report["total"] == 3
    assert report["failed"] == 0, report["items"]
    assert all(item["指标编号"].startswith("METR-") for item in report["items"])
    # 篡改显著性字段后校验必须能查出来
    store.rows(RESULT_MODULE)[0]["显著性"] = (
        "不显著" if store.rows(RESULT_MODULE)[0]["显著性"] == "显著" else "显著"
    )
    report2 = md.validate_compare_results()
    assert report2["failed"] == 1


def test_list_and_detail_read_confirmed_results():
    """列表页与详情页都读取同一份确认结果。"""
    svc = _seed_base()
    svc.submit_batch()
    items, total = svc.list_entries()
    assert total == 3
    for item in items:
        assert item["结果状态"] == "已完成"
        assert item["差异"] is not None and item["显著性"] in ("显著", "不显著")
    detail = svc.get_task_detail(1)
    assert detail["确认结果"]["task_id"] == 1
    assert detail["回执"]["task_id"] == 1
    assert detail["确认结果"]["差异"] == detail["差异"]


def test_unresolvable_group_goes_to_pending():
    """分群成员明细缺失的任务进入待补，随后处理。"""
    svc = _seed_base()
    store.rows(MODULE)[0]["分群A"] = "不存在的分群"
    resp = svc.submit_batch()
    assert resp["ok"] and resp["conflict"]
    assert resp["batch"]["完成数"] == 2
    assert resp["pending"][0]["task_id"] == 1
    assert "分群成员明细缺失" in resp["pending"][0]["原因"]
