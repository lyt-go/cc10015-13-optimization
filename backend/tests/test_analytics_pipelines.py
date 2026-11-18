"""多维分析实验平台核心链路测试。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.store import store
from app.services.experiment import ExperimentService
from app.services.sample_group import SampleGroupService
from app.services.ab_result import AbResultService
from app.services.metric_def import MetricDefService
from app.services.expr_factor import ExprFactorService
from app.services.segment_compare import SegmentCompareService
from app.services.attr_analysis import AttrAnalysisService
from app.services.anomaly_detect import AnomalyDetectService
from app.services.forecast_model import ForecastModelService
from app.services.insight_report import InsightReportService
from app.services.metric_alias import MetricAliasService
from app.services.evaluate_board import EvaluateBoardService
from app.services.data_freshness import DataFreshnessService
from app.services.cohort_track import CohortTrackService
from app.services.data_export import DataExportService
from app.services.drill_down import DrillDownService

def _clear_all():
    for k in list(store._tables.keys()):
        store._tables[k] = []
    store._sequences.clear()

def test_pipeline_experiment_to_sample_groups():
    """实验创建后自动生成对照和实验组分群。"""
    _clear_all()
    es = ExperimentService()
    entry, ms = es.create_entry({
        "方案编号": "EXPE-001",
        "实验名称": "新首页布局",
        "对照组说明": "旧版首页",
        "实验组说明": "新版首页",
        "样本总量": "10000",
        "分流比例": "5:5",
        "启动日期": "2026-11-01",
        "预期时长": "14",
    })
    assert entry is not None, ms
    sg_rows = store.rows("sample_group")
    assert len(sg_rows) >= 2, f"应创建对照组和实验组，实际{len(sg_rows)}"
    control = [r for r in sg_rows if r.get("group_type") == "control"]
    exp = [r for r in sg_rows if r.get("group_type") == "experiment"]
    assert len(control) >= 1
    assert len(exp) >= 1
    assert int(control[0].get("样本基数", 0)) > 0
    assert int(exp[0].get("样本基数", 0)) > 0

def test_sample_group_members():
    """分群能生成带差异的用户列表。"""
    _clear_all()
    # 先创建实验再用自动分群
    es = ExperimentService()
    es.create_entry({"方案编号":"EXPE-002","实验名称":"测试","对照组说明":"控","实验组说明":"试","样本总量":"200","分流比例":"5:5"})
    sg = SampleGroupService()
    members = sg.get_group_members(1)
    assert len(members) > 0, "应有分群用户"
    assert "user_id" in members[0]
    assert "metric_value" in members[0]

def test_metric_def_creation():
    """指标定义创建和口径版本。"""
    _clear_all()
    md = MetricDefService()
    entry, ms = md.create_entry({
        "指标编号": "M-001",
        "指标名称": "点击率",
        "计算公式": "clicks / impressions",
        "统计粒度": "日",
        "时间窗口": "7",
        "基准取值": "0.10",
        "精度位数": "4",
        "口径版本": "v1",
    })
    assert entry is not None, ms

def test_ab_result_analysis():
    """AB分析生成对照和实验组对比。"""
    _clear_all()
    ab = AbResultService()
    results = ab.run_analysis({"样本范围": "4"})
    assert len(results) == 4
    for r in results:
        assert "指标值" in r
        assert "对比基准" in r
        assert "差异" in r

def test_expr_factor_creation():
    """实验因子互斥规则。"""
    _clear_all()
    ef = ExprFactorService()
    entry, ms = ef.create_entry({
        "因子编号": "F-001",
        "因子名称": "按钮颜色",
        "因子层级": "UI层",
        "取值集合": "红色,蓝色",
        "分配比例": "5:5",
        "互斥规则": "颜色因子互斥",
        "关联实验": "EXPE-001",
        "生效批次": "B001",
    })
    assert entry is not None, ms

def test_anomaly_detect():
    """异常检测计算移动平均。"""
    _clear_all()
    ad = AnomalyDetectService()
    ad.create_entry({
        "规则编号": "AD-001",
        "监控指标": "点击率",
        "检测算法": "moving_average",
        "基线窗口": "7",
        "波动阈值": "0.3",
        "告警级别": "高",
        "静默时段": "0",
        "规则版本": "v1",
    })
    result = ad.run_detection(1, [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.55])
    assert result.get("ok") or "anomaly" in str(result).lower()

def test_forecast_model():
    """预测模型简单移动平均。"""
    _clear_all()
    fc = ForecastModelService()
    fc.create_entry({
        "模型编号": "FC-001",
        "预测对象": "点击率",
        "历史窗口": "7",
        "预测周期": "3",
        "模型类型": "moving_average",
        "参数配置": "window=3",
        "训练批次": "T001",
        "评估误差": "",
    })
    result = fc.train_and_predict(1, [0.10, 0.12, 0.11, 0.13, 0.15, 0.14, 0.16])
    assert result.get("ok"), result

def test_metric_alias_version():
    """口径版本注册。"""
    _clear_all()
    ma = MetricAliasService()
    entry, ms = ma.create_entry({
        "版本编号": "ALIAS-001",
        "指标编号": "M-001",
        "变更说明": "修改去重逻辑",
        "旧口径": "clicks/impressions",
        "新口径": "clicks/valid_impressions",
        "生效时间": "2026-11-16",
        "审批状态": "草稿",
        "变更人": "admin",
    })
    assert entry is not None, ms

