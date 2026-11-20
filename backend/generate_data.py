"""Builds data/payments.db — synthetic merchants, transactions, bank
accounts and settlements for the last 6 months.

Deliberately seeded scenarios (for the "minimum successful demonstration"):
  1. A mix-shift: Wallet payments in the East region have a structurally
     lower success rate. In the most recent month their share of total
     volume jumps, dragging the OVERALL success rate down even though every
     individual segment's own success rate is flat or improved slightly.
     This is the "success rate declined — why?" question the copilot must
     answer with a mix-shift explanation rather than a single root cause.
  2. Data quality issues: ~1.5% of transactions are duplicated (same
     external_ref appearing twice, simulating a double-recorded callback)
     and ~1% have a NULL amount (a dropped field). The copilot must warn
     about these rather than silently including/excluding them.
"""
import random
import sqlite3
import datetime
import os

random.seed(42)

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "..", "data", "payments.db")

SEGMENTS = ["Retail", "Travel", "Food & Beverage", "Electronics", "Subscription"]
REGIONS = ["North", "South", "East", "West"]
RISK_TIERS = ["Low", "Medium", "High"]
PAYMENT_METHODS = ["Card", "UPI", "NetBanking", "Wallet"]

# Baseline success probability per (payment_method, region). East+Wallet is
# the structurally weak segment used for the mix-shift scenario.
BASE_SUCCESS = {
    ("Card", "North"): 0.95, ("Card", "South"): 0.95, ("Card", "East"): 0.93, ("Card", "West"): 0.95,
    ("UPI", "North"): 0.97, ("UPI", "South"): 0.97, ("UPI", "East"): 0.96, ("UPI", "West"): 0.97,
    ("NetBanking", "North"): 0.90, ("NetBanking", "South"): 0.90, ("NetBanking", "East"): 0.88, ("NetBanking", "West"): 0.90,
    ("Wallet", "North"): 0.92, ("Wallet", "South"): 0.91, ("Wallet", "East"): 0.74, ("Wallet", "West"): 0.92,
}

N_MERCHANTS = 180
MONTHS_BACK = 6
TXNS_PER_DAY_RANGE = (40, 90)


def gen_merchants(conn):
    rows = []
    for i in range(1, N_MERCHANTS + 1):
        mid = f"M{i:04d}"
        onboarded = datetime.date.today() - datetime.timedelta(days=random.randint(60, 900))
        rows.append((
            mid,
            f"Merchant {i:04d}",
            random.choice(SEGMENTS),
            random.choice(REGIONS),
            onboarded.isoformat(),
            random.choices(RISK_TIERS, weights=[0.6, 0.3, 0.1])[0],
        ))
    conn.executemany(
        "INSERT INTO merchants (merchant_id, name, segment, region, onboarded_date, risk_tier) VALUES (?,?,?,?,?,?)",
        rows,
    )
    return {r[0]: {"region": r[3]} for r in rows}


def gen_bank_accounts(conn, merchant_ids):
    rows = []
    for idx, mid in enumerate(merchant_ids, start=1):
        bank_id = f"B{idx:04d}"
        full_acct = f"{random.randint(10**11, 10**12 - 1)}"
        rows.append((
            bank_id, mid, random.choice([
                "HDFC Bank", "ICICI Bank", "Axis Bank", "SBI", "Kotak Mahindra",
            ]),
            "XXXX" + full_acct[-4:],
            full_acct,
            f"BANK0{random.randint(100000,999999)}",
        ))
    conn.executemany(
        "INSERT INTO bank_accounts (bank_id, merchant_id, bank_name, account_number_masked, account_number_full, ifsc) VALUES (?,?,?,?,?,?)",
        rows,
    )
    return {mid: rows[i][0] for i, mid in enumerate(merchant_ids)}


def wallet_east_share_for_day(day_index, total_days):
    """Returns the target share of Wallet+East volume for a given day.
    Ramps up sharply in the final ~30 days to create the mix-shift.
    """
    days_from_end = total_days - day_index
    if days_from_end <= 30:
        # ramps from 8% up to 30% over the last 30 days
        progress = (30 - days_from_end) / 30.0
        return 0.08 + progress * 0.22
    return 0.08


def gen_transactions_and_settlements(conn, merchant_ids, bank_of):
    today = datetime.date.today()
    total_days = MONTHS_BACK * 30
    txn_seq = 1
    settlement_seq = 1
    duplicate_pool = []

    txn_rows = []
    settlement_rows = []

    for day_index in range(total_days):
        day = today - datetime.timedelta(days=total_days - day_index)
        n_txns = random.randint(*TXNS_PER_DAY_RANGE)
        wallet_east_target = wallet_east_share_for_day(day_index, total_days)

        for _ in range(n_txns):
            mid = random.choice(merchant_ids)

            # Decide payment_method/region combo, nudging towards wallet+east
            # according to the day's target share to create the mix shift.
            if random.random() < wallet_east_target:
                pm, region = "Wallet", "East"
            else:
                pm = random.choice(PAYMENT_METHODS)
                region = random.choice(REGIONS)

            p_success = BASE_SUCCESS[(pm, region)]
            status = "SUCCESS" if random.random() < p_success else random.choice(["FAILED", "FAILED", "PENDING"])

            amount = round(random.uniform(150, 45000), 2)
            # ~1% missing amount (data quality issue)
            if random.random() < 0.01:
                amount = None

            txn_id = f"T{txn_seq:07d}"
            txn_seq += 1
            created_at = datetime.datetime.combine(
                day, datetime.time(random.randint(0, 23), random.randint(0, 59), random.randint(0, 59))
            )
            external_ref = f"REF{txn_seq:07d}"
            bank_id = bank_of[mid]

            row = (txn_id, mid, pm, amount, "INR", status, created_at.isoformat(sep=" "), bank_id, external_ref)
            txn_rows.append(row)

            # ~1.5% chance this txn gets double-recorded under a new txn_id
            # but the SAME external_ref (simulates a duplicated callback).
            if random.random() < 0.015:
                duplicate_pool.append((row, created_at))

            if status == "SUCCESS":
                settle_status = random.choices(
                    ["SETTLED", "DELAYED", "FAILED"], weights=[0.9, 0.08, 0.02]
                )[0]
                settled_at = created_at + datetime.timedelta(days=random.choice([1, 1, 2, 3]))
                settlement_rows.append((
                    f"S{settlement_seq:07d}", mid, txn_id,
                    amount if amount else 0.0, settled_at.isoformat(sep=" "), settle_status,
                ))
                settlement_seq += 1

    # materialize duplicates as genuinely separate rows (new txn_id, same external_ref)
    for row, created_at in duplicate_pool:
        dup_id = f"T{txn_seq:07d}"
        txn_seq += 1
        dup_created = created_at + datetime.timedelta(minutes=random.randint(1, 15))
        dup_row = (dup_id, row[1], row[2], row[3], row[4], row[5], dup_created.isoformat(sep=" "), row[7], row[8])
        txn_rows.append(dup_row)

    conn.executemany(
        "INSERT INTO transactions (txn_id, merchant_id, payment_method, amount, currency, status, created_at, bank_id, external_ref) VALUES (?,?,?,?,?,?,?,?,?)",
        txn_rows,
    )
    conn.executemany(
        "INSERT INTO settlements (settlement_id, merchant_id, txn_id, settled_amount, settled_at, status) VALUES (?,?,?,?,?,?)",
        settlement_rows,
    )
    print(f"Inserted {len(txn_rows)} transactions ({len(duplicate_pool)} duplicated), {len(settlement_rows)} settlements.")


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE merchants (
            merchant_id TEXT PRIMARY KEY,
            name TEXT, segment TEXT, region TEXT,
            onboarded_date TEXT, risk_tier TEXT
        );
        CREATE TABLE bank_accounts (
            bank_id TEXT PRIMARY KEY,
            merchant_id TEXT, bank_name TEXT,
            account_number_masked TEXT, account_number_full TEXT, ifsc TEXT
        );
        CREATE TABLE transactions (
            txn_id TEXT PRIMARY KEY,
            merchant_id TEXT, payment_method TEXT, amount REAL, currency TEXT,
            status TEXT, created_at TEXT, bank_id TEXT, external_ref TEXT
        );
        CREATE TABLE settlements (
            settlement_id TEXT PRIMARY KEY,
            merchant_id TEXT, txn_id TEXT, settled_amount REAL,
            settled_at TEXT, status TEXT
        );
        CREATE INDEX idx_txn_created ON transactions(created_at);
        CREATE INDEX idx_txn_merchant ON transactions(merchant_id);
        CREATE INDEX idx_settle_txn ON settlements(txn_id);
        """
    )
    merchants = gen_merchants(conn)
    merchant_ids = list(merchants.keys())
    bank_of = gen_bank_accounts(conn, merchant_ids)
    gen_transactions_and_settlements(conn, merchant_ids, bank_of)
    conn.commit()
    conn.close()
    print(f"Wrote {DB_PATH}")


if __name__ == "__main__":
    main()
