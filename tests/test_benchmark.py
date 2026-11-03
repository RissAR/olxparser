"""
Unit test wrapper for Golden Benchmark suite.
Ensures pytest/unittest discover runs the regression gatekeeper automatically.
"""

import unittest
from benchmark import run_stress_tests, run_benchmark


class TestGoldenBenchmark(unittest.TestCase):
    def test_stress_cases_zero_hallucinations(self):
        passed, failed, errors = run_stress_tests()
        self.assertEqual(failed, 0, f"Stress test regressions found:\n" + "\n".join(errors))
        self.assertGreaterEqual(passed, 20)

    def test_overall_benchmark_passes(self):
        code = run_benchmark()
        self.assertEqual(code, 0, "Golden benchmark failed accuracy or stress criteria")


if __name__ == "__main__":
    unittest.main()
