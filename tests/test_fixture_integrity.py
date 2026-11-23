import json
from pathlib import Path
import unittest


class FixtureIntegrityTests(unittest.TestCase):
    def test_packaging_revisions_and_reserved_requests_are_present(self):
        data = json.loads((Path(__file__).parents[1] / "data/facts.json").read_text(encoding="utf-8"))
        self.assertEqual({row["revision"] for row in data["packaging"]}, {"R1", "R2"})
        self.assertEqual(len(data["requests"]), 2)
        self.assertEqual(data["transfer"]["state"], "in-transit")


if __name__ == "__main__":
    unittest.main()
