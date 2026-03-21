"""
TAIKO EA — Transaction Data Reset Script
Clears all sales/commission data so you can start fresh testing.
Keeps: users, agents, projects, project_units, system_settings, rank_promotion_log
"""

import sqlite3
import os

DB_PATH = "real_estate.db"

if not os.path.exists(DB_PATH):
    print(f"❌ Database not found at: {DB_PATH}")
    print("   Run this script from the same folder as your app.py")
    exit(1)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

print("=" * 55)
print("  TAIKO EA — Transaction Reset")
print("=" * 55)

# Show counts before
tables_to_clear = [
    ("property_listings",        "Sales listings"),
    ("commission_payments",      "Commission payments"),
    ("commission_calculations",  "Commission calculations"),
    ("commission_distributions", "Commission distributions"),
    ("taiko_commission_entries", "TAIKO commission entries"),
    ("upline_commissions",       "Upline commissions"),
    ("payment_vouchers",         "Payment vouchers"),
    ("documents",                "Documents / uploads"),
    ("agent_notifications",      "Agent notifications"),
    ("email_logs",               "Email logs"),
]

print("\nCurrent row counts:")
for table, label in tables_to_clear:
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = cursor.fetchone()[0]
        print(f"  {label:<30} {count:>6} rows")
    except Exception:
        print(f"  {label:<30}  (table not found — skipping)")

print()
confirm = input("⚠️  Type YES to clear all transaction data: ").strip()
if confirm != "YES":
    print("Aborted — no changes made.")
    conn.close()
    exit(0)

print("\nClearing tables...")
errors = []
for table, label in tables_to_clear:
    try:
        cursor.execute(f"DELETE FROM {table}")
        deleted = cursor.rowcount
        print(f"  ✅ {label:<30} {deleted} rows deleted")
    except Exception as e:
        errors.append((label, str(e)))
        print(f"  ⚠️  {label:<30} skipped ({e})")

# Reset agent cumulative_gross and total_commission back to 0
# (but keep their rank if it was manually set via admin — or reset ranks too)
print()
reset_rank = input("Reset agent cumulative_gross + total_commission to 0? (YES/no): ").strip()
if reset_rank.upper() != "NO":
    cursor.execute("""
        UPDATE users
        SET cumulative_gross = 0,
            total_commission = 0
        WHERE role = 'agent'
    """)
    print(f"  ✅ Agent totals reset ({cursor.rowcount} agents)")

reset_ranks = input("Also reset all agent ranks back to REN / 70%? (yes/NO): ").strip()
if reset_ranks.upper() == "YES":
    cursor.execute("""
        UPDATE users
        SET agent_rank      = 'REN',
            commission_rate = 70.0
        WHERE role = 'agent'
    """)
    # Clear rank promotion log too
    cursor.execute("DELETE FROM rank_promotion_log")
    print(f"  ✅ All agent ranks reset to REN (70%)")
    print(f"  ✅ Rank promotion log cleared")

conn.commit()
conn.close()

print()
print("=" * 55)
if errors:
    print(f"  Done with {len(errors)} warning(s) above.")
else:
    print("  ✅ Reset complete — ready for fresh testing!")
print("=" * 55)