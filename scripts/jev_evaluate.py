"""读取本地采集账本并生成旁路对照报告；不调用API，不修改JEV配置。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.jev_settings import evaluation_report, validate_sample_split


def generate_report(discovery_path, validation_path, output_dir, revocations_path=None):
    def records(path):
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = value.get("records") if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise ValueError("输入必须是采集账本或记录数组")
        return rows
    discovery, validation = records(discovery_path), records(validation_path)
    validate_sample_split(discovery, validation)
    revoked = records(revocations_path) if revocations_path else []
    report = evaluation_report(discovery, validation, revoked)
    report["unlabelled_validation_count"] = sum(not r.get("user_event_id") or r.get("label_source") != "user" for r in validation)
    report["sources"] = {"discovery": str(Path(discovery_path).resolve()), "validation": str(Path(validation_path).resolve())}
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# JEV 旁路对照报告", "", "仅统计已记录预测与真实用户标签；未自动启用，未宣称个人偏好验证通过。", "",
        f"发现样本：{len(discovery)}；验证样本：{len(validation)}；未标注验证样本：{report['unlabelled_validation_count']}。", "",
        "| 类别 | 模型/规则 | 标签数 | 一致数/率 | 选择数/覆盖率 | 应询问却选择数/率 | 不必要询问数/率 | 撤销数/选择数 |",
        "|---|---|---:|---:|---:|---:|---:|---:|"]
    for category, models in report["categories"].items():
        for model, stat in models.items():
            cells = [category, model, str(stat["labelled"])]
            for count, rate in (("agreement", "agreement_rate"), ("selected", "selection_coverage"),
                ("should_ask_but_selected", "unsafe_delegation_rate"), ("unnecessary_ask", "unnecessary_ask_rate")):
                cells.append(f"{stat[count]} / {stat[rate]:.1%}")
            cells.append(f"{stat['revoked']} / {stat['selected']}" if model == "jev" else "不适用")
            lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "一致率、覆盖率、两种询问比例的分母均为对应类别有标签数。撤销分母为JEV选择数。",
        "未标注样本不进入指标分母；缺失任一基线、输入指纹不一致或预测晚于标签的有标签样本会拒绝生成报告。",
        "采集来源和原始回复仍需人工核验；文件字段不能自行证明用户身份或真实采集时间。",
        "旁路选择尚未实际执行时，不得将与标签不一致记作用户撤销。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--validation", required=True)
    parser.add_argument("--output", required=True, help="新目录，拒绝覆盖旧报告")
    parser.add_argument("--revocations")
    args = parser.parse_args()
    generate_report(args.discovery, args.validation, args.output, args.revocations)
    print(str(Path(args.output).resolve()))


if __name__ == "__main__":
    main()
