"""
Run this via Railway console to add missing columns.
"""
import os
from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

engine = create_engine(DATABASE_URL)

fixes = [
    "ALTER TABLE catalog_item ADD COLUMN IF NOT EXISTS cost_price FLOAT DEFAULT 0",
    "ALTER TABLE catalog_item ADD COLUMN IF NOT EXISTS markup_pct FLOAT DEFAULT 0",
    "ALTER TABLE document ADD COLUMN IF NOT EXISTS party_trn VARCHAR(50)",
    "ALTER TABLE document ADD COLUMN IF NOT EXISTS gdrive_url VARCHAR(500)",
    """CREATE TABLE IF NOT EXISTS company_setting (
        id SERIAL PRIMARY KEY,
        key VARCHAR(100) UNIQUE NOT NULL,
        value TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS cheque_record (
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
    )""",
    """CREATE TABLE IF NOT EXISTS receipt_voucher (
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
    )""",
    "INSERT INTO doc_counter (doc_type, prefix, last_num) VALUES ('RCV','RCV-2026-',0) ON CONFLICT DO NOTHING",
]

with engine.connect() as conn:
    for sql in fixes:
        try:
            conn.execute(text(sql))
            conn.commit()
            print(f"OK: {sql[:60]}...")
        except Exception as e:
            print(f"ERR: {e}")

print("Done.")
