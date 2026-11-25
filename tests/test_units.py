"""Pure unit tests — no database or containers needed."""
from datetime import UTC, datetime, timedelta

from api.masking import mask_cnic, mask_msisdn
from common.integrity import evidence_hash, normalise
from simulator.generate import generate
from worker.graph_worker import Edge, find_chains, find_cycles, index

T0 = datetime(2026, 9, 10, 10, tzinfo=UTC)
S4 = {"min_hops": 4, "window_hours": 6, "hop_ratio_low": 0.85, "hop_ratio_high": 1.0}
S5 = {"min_cycle_len": 3, "max_cycle_len": 6, "window_hours": 72, "min_return_ratio": 0.7}


def test_masking():
    assert mask_cnic("35202-1234567-3") == "35202-*******-3"
    assert mask_msisdn("0300-1234567") == "0300-***4567"
    assert mask_cnic(None) is None


def test_evidence_hash_survives_json_roundtrip():
    ev = normalise({"ts": T0, "amount": 24500.0, "n": 3, "txns": [{"a": 1.5}]})
    assert evidence_hash(ev) == evidence_hash(normalise(ev))
    tampered = dict(ev, amount=24400.0)
    assert evidence_hash(ev) != evidence_hash(tampered)


def _e(i, h, s, d, amt):
    return Edge(f"t{i}", T0 + timedelta(hours=h), s, d, amt)


def test_layering_chain_detected_and_reported_once():
    edges = [_e(1, 0, "A", "B", 100000), _e(2, 0.5, "B", "C", 96000), _e(3, 1, "C", "D", 92000),
             _e(4, 1.5, "D", "E", 90000), _e(5, 2, "E", "F", 87000)]
    out, inc = index(edges)
    chains = find_chains(edges, out, inc, S4, T0 - timedelta(days=1))
    assert len(chains) == 1 and len(chains[0]) == 5     # only the maximal chain from the head


def test_slow_or_shrinking_chain_not_layering():
    slow = [_e(1, 0, "A", "B", 100000), _e(2, 3, "B", "C", 96000), _e(3, 6, "C", "D", 92000),
            _e(4, 9, "D", "E", 90000)]
    shrinking = [_e(1, 0, "A", "B", 100000), _e(2, 0.5, "B", "C", 50000), _e(3, 1, "C", "D", 45000),
                 _e(4, 1.5, "D", "E", 40000)]
    for edges in (slow, shrinking):
        out, inc = index(edges)
        assert find_chains(edges, out, inc, S4, T0 - timedelta(days=1)) == []


def test_round_trip_needs_time_order():
    cyc = [_e(1, 0, "A", "B", 200000), _e(2, 6, "B", "C", 190000), _e(3, 12, "C", "A", 180000)]
    out, _ = index(cyc)
    assert len(find_cycles(cyc, out, S5, T0 - timedelta(days=1))) == 1
    # same accounts, but C->A happened *before* A->B: money didn't actually go round
    backwards = [_e(1, 10, "A", "B", 200000), _e(2, 12, "B", "C", 190000), _e(3, 0, "C", "A", 180000)]
    out, _ = index(backwards)
    assert find_cycles(backwards, out, S5, T0 - timedelta(days=1)) == []


def test_generator_is_deterministic_and_labelled():
    a, b = generate(5, 20, n_customers=1000), generate(5, 20, n_customers=1000)
    assert [t["txn_id"] for t in a.txns] == [t["txn_id"] for t in b.txns]
    illicit_types = {v[1] for v in a.txn_truth.values() if v[0] == "illicit"}
    assert illicit_types  # rings planted
    assert all(a.txns[i]["ts"] <= a.txns[i + 1]["ts"] for i in range(len(a.txns) - 1))
    assert min(t["amount"] for t in a.txns) >= 100       # every txn passes the API's validation
