"""Add the TPC-DS fingerprint inputs to map.json: the pinned queries and schema (tests/fixtures,
tests/helpers.py) and the lines test_execute_tpcds runs outside the fingerprinted stages
(tools/tpcds_stages.py).

Usage: python tools/add_tpcds.py <map.json in> <sqlglot checkout> <tpcds_other.json> <map.json out>
"""
import json, os, sys
src_map, checkout, other, out = sys.argv[1:5]
M = json.load(open(src_map))
sys.path.insert(0, checkout)
from tests.helpers import load_sql_fixture_pairs, TPCDS_SCHEMA
q = [[bool(m.get("execute")), s] for m, s, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")]
M["tpcds"] = {"queries": q, "schema": TPCDS_SCHEMA, "other": json.load(open(other))}
json.dump(M, open(out, "w"), separators=(",", ":"))
print(f"TPC-DS: {len(q)} queries, {sum(a for a, _ in q)} executed", file=sys.stderr)
