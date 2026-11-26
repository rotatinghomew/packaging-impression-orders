"""核对业务方交换材料的格式和来源引用。"""
from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]


def source(name):
    return json.loads((ROOT / "inputs" / (name + ".json")).read_text(encoding="utf-8"))


class Exchanges(unittest.TestCase):
    def test_印次与位置属于不同实物组(self):
        design, press = source("designs"), source("press")
        self.assertEqual((design["plate"], design["revision"]), (press["plate"], press["revision"]))
        runs = {row["run"]: row for row in press["reports"] if row["kind"] == "printed"}
        stocks = source("stock")
        self.assertTrue(all(row["site"] in runs[row["run"]]["count"] for row in stocks))
        self.assertLess(runs["I-26"]["at"], design["withdrawal"]["effective"])
        self.assertGreater(runs["I-27"]["at"], design["withdrawal"]["effective"])
        rework = next(row for row in stocks if row["kind"] == "overlaid")
        self.assertEqual(rework["approval"], design["overlay"]["id"])
