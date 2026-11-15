"""Pruebas offline de los handlers y del ASL del SF-2. Ejecutar: python -m unittest discover -s part-a/sf2/tests -v"""
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "functions"))
import pipeline  # noqa: E402

ASL = json.loads((HERE.parent / "statemachine.asl.json").read_text(encoding="utf-8"))


def _targets(states):
    """(estado, destino) de todos los saltos, recursivo en Map/Parallel."""
    for name, st in states.items():
        for key in ("Next", "Default"):
            if key in st:
                yield name, st[key]
        for ch in st.get("Choices", []):
            yield name, ch["Next"]
        for c in st.get("Catch", []):
            yield name, c["Next"]


class AslStructure(unittest.TestCase):
    def check_block(self, block):
        states = block["States"]
        self.assertIn(block["StartAt"], states)
        for src, dst in _targets(states):
            self.assertIn(dst, states, "%s apunta a %s que no existe" % (src, dst))
        for name, st in states.items():
            if st["Type"] in ("Succeed", "Fail", "Choice"):
                continue
            self.assertTrue("Next" in st or st.get("End"), "%s no tiene Next ni End" % name)
            if "ItemProcessor" in st:
                self.check_block(st["ItemProcessor"])

    def test_top_level(self):
        self.check_block(ASL)

    def test_has_choice_and_map(self):
        types = {s["Type"] for s in ASL["States"].values()}
        self.assertIn("Map", types)
        inner = ASL["States"]["ProcessZones"]["ItemProcessor"]["States"]
        self.assertEqual(inner["PromoteOrKeep"]["Type"], "Choice")

    def test_placeholders_match_template(self):
        text = (HERE.parent / "statemachine.asl.json").read_text(encoding="utf-8")
        tpl = (HERE.parent / "template.yaml").read_text(encoding="utf-8")
        import re
        for ph in set(re.findall(r"\$\{(\w+)\}", text)):
            self.assertIn("%s:" % ph, tpl, "placeholder %s sin DefinitionSubstitution" % ph)


class Handlers(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ZONES", None)
        os.environ["PROMOTE_MIN_IMPROVEMENT"] = "0.02"

    def test_list_zones_default_and_override(self):
        self.assertEqual(len(pipeline.list_zones({})["zones"]), 3)
        self.assertEqual(pipeline.list_zones({"zones": ["A"]})["zones"], ["A"])

    def test_sync_failure_is_simulated(self):
        with self.assertRaises(RuntimeError):
            pipeline.sync_bigquery({"force_failure": True})
        self.assertGreater(pipeline.sync_bigquery({"run_id": "x"})["rows_synced"], 0)

    def test_mocks_are_deterministic(self):
        a = pipeline.train_zone({"zone_id": "Z1", "run_id": "r1"})
        b = pipeline.train_zone({"zone_id": "Z1", "run_id": "r1"})
        self.assertEqual(a, b)

    def test_evaluate_promote_flag_follows_threshold(self):
        cand = pipeline.train_zone({"zone_id": "Z1", "run_id": "r1"})
        os.environ["PROMOTE_MIN_IMPROVEMENT"] = "-1"
        self.assertTrue(pipeline.evaluate_model(cand)["evaluation"]["promote"])
        os.environ["PROMOTE_MIN_IMPROVEMENT"] = "5"
        self.assertFalse(pipeline.evaluate_model(cand)["evaluation"]["promote"])

    def test_full_flow_like_the_state_machine(self):
        zones = pipeline.list_zones({})["zones"]
        results = []
        for z in zones:
            ev = pipeline.evaluate_model(pipeline.train_zone({"zone_id": z, "run_id": "run-1"}))
            if ev["evaluation"]["promote"]:
                results.append(pipeline.promote_model(ev))
            else:
                results.append({"zone_id": z, "promoted": False, "status": "OK"})
        results.append({"zone_id": "ZFAIL", "promoted": False, "status": "FAILED"})
        out = pipeline.summarize({"run_id": "run-1", "rows_synced": 10, "results": results})
        self.assertEqual(out["zones_total"], 4)
        self.assertEqual(out["failed"], 1)
        self.assertEqual(out["promoted"] + out["kept"] + out["failed"], 4)


if __name__ == "__main__":
    unittest.main()
