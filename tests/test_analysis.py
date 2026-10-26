import copy
import csv
import importlib.util
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "market-evidence"
spec = importlib.util.spec_from_file_location("analyzer", SKILL / "scripts" / "analyze_table.py")
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)

try:
    import openpyxl
except ImportError:
    openpyxl = None


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.config_path = SKILL / "assets" / "examples" / "analysis-config.json"
        self.input_path = SKILL / "assets" / "examples" / "market.csv"
        self.config = json.loads(self.config_path.read_text())
        self.rows = analyzer.read_table(self.input_path, self.config)

    def test_hand_calculated_example_and_distinct_metric_populations(self):
        result, review = analyzer.analyze(self.rows, self.config)
        self.assertEqual((result["input_rows"], result["eligible_rows"], result["excluded_rows"]), (9, 7, 2))
        self.assertEqual(result["metrics"]["amount_total"], Decimal("8200"))
        self.assertEqual(result["metrics"]["quantity_total"], Decimal("14"))
        self.assertEqual(result["metrics"]["amount_n"], 6)
        self.assertEqual(result["metrics"]["quantity_n"], 6)
        self.assertEqual(result["metrics"]["project_count"], 6)
        self.assertEqual(result["price_n"], 6)
        medians = {(r["model"], r["configuration"], r["month"]): r["median_unit_price"] for r in result["price_strata"]}
        self.assertEqual(medians[("A100", "standard", "2025-01")], Decimal("550"))
        self.assertEqual(medians[("A100", "standard", "2025-02")], Decimal("750"))
        self.assertEqual(len(medians), 4)
        unknown = next(g for g in result["groups"] if g["brand"] == "")
        self.assertIsNone(unknown["amount_share"])
        self.assertEqual(unknown["quantity_total"], 1)
        self.assertEqual(sum(g["amount_total"] or 0 for g in result["groups"]), 8200)
        self.assertAlmostEqual(sum(g["amount_share"] or 0 for g in result["groups"]), Decimal(1))
        self.assertTrue(any(r["record_id"] == "r005" and r["metric"] == "quantity" for r in review))

    def test_all_conflicting_ids_excluded_not_arbitrary_first(self):
        rows = copy.deepcopy(self.rows[:1]) * 2
        rows[1] = dict(rows[1], amount="2000", _row=3)
        result, review = analyzer.analyze(rows, self.config)
        self.assertEqual(result["eligible_rows"], 0)
        self.assertEqual(result["excluded_rows"], 2)
        self.assertEqual({r["reason"] for r in review}, {"conflicting_record_id"})

    def test_distinct_lines_same_project_and_non_additive_groups(self):
        rows = copy.deepcopy(self.rows[:2])
        rows[1]["brand"] = "Brand B"
        result, _ = analyzer.analyze(rows, self.config)
        self.assertEqual(result["eligible_rows"], 2)
        self.assertEqual(result["metrics"]["project_count"], 1)
        self.assertEqual(sum(g["project_count"] for g in result["groups"]), 2)

    def test_empty_scope_nulls_and_zero_amount_denominator(self):
        result, _ = analyzer.analyze([], self.config)
        self.assertIsNone(result["metrics"]["amount_total"])
        self.assertEqual(result["groups"], [])
        rows = [dict(self.rows[0], amount="0", unit_price="")]
        result, _ = analyzer.analyze(rows, self.config)
        self.assertEqual(result["metrics"]["amount_total"], 0)
        self.assertIsNone(result["groups"][0]["amount_share"])

    def test_scope_excludes_wrong_units_stage_tax_currency_dates_and_grain(self):
        for field, value in [("stage", "contract"), ("currency", "USD"), ("quantity_unit", "set"),
                             ("tax_basis", "tax_exclusive"), ("amount_basis", "package"),
                             ("date", "2024-12-31"), ("date", "2025-02-30"), ("date", "01/02/2025")]:
            with self.subTest(field=field, value=value):
                result, review = analyzer.analyze([dict(self.rows[0], **{field: value})], self.config)
                self.assertEqual(result["eligible_rows"], 0)
                self.assertTrue(review[0]["reason"].startswith("outside_scope:"))

    def test_numeric_parser_does_not_guess_missing_locale_or_scale(self):
        self.assertEqual(analyzer.number("1,234.50"), Decimal("1234.50"))
        for value in ["", "NA", "NaN", "Infinity", "-1", "1,23", "1e3", "1万元", "12%", "=1+1"]:
            self.assertIsNone(analyzer.number(value), value)
        self.assertIsNone(analyzer.number("0", positive=True))
        self.assertEqual(analyzer.number("0"), 0)

    def test_price_derivation_requires_explicit_choice_and_does_not_fix_bad_value(self):
        row = dict(self.rows[0], unit_price="")
        result, _ = analyzer.analyze([row], self.config)
        self.assertEqual(result["price_n"], 0)
        self.config["derive_unit_price"] = True
        result, review = analyzer.analyze([row], self.config)
        self.assertEqual(result["price_strata"][0]["median_unit_price"], 500)
        self.assertTrue(any(r["reason"] == "unit_price_derived_from_line_amount" for r in review))
        result, _ = analyzer.analyze([dict(row, unit_price="bad")], self.config)
        self.assertEqual(result["price_n"], 0)

    def test_price_conflict_and_missing_model_only_remove_price(self):
        for row in [dict(self.rows[0], unit_price="900"), dict(self.rows[0], model="")]:
            result, _ = analyzer.analyze([row], self.config)
            self.assertEqual(result["price_n"], 0)
            self.assertEqual(result["metrics"]["amount_total"], 1000)
            self.assertEqual(result["metrics"]["quantity_total"], 2)

    def test_missing_project_key_does_not_erase_amount(self):
        result, review = analyzer.analyze([dict(self.rows[0], project_id="")], self.config)
        self.assertEqual(result["metrics"]["project_count"], 0)
        self.assertEqual(result["metrics"]["project_missing_rows"], 1)
        self.assertEqual(result["metrics"]["amount_total"], 1000)
        self.assertTrue(any(r["metric"] == "project_count" for r in review))

    def test_csv_formula_injection_escape_and_empty_bucket(self):
        payload = analyzer.csv_bytes([{"label": " =1+1", "amount": Decimal("1.20")}, {"label": "", "amount": None}], ["label", "amount"])
        rows = list(csv.DictReader(io.StringIO(payload.decode("utf-8-sig"))))
        self.assertEqual(rows[0], {"label": "' =1+1", "amount": "1.20"})
        self.assertEqual(rows[1], {"label": "", "amount": ""})

    def test_real_output_hashes_no_input_mutation_and_no_overwrite(self):
        original = self.input_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result"
            analyzer.run(self.input_path, self.config_path, output)
            manifest = json.loads((output / "manifest.json").read_text())
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["metrics"]["amount_total"], "8200")
            self.assertEqual(manifest["input"]["sha256"], analyzer.sha256(original))
            self.assertEqual(manifest["input"]["name"], "market.csv")
            self.assertFalse(manifest["sent"])
            self.assertEqual(len(list(output.iterdir())), 6)
            for item in manifest["outputs"]:
                self.assertEqual(analyzer.sha256((output / item["name"]).read_bytes()), item["sha256"])
            with self.assertRaisesRegex(ValueError, "已存在"):
                analyzer.run(self.input_path, self.config_path, output)
        self.assertEqual(self.input_path.read_bytes(), original)

    def test_rejects_duplicate_headers_wrong_width_and_incomplete_config(self):
        with self.assertRaises(ValueError):
            analyzer.checked_rows(["a", "a"], [], self.config)
        headers = list(self.config["columns"].values())
        with self.assertRaises(ValueError):
            analyzer.checked_rows(headers, [(2, ["short"])], self.config)
        config = copy.deepcopy(self.config)
        del config["scope"]["tax_basis"]
        with self.assertRaises(ValueError):
            analyzer.validate_config(config)

    @unittest.skipIf(openpyxl is None, "optional XLSX reader not installed")
    def test_xlsx_read_matches_csv_and_rejects_formulas(self):
        # This is a temporary test fixture, not a user-facing workbook deliverable.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "Detail"
            for row in csv.reader(io.StringIO(self.input_path.read_text())):
                sheet.append(row)
            workbook.save(path)
            self.assertEqual(analyzer.read_table(path, self.config, "Detail"), self.rows)
            with self.assertRaisesRegex(ValueError, "指定"):
                analyzer.read_table(path, self.config)
            sheet["M2"] = "=500*2"
            workbook.save(path)
            with self.assertRaisesRegex(ValueError, "公式"):
                analyzer.read_table(path, self.config, "Detail")
            workbook.close()

    @unittest.skipIf(openpyxl is None, "optional XLSX reader not installed")
    def test_xlsx_bad_declared_dimensions_still_reads_actual_data(self):
        import zipfile
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original.xlsx"
            changed = Path(tmp) / "changed.xlsx"
            workbook = openpyxl.Workbook()
            workbook.active.title = "Detail"
            for row in csv.reader(io.StringIO(self.input_path.read_text())):
                workbook.active.append(row)
            workbook.save(original)
            workbook.close()
            with zipfile.ZipFile(original) as src, zipfile.ZipFile(changed, "w") as dst:
                for item in src.infolist():
                    content = src.read(item.filename)
                    if item.filename == "xl/worksheets/sheet1.xml":
                        root = ET.fromstring(content)
                        dimension = root.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}dimension")
                        self.assertIsNotNone(dimension)
                        self.assertEqual(dimension.get("ref"), "A1:O10")
                        dimension.set("ref", "A1:A1")
                        content = ET.tostring(root, encoding="utf-8")
                    dst.writestr(item, content)
            self.assertEqual(len(analyzer.read_table(changed, self.config, "Detail")), 9)


if __name__ == "__main__":
    unittest.main()
