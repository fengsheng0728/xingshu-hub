# -*- coding: utf-8 -*-
"""
K1 同义词验收测试集（附录 F v1.7，2026-08-06 用户审定）

用途：bge 模型文件就位前的空窗期预建测试集——模型一到位、rebuild 一跑，
      `pytest -m requires_sentence_model` 一条命令出验收结论（测试先于证据）。

设计：
  - 50 组中文同义词（语义相近、字面不同——hasher 词袋无区分度，sentence 语义模型应有高 cos）
  - 10 组跨语言用例（中英对应——hasher 完全失效，sentence 应有一定对齐）
  - 全部参数化 + @pytest.mark.requires_sentence_model
  - hasher provider（模型未就位）→ 自动 skip

判定标准（双断言，防单点侥幸）：
  1. 同义词对 cos > 0.45（绝对：语义模型应显著区分）
  2. 同义词对 cos - 无关对基线 cos > 0.15（相对：区分度而非绝对值）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 50 组中文同义词（语义相近、字面不同） ──
SYNONYMS_ZH = [
    ("销售", "营销"), ("招聘", "招人"), ("合同", "契约"), ("客户", "顾客"),
    ("公司", "企业"), ("员工", "职员"), ("经理", "主管"), ("会议室", "会议厅"),
    ("财务报表", "财务报告"), ("季度总结", "季度汇报"), ("产品发布", "新品上市"),
    ("市场调研", "市场调查"), ("售后服务", "客户支持"), ("供应链", "物流链"),
    ("预算审批", "预算审核"), ("绩效考核", "员工评估"), ("培训计划", "培训方案"),
    ("项目进度", "项目进展"), ("离职率", "员工流失率"), ("招聘渠道", "人才渠道"),
    ("利润率", "盈利水平"), ("现金流", "资金流"), ("股权结构", "持股结构"),
    ("董事会", "董事会成员"), ("年度目标", "年度指标"), ("销售目标", "业绩目标"),
    ("客户满意度", "顾客满意度"), ("品牌形象", "品牌声誉"), ("市场占有率", "市场份额"),
    ("竞争对手", "竞品公司"), ("定价策略", "价格策略"), ("促销活动", "推广活动"),
    ("渠道商", "经销商"), ("直营店", "品牌直营店"), ("加盟商", "加盟店"),
    ("库存管理", "存货管理"), ("订单处理", "订单管理"), ("发货流程", "物流流程"),
    ("退款政策", "退货政策"), ("客服工单", "服务工单"), ("产品手册", "产品说明书"),
    ("操作手册", "使用指南"), ("故障排查", "问题排查"), ("系统升级", "版本更新"),
    ("数据备份", "数据备份恢复"), ("网络安全", "信息安全"), ("用户权限", "访问权限"),
    ("登录认证", "身份认证"), ("密码重置", "密码找回"), ("工作报告", "工作总结"),
]

# ── 10 组跨语言（中英对应） ──
SYNONYMS_XL = [
    ("销售", "sales"), ("招聘", "recruitment"), ("合同", "contract"),
    ("客户", "customer"), ("财务报表", "financial report"), ("市场调研", "market research"),
    ("供应链", "supply chain"), ("绩效考核", "performance review"),
    ("售后服务", "after-sales service"), ("年度目标", "annual target"),
]

# 无关对基线（区分度参照：语义无关，cos 应显著低于同义词）
UNRELATED = ("财务报表", "天气预报")


def _load_model():
    """加载当前 provider（sentence 未就位 → skip）"""
    import models
    from db import get_embedding_provider

    provider = models.CONFIG.EMBEDDING_PROVIDER or "hasher"
    if provider != "sentence":
        pytest.skip(f"requires_sentence_model: 当前 provider={provider}（模型未就位，hasher 无语义区分度）")
    try:
        return get_embedding_provider("sentence", model_path=models.CONFIG.EMBEDDING_MODEL_PATH)
    except Exception as e:
        pytest.skip(f"requires_sentence_model: 模型加载失败 {type(e).__name__}: {e}")


def _cos(a, b):
    import numpy as np
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))


@pytest.fixture(scope="module")
def sentence_model():
    return _load_model()


@pytest.fixture(scope="module")
def unrelated_cos(sentence_model):
    """无关对基线 cos（同义词必须显著高于它）"""
    a, b = UNRELATED
    return _cos(sentence_model.encode(a), sentence_model.encode(b))


@pytest.mark.requires_sentence_model
@pytest.mark.parametrize("w1,w2", SYNONYMS_ZH, ids=[f"{a}~{b}" for a, b in SYNONYMS_ZH])
def test_zh_synonym(sentence_model, unrelated_cos, w1, w2):
    """中文同义词：绝对 cos > 0.45 且相对区分度 > 0.15"""
    sim = _cos(sentence_model.encode(w1), sentence_model.encode(w2))
    assert sim > 0.45, f"同义词({w1},{w2}) cos={sim:.3f} 低于绝对阈值 0.45"
    assert sim - unrelated_cos > 0.15, (
        f"同义词({w1},{w2}) cos={sim:.3f} 与无关基线 {unrelated_cos:.3f} "
        f"区分度不足（{sim - unrelated_cos:.3f} < 0.15）")


@pytest.mark.requires_sentence_model
@pytest.mark.parametrize("zh,en", SYNONYMS_XL, ids=[f"{a}~{b}" for a, b in SYNONYMS_XL])
def test_cross_lingual(sentence_model, unrelated_cos, zh, en):
    """跨语言（中英）：绝对 cos > 0.35 且相对区分度 > 0.10（多语言对齐弱于同语种）"""
    sim = _cos(sentence_model.encode(zh), sentence_model.encode(en))
    assert sim > 0.35, f"跨语言({zh},{en}) cos={sim:.3f} 低于绝对阈值 0.35"
    assert sim - unrelated_cos > 0.10, (
        f"跨语言({zh},{en}) cos={sim:.3f} 与无关基线 {unrelated_cos:.3f} "
        f"区分度不足（{sim - unrelated_cos:.3f} < 0.10）")
