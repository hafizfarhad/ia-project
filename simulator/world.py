"""The synthetic wallet population: customers, agents, billers, employers, external banks."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from common.config import load_yaml

# city -> (CNIC district prefix, population weight)
CITIES: dict[str, tuple[str, float]] = {
    "Karachi": ("42101", 0.22), "Lahore": ("35202", 0.18), "Islamabad": ("61101", 0.07),
    "Rawalpindi": ("37405", 0.07), "Faisalabad": ("33100", 0.08), "Multan": ("36302", 0.06),
    "Peshawar": ("17301", 0.08), "Quetta": ("54400", 0.04), "Hyderabad": ("41303", 0.06),
    "Sialkot": ("34603", 0.04), "Gujranwala": ("34101", 0.05), "Sukkur": ("45504", 0.05),
}
FIRST = ["Ahmed", "Ali", "Hassan", "Usman", "Bilal", "Hamza", "Imran", "Kashif", "Zeeshan", "Faisal",
         "Asad", "Saad", "Waqas", "Tariq", "Naveed", "Junaid", "Adnan", "Shahid", "Fahad", "Omer",
         "Ayesha", "Fatima", "Sana", "Hira", "Mahnoor", "Zainab", "Amna", "Maryam", "Rabia", "Saima",
         "Nida", "Kiran", "Sadia", "Bushra", "Iqra", "Areeba", "Mehwish", "Samina", "Farah", "Uzma"]
LAST = ["Khan", "Ahmed", "Malik", "Butt", "Chaudhry", "Qureshi", "Sheikh", "Mirza", "Siddiqui",
        "Hussain", "Raza", "Abbasi", "Baig", "Javed", "Iqbal", "Rana", "Memon", "Shah", "Awan", "Gill"]
MIN_TXN_PKR = 100.0
MSISDN_PREFIX = ["0300", "0301", "0303", "0310", "0312", "0315", "0321", "0333", "0336", "0345", "0346"]


@dataclass
class Account:
    account_id: str
    kind: str                 # customer | agent | biller | employer | external
    name: str
    city: str | None
    opened_at: datetime
    tier: str | None = None
    cnic: str | None = None
    msisdn: str | None = None
    device_id: str | None = None
    # simulation-only (not sent to the DB)
    home_agent: str | None = None
    contacts: list[str] = field(default_factory=list)

    def db_row(self) -> dict:
        return {k: getattr(self, k) for k in
                ("account_id", "kind", "name", "cnic", "msisdn", "tier", "city", "device_id", "opened_at")}


class World:
    def __init__(self, seed: int, start: datetime, n_customers: int = 2000, n_agents: int = 50,
                 n_billers: int = 20, n_employers: int = 5, n_external: int = 5):
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.start = start
        self.limits = load_yaml("limits")["tiers"]
        self.accounts: dict[str, Account] = {}
        self._txn_seq = 0
        self.txns: list[dict] = []
        self.txn_truth: dict[str, tuple[str, str, str | None]] = {}      # txn_id -> (label, typology, ring)
        self.acct_truth: dict[str, tuple[str, str, str | None]] = {}     # account -> (label, typology, ring)

        cities = list(CITIES)
        weights = np.array([CITIES[c][1] for c in cities])
        weights = weights / weights.sum()

        def opened() -> datetime:
            return start - timedelta(days=int(self.rng.integers(30, 1500)))

        for i in range(n_agents):
            city = cities[i % len(cities)] if i < len(cities) else str(self.rng.choice(cities, p=weights))
            self._add(Account(f"A{i+1:03d}", "agent", f"{self.rng.choice(LAST)} Mobile Shop {city}",
                              city, opened(), device_id=self._device()))
        biller_names = ["Electric Utility", "Gas Utility", "Water Board", "Internet ISP",
                        "Telco Prepaid", "School Fees", "Insurance Co", "Online Store"]
        for i in range(n_billers):
            self._add(Account(f"B{i+1:03d}", "biller", f"{biller_names[i % len(biller_names)]} {i+1}",
                              None, opened()))
        for i in range(n_employers):
            self._add(Account(f"E{i+1:02d}", "employer", f"Employer Pvt Ltd {i+1}",
                              str(self.rng.choice(cities, p=weights)), opened(), tier="L2",
                              device_id=self._device()))
        for i in range(n_external):
            self._add(Account(f"X-BANK-{i+1:02d}", "external", f"External Bank {i+1}", None, opened()))

        all_agents = [a.account_id for a in self.of_kind("agent")]
        agents_by_city: dict[str, list[str]] = {}
        for a in self.of_kind("agent"):
            agents_by_city.setdefault(a.city, []).append(a.account_id)

        # Customer IDs are shuffled so nothing about an ID reveals its role.
        ids = self.rng.permutation(np.arange(100000, 100000 + n_customers * 7))[:n_customers]
        tiers = self.rng.choice(["L0", "L1", "L2"], size=n_customers, p=[0.5, 0.35, 0.15])
        for cid, tier in zip(ids, tiers, strict=True):
            city = str(self.rng.choice(cities, p=weights))
            first, last = self.rng.choice(FIRST), self.rng.choice(LAST)
            self._add(Account(
                f"C{cid}", "customer", f"{first} {last}", city, opened(), tier=str(tier),
                cnic=f"{CITIES[city][0]}-{self.rng.integers(1000000, 9999999)}-{self.rng.integers(1, 10)}",
                msisdn=f"{self.rng.choice(MSISDN_PREFIX)}-{self.rng.integers(1000000, 9999999)}",
                device_id=self._device(),
                home_agent=str(self.rng.choice(agents_by_city.get(city) or all_agents)),
            ))

        customers = [a.account_id for a in self.of_kind("customer")]
        by_city: dict[str, list[str]] = {}
        for c in customers:
            by_city.setdefault(self.accounts[c].city, []).append(c)
        for c in customers:
            acc = self.accounts[c]
            n = int(self.rng.integers(3, 9))
            local = [x for x in by_city[acc.city] if x != c]
            k_local = min(len(local), int(round(n * 0.7)))
            picks = list(self.rng.choice(local, size=k_local, replace=False)) if k_local else []
            picks += list(self.rng.choice(customers, size=n - k_local, replace=False))
            acc.contacts = [str(p) for p in dict.fromkeys(picks) if p != c]

        # Customers not yet given a special role; behaviours/typologies draw from this pool.
        self.free: list[str] = list(self.rng.permutation(customers))

    # ------------------------------------------------------------------ helpers
    def _add(self, acc: Account) -> None:
        self.accounts[acc.account_id] = acc

    def _device(self) -> str:
        return "D" + self.rng.bytes(6).hex()

    def of_kind(self, kind: str) -> list[Account]:
        return [a for a in self.accounts.values() if a.kind == kind]

    def take(self, n: int, tier: str | None = None) -> list[str]:
        """Reserve n customers from the free pool for a special role (optionally forcing their tier)."""
        out = [self.free.pop() for _ in range(n)]
        if tier:
            for a in out:
                self.accounts[a].tier = tier
        return out

    def daily_limit(self, tier: str | None) -> float:
        return float(self.limits[tier or "L2"]["daily"])

    def at(self, day: int, hour: float) -> datetime:
        return self.start + timedelta(days=day, hours=hour)

    def rand_hour(self) -> float:
        """Hour of day with a daytime peak and quiet nights."""
        h = HOUR_P
        return float(self.rng.choice(24, p=h) + self.rng.random())

    def txn(self, ts: datetime, txn_type: str, sender: str, receiver: str, amount: float,
            device: str | None = "sender", truth: tuple[str, str, str | None] | None = None) -> dict:
        s, r = self.accounts[sender], self.accounts[receiver]
        if device == "sender":
            device = s.device_id
        self._txn_seq += 1
        # Wallets have a minimum transaction value; small splits and roundings never go below it.
        amount = max(MIN_TXN_PKR, round(float(amount), 2))
        t = {
            "txn_id": f"T{self.seed:03d}{self._txn_seq:08d}{self.rng.bytes(2).hex()}",
            "ts": ts, "txn_type": txn_type, "sender_id": sender, "receiver_id": receiver,
            "amount": amount, "device_id": device,
            "city": s.city or r.city,
        }
        self.txns.append(t)
        if truth:
            self.txn_truth[t["txn_id"]] = truth
        return t

    def mark(self, accounts: list[str], label: str, typology: str, ring: str | None) -> None:
        for a in accounts:
            # illicit wins over lookalike if an account somehow has both
            if a not in self.acct_truth or label == "illicit":
                self.acct_truth[a] = (label, typology, ring)


_h = np.array([0.2, 0.1, 0.05, 0.05, 0.05, 0.2, 0.5, 1.0, 1.6, 2.0, 2.2, 2.3,
               2.2, 2.0, 2.0, 2.1, 2.2, 2.4, 2.6, 2.7, 2.5, 2.0, 1.2, 0.5])
HOUR_P = _h / _h.sum()
