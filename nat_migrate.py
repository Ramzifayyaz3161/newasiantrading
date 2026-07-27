"""
NAT v3 → v4 Migration Script
Run ONCE after deploying nat_web_app.py v4 to Railway.
Safe to run multiple times (checks before altering).

Usage:
  python nat_migrate.py
or via Railway deploy hook / shell.
"""
import os, sys
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///nat_ops.db')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

engine = create_engine(DATABASE_URL)

MIGRATIONS = [
    # Document table
    ("document", "party_trn",    "VARCHAR(50)"),
    ("document", "gdrive_url",   "VARCHAR(500)"),
    # CatalogItem table
    ("catalog_item", "cost_price", "FLOAT DEFAULT 0"),
    ("catalog_item", "markup_pct", "FLOAT DEFAULT 0"),
]

NEW_TABLES = [
    # ChequeRecord
    """
    CREATE TABLE IF NOT EXISTS cheque_record (
        id SERIAL PRIMARY KEY,
        company_name VARCHAR(200),
        party_trn VARCHAR(50),
        invoice_ref VARCHAR(30),
        delivery_note_no VARCHAR(30),
        cheque_number VARCHAR(50),
        cheque_date VARCHAR(20),
        bank_name VARCHAR(100),
        clearance_date VARCHAR(20),
        signatory VARCHAR(100),
        total_amount FLOAT DEFAULT 0,
        status VARCHAR(20) DEFAULT 'Pending',
        notes TEXT,
        added_by VARCHAR(100),
        added_at TIMESTAMP DEFAULT NOW()
    )
    """,
    # ReceiptVoucher
    """
    CREATE TABLE IF NOT EXISTS receipt_voucher (
        id SERIAL PRIMARY KEY,
        ref VARCHAR(30) UNIQUE,
        date VARCHAR(20),
        received_from VARCHAR(200),
        amount FLOAT DEFAULT 0,
        invoice_ref VARCHAR(30),
        payment_method VARCHAR(30),
        cheque_number VARCHAR(50),
        bank_name VARCHAR(100),
        received_by VARCHAR(100),
        notes TEXT,
        added_at TIMESTAMP DEFAULT NOW(),
        gdrive_url VARCHAR(500)
    )
    """,
    # CompanySetting
    """
    CREATE TABLE IF NOT EXISTS company_setting (
        id SERIAL PRIMARY KEY,
        key VARCHAR(100) UNIQUE NOT NULL,
        value TEXT
    )
    """,
]

def run():
    print("NAT v3 → v4 Migration")
    print("=" * 40)
    with engine.connect() as conn:
        # Create new tables
        for sql in NEW_TABLES:
            try:
                conn.execute(text(sql))
                conn.commit()
                tname = sql.strip().split('\n')[1].split('(')[0].replace('CREATE TABLE IF NOT EXISTS','').strip()
                print(f"Table OK: {tname}")
            except Exception as e:
                print(f"Table note: {e}")

        # Add missing columns
        for table, col, col_type in MIGRATIONS:
            try:
                conn.execute(text(f"SELECT {col} FROM {table} LIMIT 1"))
                print(f"Column exists: {table}.{col}")
            except:
                try:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"))
                    conn.commit()
                    print(f"Added: {table}.{col} ({col_type})")
                except Exception as e:
                    print(f"Error adding {table}.{col}: {e}")

        # Add RCV counter if missing
        try:
            conn.execute(text("INSERT INTO doc_counter (doc_type, prefix, last_num) "
                              "VALUES ('RCV','RCV-2026-',0) ON CONFLICT DO NOTHING"))
            conn.commit()
            print("RCV counter ensured")
        except Exception as e:
            print(f"Counter note: {e}")

    print("\nMigration complete. Deploy v4 app.")

if __name__ == '__main__':
    run()
