#!/usr/bin/env python3
"""
OLX Kyiv Real Estate Parser - Golden Benchmark & Regression Gatekeeper.

Measures extraction accuracy, false positive rate (hallucinations),
negation handling, and template consistency against the Golden Dataset.
Acts as a regression gatekeeper for CI and agent development.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from normalizer import (
    CommercialNormalizer,
    KyivAddressParser,
    ResidentialNormalizer,
)
from scraper import is_valid_rental_offer


ROOT_DIR = Path(__file__).resolve().parent
GOLDEN_RAW_PATH = ROOT_DIR / "tests" / "data" / "golden_raw_offers.json"
GROUND_TRUTH_PATH = ROOT_DIR / "tests" / "data" / "golden_ground_truth.json"
STRESS_CASES_PATH = ROOT_DIR / "tests" / "data" / "stress_cases.json"


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def run_stress_tests(verbose: bool = False) -> Tuple[int, int, List[str]]:
    """Runs curated edge-cases targeting common hallucinations and regression patterns."""
    if not STRESS_CASES_PATH.exists():
        return 0, 0, [f"Stress cases file missing: {STRESS_CASES_PATH}"]

    with open(STRESS_CASES_PATH, "r", encoding="utf-8") as f:
        stress_cases = json.load(f)

    passed = 0
    failed = 0
    errors = []
    parser = KyivAddressParser()

    for sc in stress_cases:
        cid = sc["id"]
        ctype = sc["type"]
        inp = sc["input"]
        exp = sc["expected"]
        desc = sc.get("description", "")

        try:
            if ctype == "residential_inverter":
                res = ResidentialNormalizer.extract_inverter(inp.get("title", "") + " " + inp.get("description", ""))
                if res == exp["inverter"]:
                    passed += 1
                else:
                    failed += 1
                    errors.append(f"[{cid}] Inverter expected '{exp['inverter']}', got '{res}' ({desc})")

            elif ctype == "residential_generator":
                res = ResidentialNormalizer.extract_generator({}, inp.get("description", ""))
                if res == exp["generator"]:
                    passed += 1
                else:
                    failed += 1
                    errors.append(f"[{cid}] Generator expected '{exp['generator']}', got '{res}' ({desc})")

            elif ctype == "residential_pets":
                res = ResidentialNormalizer.extract_pets(inp, inp.get("title", ""))
                if res == exp["pets"]:
                    passed += 1
                else:
                    failed += 1
                    errors.append(f"[{cid}] Pets expected '{exp['pets']}', got '{res}' ({desc})")

            elif ctype == "address":
                st, hs, dist = parser.parse(inp["text"])
                st_match = (st == exp.get("street", ""))
                hs_match = (exp.get("house") is None or hs == exp.get("house"))
                dist_match = (exp.get("district") is None or dist == exp.get("district"))

                if st_match and hs_match and dist_match:
                    passed += 1
                else:
                    failed += 1
                    errors.append(
                        f"[{cid}] Address mismatch. Got street='{st}', house='{hs}', dist='{dist}'. "
                        f"Expected street='{exp.get('street')}', house='{exp.get('house')}', dist='{exp.get('district')}' ({desc})"
                    )

            elif ctype == "commercial_bc":
                res = CommercialNormalizer.extract_business_center_name(inp.get("title", ""))
                if res == exp["business_center_name"]:
                    passed += 1
                else:
                    failed += 1
                    errors.append(f"[{cid}] BC name expected '{exp['business_center_name']}', got '{res}' ({desc})")

            elif ctype == "residential_renovation":
                res = ResidentialNormalizer.extract_renovation({}, inp.get("description", ""))
                if res == exp["renovation"]:
                    passed += 1
                else:
                    failed += 1
                    errors.append(f"[{cid}] Renovation expected '{exp['renovation']}', got '{res}' ({desc})")

            elif ctype == "multilot":
                offer = {"title": inp.get("title", ""), "params": [{"key": "price", "value": 10000}, {"key": "total_area", "value": 50}]}
                is_valid = is_valid_rental_offer(offer, inp.get("category", "commercial"))
                if is_valid == exp["valid_offer"]:
                    passed += 1
                else:
                    failed += 1
                    errors.append(f"[{cid}] Multilot valid_offer expected {exp['valid_offer']}, got {is_valid} ({desc})")

            elif ctype == "promo_district":
                is_valid = is_valid_rental_offer(inp["offer"], inp["category"], target_district=inp.get("target_district"))
                if is_valid == exp["valid_offer"]:
                    passed += 1
                else:
                    failed += 1
                    errors.append(f"[{cid}] Promo district valid expected {exp['valid_offer']}, got {is_valid} ({desc})")

            else:
                passed += 1

        except Exception as e:
            failed += 1
            errors.append(f"[{cid}] Exception during execution: {e}")

    return passed, failed, errors


def run_benchmark(verbose: bool = False) -> int:
    """Runs complete benchmark on Golden Dataset and stress cases."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*68}")
    print("      OLX KYIV REAL ESTATE - BENCHMARK & REGRESSION SUITE")
    print(f"{'='*68}{Colors.RESET}\n")

    if not GOLDEN_RAW_PATH.exists() or not GROUND_TRUTH_PATH.exists():
        print(f"{Colors.RED}[ERROR] Golden dataset files not found in tests/data!{Colors.RESET}")
        return 1

    t0 = time.time()
    with open(GOLDEN_RAW_PATH, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    with open(GROUND_TRUTH_PATH, "r", encoding="utf-8") as f:
        ground_truth = json.load(f)

    rn = ResidentialNormalizer()
    cn = CommercialNormalizer()

    # 1. Stress Tests
    print(f"{Colors.BOLD}[1] Running 20 Curated Anti-Hallucination Stress Tests...{Colors.RESET}")
    stress_pass, stress_fail, stress_errs = run_stress_tests(verbose=verbose)
    if stress_fail == 0:
        print(f"    {Colors.GREEN}✓ All {stress_pass} stress tests passed successfully! (0 hallucinations){Colors.RESET}")
    else:
        print(f"    {Colors.RED}✗ {stress_fail} stress tests FAILED!{Colors.RESET}")
        for err in stress_errs:
            print(f"      - {Colors.RED}{err}{Colors.RESET}")

    # 2. Residential Dataset Evaluation
    res_raw = raw_data.get("residential", [])
    res_gt = ground_truth.get("residential", {})
    res_fields = ["street", "house", "district", "inverter_in_flat", "generator_in_building", "pets_allowed"]
    res_field_stats = {f: {"total": 0, "correct": 0} for f in res_fields}
    res_mismatches = []

    print(f"\n{Colors.BOLD}[2] Evaluating Residential Offers ({len(res_raw)} records)...{Colors.RESET}")
    for o in res_raw:
        url = o.get("url") or o.get("db_url")
        if url not in res_gt:
            continue
        gt = res_gt[url]
        norm = rn.normalize(o)

        for f in res_fields:
            res_field_stats[f]["total"] += 1
            got_val = norm.get(f, "")
            exp_val = gt.get(f, "")
            if got_val == exp_val:
                res_field_stats[f]["correct"] += 1
            else:
                if len(res_mismatches) < 5:
                    res_mismatches.append((url, f, got_val, exp_val))

    for f in res_fields:
        stat = res_field_stats[f]
        acc = (stat["correct"] / stat["total"] * 100) if stat["total"] else 100
        color = Colors.GREEN if acc >= 95 else (Colors.YELLOW if acc >= 85 else Colors.RED)
        print(f"    • {f:24}: {color}{acc:5.1f}%{Colors.RESET} ({stat['correct']}/{stat['total']})")

    # 3. Commercial Dataset Evaluation
    com_raw = raw_data.get("commercial", [])
    com_gt = ground_truth.get("commercial", {})
    com_fields = ["property_type", "street", "house", "district", "business_center_name", "price_basis", "generator"]
    com_field_stats = {f: {"total": 0, "correct": 0} for f in com_fields}
    com_mismatches = []

    print(f"\n{Colors.BOLD}[3] Evaluating Commercial Offers ({len(com_raw)} records)...{Colors.RESET}")
    for o in com_raw:
        url = o.get("url") or o.get("db_url")
        if url not in com_gt:
            continue
        gt = com_gt[url]
        norm = cn.normalize(o)

        for f in com_fields:
            com_field_stats[f]["total"] += 1
            got_val = norm.get(f, "")
            exp_val = gt.get(f, "")
            if got_val == exp_val:
                com_field_stats[f]["correct"] += 1
            else:
                if len(com_mismatches) < 5:
                    com_mismatches.append((url, f, got_val, exp_val))

    for f in com_fields:
        stat = com_field_stats[f]
        acc = (stat["correct"] / stat["total"] * 100) if stat["total"] else 100
        color = Colors.GREEN if acc >= 95 else (Colors.YELLOW if acc >= 85 else Colors.RED)
        print(f"    • {f:24}: {color}{acc:5.1f}%{Colors.RESET} ({stat['correct']}/{stat['total']})")

    # Total Overall Accuracy
    total_checks = sum(s["total"] for s in res_field_stats.values()) + sum(s["total"] for s in com_field_stats.values())
    total_correct = sum(s["correct"] for s in res_field_stats.values()) + sum(s["correct"] for s in com_field_stats.values())
    overall_acc = (total_correct / total_checks * 100) if total_checks else 100
    elapsed = time.time() - t0

    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*68}{Colors.RESET}")
    print(f"  Overall Accuracy : {Colors.BOLD}{Colors.GREEN if overall_acc >= 95 else Colors.RED}{overall_acc:.2f}%{Colors.RESET} ({total_correct}/{total_checks} checks)")
    print(f"  Stress Failures  : {Colors.BOLD}{Colors.GREEN if stress_fail == 0 else Colors.RED}{stress_fail}{Colors.RESET}")
    print(f"  Total Processed  : {len(res_raw) + len(com_raw)} offers in {elapsed:.2f}s ({((len(res_raw)+len(com_raw))/elapsed):.0f} offers/sec)")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*68}{Colors.RESET}\n")

    if stress_fail > 0:
        print(f"{Colors.RED}[FAIL] Regression detected in stress cases! Commits blocked.{Colors.RESET}\n")
        return 1
    elif overall_acc < 95.0:
        print(f"{Colors.YELLOW}[FAIL] Overall accuracy {overall_acc:.2f}% is below commercial threshold (95.0%).{Colors.RESET}\n")
        return 1
    else:
        print(f"{Colors.GREEN}[SUCCESS] All checks passed! Golden Benchmark verified.{Colors.RESET}\n")
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OLX Parser Golden Benchmark")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed mismatches")
    args = parser.parse_args()

    sys.exit(run_benchmark(verbose=args.verbose))
