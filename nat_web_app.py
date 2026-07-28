"""
NAT Web Operations System v4
==============================
New Asian General Trading LLC
Changes v4:
  - CompanySetting model: stores logo + stamp as base64 in DB (Railway-safe)
  - LPO PDF: authorized signatory only, no receiver block
  - Invoice PDF: customer TRN shown, "Delivery Note Number" field
  - DO PDF: customer TRN + company TRN shown
  - Invoice/LPO PDF: NAT stamp image beside signature when uploaded
  - Settings: stamp upload + logo upload stored in DB
  - Accounts tab: cheque tracking (all agreed fields)
  - Receipt Voucher: auto-sequential, PDF, email copy
  - Profit tab: internal margin view per invoice
  - Items catalog: cost_price + markup_pct fields
  - Google Drive: auto-upload every PDF to Drive folder
  - Nav: Accounts, Receipts, Profit tabs added
"""

import os, sys, json, re, io, csv, base64, time
from datetime import datetime, timedelta
from functools import wraps
from collections import defaultdict

from flask import (Flask, render_template_string, request, redirect,
                   url_for, session, flash, jsonify, send_file, abort)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, Image)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    HAS_RL = True
except ImportError:
    HAS_RL = False

try:
    import pandas as pd
    HAS_PD = True
except ImportError:
    HAS_PD = False

try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    from google.oauth2 import service_account
    HAS_GDRIVE = True
except ImportError:
    HAS_GDRIVE = False

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'nat-ops-2026-secret-v4')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 'sqlite:///nat_ops.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = \
        app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

db = SQLAlchemy(app)

# Falls back to file if present (local dev), else DB
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Logo.jpeg')

COMPANY = {
    "name":    "NEW ASIAN GENERAL TRADING LLC",
    "address": "Sharjah Media City, Sharjah, UAE | P.O. Box 515000",
    "phone":   "050 4864700 / 050 3161007",
    "email1":  "info@newasiantrading.com",
    "email2":  "newasiantrd@emirates.net.ae",
    "web":     "www.newasiantrading.com",
    "trn":     "104046372900003",
    "license": "2322591.01 — Shams Free Zone",
}

_login_attempts = defaultdict(list)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 900

def check_rate_limit(ip):
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < LOGIN_LOCKOUT_SECONDS]
    _login_attempts[ip] = attempts
    return len(attempts) < LOGIN_MAX_ATTEMPTS

def record_failed_login(ip):
    _login_attempts[ip].append(time.time())

def get_lockout_remaining(ip):
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < LOGIN_LOCKOUT_SECONDS]
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        return int(LOGIN_LOCKOUT_SECONDS - (now - min(attempts)))
    return 0

# ── Models ──────────────────────────────────────────────────

class CompanySetting(db.Model):
    """Key-value store for company-wide settings: logo_b64, stamp_b64, gdrive_folder_id, etc."""
    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)

    @staticmethod
    def get(key, default=None):
        r = CompanySetting.query.filter_by(key=key).first()
        return r.value if r else default

    @staticmethod
    def set(key, value):
        r = CompanySetting.query.filter_by(key=key).first()
        if r:
            r.value = value
        else:
            db.session.add(CompanySetting(key=key, value=value))
        db.session.commit()

class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(200), unique=True, nullable=False)
    email2        = db.Column(db.String(200))
    password      = db.Column(db.String(256), nullable=False)
    role          = db.Column(db.String(20), default='editor')
    active        = db.Column(db.Boolean, default=True)
    signature_b64 = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

class DocCounter(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    doc_type = db.Column(db.String(10), unique=True)
    prefix   = db.Column(db.String(20))
    last_num = db.Column(db.Integer, default=0)
    updated  = db.Column(db.DateTime, default=datetime.utcnow)

class Document(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    ref            = db.Column(db.String(30), unique=True, nullable=False)
    doc_type       = db.Column(db.String(10))
    date           = db.Column(db.String(20))
    party_name     = db.Column(db.String(200))
    party_trn      = db.Column(db.String(50))   # NEW: customer/vendor TRN on doc
    contact        = db.Column(db.String(100))
    phone          = db.Column(db.String(50))
    email          = db.Column(db.String(200))
    items_json     = db.Column(db.Text)
    subtotal       = db.Column(db.Float, default=0)
    vat            = db.Column(db.Float, default=0)
    total          = db.Column(db.Float, default=0)
    lpo_ref        = db.Column(db.String(30))
    do_ref         = db.Column(db.String(30))
    enquiry_ref    = db.Column(db.String(30))
    quotation_ref  = db.Column(db.String(30))   # repurposed: delivery note number on INV
    payment_terms  = db.Column(db.String(100))
    delivery_terms = db.Column(db.String(100))
    due_date       = db.Column(db.String(20))
    remarks        = db.Column(db.Text)
    status         = db.Column(db.String(30), default='Open')
    created_by     = db.Column(db.String(100))
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    gdrive_url     = db.Column(db.String(500))  # NEW: Google Drive link after upload

class Client(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200))
    contact    = db.Column(db.String(100))
    phone      = db.Column(db.String(50))
    email      = db.Column(db.String(200))
    address    = db.Column(db.String(300))
    trn        = db.Column(db.String(50))
    license_no = db.Column(db.String(50))
    notes      = db.Column(db.Text)
    active     = db.Column(db.Boolean, default=True)
    added_by   = db.Column(db.String(100))
    added_at   = db.Column(db.DateTime, default=datetime.utcnow)

class Vendor(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200))
    contact    = db.Column(db.String(100))
    phone      = db.Column(db.String(50))
    email      = db.Column(db.String(200))
    address    = db.Column(db.String(300))
    trn        = db.Column(db.String(50))
    license_no = db.Column(db.String(50))
    products   = db.Column(db.Text)
    notes      = db.Column(db.Text)
    active     = db.Column(db.Boolean, default=True)
    added_by   = db.Column(db.String(100))
    added_at   = db.Column(db.DateTime, default=datetime.utcnow)

class Logistics(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    date          = db.Column(db.String(20))
    courier       = db.Column(db.String(200))
    tracking_no   = db.Column(db.String(100))
    linked_ref    = db.Column(db.String(30))
    origin        = db.Column(db.String(200))
    destination   = db.Column(db.String(200))
    charge_aed    = db.Column(db.Float, default=0)
    status        = db.Column(db.String(30), default='Booked')
    weight_kg     = db.Column(db.String(30))
    dimensions    = db.Column(db.String(100))
    vehicle_plate = db.Column(db.String(30))
    driver_name   = db.Column(db.String(100))
    notes         = db.Column(db.Text)
    added_by      = db.Column(db.String(100))
    added_at      = db.Column(db.DateTime, default=datetime.utcnow)

class CloudArchive(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    doc_type    = db.Column(db.String(50))
    doc_number  = db.Column(db.String(50))
    date        = db.Column(db.String(20))
    party_name  = db.Column(db.String(200))
    amount_aed  = db.Column(db.Float, default=0)
    cloud_url   = db.Column(db.Text)
    description = db.Column(db.Text)
    upload_date = db.Column(db.String(20))
    added_by    = db.Column(db.String(100))
    added_at    = db.Column(db.DateTime, default=datetime.utcnow)

class Contact(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(200))
    phone           = db.Column(db.String(50))
    email           = db.Column(db.String(200))
    company         = db.Column(db.String(200))
    source          = db.Column(db.String(50))
    count           = db.Column(db.Integer, default=0)
    # --- Outbound pipeline fields ---
    title           = db.Column(db.String(100))
    linkedin        = db.Column(db.String(300))
    website         = db.Column(db.String(300))
    email_quality   = db.Column(db.String(20))
    icp_notes       = db.Column(db.Text)
    outreach_status = db.Column(db.String(30), default='New')
    meeting_date    = db.Column(db.String(30))
    meeting_notes   = db.Column(db.Text)
    added_at        = db.Column(db.DateTime, default=datetime.utcnow)
    logs            = db.relationship('OutreachLog', backref='contact', lazy=True, cascade='all, delete-orphan')

class OutreachLog(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey('contact.id'), nullable=False)
    date       = db.Column(db.String(30))
    channel    = db.Column(db.String(30))
    subject    = db.Column(db.String(200))
    notes      = db.Column(db.Text)
    added_by   = db.Column(db.String(100))
    added_at   = db.Column(db.DateTime, default=datetime.utcnow)

class CatalogItem(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(200), nullable=False)
    spec        = db.Column(db.String(300))
    unit        = db.Column(db.String(30), default='Pcs')
    category    = db.Column(db.String(100))
    vendors     = db.Column(db.Text)
    last_price  = db.Column(db.Float, default=0)   # selling price (last used)
    cost_price  = db.Column(db.Float, default=0)   # NEW: supplier cost price
    markup_pct  = db.Column(db.Float, default=0)   # NEW: default markup %
    active      = db.Column(db.Boolean, default=True)
    added_by    = db.Column(db.String(100))
    added_at    = db.Column(db.DateTime, default=datetime.utcnow)

class ItemPriceLog(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    item_id   = db.Column(db.Integer, db.ForeignKey('catalog_item.id'))
    price     = db.Column(db.Float, default=0)
    vendor    = db.Column(db.String(200))
    doc_ref   = db.Column(db.String(30))
    logged_at = db.Column(db.DateTime, default=datetime.utcnow)

class AuditLog(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    user_name   = db.Column(db.String(100))
    action      = db.Column(db.String(20))
    table_name  = db.Column(db.String(50))
    record_id   = db.Column(db.Integer)
    record_name = db.Column(db.String(200))
    changes     = db.Column(db.Text)
    logged_at   = db.Column(db.DateTime, default=datetime.utcnow)

# NEW: Accounts / Cheque Tracking
class ChequeRecord(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    company_name     = db.Column(db.String(200))
    party_trn        = db.Column(db.String(50))
    invoice_ref      = db.Column(db.String(30))
    delivery_note_no = db.Column(db.String(30))
    cheque_number    = db.Column(db.String(50))
    cheque_date      = db.Column(db.String(20))
    bank_name        = db.Column(db.String(100))
    clearance_date   = db.Column(db.String(20))
    signatory        = db.Column(db.String(100))
    total_amount     = db.Column(db.Float, default=0)
    status           = db.Column(db.String(20), default='Pending')  # Pending/Cleared/Bounced
    notes            = db.Column(db.Text)
    added_by         = db.Column(db.String(100))
    added_at         = db.Column(db.DateTime, default=datetime.utcnow)

# NEW: Receipt Vouchers
class ReceiptVoucher(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    ref            = db.Column(db.String(30), unique=True)
    date           = db.Column(db.String(20))
    received_from  = db.Column(db.String(200))
    amount         = db.Column(db.Float, default=0)
    invoice_ref    = db.Column(db.String(30))
    payment_method = db.Column(db.String(30))  # Cheque/Cash/Transfer
    cheque_number  = db.Column(db.String(50))
    bank_name      = db.Column(db.String(100))
    received_by    = db.Column(db.String(100))
    notes          = db.Column(db.Text)
    added_at       = db.Column(db.DateTime, default=datetime.utcnow)
    gdrive_url     = db.Column(db.String(500))

# ── Auth ────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        u = db.session.get(User, session['user_id'])
        if not u or u.role != 'admin':
            flash('Admin access required.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

# ── Audit helper ────────────────────────────────────────────

def audit(action, table_name, record_id, record_name, changes=None):
    db.session.add(AuditLog(
        user_name=session.get('user_name', 'system'),
        action=action, table_name=table_name,
        record_id=record_id, record_name=record_name,
        changes=json.dumps(changes or [])
    ))

def diff_record(old_obj, new_data, fields):
    changes = []
    for f in fields:
        old_val = str(getattr(old_obj, f, '') or '')
        new_val = str(new_data.get(f, '') or '')
        if old_val.strip() != new_val.strip():
            changes.append({'field': f, 'old': old_val, 'new': new_val})
    return changes

# ── Numbering ───────────────────────────────────────────────

def get_next_ref(doc_type):
    prefixes = {
        'LPO':'LPO-2026-','INV':'INV-2026-','DO':'DO-2026-',
        'QUO':'QUO-2026-','ENQ':'ENQ-2026-','RCV':'RCV-2026-'
    }
    c = DocCounter.query.filter_by(doc_type=doc_type).first()
    if not c:
        c = DocCounter(doc_type=doc_type,
                       prefix=prefixes.get(doc_type, doc_type+'-2026-'),
                       last_num=0)
        db.session.add(c)
    c.last_num += 1
    c.updated = datetime.utcnow()
    db.session.commit()
    return f"{c.prefix}{str(c.last_num).zfill(3)}"

def ref_exists(ref):
    return Document.query.filter_by(ref=ref).first() is not None

# ── Google Drive helper ──────────────────────────────────────

def gdrive_upload(pdf_buf, filename, doc_type):
    """Upload PDF to Google Drive. Returns shareable URL or None."""
    if not HAS_GDRIVE:
        return None
    creds_json = CompanySetting.get('gdrive_creds_json')
    folder_id  = CompanySetting.get('gdrive_folder_id')
    if not creds_json or not folder_id:
        return None
    try:
        creds_info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        service = build('drive', 'v3', credentials=creds)

        # Sub-folder per doc type
        subfolder_name = {
            'INV': 'Invoices', 'LPO': 'LPOs', 'DO': 'DeliveryNotes',
            'QUO': 'Quotations', 'ENQ': 'Enquiries', 'RCV': 'Receipts'
        }.get(doc_type, 'Documents')

        # Find or create subfolder
        q = (f"name='{subfolder_name}' and '{folder_id}' in parents "
             f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
        results = service.files().list(q=q, fields='files(id)').execute()
        if results['files']:
            subfolder_id = results['files'][0]['id']
        else:
            meta = {'name': subfolder_name, 'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [folder_id]}
            subfolder_id = service.files().create(body=meta, fields='id').execute()['id']

        # Upload
        pdf_buf.seek(0)
        file_meta = {'name': filename, 'parents': [subfolder_id]}
        media = MediaIoBaseUpload(pdf_buf, mimetype='application/pdf')
        f = service.files().create(body=file_meta, media_body=media, fields='id').execute()
        file_id = f['id']

        # Make public readable
        service.permissions().create(
            fileId=file_id,
            body={'type': 'anyone', 'role': 'reader'}
        ).execute()
        return f"https://drive.google.com/file/d/{file_id}/view"
    except Exception as e:
        print(f"GDrive upload failed: {e}")
        return None

# ── Logo / Stamp helpers ─────────────────────────────────────

def get_logo_b64():
    """Return logo as data URI string or None."""
    # DB first (Railway-safe)
    val = CompanySetting.get('logo_b64')
    if val:
        return val
    # Fallback: file on disk (local dev)
    if os.path.exists(LOGO_PATH):
        with open(LOGO_PATH, 'rb') as f:
            raw = f.read()
        return 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()
    return None

def get_stamp_b64():
    return CompanySetting.get('stamp_b64')

def logo_image_for_pdf():
    """Return ReportLab Image object for logo, or None."""
    b64 = get_logo_b64()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64.split(',')[-1])
        return Image(io.BytesIO(raw), width=22*mm, height=22*mm)
    except:
        return None

def stamp_image_for_pdf():
    """Return ReportLab Image object for stamp, or None."""
    b64 = get_stamp_b64()
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64.split(',')[-1])
        return Image(io.BytesIO(raw), width=28*mm, height=28*mm)
    except:
        return None

# ── PDF ─────────────────────────────────────────────────────

def generate_pdf(doc):
    if not HAS_RL:
        return None
    buf    = io.BytesIO()
    items  = json.loads(doc.items_json or '[]')
    pdoc_  = SimpleDocTemplate(buf, pagesize=A4,
                               leftMargin=15*mm, rightMargin=15*mm,
                               topMargin=15*mm, bottomMargin=15*mm)
    nat_blue = colors.HexColor('#1A3A6B')
    nat_gold = colors.HexColor('#B45309')
    sn = ParagraphStyle('n', fontSize=8, leading=11)
    story = []

    # ── Header ──
    logo_img = logo_image_for_pdf()
    logo_cell = logo_img or Paragraph(
        '<b><font color="#B45309" size=16>NAT</font></b>',
        ParagraphStyle('lg', fontSize=16, fontName='Helvetica-Bold'))

    titles = {'LPO':'LOCAL PURCHASE ORDER','INV':'TAX INVOICE',
              'DO':'DELIVERY NOTE','QUO':'QUOTATION','ENQ':'ENQUIRY'}
    doc_title = titles.get(doc.doc_type, doc.doc_type)

    hdr = Table([[
        logo_cell,
        Paragraph(f'<b>{COMPANY["name"]}</b><br/>'
                  f'<font size=7>{COMPANY["address"]}<br/>'
                  f'Tel: {COMPANY["phone"]}<br/>'
                  f'{COMPANY["email1"]} | {COMPANY["email2"]}<br/>'
                  f'TRN: {COMPANY["trn"]}</font>', sn),
        Paragraph(f'<b><font color="#B45309" size=12>{doc_title}</font></b><br/><br/>'
                  f'<b>Ref:</b> {doc.ref}<br/>'
                  f'<b>Date:</b> {doc.date}<br/>'
                  f'<b>By:</b> {doc.created_by}',
                  ParagraphStyle('r', fontSize=9, alignment=TA_RIGHT, leading=13))
    ]], colWidths=[22*mm, 103*mm, 55*mm])
    hdr.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('VALIGN', (2,0), (2,0), 'TOP'),
        ('LINEBELOW', (0,0), (-1,-1), 1.5, nat_blue),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (1,0), (1,0), 4),
    ]))
    story.append(hdr)
    story.append(Spacer(1, 5*mm))

    # ── Party / refs block ──
    plabel = "Vendor / Supplier" if doc.doc_type == 'LPO' else "Client / Customer"

    # TRN line for customer/vendor
    party_trn_line = ''
    if doc.party_trn:
        party_trn_line = f'<br/>TRN: <b>{doc.party_trn}</b>'

    refs = ''
    if doc.doc_type == 'INV':
        if doc.quotation_ref:
            refs += f'<b>Delivery Note No:</b> {doc.quotation_ref}<br/>'
        if doc.lpo_ref:
            refs += f'<b>Client PO/LPO:</b> {doc.lpo_ref}<br/>'
    else:
        if doc.lpo_ref:       refs += f'<b>LPO Ref:</b> {doc.lpo_ref}<br/>'
        if doc.do_ref:        refs += f'<b>DO Ref:</b> {doc.do_ref}<br/>'
        if doc.quotation_ref: refs += f'<b>Quo Ref:</b> {doc.quotation_ref}<br/>'
        if doc.enquiry_ref:   refs += f'<b>Enq Ref:</b> {doc.enquiry_ref}<br/>'

    refs += f'<b>Payment:</b> {doc.payment_terms or "Net 30 Days"}<br/>'
    refs += f'<b>Delivery:</b> {doc.delivery_terms or "Ex-Works"}'

    # Show company TRN on DO
    if doc.doc_type == 'DO':
        refs += f'<br/><b>Our TRN:</b> {COMPANY["trn"]}'

    party = Table([[
        Paragraph(f'<b>{plabel}:</b> {doc.party_name}<br/>'
                  f'Contact: {doc.contact or ""}<br/>'
                  f'Phone: {doc.phone or ""}<br/>'
                  f'Email: {doc.email or ""}'
                  f'{party_trn_line}', sn),
        Paragraph(refs, ParagraphStyle('r2', fontSize=8, alignment=TA_RIGHT, leading=13))
    ]], colWidths=[100*mm, 80*mm])
    party.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8F9FA')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.grey),
        ('PADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(party)
    story.append(Spacer(1, 5*mm))

    # ── Items table ──
    show_price = doc.doc_type != 'DO'
    if show_price:
        hdrs = ["S.N.","Description","Spec","Qty","Unit","Rate (AED)","Amount (AED)"]
        cw   = [12*mm, 55*mm, 40*mm, 15*mm, 15*mm, 22*mm, 22*mm]
    else:
        hdrs = ["S.N.","Description","Spec","Qty","Unit"]
        cw   = [12*mm, 70*mm, 60*mm, 18*mm, 18*mm]

    rows = [hdrs]
    for i, item in enumerate(items, 1):
        if show_price:
            rows.append([str(i), item.get('desc',''), item.get('spec',''),
                         str(item.get('qty','')), item.get('unit','Pcs'),
                         f'{float(item.get("rate",0)):,.2f}',
                         f'{float(item.get("total",0)):,.2f}'])
        else:
            rows.append([str(i), item.get('desc',''), item.get('spec',''),
                         str(item.get('qty','')), item.get('unit','Pcs')])

    while len(rows) < 9:
        rows.append(['']*len(hdrs))

    t = Table(rows, colWidths=cw)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), nat_blue),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('ALIGN', (1,1), (2,-1), 'LEFT'),
        ('ALIGN', (-1,1), (-1,-1), 'RIGHT'),
        ('ALIGN', (-2,1), (-2,-1), 'RIGHT'),
        ('GRID', (0,0), (-1,-1), 0.4, colors.grey),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F5F7FA')]),
        ('PADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(t)
    story.append(Spacer(1, 4*mm))

    # ── Totals ──
    if show_price:
        tr = Table([
            ['','','','','','Subtotal / المجموع:', f'AED {doc.subtotal:,.2f}'],
            ['','','','','','VAT 5% / ضريبة:',    f'AED {doc.vat:,.2f}'],
            ['','','','','','TOTAL / الإجمالي:',   f'AED {doc.total:,.2f}'],
        ], colWidths=cw)
        tr.setStyle(TableStyle([
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('FONTNAME', (-2,0), (-1,-1), 'Helvetica-Bold'),
            ('ALIGN', (-1,0), (-1,-1), 'RIGHT'),
            ('ALIGN', (-2,0), (-2,-1), 'RIGHT'),
            ('BACKGROUND', (0,2), (-1,2), nat_blue),
            ('TEXTCOLOR', (0,2), (-1,2), colors.white),
            ('PADDING', (0,0), (-1,-1), 4),
        ]))
        story.append(tr)
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph(
            f'<b>Amount in Words:</b> AED {doc.total:,.2f} — UAE Dirhams Only',
            ParagraphStyle('w', fontSize=8, borderColor=colors.grey,
                           borderWidth=0.5, borderPadding=4, leading=12)))
        story.append(Spacer(1, 8*mm))

    # ── Signature + Stamp block ──
    creator = User.query.filter_by(name=doc.created_by).first()
    user_sig_img = None
    if creator and creator.signature_b64:
        try:
            raw = base64.b64decode(creator.signature_b64.split(',')[-1])
            user_sig_img = Image(io.BytesIO(raw), width=35*mm, height=15*mm)
        except:
            pass

    stamp_img = stamp_image_for_pdf()

    # Build authorized signatory cell
    sig_elements = []
    if user_sig_img:
        sig_elements.append(user_sig_img)
    sig_elements.append(Paragraph(
        f'<br/><b>___________________________</b><br/>'
        f'Authorised Signatory<br/>'
        f'<b>{doc.created_by}</b><br/>'
        f'<font size=7>New Asian General Trading LLC</font>',
        ParagraphStyle('s2', fontSize=8, alignment=TA_CENTER, leading=12)))
    if stamp_img:
        sig_elements.append(stamp_img)

    # LPO and INV: authorized signatory only (no receiver block)
    # DO: keep receiver block for warehouse signature
    if doc.doc_type == 'DO':
        sig_row = [[
            Paragraph('<br/><br/><br/><b>___________________________</b><br/>'
                      "Receiver's Signature<br/>"
                      '<font size=7>Name: _____________ Date: _______</font>',
                      ParagraphStyle('s1', fontSize=8, alignment=TA_CENTER, leading=13)),
            sig_elements
        ]]
        col_widths = [90*mm, 90*mm]
    else:
        # LPO / INV / QUO / ENQ — authorized signatory only, centered
        sig_row = [[sig_elements]]
        col_widths = [180*mm]

    sig_table = Table(sig_row, colWidths=col_widths)
    sig_table.setStyle(TableStyle([
        ('LINEABOVE', (0,0), (-1,0), 0.5, colors.HexColor('#CCCCCC')),
        ('PADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'BOTTOM'),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
    ]))
    story.append(sig_table)
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph(
        f'E. & O. E.  •  {COMPANY["name"]}  •  TRN: {COMPANY["trn"]}  •  License: {COMPANY["license"]}',
        ParagraphStyle('ft', fontSize=7, alignment=TA_CENTER,
                       textColor=colors.grey, leading=10)))
    pdoc_.build(story)
    buf.seek(0)
    return buf


def generate_receipt_pdf(rcv):
    """Generate Receipt Voucher PDF."""
    if not HAS_RL:
        return None
    buf   = io.BytesIO()
    pdoc_ = SimpleDocTemplate(buf, pagesize=A4,
                               leftMargin=20*mm, rightMargin=20*mm,
                               topMargin=20*mm, bottomMargin=20*mm)
    nat_blue = colors.HexColor('#1A3A6B')
    story = []
    sn = ParagraphStyle('n', fontSize=9, leading=13)

    logo_img = logo_image_for_pdf()
    logo_cell = logo_img or Paragraph(
        '<b><font color="#B45309" size=16>NAT</font></b>',
        ParagraphStyle('lg', fontSize=16, fontName='Helvetica-Bold'))

    hdr = Table([[
        logo_cell,
        Paragraph(f'<b>{COMPANY["name"]}</b><br/>'
                  f'<font size=7>{COMPANY["address"]}<br/>'
                  f'TRN: {COMPANY["trn"]}</font>', sn),
        Paragraph('<b><font color="#B45309" size=14>RECEIPT VOUCHER</font></b><br/><br/>'
                  f'<b>Ref:</b> {rcv.ref}<br/>'
                  f'<b>Date:</b> {rcv.date}',
                  ParagraphStyle('r', fontSize=9, alignment=TA_RIGHT, leading=13))
    ]], colWidths=[22*mm, 103*mm, 55*mm])
    hdr.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LINEBELOW', (0,0), (-1,-1), 1.5, nat_blue),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(hdr)
    story.append(Spacer(1, 8*mm))

    details = Table([
        ['Received From:', rcv.received_from or ''],
        ['Amount (AED):', f'AED {rcv.amount:,.2f}'],
        ['Against Invoice:', rcv.invoice_ref or ''],
        ['Payment Method:', rcv.payment_method or ''],
        ['Cheque No.:', rcv.cheque_number or '-'],
        ['Bank:', rcv.bank_name or '-'],
        ['Received By:', rcv.received_by or ''],
        ['Notes:', rcv.notes or '-'],
    ], colWidths=[50*mm, 120*mm])
    details.setStyle(TableStyle([
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('PADDING', (0,0), (-1,-1), 7),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.HexColor('#F8F9FA'), colors.white]),
        ('GRID', (0,0), (-1,-1), 0.3, colors.grey),
    ]))
    story.append(details)
    story.append(Spacer(1, 16*mm))

    # Amount in box
    story.append(Table([[
        Paragraph(f'<b>Amount Received: AED {rcv.amount:,.2f}</b>',
                  ParagraphStyle('ab', fontSize=14, fontName='Helvetica-Bold',
                                 alignment=TA_CENTER, textColor=nat_blue))
    ]], colWidths=[170*mm]))

    story.append(Spacer(1, 16*mm))

    # Signature
    stamp_img = stamp_image_for_pdf()
    sig_content = [
        Paragraph('<br/><br/><b>___________________________</b><br/>'
                  f'<b>{rcv.received_by}</b><br/>'
                  '<font size=7>New Asian General Trading LLC</font>',
                  ParagraphStyle('s', fontSize=8, alignment=TA_CENTER, leading=12))
    ]
    if stamp_img:
        sig_content.insert(0, stamp_img)

    sig_t = Table([[sig_content]], colWidths=[170*mm])
    sig_t.setStyle(TableStyle([
        ('ALIGN', (0,0), (0,0), 'CENTER'),
        ('LINEABOVE', (0,0), (0,0), 0.5, colors.grey),
        ('PADDING', (0,0), (0,0), 6),
    ]))
    story.append(sig_t)

    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        f'{COMPANY["name"]}  •  TRN: {COMPANY["trn"]}',
        ParagraphStyle('ft', fontSize=7, alignment=TA_CENTER,
                       textColor=colors.grey)))
    pdoc_.build(story)
    buf.seek(0)
    return buf


def generate_vat_pdf(quarter, year, output_tax, input_tax, net_vat, inv_rows, lpo_rows):
    if not HAS_RL:
        return None
    buf  = io.BytesIO()
    pdoc_ = SimpleDocTemplate(buf, pagesize=A4,
                              leftMargin=15*mm, rightMargin=15*mm,
                              topMargin=15*mm, bottomMargin=15*mm)
    nat_blue = colors.HexColor('#1A3A6B')
    story    = []
    sn = ParagraphStyle('n', fontSize=9, leading=13)
    sb = ParagraphStyle('b', fontSize=10, fontName='Helvetica-Bold', leading=14)

    story.append(Paragraph(f'VAT RETURN AUDIT REPORT — Q{quarter} {year}',
        ParagraphStyle('t', fontSize=14, fontName='Helvetica-Bold',
                       alignment=TA_CENTER, textColor=nat_blue, leading=18)))
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        f'{COMPANY["name"]} | TRN: {COMPANY["trn"]} | Generated: {datetime.now().strftime("%d/%m/%Y")}',
        ParagraphStyle('s', fontSize=8, alignment=TA_CENTER, textColor=colors.grey)))
    story.append(Spacer(1, 6*mm))

    summary = Table([
        ['Output VAT (from Invoices)', f'AED {output_tax:,.2f}'],
        ['Input VAT (from LPOs)',      f'AED {input_tax:,.2f}'],
        ['Net VAT Payable to FTA',     f'AED {net_vat:,.2f}'],
    ], colWidths=[120*mm, 60*mm])
    summary.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#E8F4FF')),
        ('BACKGROUND', (0,1), (-1,1), colors.HexColor('#FFF8E8')),
        ('BACKGROUND', (0,2), (-1,2), nat_blue),
        ('TEXTCOLOR', (0,2), (-1,2), colors.white),
        ('FONTNAME', (0,2), (-1,2), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('PADDING', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
    ]))
    story.append(summary)
    story.append(Spacer(1, 6*mm))

    for label, doc_rows in [('Sales Invoices (Output Tax)', inv_rows),
                             ('Purchase LPOs (Input Tax)', lpo_rows)]:
        story.append(Paragraph(label, sb))
        story.append(Spacer(1, 3*mm))
        data = [['Ref','Date','Party','Subtotal AED','VAT AED','Total AED']]
        for r in doc_rows:
            data.append([r.ref, r.date, r.party_name[:30],
                         f'{r.subtotal:,.2f}', f'{r.vat:,.2f}', f'{r.total:,.2f}'])
        tbl = Table(data, colWidths=[28*mm,22*mm,50*mm,28*mm,22*mm,28*mm])
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), nat_blue),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 7),
            ('GRID', (0,0), (-1,-1), 0.3, colors.grey),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F5F7FA')]),
            ('PADDING', (0,0), (-1,-1), 4),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 6*mm))

    pdoc_.build(story)
    buf.seek(0)
    return buf


# ── CSS ──────────────────────────────────────────────────────

CSS = """
*{box-sizing:border-box;margin:0;padding:0;font-family:Arial,sans-serif}
body{background:#f0f2f5;font-size:13px;color:#222}
.topbar{background:#1a3a6b;color:#fff;height:56px;display:flex;align-items:center;
        padding:0 20px;justify-content:space-between;position:sticky;top:0;z-index:100;
        box-shadow:0 2px 8px rgba(0,0,0,0.3)}
.topbar-left{display:flex;align-items:center;gap:12px}
.topbar-logo{height:38px;width:auto;border-radius:4px}
.topbar-logo-text{background:#b45309;width:38px;height:38px;border-radius:6px;
                  display:flex;align-items:center;justify-content:center;
                  font-weight:bold;font-size:16px;color:#fff}
.topbar-title{font-size:14px;font-weight:bold}
.topbar-sub{font-size:10px;opacity:0.65}
.topbar-right{display:flex;align-items:center;gap:16px;font-size:12px}
.topbar-right a{color:rgba(255,255,255,0.75);text-decoration:none}
.topbar-right a:hover{color:#fff}
.nav{background:#0f2244;display:flex;overflow-x:auto;padding:0 12px}
.nav a{padding:10px 14px;font-size:12px;color:rgba(255,255,255,0.55);
       text-decoration:none;white-space:nowrap;border-bottom:2px solid transparent}
.nav a:hover{color:rgba(255,255,255,0.85)}
.nav a.active{color:#ffd700;border-bottom:2px solid #ffd700;font-weight:bold}
.main{max-width:1200px;margin:0 auto;padding:20px 16px}
.card{background:#fff;border-radius:10px;padding:20px;margin-bottom:16px;
      box-shadow:0 1px 4px rgba(0,0,0,0.06);border:1px solid #e8eaf0}
.card h2{font-size:15px;color:#1a3a6b;margin-bottom:14px;padding-bottom:8px;
         border-bottom:2px solid #e8eaf0;display:flex;align-items:center;gap:8px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.stat{background:#fff;border-radius:10px;padding:16px;border:1px solid #e8eaf0;
      box-shadow:0 1px 4px rgba(0,0,0,0.05)}
.stat-label{font-size:10px;color:#888;text-transform:uppercase;font-weight:bold;margin-bottom:4px}
.stat-value{font-size:22px;font-weight:bold;color:#1a3a6b}
.stat-sub{font-size:11px;margin-top:3px}
.green{color:#27ae60}.red{color:#e74c3c}.orange{color:#f39c12}.blue{color:#2980b9}
.btn{padding:8px 18px;border:none;border-radius:6px;cursor:pointer;font-size:12px;
     font-weight:bold;text-decoration:none;display:inline-block;transition:opacity 0.15s}
.btn:hover{opacity:0.85}
.btn-primary{background:#1a3a6b;color:#fff}
.btn-success{background:#27ae60;color:#fff}
.btn-danger{background:#c0392b;color:#fff}
.btn-warning{background:#f39c12;color:#fff}
.btn-info{background:#2980b9;color:#fff}
.btn-purple{background:#7b2fbe;color:#fff}
.btn-sm{padding:5px 12px;font-size:11px}
.btn-outline{background:#fff;color:#1a3a6b;border:1.5px solid #1a3a6b}
table{width:100%;border-collapse:collapse;font-size:12px}
th{background:#1a3a6b;color:#fff;padding:9px 12px;text-align:left;font-size:11px}
td{padding:8px 12px;border-bottom:1px solid #f0f2f5;vertical-align:middle}
tr:hover td{background:#f8f9ff}
.badge{padding:3px 9px;border-radius:10px;font-size:10px;font-weight:bold;display:inline-block}
.badge-green{background:#d4edda;color:#155724}
.badge-orange{background:#fff3cd;color:#856404}
.badge-red{background:#f8d7da;color:#721c24}
.badge-blue{background:#cce5ff;color:#004085}
.badge-grey{background:#e2e3e5;color:#383d41}
.badge-purple{background:#e2d9f3;color:#4b2994}
.form-group{margin-bottom:12px}
.form-group label{display:block;font-size:10px;font-weight:bold;color:#555;
                  margin-bottom:3px;text-transform:uppercase;letter-spacing:0.03em}
.form-group input,.form-group select,.form-group textarea{
  width:100%;padding:8px 11px;border:1px solid #ddd;border-radius:6px;
  font-size:13px;transition:border-color 0.15s}
.form-group input:focus,.form-group select:focus,.form-group textarea:focus{
  border-color:#1a3a6b;outline:none}
.form-group textarea{resize:vertical;min-height:60px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.grid4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px}
.alert{padding:10px 14px;border-radius:6px;margin-bottom:14px;font-size:12px}
.alert-success{background:#d4edda;border-left:4px solid #27ae60;color:#155724}
.alert-error{background:#f8d7da;border-left:4px solid #e74c3c;color:#721c24}
.alert-warning{background:#fff3cd;border-left:4px solid #ffc107;color:#856404}
.items-header{display:grid;gap:4px;background:#1a3a6b;color:#fff;
              padding:7px 6px;font-size:10px;font-weight:bold;border-radius:6px 6px 0 0}
.item-row-grid{display:grid;gap:4px;padding:5px 6px;border-bottom:1px solid #eee;align-items:center}
.item-row-grid input{padding:5px 7px;border:1px solid #ddd;border-radius:4px;font-size:12px;width:100%}
.amount-field{background:#f5f5f5 !important}
.totals-box{background:#f8f9ff;border:1px solid #dce0f0;border-radius:8px;
            padding:14px;margin-top:10px;max-width:300px;margin-left:auto}
.total-row{display:flex;justify-content:space-between;padding:4px 0;font-size:12px;color:#555}
.total-row.grand{font-weight:bold;font-size:15px;border-top:2px solid #1a3a6b;
                 padding-top:8px;color:#1a3a6b}
.ref-box{display:flex;align-items:center;gap:10px;background:#f0f4ff;
         border:2px solid #1a3a6b;border-radius:8px;padding:10px 14px;margin-bottom:14px}
.import-box{background:#f8f9ff;border:2px dashed #1a3a6b;border-radius:8px;
            padding:16px;margin-bottom:14px;text-align:center}
.aged-current{color:#27ae60;font-weight:bold}
.aged-30{color:#f39c12;font-weight:bold}
.aged-60{color:#e67e22;font-weight:bold}
.aged-90{color:#e74c3c;font-weight:bold}
.autocomplete-list{position:absolute;background:#fff;border:1px solid #1a3a6b;
                   border-radius:0 0 6px 6px;z-index:999;width:100%;max-height:200px;
                   overflow-y:auto;box-shadow:0 4px 12px rgba(0,0,0,0.1)}
.autocomplete-item{padding:8px 12px;cursor:pointer;font-size:12px;border-bottom:1px solid #f0f0f0}
.autocomplete-item:hover{background:#f0f4ff}
.autocomplete-wrap{position:relative}
@media(max-width:600px){.grid2,.grid3,.grid4{grid-template-columns:1fr}
  .stats{grid-template-columns:1fr 1fr}}
"""

def logo_tag():
    b64 = get_logo_b64()
    if b64:
        return f'<img src="{b64}" class="topbar-logo" alt="NAT Logo">'
    return '<div class="topbar-logo-text">N</div>'

def base_page(content, active_nav='', title='NAT Ops'):
    nav_items = [
        ('dashboard','Dashboard','/dashboard'),
        ('lpo','New LPO','/doc/lpo/new'),
        ('inv','New Invoice','/doc/inv/new'),
        ('do','Delivery Note','/doc/do/new'),
        ('quo','Quotation','/doc/quo/new'),
        ('enq','Enquiry','/doc/enq/new'),
        ('pipeline','Pipeline','/pipeline'),
        ('clients','Clients','/clients'),
        ('vendors','Vendors','/vendors'),
        ('catalog','Catalog','/catalog'),
        ('contacts','Contacts','/contacts'),
        ('logistics','Logistics','/logistics'),
        ('archive','Archive','/archive'),
    ]
    is_admin = session.get('user_role') == 'admin'
    if is_admin:
        nav_items += [
            ('accounts','Accounts','/accounts'),
            ('receipts','Receipts','/receipts'),
            ('profit','Profit','/profit'),
            ('vat','VAT / FTA','/vat'),
            ('reports','Reports','/reports'),
            ('receivables','Receivables','/receivables'),
            ('audit','Audit Log','/audit'),
            ('settings','Settings','/settings'),
        ]

    nav_html = ''.join(
        f'<a href="{url}" class="{"active" if active_nav==key else ""}">{label}</a>'
        for key, label, url in nav_items)

    user_html = ''
    if session.get('user_id'):
        user_html = (f'<span>{session.get("user_name","")} '
                     f'<span style="opacity:0.5">({session.get("user_role","")})</span></span>'
                     f'<a href="/logout">Logout</a>')

    flashes = ''
    for cat, msg in session.pop('_flashes', []):
        flashes += f'<div class="alert alert-{cat}">{msg}</div>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — New Asian General Trading</title>
<style>{CSS}</style>
</head>
<body>
<div class="topbar">
  <div class="topbar-left">
    {logo_tag()}
    <div>
      <div class="topbar-title">New Asian General Trading LLC</div>
      <div class="topbar-sub">Operations Portal &bull; ops.newasiantrading.com</div>
    </div>
  </div>
  <div class="topbar-right">{user_html}</div>
</div>
<nav class="nav">{nav_html}</nav>
<div class="main">
{flashes}
{content}
</div>
<script>
function calcTotals(){{
  let sub=0;
  document.querySelectorAll('.item-row').forEach(r=>{{
    const q=parseFloat(r.querySelector('.qty')?.value)||0;
    const p=parseFloat(r.querySelector('.rate')?.value)||0;
    const t=q*p;
    const a=r.querySelector('.amount');
    if(a){{a.value=t.toFixed(2);}}
    sub+=t;
  }});
  const st=document.getElementById('subtotal');
  const va=document.getElementById('vat-amt');
  const ta=document.getElementById('total-amt');
  if(st)st.textContent='AED '+sub.toFixed(2);
  const vat=sub*0.05;
  if(va)va.textContent='AED '+vat.toFixed(2);
  if(ta)ta.textContent='AED '+(sub+vat).toFixed(2);
  const hs=document.getElementById('h-subtotal');
  const hv=document.getElementById('h-vat');
  const ht=document.getElementById('h-total');
  if(hs)hs.value=sub.toFixed(2);
  if(hv)hv.value=vat.toFixed(2);
  if(ht)ht.value=(sub+vat).toFixed(2);
}}
function addRow(showPrice){{
  const n=document.querySelectorAll('.item-row').length+1;
  const div=document.createElement('div');
  div.className='item-row-grid item-row';
  const cols=showPrice?'30px 3fr 2fr 70px 65px 90px 90px 36px':'30px 3fr 2fr 80px 80px 36px';
  div.style.gridTemplateColumns=cols;
  div.innerHTML=`<span style="text-align:center;font-weight:bold;color:#888">${{n}}</span>
    <div class="autocomplete-wrap" style="position:relative">
      <input class="desc" name="desc[]" placeholder="Item description" autocomplete="off"
             oninput="catalogSearch(this)">
      <div class="autocomplete-list" style="display:none"></div>
    </div>
    <input class="spec" name="spec[]" placeholder="Size / specification">
    <input class="qty" type="number" value="1" name="qty[]" min="0" step="0.01" oninput="calcTotals()">
    <input class="unit" value="Pcs" name="unit[]">
    ${{showPrice?`<input class="rate" type="number" value="0" step="0.01" name="rate[]" oninput="calcTotals()">
    <input class="amount amount-field" readonly value="0.00" name="amount[]">`:''}}
    <button type="button" onclick="this.closest('.item-row').remove();calcTotals()"
      style="background:#fee;color:#c00;border:1px solid #fcc;border-radius:4px;
             padding:4px 7px;cursor:pointer;font-size:12px">✕</button>`;
  document.getElementById('items-body').appendChild(div);
}}
function catalogSearch(input){{
  const q=input.value.trim();
  const list=input.nextElementSibling;
  if(q.length<2){{list.style.display='none';return;}}
  fetch('/catalog/search?q='+encodeURIComponent(q))
    .then(r=>r.json()).then(items=>{{
      if(!items.length){{list.style.display='none';return;}}
      list.innerHTML=items.map(i=>
        `<div class="autocomplete-item"
              onclick="fillItem(this,'${{i.name}}','${{i.spec}}','${{i.unit}}','${{i.last_price}}')"
              data-name="${{i.name}}" data-spec="${{i.spec}}"
              data-unit="${{i.unit}}" data-price="${{i.last_price}}">
          <strong>${{i.name}}</strong>
          <span style="color:#888;font-size:11px"> — ${{i.spec||''}} | ${{i.unit}} | AED ${{i.last_price||0}}</span>
        </div>`).join('');
      list.style.display='block';
    }});
}}
function fillItem(el,name,spec,unit,price){{
  const row=el.closest('.item-row');
  const descInput=row.querySelector('.desc');
  const specInput=row.querySelector('.spec');
  const unitInput=row.querySelector('.unit');
  const rateInput=row.querySelector('.rate');
  if(descInput)descInput.value=name;
  if(specInput)specInput.value=spec;
  if(unitInput)unitInput.value=unit;
  if(rateInput&&price)rateInput.value=price;
  el.closest('.autocomplete-list').style.display='none';
  calcTotals();
}}
document.addEventListener('click',function(e){{
  if(!e.target.closest('.autocomplete-wrap')){{
    document.querySelectorAll('.autocomplete-list').forEach(l=>l.style.display='none');
  }}
}});
</script>
</body></html>'''


# ── Routes: Auth ─────────────────────────────────────────────

@app.route('/logo')
def serve_logo():
    if os.path.exists(LOGO_PATH):
        return send_file(LOGO_PATH, mimetype='image/jpeg')
    abort(404)

@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    ip = request.remote_addr
    if request.method == 'POST':
        remaining = get_lockout_remaining(ip)
        if remaining > 0:
            flash(f'Too many failed attempts. Try again in {remaining//60+1} min.', 'error')
        else:
            email    = request.form.get('email','').lower().strip()
            password = request.form.get('password','')
            user = User.query.filter(
                db.or_(User.email==email, User.email2==email)
            ).first()
            if user and user.active and check_password_hash(user.password, password):
                _login_attempts[ip] = []
                session.permanent = True
                session['user_id']    = user.id
                session['user_name']  = user.name
                session['user_role']  = user.role
                session['user_email'] = user.email
                return redirect(url_for('dashboard'))
            record_failed_login(ip)
            left = LOGIN_MAX_ATTEMPTS - len(_login_attempts[ip])
            if left > 0:
                flash(f'Invalid email or password. {left} attempt(s) remaining.', 'error')
            else:
                flash('Account locked for 15 minutes.', 'error')

    logo_b64 = get_logo_b64()
    logo_html = (f'<img src="{logo_b64}" style="height:55px;margin-bottom:10px" alt="NAT">'
                 if logo_b64
                 else '<div style="font-size:48px;color:#b45309;font-weight:bold;margin-bottom:8px">NAT</div>')
    flashes = ''.join(
        f'<div class="alert alert-{c}" style="text-align:left">{m}</div>'
        for c, m in session.pop('_flashes', []))

    return f'''<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — NAT Ops</title><style>{CSS}
.login-wrap{{max-width:400px;margin:60px auto;padding:16px}}</style></head><body>
<div class="login-wrap">
  <div class="card" style="text-align:center">
    {logo_html}
    <div style="font-size:17px;font-weight:bold;color:#1a3a6b;margin-bottom:2px">
      New Asian General Trading LLC</div>
    <div style="font-size:12px;color:#888;margin-bottom:20px">Staff Operations Portal</div>
    {flashes}
    <form method="POST">
      <div class="form-group" style="text-align:left">
        <label>Email Address</label>
        <input type="email" name="email" required placeholder="your@email.com" autofocus>
      </div>
      <div class="form-group" style="text-align:left">
        <label>Password</label>
        <input type="password" name="password" required>
      </div>
      <button type="submit" class="btn btn-primary"
              style="width:100%;padding:11px;font-size:14px">Access Portal</button>
    </form>
    <div style="margin-top:14px;font-size:11px;color:#aaa">
      www.newasiantrading.com &bull; TRN: 104046372900003
    </div>
  </div>
</div></body></html>'''

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin/fix-users')
def fix_users():
    emails = ['rameez@newasiantrd.com','newasiantrd@emirates.net.ae','sales@newasiantrd.com']
    updated = []
    for email in emails:
        u = User.query.filter_by(email=email).first()
        if u and u.role != 'admin':
            u.role = 'admin'
            updated.append(u.name)
    db.session.commit()
    return (f'<h2>Done.</h2><p>Made admin: {", ".join(updated) or "all already admin"}</p>'
            f'<a href="/dashboard">Dashboard</a>')

# ── Dashboard ────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    now  = datetime.utcnow()
    m, y = now.month, now.year
    lm   = m-1 if m>1 else 12
    ly   = y if m>1 else y-1

    inv_this = Document.query.filter(
        Document.doc_type=='INV', Document.status!='VOID',
        Document.date.like(f'%-{str(m).zfill(2)}-%')).all()
    inv_last = Document.query.filter(
        Document.doc_type=='INV', Document.status!='VOID',
        Document.date.like(f'%-{str(lm).zfill(2)}-%')).all()

    revenue  = sum(d.total or 0 for d in inv_this)
    rev_last = sum(d.total or 0 for d in inv_last)
    rev_chg  = round(((revenue-rev_last)/rev_last*100) if rev_last else 0)

    open_lpos   = Document.query.filter_by(doc_type='LPO', status='Raised').count()
    unpaid_docs = Document.query.filter(
        Document.doc_type=='INV', Document.status.in_(['Issued','Overdue'])).all()
    unpaid = sum(d.total or 0 for d in unpaid_docs)

    q_n = (m-1)//3 + 1
    q_months = [(q_n-1)*3+1, (q_n-1)*3+2, (q_n-1)*3+3]
    vat_docs = Document.query.filter(Document.doc_type=='INV', Document.status!='VOID').all()
    vat_due  = sum(d.vat or 0 for d in vat_docs
                   if d.date and int(d.date.split('-')[1] if '-' in d.date
                   else d.date.split('/')[1]) in q_months)

    recent = Document.query.filter(
        Document.status!='VOID').order_by(Document.created_at.desc()).limit(10).all()

    badge = {'Open':'badge-grey','Raised':'badge-orange','Sent':'badge-blue',
             'Issued':'badge-purple','Delivered':'badge-blue','Paid':'badge-green',
             'VOID':'badge-red','Overdue':'badge-red'}
    rows = ''.join(f'''<tr>
      <td><a href="/doc/{d.doc_type.lower()}/{d.id}"
             style="color:#1a3a6b;font-weight:bold">{d.ref}</a></td>
      <td><span class="badge badge-blue">{d.doc_type}</span></td>
      <td>{d.party_name}</td>
      <td>{"AED {:,.2f}".format(d.total) if d.total else "-"}</td>
      <td><span class="badge {badge.get(d.status,'badge-grey')}">{d.status}</span></td>
      <td>{d.date}</td><td>{d.created_by}</td></tr>''' for d in recent) or \
      '<tr><td colspan="7" style="text-align:center;color:#999;padding:20px">No transactions yet.</td></tr>'

    is_admin = session.get('user_role') == 'admin'
    vat_widget = f'''<div style="background:#1a3a6b;color:#fff;border-radius:10px;
        padding:14px;margin-bottom:12px">
      <div style="font-size:11px;opacity:0.7;text-transform:uppercase;font-weight:bold">
        VAT Due — Q{q_n} {y}</div>
      <div style="font-size:22px;font-weight:bold;margin:4px 0">AED {vat_due:,.0f}</div>
      <div style="font-size:11px;color:#ffd700">
        Net output - input tax &bull;
        <a href="/vat" style="color:#ffd700">View FTA Report →</a></div>
    </div>''' if is_admin else ''

    # Pending cheques count
    pending_cheques = ChequeRecord.query.filter_by(status='Pending').count() if is_admin else 0
    cheque_stat = f'''<div class="stat" style="border-left:4px solid #f39c12">
      <div class="stat-label">Pending Cheques</div>
      <div class="stat-value orange">{pending_cheques}</div>
      <div class="stat-sub"><a href="/accounts" style="color:#2980b9">View Accounts →</a></div>
    </div>''' if is_admin else ''

    content = f'''{vat_widget}
<div class="stats">
  <div class="stat">
    <div class="stat-label">Revenue This Month</div>
    <div class="stat-value">AED {revenue:,.0f}</div>
    <div class="stat-sub {'green' if rev_chg>=0 else 'red'}">
      {'+' if rev_chg>=0 else ''}{rev_chg}% vs last month</div>
  </div>
  <div class="stat">
    <div class="stat-label">Open LPOs</div>
    <div class="stat-value">{open_lpos}</div>
    <div class="stat-sub blue">Awaiting delivery</div>
  </div>
  <div class="stat">
    <div class="stat-label">Unpaid Invoices</div>
    <div class="stat-value">AED {unpaid:,.0f}</div>
    <div class="stat-sub red">{len(unpaid_docs)} invoices pending</div>
  </div>
  <div class="stat">
    <div class="stat-label">Clients</div>
    <div class="stat-value">{Client.query.filter_by(active=True).count()}</div>
    <div class="stat-sub blue">Active</div>
  </div>
  {cheque_stat}
</div>
<div style="display:grid;grid-template-columns:2fr 1fr;gap:16px">
  <div class="card">
    <h2>Recent Transactions</h2>
    <table><thead><tr><th>Reference</th><th>Type</th><th>Party</th>
    <th>Value (AED)</th><th>Stage</th><th>Date</th><th>By</th></tr></thead>
    <tbody>{rows}</tbody></table>
  </div>
  <div class="card">
    <h2>Quick Actions</h2>
    <div style="display:flex;flex-direction:column;gap:8px">
      <a href="/doc/lpo/new" class="btn btn-primary">+ New LPO (to Vendor)</a>
      <a href="/doc/inv/new" class="btn btn-success">+ New Invoice (to Client)</a>
      <a href="/doc/do/new" class="btn btn-info">+ Delivery Note</a>
      <a href="/doc/quo/new" class="btn btn-warning" style="color:#fff">+ Quotation</a>
      <a href="/doc/enq/new" class="btn btn-outline">+ Log Enquiry</a>
      <a href="/receipts" class="btn btn-outline">+ Receipt Voucher</a>
      <a href="/receivables" class="btn btn-outline" style="color:#c0392b;border-color:#c0392b">
        ⚠ Aged Receivables</a>
    </div>
  </div>
</div>'''
    return base_page(content, 'dashboard', 'Dashboard')


# ── Document form helper ─────────────────────────────────────

def doc_form_page(doc_type, doc_title, next_ref, prefill=None):
    p = prefill or {}
    show_price = doc_type != 'DO'
    grid_cols  = ('30px 3fr 2fr 70px 65px 90px 90px 36px' if show_price
                  else '30px 3fr 2fr 80px 80px 36px')
    hdr_labels = (['#','Description','Specification','Qty','Unit','Rate (AED)','Amount (AED)','']
                  if show_price else ['#','Description','Specification','Qty','Unit',''])
    hdr_cells  = ''.join(f'<span>{h}</span>' for h in hdr_labels)

    item_rows = ''
    for i in range(1, 5):
        price_cells = ''
        if show_price:
            price_cells = '''<input class="rate" type="number" value="0" step="0.01"
                              name="rate[]" oninput="calcTotals()">
                             <input class="amount amount-field" readonly value="0.00" name="amount[]">'''
        item_rows += f'''<div class="item-row-grid item-row" style="grid-template-columns:{grid_cols}">
          <span style="text-align:center;font-weight:bold;color:#888">{i}</span>
          <div class="autocomplete-wrap" style="position:relative">
            <input class="desc" name="desc[]" placeholder="Item description"
                   value="{p.get(f'd{i}','')}" autocomplete="off" oninput="catalogSearch(this)">
            <div class="autocomplete-list" style="display:none"></div>
          </div>
          <input class="spec" name="spec[]" placeholder="Size / spec" value="{p.get(f's{i}','')}">
          <input class="qty" type="number" value="{p.get(f'q{i}',1)}"
                 name="qty[]" min="0" step="0.01" oninput="calcTotals()">
          <input class="unit" value="{p.get(f'u{i}','Pcs')}" name="unit[]">
          {price_cells}
          <button type="button" onclick="this.closest(\'.item-row\').remove();calcTotals()"
            style="background:#fee;color:#c00;border:1px solid #fcc;border-radius:4px;
                   padding:4px 7px;cursor:pointer;font-size:12px">✕</button>
        </div>'''

    # Extra fields per doc type
    extra_fields = ''
    if doc_type == 'LPO':
        extra_fields = f'''
          <div class="form-group"><label>Delivery Terms</label>
            <input name="delivery_terms" value="Delivered to our warehouse"></div>
          <div class="form-group"><label>Linked Enquiry Ref</label>
            <input name="enquiry_ref" placeholder="ENQ-2026-..."
                   value="{p.get('enquiry_ref','')}"></div>'''
    elif doc_type == 'INV':
        extra_fields = f'''
          <div class="form-group">
            <label>Delivery Note Number (from DO)</label>
            <input name="quotation_ref" placeholder="DO-2026-..."
                   value="{p.get('quotation_ref','')}"></div>
          <div class="form-group"><label>Client LPO / PO Ref</label>
            <input name="lpo_ref" placeholder="Client PO number"
                   value="{p.get('lpo_ref','')}"></div>
          <div class="form-group"><label>Customer TRN</label>
            <input name="party_trn" placeholder="104..."
                   value="{p.get('party_trn','')}"></div>
          <div class="form-group"><label>Payment Due Date</label>
            <input type="date" name="due_date" value="{p.get('due_date','')}"></div>'''
    elif doc_type == 'DO':
        extra_fields = f'''
          <div class="form-group"><label>Invoice Ref</label>
            <input name="do_ref" placeholder="INV-2026-..."
                   value="{p.get('inv_ref','')}"></div>
          <div class="form-group"><label>LPO Reference</label>
            <input name="lpo_ref" placeholder="LPO-2026-..."
                   value="{p.get('lpo_ref','')}"></div>
          <div class="form-group"><label>Customer TRN</label>
            <input name="party_trn" placeholder="104..."
                   value="{p.get('party_trn','')}"></div>
          <div class="form-group"><label>Delivery Address / Site</label>
            <input name="delivery_terms" placeholder="Site address"
                   value="{p.get('address','')}"></div>'''

    totals_html = ''
    if show_price:
        totals_html = '''<div class="totals-box">
          <div class="total-row"><span>Subtotal / المجموع</span>
            <span id="subtotal">AED 0.00</span></div>
          <div class="total-row"><span>VAT 5% / ضريبة</span>
            <span id="vat-amt">AED 0.00</span></div>
          <div class="total-row grand"><span>Total / الإجمالي</span>
            <span id="total-amt">AED 0.00</span></div>
        </div>'''

    sp_js = 'true' if show_price else 'false'
    today = datetime.now().strftime('%Y-%m-%d')

    content = f'''<div class="card">
  <h2>{doc_title}</h2>
  <form method="POST" id="doc-form">
    <div class="ref-box">
      <span style="font-size:12px;color:#555;font-weight:bold">Ref No.:</span>
      <input type="text" name="ref" value="{next_ref}"
             style="border:1px solid #1a3a6b;border-radius:5px;padding:6px 10px;
                    font-size:15px;font-family:monospace;font-weight:bold;
                    color:#1a3a6b;width:180px">
    </div>
    <div class="grid3">
      <div class="form-group">
        <label>{"Vendor / Supplier" if doc_type=="LPO" else "Client"} Name *</label>
        <input name="party_name" required placeholder="Company name"
               value="{p.get('party_name','')}">
      </div>
      <div class="form-group"><label>Contact Person</label>
        <input name="contact" value="{p.get('contact','')}"></div>
      <div class="form-group"><label>Date</label>
        <input type="date" name="date" value="{today}"></div>
      <div class="form-group"><label>Phone</label>
        <input name="phone" placeholder="+971..." value="{p.get('phone','')}"></div>
      <div class="form-group"><label>Email</label>
        <input type="email" name="email" value="{p.get('email','')}"></div>
      <div class="form-group"><label>Payment Terms</label>
        <input name="payment_terms" value="Net 30 Days"></div>
      {extra_fields}
    </div>
    <div class="card" style="margin-top:4px">
      <h2>Line Items</h2>
      <div class="items-header" style="grid-template-columns:{grid_cols}">{hdr_cells}</div>
      <div id="items-body">{item_rows}</div>
      <div style="margin-top:10px">
        <button type="button" onclick="addRow({sp_js})"
          style="background:#e8f4ff;color:#1a3a6b;border:1px solid #1a3a6b;
                 border-radius:6px;padding:7px 16px;cursor:pointer;
                 font-size:12px;font-weight:bold">+ Add Row</button>
      </div>
      {totals_html}
      <input type="hidden" id="h-subtotal" name="subtotal" value="0">
      <input type="hidden" id="h-vat"      name="vat"      value="0">
      <input type="hidden" id="h-total"    name="total"    value="0">
    </div>
    <div class="form-group" style="margin-top:10px">
      <label>Remarks / Notes</label>
      <textarea name="remarks"></textarea>
    </div>
    <div style="display:flex;gap:10px">
      <button type="submit" class="btn btn-primary"
              style="font-size:14px;padding:11px 28px">
        Save &amp; Generate PDF
      </button>
      <a href="/dashboard" class="btn btn-outline">Cancel</a>
    </div>
  </form>
</div>'''
    return base_page(content, doc_type.lower(), doc_title)

# ── Document routes ──────────────────────────────────────────

DOC_LABELS = {
    'lpo':('LPO','Local Purchase Order'),
    'inv':('INV','Tax Invoice'),
    'do': ('DO', 'Delivery Note'),
    'quo':('QUO','Quotation'),
    'enq':('ENQ','Enquiry'),
}

@app.route('/doc/<dtype>/new', methods=['GET','POST'])
@login_required
def doc_new(dtype):
    cfg = DOC_LABELS.get(dtype.lower())
    if not cfg: abort(404)
    doc_type, doc_title = cfg

    if request.method == 'POST':
        ref = request.form.get('ref','').strip() or get_next_ref(doc_type)
        if ref_exists(ref):
            flash(f'{ref} already exists. Use a different number.', 'error')
            return doc_form_page(doc_type, doc_title, get_next_ref(doc_type))

        descs   = request.form.getlist('desc[]')
        specs   = request.form.getlist('spec[]')
        qtys    = request.form.getlist('qty[]')
        units   = request.form.getlist('unit[]')
        rates   = request.form.getlist('rate[]')
        amounts = request.form.getlist('amount[]')
        items   = [{'desc':descs[i].strip(),
                    'spec':specs[i] if i<len(specs) else '',
                    'qty':float(qtys[i]) if i<len(qtys) else 1,
                    'unit':units[i] if i<len(units) else 'Pcs',
                    'rate':float(rates[i]) if i<len(rates) and rates[i] else 0,
                    'total':float(amounts[i]) if i<len(amounts) and amounts[i] else 0}
                   for i,d in enumerate(descs) if d.strip()]

        subtotal = float(request.form.get('subtotal',0) or 0)
        vat_amt  = float(request.form.get('vat',0) or 0)
        total    = float(request.form.get('total',0) or 0)
        if doc_type == 'DO':
            subtotal = vat_amt = total = 0

        status_map = {'LPO':'Raised','INV':'Issued','DO':'Delivered',
                      'QUO':'Sent','ENQ':'Open'}
        date_raw = request.form.get('date', datetime.now().strftime('%Y-%m-%d'))
        try:
            date_fmt = datetime.strptime(date_raw,'%Y-%m-%d').strftime('%d/%m/%Y')
        except:
            date_fmt = date_raw

        doc = Document(
            ref=ref, doc_type=doc_type, date=date_fmt,
            party_name=request.form.get('party_name',''),
            party_trn=request.form.get('party_trn',''),
            contact=request.form.get('contact',''),
            phone=request.form.get('phone',''),
            email=request.form.get('email',''),
            items_json=json.dumps(items),
            subtotal=subtotal, vat=vat_amt, total=total,
            lpo_ref=request.form.get('lpo_ref',''),
            do_ref=request.form.get('do_ref',''),
            enquiry_ref=request.form.get('enquiry_ref',''),
            quotation_ref=request.form.get('quotation_ref',''),
            payment_terms=request.form.get('payment_terms','Net 30 Days'),
            delivery_terms=request.form.get('delivery_terms',''),
            due_date=request.form.get('due_date',''),
            remarks=request.form.get('remarks',''),
            status=status_map.get(doc_type,'Open'),
            created_by=session.get('user_name','')
        )
        db.session.add(doc)
        db.session.commit()

        # Log item prices to catalog
        for item in items:
            if item.get('desc') and item.get('rate',0) > 0:
                cat_item = CatalogItem.query.filter(
                    db.func.lower(CatalogItem.name)==item['desc'].lower()
                ).first()
                if cat_item:
                    cat_item.last_price = item['rate']
                    db.session.add(ItemPriceLog(
                        item_id=cat_item.id, price=item['rate'],
                        vendor=doc.party_name if doc_type=='LPO' else '',
                        doc_ref=doc.ref))
                    db.session.commit()

        # Auto-fill party_trn from client DB if not provided
        if not doc.party_trn and doc_type in ('INV','DO'):
            client = Client.query.filter(
                db.func.lower(Client.name)==doc.party_name.lower()
            ).first()
            if client and client.trn:
                doc.party_trn = client.trn
                db.session.commit()

        # Generate PDF and upload to Drive
        pdf_buf = generate_pdf(doc)
        gdrive_url = None
        if pdf_buf:
            pdf_buf_copy = io.BytesIO(pdf_buf.getvalue())
            gdrive_url = gdrive_upload(pdf_buf_copy, f'{doc.ref}.pdf', doc_type)
            if gdrive_url:
                doc.gdrive_url = gdrive_url
                db.session.commit()
            pdf_buf.seek(0)

        phone_clean = re.sub(r'[^0-9]','', doc.phone or '')
        if phone_clean.startswith('0'):
            phone_clean = '971' + phone_clean[1:]

        do_btn = ''
        if doc_type == 'INV':
            do_btn = (f'<a href="/doc/do/from-invoice/{doc.id}" '
                      f'class="btn btn-info">Create Delivery Note</a>')
        enq_to_quo_btn = ''
        if doc_type == 'ENQ':
            enq_to_quo_btn = (f'<a href="/doc/quo/from-enq/{doc.id}" '
                              f'class="btn btn-warning" style="color:#fff">Convert to Quotation →</a>')
        wa_btn = ''
        if phone_clean:
            wa_msg = (f"Dear {doc.party_name}, please find our {doc_title} "
                      f"ref {doc.ref}. New Asian General Trading LLC | 050 4864700")
            wa_btn = (f'<a href="https://wa.me/{phone_clean}?text='
                      f'{wa_msg.replace(" ","%20")}" class="btn btn-success" target="_blank">💬 WhatsApp</a>')
        email_btn = ''
        if doc.email:
            email_btn = (f'<a href="mailto:{doc.email}?subject={ref} — New Asian General Trading LLC'
                         f'" class="btn btn-success">✉ Email</a>')
        drive_btn = ''
        if gdrive_url:
            drive_btn = (f'<a href="{gdrive_url}" target="_blank" '
                         f'class="btn btn-outline">☁ View on Drive</a>')

        total_str = f'AED {doc.total:,.2f}' if doc.total else ''
        confirm = f'''<div style="max-width:560px;margin:0 auto">
  <div class="card" style="text-align:center">
    <div style="font-size:44px;margin-bottom:10px">✅</div>
    <div style="font-size:16px;font-weight:bold;color:#1a3a6b">{doc_title} Saved</div>
    <div style="font-size:26px;font-weight:bold;color:#1a3a6b;
                font-family:monospace;margin:8px 0">{doc.ref}</div>
    <div style="font-size:14px;color:#555">{doc.party_name}</div>
    <div style="font-size:12px;color:#888;margin-bottom:6px">
      {doc.date} &bull; {doc.created_by}</div>
    {f'<div style="font-size:20px;font-weight:bold;color:#27ae60;margin:10px 0">{total_str}</div>' if total_str else ''}
    <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:14px">
      <a href="/doc/{dtype}/pdf/{doc.id}" class="btn btn-primary" target="_blank">⬇ Download PDF</a>
      {email_btn}
      {wa_btn}
      {do_btn}
      {enq_to_quo_btn}
      {drive_btn}
    </div>
    <div style="margin-top:16px;display:flex;gap:8px;justify-content:center">
      <a href="/doc/{dtype}/new" class="btn btn-outline">+ New {doc_title}</a>
      <a href="/pipeline" class="btn btn-outline">Pipeline</a>
      <a href="/dashboard" class="btn btn-outline">Dashboard</a>
    </div>
  </div>
</div>'''
        return base_page(confirm, dtype, f'{ref} Saved')

    return doc_form_page(doc_type, doc_title, get_next_ref(doc_type))


@app.route('/doc/do/from-invoice/<int:inv_id>')
@login_required
def do_from_invoice(inv_id):
    inv   = Document.query.get_or_404(inv_id)
    items = json.loads(inv.items_json or '[]')
    for item in items:
        item['rate'] = item['total'] = 0
    prefill = {
        'party_name': inv.party_name, 'contact': inv.contact,
        'phone': inv.phone, 'email': inv.email,
        'inv_ref': inv.ref, 'lpo_ref': inv.lpo_ref or '',
        'party_trn': inv.party_trn or '',
    }
    for i, item in enumerate(items[:4], 1):
        prefill[f'd{i}'] = item.get('desc','')
        prefill[f's{i}'] = item.get('spec','')
        prefill[f'q{i}'] = item.get('qty',1)
        prefill[f'u{i}'] = item.get('unit','Pcs')
    return doc_form_page('DO', 'Delivery Note', get_next_ref('DO'), prefill)


@app.route('/doc/quo/from-enq/<int:enq_id>')
@login_required
def quo_from_enq(enq_id):
    enq   = Document.query.get_or_404(enq_id)
    items = json.loads(enq.items_json or '[]')
    prefill = {
        'party_name': enq.party_name, 'contact': enq.contact,
        'phone': enq.phone, 'email': enq.email, 'enquiry_ref': enq.ref,
    }
    for i, item in enumerate(items[:4], 1):
        prefill[f'd{i}'] = item.get('desc','')
        prefill[f's{i}'] = item.get('spec','')
        prefill[f'q{i}'] = item.get('qty',1)
        prefill[f'u{i}'] = item.get('unit','Pcs')
    enq.status = 'Sent'
    db.session.commit()
    flash(f'Enquiry {enq.ref} converted to Quotation.', 'success')
    return doc_form_page('QUO', 'Quotation', get_next_ref('QUO'), prefill)


@app.route('/doc/<dtype>/pdf/<int:doc_id>')
@login_required
def doc_pdf(dtype, doc_id):
    doc = Document.query.get_or_404(doc_id)
    if not HAS_RL:
        flash('ReportLab not installed.', 'error')
        return redirect(url_for('dashboard'))
    buf = generate_pdf(doc)
    if not buf:
        flash('PDF generation failed.', 'error')
        return redirect(url_for('dashboard'))
    return send_file(buf, download_name=f'{doc.ref}.pdf',
                     as_attachment=True, mimetype='application/pdf')


@app.route('/doc/<dtype>/<int:doc_id>')
@login_required
def doc_view(dtype, doc_id):
    doc   = Document.query.get_or_404(doc_id)
    items = json.loads(doc.items_json or '[]')
    rows  = ''.join(
        f'<tr><td>{i}</td><td>{it.get("desc","")}</td><td>{it.get("spec","")}</td>'
        f'<td>{it.get("qty","")}</td><td>{it.get("unit","")}</td>'
        f'<td>AED {float(it.get("rate",0)):,.2f}</td>'
        f'<td>AED {float(it.get("total",0)):,.2f}</td></tr>'
        for i, it in enumerate(items, 1))

    phone_clean = re.sub(r'[^0-9]','', doc.phone or '')
    if phone_clean.startswith('0'): phone_clean = '971' + phone_clean[1:]

    is_admin = session.get('user_role') == 'admin'
    status_options = {
        'LPO':['Raised','Delivered','Paid','VOID'],
        'INV':['Issued','Overdue','Paid','VOID'],
        'DO': ['Delivered','VOID'],
        'QUO':['Sent','Accepted','Rejected','VOID'],
        'ENQ':['Open','Sent','VOID'],
    }
    status_form = ''
    if is_admin and doc.status != 'VOID':
        opts = status_options.get(doc.doc_type, ['Open','Paid','VOID'])
        opt_html = ''.join(
            f'<option value="{s}" {"selected" if s==doc.status else ""}>{s}</option>'
            for s in opts)
        status_form = f'''
        <form method="POST" action="/doc/status/{doc.id}"
              style="display:inline-flex;gap:8px;align-items:center;margin-left:8px">
          <select name="status" style="padding:5px 8px;border:1px solid #ddd;
                  border-radius:5px;font-size:12px">{opt_html}</select>
          <button type="submit" class="btn btn-sm btn-warning" style="color:#fff">Update</button>
        </form>'''

    badge_map = {'Open':'badge-grey','Raised':'badge-orange','Sent':'badge-blue',
                 'Issued':'badge-purple','Delivered':'badge-blue','Accepted':'badge-green',
                 'Rejected':'badge-red','Paid':'badge-green','VOID':'badge-red','Overdue':'badge-red'}

    drive_link = ''
    if doc.gdrive_url:
        drive_link = f'<a href="{doc.gdrive_url}" target="_blank" class="btn btn-outline btn-sm">☁ Drive</a>'

    content = f'''<div class="card">
  <h2>{doc.ref} — {doc.doc_type}</h2>
  <div class="grid3" style="margin-bottom:14px">
    <div><b>Party:</b> {doc.party_name}<br>
         {f"<b>TRN:</b> {doc.party_trn}<br>" if doc.party_trn else ""}
         <b>Contact:</b> {doc.contact or "-"}<br>
         <b>Phone:</b> {doc.phone or "-"}</div>
    <div><b>Date:</b> {doc.date}<br>
         <b>Status:</b> <span class="badge {badge_map.get(doc.status,'badge-grey')}">{doc.status}</span><br>
         <b>Created by:</b> {doc.created_by}</div>
    <div>{"<b>DN No:</b> "+doc.quotation_ref+"<br>" if doc.quotation_ref and doc.doc_type=="INV" else ""}
         {"<b>Quo Ref:</b> "+doc.quotation_ref+"<br>" if doc.quotation_ref and doc.doc_type!="INV" else ""}
         {"<b>LPO Ref:</b> "+doc.lpo_ref+"<br>" if doc.lpo_ref else ""}
         {"<b>Due:</b> "+doc.due_date+"<br>" if doc.due_date else ""}</div>
  </div>
  <table><thead><tr><th>#</th><th>Description</th><th>Spec</th>
    <th>Qty</th><th>Unit</th><th>Rate</th><th>Amount</th></tr></thead>
  <tbody>{rows}</tbody></table>
  {"<div style='text-align:right;margin-top:10px;font-size:13px'>"
   f"Subtotal: AED {doc.subtotal:,.2f} | VAT 5%: AED {doc.vat:,.2f} | "
   f"<b>Total: AED {doc.total:,.2f}</b></div>" if doc.total else ""}
  <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center">
    <a href="/doc/{dtype}/pdf/{doc.id}" class="btn btn-primary btn-sm"
       target="_blank">⬇ PDF</a>
    {drive_link}
    {('a href="/doc/do/from-invoice/'+str(doc.id)+'" class="btn btn-info btn-sm">Create DO</a>') if doc.doc_type=='INV' else ""}
    {('a href="/doc/quo/from-enq/'+str(doc.id)+'" class="btn btn-warning btn-sm" style="color:#fff">→QUO</a>') if doc.doc_type=='ENQ' and doc.status!='VOID' else ""}
    {f'<a href="https://wa.me/{phone_clean}" class="btn btn-success btn-sm" target="_blank">💬 WA</a>' if phone_clean else ""}
    {status_form}
  </div>
</div>'''
    return base_page(content, '', doc.ref)


@app.route('/doc/status/<int:doc_id>', methods=['POST'])
@admin_required
def doc_status_update(doc_id):
    doc = Document.query.get_or_404(doc_id)
    new_status = request.form.get('status','')
    if new_status:
        doc.status = new_status
        db.session.commit()
        flash(f'{doc.ref} updated to {new_status}.', 'success')
    return redirect(url_for('doc_view', dtype=doc.doc_type.lower(), doc_id=doc.id))


@app.route('/doc/void/<int:doc_id>')
@admin_required
def doc_void(doc_id):
    doc = Document.query.get_or_404(doc_id)
    doc.status = 'VOID'
    db.session.commit()
    flash(f'{doc.ref} voided.', 'success')
    return redirect(url_for('pipeline'))


@app.route('/pipeline')
@login_required
def pipeline():
    dtype  = request.args.get('type','')
    q      = Document.query.filter(Document.status!='VOID')
    if dtype: q = q.filter_by(doc_type=dtype.upper())
    docs   = q.order_by(Document.created_at.desc()).all()

    badge = {'Open':'badge-grey','Raised':'badge-orange','Sent':'badge-blue',
             'Issued':'badge-purple','Delivered':'badge-blue',
             'Paid':'badge-green','VOID':'badge-red','Overdue':'badge-red',
             'Accepted':'badge-green','Rejected':'badge-red'}
    is_admin = session.get('user_role') == 'admin'
    rows = ''.join(f'''<tr>
      <td><a href="/doc/{d.doc_type.lower()}/{d.id}"
             style="color:#1a3a6b;font-weight:bold">{d.ref}</a></td>
      <td><span class="badge badge-blue">{d.doc_type}</span></td>
      <td>{d.party_name}</td>
      <td>{"AED {:,.2f}".format(d.total) if d.total else "-"}</td>
      <td><span class="badge {badge.get(d.status,'badge-grey')}">{d.status}</span></td>
      <td>{d.date}</td><td>{d.created_by}</td>
      <td style="white-space:nowrap">
        <a href="/doc/{d.doc_type.lower()}/pdf/{d.id}"
           class="btn btn-sm btn-primary" target="_blank">PDF</a>
        {('<a href="/doc/void/'+str(d.id)+'" class="btn btn-sm btn-danger" '
          'onclick="return confirm(\'Void '+d.ref+'?\')">Void</a>')
         if is_admin and d.status!='VOID' else ""}
      </td></tr>''' for d in docs) or \
      '<tr><td colspan="8" style="text-align:center;color:#999;padding:20px">No records.</td></tr>'

    content = f'''<div class="card">
  <h2>Transaction Pipeline ({len(docs)} records)</h2>
  <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">
    <a href="/pipeline" class="btn btn-sm {'btn-primary' if not dtype else 'btn-outline'}">All</a>
    {''.join(f'<a href="/pipeline?type={t}" class="btn btn-sm btn-outline">{t}</a>'
             for t in ['LPO','INV','DO','QUO','ENQ'])}
  </div>
  <table><thead><tr><th>Reference</th><th>Type</th><th>Party</th>
    <th>Value (AED)</th><th>Stage</th><th>Date</th><th>By</th><th>Actions</th>
  </tr></thead><tbody>{rows}</tbody></table>
</div>'''
    return base_page(content, 'pipeline', 'Pipeline')


# ── Accounts (Cheque Tracking) ────────────────────────────────

@app.route('/accounts', methods=['GET','POST'])
@admin_required
def accounts():
    if request.method == 'POST':
        action = request.form.get('action','add')
        if action == 'add':
            db.session.add(ChequeRecord(
                company_name=request.form.get('company_name',''),
                party_trn=request.form.get('party_trn',''),
                invoice_ref=request.form.get('invoice_ref',''),
                delivery_note_no=request.form.get('delivery_note_no',''),
                cheque_number=request.form.get('cheque_number',''),
                cheque_date=request.form.get('cheque_date',''),
                bank_name=request.form.get('bank_name',''),
                clearance_date=request.form.get('clearance_date',''),
                signatory=request.form.get('signatory',''),
                total_amount=float(request.form.get('total_amount',0) or 0),
                status=request.form.get('status','Pending'),
                notes=request.form.get('notes',''),
                added_by=session.get('user_name','')
            ))
            db.session.commit()
            flash('Cheque record added.', 'success')
        elif action == 'update_status':
            rid = int(request.form.get('record_id',0))
            r   = db.session.get(ChequeRecord, rid)
            if r:
                r.status = request.form.get('status', r.status)
                r.clearance_date = request.form.get('clearance_date', r.clearance_date)
                db.session.commit()
                flash('Status updated.', 'success')
        elif action == 'delete':
            rid = int(request.form.get('record_id',0))
            r   = db.session.get(ChequeRecord, rid)
            if r:
                db.session.delete(r)
                db.session.commit()
                flash('Record deleted.', 'success')
        return redirect(url_for('accounts'))

    status_f = request.args.get('status','')
    q_str    = request.args.get('q','').lower()
    records  = ChequeRecord.query.order_by(ChequeRecord.added_at.desc()).all()
    if status_f:
        records = [r for r in records if r.status == status_f]
    if q_str:
        records = [r for r in records if q_str in (
            r.company_name+r.cheque_number+r.invoice_ref+r.bank_name+'').lower()]

    total_pending = sum(r.total_amount for r in ChequeRecord.query.filter_by(status='Pending').all())
    total_cleared = sum(r.total_amount for r in ChequeRecord.query.filter_by(status='Cleared').all())
    total_bounced = sum(r.total_amount for r in ChequeRecord.query.filter_by(status='Bounced').all())

    status_badge = {'Pending':'badge-orange','Cleared':'badge-green','Bounced':'badge-red'}

    rows = ''
    for r in records:
        rows += f'''<tr>
          <td><strong>{r.company_name}</strong>
            {f"<br/><small style='color:#888;font-family:monospace'>{r.party_trn}</small>" if r.party_trn else ""}</td>
          <td style="font-family:monospace;font-size:11px">
            {f'<a href="/doc/inv/{{}}" style="color:#1a3a6b">{r.invoice_ref}</a>' if r.invoice_ref else "-"}</td>
          <td style="font-size:11px">{r.delivery_note_no or "-"}</td>
          <td style="font-family:monospace">{r.cheque_number or "-"}</td>
          <td>{r.cheque_date or "-"}</td>
          <td>{r.bank_name or "-"}</td>
          <td>{r.clearance_date or "-"}</td>
          <td>{r.signatory or "-"}</td>
          <td style="font-weight:bold;color:#1a3a6b">AED {r.total_amount:,.2f}</td>
          <td><span class="badge {status_badge.get(r.status,'badge-grey')}">{r.status}</span></td>
          <td style="white-space:nowrap">
            <form method="POST" style="display:inline-flex;gap:4px;align-items:center">
              <input type="hidden" name="action" value="update_status">
              <input type="hidden" name="record_id" value="{r.id}">
              <select name="status" style="padding:3px 5px;border:1px solid #ddd;
                      border-radius:4px;font-size:11px">
                <option {"selected" if r.status=="Pending" else ""}>Pending</option>
                <option {"selected" if r.status=="Cleared" else ""}>Cleared</option>
                <option {"selected" if r.status=="Bounced" else ""}>Bounced</option>
              </select>
              <input type="date" name="clearance_date" value="{r.clearance_date or ''}"
                     style="padding:3px 5px;border:1px solid #ddd;border-radius:4px;
                            font-size:11px;width:120px">
              <button type="submit" class="btn btn-sm btn-warning"
                      style="color:#fff">✓</button>
            </form>
            <form method="POST" style="display:inline">
              <input type="hidden" name="action" value="delete">
              <input type="hidden" name="record_id" value="{r.id}">
              <button type="submit" class="btn btn-sm btn-danger"
                      onclick="return confirm('Delete this record?')">✕</button>
            </form>
          </td>
        </tr>'''

    if not records:
        rows = '<tr><td colspan="11" style="text-align:center;color:#999;padding:20px">No cheque records yet.</td></tr>'

    content = f'''<div class="stats">
  <div class="stat" style="border-left:4px solid #f39c12">
    <div class="stat-label">Pending</div>
    <div class="stat-value orange">AED {total_pending:,.0f}</div>
    <div class="stat-sub">{ChequeRecord.query.filter_by(status="Pending").count()} cheques</div>
  </div>
  <div class="stat" style="border-left:4px solid #27ae60">
    <div class="stat-label">Cleared</div>
    <div class="stat-value green">AED {total_cleared:,.0f}</div>
    <div class="stat-sub">{ChequeRecord.query.filter_by(status="Cleared").count()} cheques</div>
  </div>
  <div class="stat" style="border-left:4px solid #e74c3c">
    <div class="stat-label">Bounced</div>
    <div class="stat-value red">AED {total_bounced:,.0f}</div>
    <div class="stat-sub">{ChequeRecord.query.filter_by(status="Bounced").count()} cheques</div>
  </div>
</div>
<div class="card">
  <h2>Accounts — Cheque Tracking</h2>
  <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center">
    <form method="GET" style="display:flex;gap:6px">
      <input type="text" name="q" value="{request.args.get('q','')}"
             placeholder="Search company, cheque, invoice..."
             style="padding:7px 11px;border:1px solid #ddd;border-radius:6px;font-size:12px;width:220px">
      <select name="status" style="padding:7px;border:1px solid #ddd;border-radius:6px;font-size:12px">
        <option value="">All Status</option>
        <option {"selected" if status_f=="Pending" else ""}>Pending</option>
        <option {"selected" if status_f=="Cleared" else ""}>Cleared</option>
        <option {"selected" if status_f=="Bounced" else ""}>Bounced</option>
      </select>
      <button type="submit" class="btn btn-primary btn-sm">Filter</button>
    </form>
    <button onclick="document.getElementById('add-cheque').style.display='block'"
            class="btn btn-success btn-sm">+ Add Cheque Record</button>
    <a href="/accounts/export" class="btn btn-outline btn-sm">⬇ Export PDF</a>
  </div>

  <div id="add-cheque" style="display:none;background:#f8f9ff;border:1px solid #dce0f0;
       border-radius:8px;padding:16px;margin-bottom:14px">
    <form method="POST">
      <input type="hidden" name="action" value="add">
      <div class="grid3">
        <div class="form-group"><label>Company Name *</label>
          <input name="company_name" required></div>
        <div class="form-group"><label>Customer TRN</label>
          <input name="party_trn" placeholder="104..."></div>
        <div class="form-group"><label>Invoice Ref</label>
          <input name="invoice_ref" placeholder="INV-2026-..."></div>
        <div class="form-group"><label>Delivery Note No.</label>
          <input name="delivery_note_no" placeholder="DO-2026-..."></div>
        <div class="form-group"><label>Cheque Number</label>
          <input name="cheque_number"></div>
        <div class="form-group"><label>Cheque Date</label>
          <input type="date" name="cheque_date"></div>
        <div class="form-group"><label>Bank Name</label>
          <input name="bank_name" placeholder="Emirates NBD, FAB..."></div>
        <div class="form-group"><label>Signatory Authority</label>
          <input name="signatory"></div>
        <div class="form-group"><label>Total Amount (AED)</label>
          <input type="number" name="total_amount" value="0" step="0.01"></div>
        <div class="form-group"><label>Status</label>
          <select name="status">
            <option>Pending</option><option>Cleared</option><option>Bounced</option>
          </select></div>
        <div class="form-group"><label>Clearance Date</label>
          <input type="date" name="clearance_date"></div>
      </div>
      <div class="form-group"><label>Notes</label>
        <textarea name="notes"></textarea></div>
      <button type="submit" class="btn btn-primary">Save Record</button>
      <button type="button"
        onclick="document.getElementById('add-cheque').style.display='none'"
        class="btn btn-outline" style="margin-left:8px">Cancel</button>
    </form>
  </div>

  <div style="overflow-x:auto">
  <table style="min-width:1200px"><thead><tr>
    <th>Company / TRN</th><th>Invoice Ref</th><th>DO Number</th>
    <th>Cheque No.</th><th>Cheque Date</th><th>Bank</th>
    <th>Clearance Date</th><th>Signatory</th>
    <th>Amount (AED)</th><th>Status</th><th>Actions</th>
  </tr></thead><tbody>{rows}</tbody></table>
  </div>
</div>'''
    return base_page(content, 'accounts', 'Accounts')


@app.route('/accounts/export')
@admin_required
def accounts_export():
    """Export cheque records as PDF."""
    if not HAS_RL:
        flash('ReportLab not installed.', 'error')
        return redirect(url_for('accounts'))
    records = ChequeRecord.query.order_by(ChequeRecord.cheque_date.desc()).all()
    buf   = io.BytesIO()
    pdoc_ = SimpleDocTemplate(buf, pagesize=A4,
                               leftMargin=10*mm, rightMargin=10*mm,
                               topMargin=15*mm, bottomMargin=15*mm)
    nat_blue = colors.HexColor('#1A3A6B')
    story = []
    sn = ParagraphStyle('n', fontSize=8, leading=11)

    story.append(Paragraph(
        f'ACCOUNTS — CHEQUE REGISTER | {COMPANY["name"]}',
        ParagraphStyle('t', fontSize=12, fontName='Helvetica-Bold',
                       alignment=TA_CENTER, textColor=nat_blue)))
    story.append(Paragraph(
        f'Generated: {datetime.now().strftime("%d/%m/%Y %H:%M")} | TRN: {COMPANY["trn"]}',
        ParagraphStyle('s', fontSize=8, alignment=TA_CENTER, textColor=colors.grey)))
    story.append(Spacer(1, 5*mm))

    data = [['Company','Invoice','DO No.','Cheque No.','Date','Bank','Amount AED','Status']]
    for r in records:
        data.append([
            r.company_name[:25], r.invoice_ref or '-', r.delivery_note_no or '-',
            r.cheque_number or '-', r.cheque_date or '-', r.bank_name or '-',
            f'{r.total_amount:,.2f}', r.status
        ])

    tbl = Table(data, colWidths=[35*mm,22*mm,22*mm,22*mm,20*mm,25*mm,22*mm,18*mm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), nat_blue),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 7),
        ('GRID', (0,0), (-1,-1), 0.3, colors.grey),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F5F7FA')]),
        ('PADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(tbl)

    # Totals
    total_pending = sum(r.total_amount for r in records if r.status=='Pending')
    total_cleared = sum(r.total_amount for r in records if r.status=='Cleared')
    story.append(Spacer(1, 5*mm))
    story.append(Paragraph(
        f'Pending: AED {total_pending:,.2f}  |  Cleared: AED {total_cleared:,.2f}',
        ParagraphStyle('tot', fontSize=9, fontName='Helvetica-Bold',
                       alignment=TA_RIGHT)))

    pdoc_.build(story)
    buf.seek(0)
    return send_file(buf,
                     download_name=f'NAT_Cheque_Register_{datetime.now().strftime("%Y%m%d")}.pdf',
                     as_attachment=True, mimetype='application/pdf')


# ── Receipt Vouchers ──────────────────────────────────────────

@app.route('/receipts', methods=['GET','POST'])
@login_required
def receipts():
    if request.method == 'POST':
        action = request.form.get('action','add')
        if action == 'add':
            ref  = get_next_ref('RCV')
            date_raw = request.form.get('date', datetime.now().strftime('%Y-%m-%d'))
            try:
                date_fmt = datetime.strptime(date_raw,'%Y-%m-%d').strftime('%d/%m/%Y')
            except:
                date_fmt = date_raw

            rcv = ReceiptVoucher(
                ref=ref,
                date=date_fmt,
                received_from=request.form.get('received_from',''),
                amount=float(request.form.get('amount',0) or 0),
                invoice_ref=request.form.get('invoice_ref',''),
                payment_method=request.form.get('payment_method','Cheque'),
                cheque_number=request.form.get('cheque_number',''),
                bank_name=request.form.get('bank_name',''),
                received_by=session.get('user_name',''),
                notes=request.form.get('notes',''),
            )
            db.session.add(rcv)
            db.session.commit()

            # Generate PDF + upload to Drive
            pdf_buf = generate_receipt_pdf(rcv)
            if pdf_buf:
                pdf_copy = io.BytesIO(pdf_buf.getvalue())
                gurl = gdrive_upload(pdf_copy, f'{rcv.ref}.pdf', 'RCV')
                if gurl:
                    rcv.gdrive_url = gurl
                    db.session.commit()

            # Mark linked invoice as Paid if provided
            inv_ref = request.form.get('invoice_ref','').strip()
            if inv_ref:
                inv_doc = Document.query.filter_by(ref=inv_ref).first()
                if inv_doc and inv_doc.status in ('Issued','Overdue'):
                    inv_doc.status = 'Paid'
                    db.session.commit()

            flash(f'Receipt Voucher {ref} created.', 'success')
            return redirect(url_for('receipt_view', rcv_id=rcv.id))
        return redirect(url_for('receipts'))

    all_rcv = ReceiptVoucher.query.order_by(ReceiptVoucher.added_at.desc()).all()
    rows = ''.join(f'''<tr>
      <td style="font-family:monospace;font-weight:bold;color:#1a3a6b">{r.ref}</td>
      <td>{r.date}</td>
      <td>{r.received_from}</td>
      <td style="font-weight:bold">AED {r.amount:,.2f}</td>
      <td style="font-size:11px">{r.invoice_ref or "-"}</td>
      <td><span class="badge badge-grey">{r.payment_method}</span></td>
      <td style="font-size:11px">{r.cheque_number or "-"}</td>
      <td style="font-size:11px">{r.bank_name or "-"}</td>
      <td>{r.received_by}</td>
      <td style="white-space:nowrap">
        <a href="/receipts/{r.id}" class="btn btn-sm btn-primary">View</a>
        <a href="/receipts/pdf/{r.id}" class="btn btn-sm btn-outline"
           target="_blank">PDF</a>
        {f'<a href="{r.gdrive_url}" target="_blank" class="btn btn-sm btn-outline">☁</a>' if r.gdrive_url else ""}
      </td>
    </tr>''' for r in all_rcv) or \
    '<tr><td colspan="10" style="text-align:center;color:#999;padding:20px">No receipts yet.</td></tr>'

    content = f'''<div class="card">
  <h2>Receipt Vouchers ({len(all_rcv)} records)</h2>
  <button onclick="document.getElementById('add-rcv').style.display='block'"
          class="btn btn-success btn-sm" style="margin-bottom:14px">+ New Receipt Voucher</button>

  <div id="add-rcv" style="display:none;background:#f8f9ff;border:1px solid #dce0f0;
       border-radius:8px;padding:16px;margin-bottom:14px">
    <form method="POST">
      <input type="hidden" name="action" value="add">
      <div class="grid3">
        <div class="form-group"><label>Date</label>
          <input type="date" name="date" value="{datetime.now().strftime('%Y-%m-%d')}"></div>
        <div class="form-group"><label>Received From *</label>
          <input name="received_from" required placeholder="Company name"></div>
        <div class="form-group"><label>Amount (AED) *</label>
          <input type="number" name="amount" value="0" step="0.01" required></div>
        <div class="form-group"><label>Against Invoice Ref</label>
          <input name="invoice_ref" placeholder="INV-2026-..."></div>
        <div class="form-group"><label>Payment Method</label>
          <select name="payment_method">
            <option>Cheque</option><option>Cash</option><option>Bank Transfer</option>
          </select></div>
        <div class="form-group"><label>Cheque Number</label>
          <input name="cheque_number"></div>
        <div class="form-group"><label>Bank Name</label>
          <input name="bank_name" placeholder="Emirates NBD, FAB..."></div>
      </div>
      <div class="form-group"><label>Notes</label>
        <textarea name="notes"></textarea></div>
      <button type="submit" class="btn btn-primary">Create Receipt Voucher</button>
      <button type="button"
        onclick="document.getElementById('add-rcv').style.display='none'"
        class="btn btn-outline" style="margin-left:8px">Cancel</button>
    </form>
  </div>

  <table><thead><tr><th>Ref</th><th>Date</th><th>Received From</th>
    <th>Amount</th><th>Invoice Ref</th><th>Method</th>
    <th>Cheque No.</th><th>Bank</th><th>By</th><th>Actions</th>
  </tr></thead><tbody>{rows}</tbody></table>
</div>'''
    return base_page(content, 'receipts', 'Receipt Vouchers')


@app.route('/receipts/<int:rcv_id>')
@login_required
def receipt_view(rcv_id):
    rcv = ReceiptVoucher.query.get_or_404(rcv_id)
    content = f'''<div class="card" style="max-width:600px;margin:0 auto">
  <h2>Receipt Voucher — {rcv.ref}</h2>
  <div class="grid2" style="margin-bottom:16px">
    <div><b>Date:</b> {rcv.date}</div>
    <div><b>Received From:</b> {rcv.received_from}</div>
    <div><b>Amount:</b> <span style="font-size:18px;font-weight:bold;color:#27ae60">
      AED {rcv.amount:,.2f}</span></div>
    <div><b>Invoice Ref:</b> {rcv.invoice_ref or "-"}</div>
    <div><b>Payment Method:</b> {rcv.payment_method}</div>
    <div><b>Cheque No.:</b> {rcv.cheque_number or "-"}</div>
    <div><b>Bank:</b> {rcv.bank_name or "-"}</div>
    <div><b>Received By:</b> {rcv.received_by}</div>
  </div>
  {"<p><b>Notes:</b> "+rcv.notes+"</p>" if rcv.notes else ""}
  <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
    <a href="/receipts/pdf/{rcv.id}" class="btn btn-primary" target="_blank">⬇ Download PDF</a>
    {f'<a href="{rcv.gdrive_url}" target="_blank" class="btn btn-outline">☁ View on Drive</a>' if rcv.gdrive_url else ""}
    {f'<a href="mailto:?subject=Receipt Voucher {rcv.ref}&body=Dear {rcv.received_from},%0A%0APlease find your receipt voucher {rcv.ref} for AED {rcv.amount:,.2f}.%0A%0ANew Asian General Trading LLC" class="btn btn-success">✉ Email</a>'}
    <a href="/receipts" class="btn btn-outline">← Back</a>
  </div>
</div>'''
    return base_page(content, 'receipts', rcv.ref)


@app.route('/receipts/pdf/<int:rcv_id>')
@login_required
def receipt_pdf(rcv_id):
    rcv = ReceiptVoucher.query.get_or_404(rcv_id)
    buf = generate_receipt_pdf(rcv)
    if not buf:
        flash('PDF generation failed.', 'error')
        return redirect(url_for('receipts'))
    return send_file(buf, download_name=f'{rcv.ref}.pdf',
                     as_attachment=True, mimetype='application/pdf')


# ── Profit / Margin Tab ───────────────────────────────────────

@app.route('/profit')
@admin_required
def profit():
    """Internal margin view — not customer-facing."""
    inv_docs = Document.query.filter(
        Document.doc_type=='INV',
        Document.status!='VOID'
    ).order_by(Document.created_at.desc()).all()

    total_revenue = 0
    total_cost    = 0
    rows = ''
    for doc in inv_docs:
        items = json.loads(doc.items_json or '[]')
        doc_revenue = doc.subtotal or 0
        doc_cost    = 0
        doc_margin  = 0

        item_detail = []
        for item in items:
            sell_rate  = float(item.get('rate', 0))
            qty        = float(item.get('qty', 0))
            sell_total = sell_rate * qty

            # Look up cost price from catalog
            cat_item = CatalogItem.query.filter(
                db.func.lower(CatalogItem.name)==item.get('desc','').lower()
            ).first()
            cost_rate  = cat_item.cost_price if cat_item and cat_item.cost_price else 0
            cost_total = cost_rate * qty
            margin_aed = sell_total - cost_total
            margin_pct = round((margin_aed / sell_total * 100), 1) if sell_total else 0

            doc_cost += cost_total
            item_detail.append({
                'desc': item.get('desc',''), 'qty': qty,
                'sell': sell_rate, 'cost': cost_rate,
                'margin_aed': margin_aed, 'margin_pct': margin_pct
            })

        doc_margin = doc_revenue - doc_cost
        margin_pct_doc = round((doc_margin / doc_revenue * 100), 1) if doc_revenue else 0
        total_revenue += doc_revenue
        total_cost    += doc_cost

        color = '#27ae60' if margin_pct_doc >= 15 else '#f39c12' if margin_pct_doc >= 5 else '#e74c3c'

        item_rows = ''.join(
            f'<tr style="font-size:11px">'
            f'<td style="padding-left:20px;color:#555">{it["desc"]}</td>'
            f'<td>{it["qty"]}</td>'
            f'<td>AED {it["sell"]:,.2f}</td>'
            f'<td style="color:#e74c3c">AED {it["cost"]:,.2f}</td>'
            f'<td style="color:#27ae60">AED {it["margin_aed"]:,.2f}</td>'
            f'<td style="color:{color if it["margin_pct"]>0 else "#e74c3c"};font-weight:bold">'
            f'{it["margin_pct"]}%</td>'
            f'</tr>' for it in item_detail
        )

        rows += f'''<tr>
          <td><a href="/doc/inv/{doc.id}" style="color:#1a3a6b;font-weight:bold">{doc.ref}</a></td>
          <td>{doc.party_name}</td>
          <td>{doc.date}</td>
          <td>AED {doc_revenue:,.2f}</td>
          <td style="color:#e74c3c">{"AED {:,.2f}".format(doc_cost) if doc_cost else "<span style='color:#999'>No cost data</span>"}</td>
          <td style="color:{color};font-weight:bold">
            {"AED {:,.2f}".format(doc_margin) if doc_cost else "-"}</td>
          <td style="color:{color};font-weight:bold">
            {str(margin_pct_doc)+"%" if doc_cost else "-"}</td>
          <td>
            <button onclick="document.getElementById('detail-{doc.id}').style.display=
              document.getElementById('detail-{doc.id}').style.display=='none'?'table-row':'none'"
              class="btn btn-sm btn-outline">Details</button>
          </td>
        </tr>
        <tr id="detail-{doc.id}" style="display:none;background:#f8f9ff">
          <td colspan="8" style="padding:0">
            <table style="width:100%;margin:0">
              <thead><tr style="background:#e8eaf0">
                <th style="padding-left:20px">Item</th><th>Qty</th>
                <th>Sell Rate</th><th>Cost Rate</th>
                <th>Margin AED</th><th>Margin %</th>
              </tr></thead>
              <tbody>{item_rows}</tbody>
            </table>
          </td>
        </tr>'''

    total_margin = total_revenue - total_cost
    total_margin_pct = round((total_margin / total_revenue * 100), 1) if total_revenue else 0

    note = ''
    if not CatalogItem.query.filter(CatalogItem.cost_price > 0).first():
        note = '''<div class="alert alert-warning">
          No cost prices set yet. Go to <a href="/catalog">Catalog</a> and add
          cost prices for each item to see accurate margin data.
        </div>'''

    content = f'''{note}
<div class="stats">
  <div class="stat">
    <div class="stat-label">Total Revenue (ex-VAT)</div>
    <div class="stat-value">AED {total_revenue:,.0f}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Total Cost (from Catalog)</div>
    <div class="stat-value red">AED {total_cost:,.0f}</div>
  </div>
  <div class="stat" style="border:2px solid #1a3a6b">
    <div class="stat-label">Gross Margin</div>
    <div class="stat-value green">AED {total_margin:,.0f}</div>
    <div class="stat-sub">{total_margin_pct}% overall</div>
  </div>
</div>
<div class="card">
  <h2>Margin by Invoice — Internal View Only</h2>
  <p style="font-size:11px;color:#888;margin-bottom:12px">
    This page is not visible to clients. Cost prices come from the Catalog.
    Items without a catalog cost price show no margin data.
  </p>
  <table><thead><tr>
    <th>Invoice</th><th>Client</th><th>Date</th>
    <th>Revenue</th><th>Cost</th><th>Margin AED</th><th>Margin %</th><th></th>
  </tr></thead><tbody>{rows}</tbody></table>
</div>'''
    return base_page(content, 'profit', 'Profit & Margin')


# ── Settings (with logo + stamp upload) ──────────────────────

@app.route('/settings', methods=['GET','POST'])
@admin_required
def settings():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'upload_logo':
            f = request.files.get('logo_file')
            if f and f.filename:
                ext = f.filename.rsplit('.',1)[-1].lower()
                if ext in ['png','jpg','jpeg']:
                    raw  = f.read()
                    mime = 'image/png' if ext=='png' else 'image/jpeg'
                    b64  = f'data:{mime};base64,' + base64.b64encode(raw).decode()
                    CompanySetting.set('logo_b64', b64)
                    flash('Company logo updated. Refresh to see it in the topbar.', 'success')
                else:
                    flash('Only PNG or JPG accepted.', 'error')

        elif action == 'upload_stamp':
            f = request.files.get('stamp_file')
            if f and f.filename:
                ext = f.filename.rsplit('.',1)[-1].lower()
                if ext in ['png','jpg','jpeg']:
                    raw  = f.read()
                    mime = 'image/png' if ext=='png' else 'image/jpeg'
                    b64  = f'data:{mime};base64,' + base64.b64encode(raw).decode()
                    CompanySetting.set('stamp_b64', b64)
                    flash('NAT stamp uploaded. It will appear on Invoice and LPO PDFs.', 'success')
                else:
                    flash('Only PNG or JPG accepted.', 'error')

        elif action == 'clear_stamp':
            CompanySetting.set('stamp_b64', None)
            flash('Stamp cleared.', 'success')

        elif action == 'clear_logo':
            CompanySetting.set('logo_b64', None)
            flash('Logo cleared from DB (file on disk will be used if present).', 'success')

        elif action == 'gdrive_settings':
            CompanySetting.set('gdrive_folder_id', request.form.get('folder_id','').strip())
            creds_file = request.files.get('creds_file')
            if creds_file and creds_file.filename:
                try:
                    creds_json = creds_file.read().decode('utf-8')
                    json.loads(creds_json)  # validate
                    CompanySetting.set('gdrive_creds_json', creds_json)
                    flash('Google Drive credentials saved.', 'success')
                except Exception as e:
                    flash(f'Invalid JSON credentials: {e}', 'error')
            else:
                flash('Google Drive folder ID saved.', 'success')

        elif action == 'add_user':
            email = request.form.get('email','').lower().strip()
            if User.query.filter_by(email=email).first():
                flash('Email already exists.', 'error')
            else:
                db.session.add(User(
                    name=request.form.get('name',''),
                    email=email,
                    email2=request.form.get('email2','').lower().strip() or None,
                    password=generate_password_hash(request.form.get('password','')),
                    role=request.form.get('role','editor')
                ))
                db.session.commit()
                flash('User created.', 'success')

        elif action == 'update_counters':
            for dt in ['LPO','INV','DO','QUO','ENQ','RCV']:
                c = DocCounter.query.filter_by(doc_type=dt).first()
                val = int(request.form.get(f'num_{dt}', c.last_num if c else 0))
                if c:
                    c.last_num = val
                else:
                    prefixes = {'LPO':'LPO-2026-','INV':'INV-2026-','DO':'DO-2026-',
                                'QUO':'QUO-2026-','ENQ':'ENQ-2026-','RCV':'RCV-2026-'}
                    db.session.add(DocCounter(doc_type=dt, prefix=prefixes[dt], last_num=val))
            db.session.commit()
            flash('Counters updated.', 'success')

        elif action == 'change_password':
            uid  = int(request.form.get('user_id',0))
            u    = db.session.get(User, uid)
            newp = request.form.get('new_password','')
            if u and newp:
                u.password = generate_password_hash(newp)
                db.session.commit()
                flash(f'Password updated for {u.name}.', 'success')

        elif action == 'change_role':
            uid      = int(request.form.get('user_id',0))
            u        = db.session.get(User, uid)
            new_role = request.form.get('new_role','editor')
            if u:
                u.role = new_role
                db.session.commit()
                flash(f'Role updated for {u.name}.', 'success')

        elif action == 'upload_signature':
            uid      = int(request.form.get('user_id',0))
            u        = db.session.get(User, uid)
            sig_file = request.files.get('signature_file')
            if u and sig_file and sig_file.filename:
                ext = sig_file.filename.rsplit('.',1)[-1].lower()
                if ext in ['png','jpg','jpeg']:
                    raw  = sig_file.read()
                    b64  = base64.b64encode(raw).decode()
                    mime = 'image/png' if ext=='png' else 'image/jpeg'
                    u.signature_b64 = f'data:{mime};base64,{b64}'
                    db.session.commit()
                    flash(f'Signature uploaded for {u.name}.', 'success')
                else:
                    flash('Only PNG or JPG signatures accepted.', 'error')

        elif action == 'clear_signature':
            uid = int(request.form.get('user_id',0))
            u   = db.session.get(User, uid)
            if u:
                u.signature_b64 = None
                db.session.commit()
                flash(f'Signature cleared for {u.name}.', 'success')

        return redirect(url_for('settings'))

    users    = User.query.all()
    counters = DocCounter.query.all()
    logo_b64 = get_logo_b64()
    stamp_b64 = get_stamp_b64()
    folder_id = CompanySetting.get('gdrive_folder_id','')
    has_creds = bool(CompanySetting.get('gdrive_creds_json'))

    user_rows = ''
    for u in users:
        sig_html = ''
        if u.signature_b64:
            sig_html = f'<img src="{u.signature_b64}" style="height:28px;border:1px solid #ddd;border-radius:3px;margin-right:6px">'
        user_rows += f'''<tr>
          <td>{u.name}</td><td style="font-size:11px">{u.email}</td>
          <td style="font-size:11px">{u.email2 or "-"}</td>
          <td>
            <form method="POST" style="display:inline">
              <input type="hidden" name="action" value="change_role">
              <input type="hidden" name="user_id" value="{u.id}">
              <select name="new_role" onchange="this.form.submit()"
                      style="padding:3px 6px;border:1px solid #ddd;border-radius:4px;font-size:11px">
                <option value="admin" {"selected" if u.role=="admin" else ""}>admin</option>
                <option value="editor" {"selected" if u.role=="editor" else ""}>editor</option>
              </select>
            </form>
          </td>
          <td>
            <form method="POST" style="display:inline;gap:4px">
              <input type="hidden" name="action" value="change_password">
              <input type="hidden" name="user_id" value="{u.id}">
              <input type="password" name="new_password" placeholder="New password"
                     style="padding:4px 8px;border:1px solid #ddd;border-radius:4px;font-size:11px;width:130px">
              <button type="submit" class="btn btn-sm btn-warning" style="color:#fff">Reset</button>
            </form>
          </td>
          <td>
            {sig_html}
            <form method="POST" enctype="multipart/form-data" style="display:inline">
              <input type="hidden" name="action" value="upload_signature">
              <input type="hidden" name="user_id" value="{u.id}">
              <input type="file" name="signature_file" accept=".png,.jpg,.jpeg"
                     style="font-size:11px;max-width:160px">
              <button type="submit" class="btn btn-sm btn-info">Upload Sig</button>
            </form>
            {"<form method='POST' style='display:inline'><input type='hidden' name='action' value='clear_signature'><input type='hidden' name='user_id' value='"+str(u.id)+"'><button type='submit' class='btn btn-sm btn-danger'>Clear</button></form>" if u.signature_b64 else ""}
          </td>
        </tr>'''

    counter_rows = ''.join(f'''<tr>
      <td><span class="badge badge-blue">{c.doc_type}</span></td>
      <td>{c.last_num}</td>
      <td style="font-family:monospace">{c.prefix}{str(c.last_num+1).zfill(3)}</td>
      <td><input type="number" name="num_{c.doc_type}" value="{c.last_num}"
                 style="width:80px;padding:4px;border:1px solid #ddd;border-radius:4px;font-size:12px">
      </td></tr>''' for c in counters)

    logo_preview = (f'<img src="{logo_b64}" style="height:50px;border:1px solid #ddd;'
                    f'border-radius:4px;margin-right:8px">'
                    if logo_b64 else '<span style="color:#999">No logo in DB (using file)</span>')
    stamp_preview = (f'<img src="{stamp_b64}" style="height:50px;border:1px solid #ddd;'
                     f'border-radius:4px;margin-right:8px">'
                     if stamp_b64 else '<span style="color:#999">No stamp uploaded</span>')

    content = f'''
<div class="card">
  <h2>Company Logo & Stamp</h2>
  <div class="grid2" style="gap:20px">
    <div>
      <div style="font-size:12px;font-weight:bold;color:#555;margin-bottom:8px">Current Logo</div>
      <div style="display:flex;align-items:center;margin-bottom:10px">{logo_preview}</div>
      <form method="POST" enctype="multipart/form-data" style="display:flex;gap:8px;align-items:center">
        <input type="hidden" name="action" value="upload_logo">
        <input type="file" name="logo_file" accept=".png,.jpg,.jpeg" style="font-size:12px">
        <button type="submit" class="btn btn-sm btn-primary">Upload Logo</button>
      </form>
      {"<form method='POST' style='margin-top:6px'><input type='hidden' name='action' value='clear_logo'><button type='submit' class='btn btn-sm btn-danger'>Clear DB Logo</button></form>" if logo_b64 else ""}
    </div>
    <div>
      <div style="font-size:12px;font-weight:bold;color:#555;margin-bottom:8px">
        NAT Stamp (appears on Invoice &amp; LPO PDFs)</div>
      <div style="display:flex;align-items:center;margin-bottom:10px">{stamp_preview}</div>
      <form method="POST" enctype="multipart/form-data" style="display:flex;gap:8px;align-items:center">
        <input type="hidden" name="action" value="upload_stamp">
        <input type="file" name="stamp_file" accept=".png,.jpg,.jpeg" style="font-size:12px">
        <button type="submit" class="btn btn-sm btn-primary">Upload Stamp</button>
      </form>
      {"<form method='POST' style='margin-top:6px'><input type='hidden' name='action' value='clear_stamp'><button type='submit' class='btn btn-sm btn-danger'>Clear Stamp</button></form>" if stamp_b64 else ""}
    </div>
  </div>
</div>

<div class="card">
  <h2>Google Drive Integration</h2>
  <p style="font-size:12px;color:#555;margin-bottom:12px">
    Every generated PDF will auto-upload to your Google Drive folder.
    Folder: <code>sales@newasiantrd.com</code> — 2TB available.
  </p>
  <form method="POST" enctype="multipart/form-data">
    <input type="hidden" name="action" value="gdrive_settings">
    <div class="grid2">
      <div class="form-group">
        <label>Google Drive Folder ID</label>
        <input name="folder_id" value="{folder_id}"
               placeholder="1AbCdEfGhIjKlMnOpQrStUvWxYz...">
        <small style="color:#888;font-size:11px">
          The folder ID from the Drive URL: drive.google.com/drive/folders/<b>FOLDER_ID</b>
        </small>
      </div>
      <div class="form-group">
        <label>Service Account JSON (credentials file)</label>
        <input type="file" name="creds_file" accept=".json" style="font-size:12px">
        <small style="color:#888;font-size:11px">
          Status: {'✅ Credentials loaded' if has_creds else '⚠ No credentials — upload JSON from Google Cloud Console'}
        </small>
      </div>
    </div>
    <button type="submit" class="btn btn-primary btn-sm">Save Drive Settings</button>
  </form>
</div>

<div class="card">
  <h2>User Management & Signatures</h2>
  <div style="overflow-x:auto">
  <table style="min-width:900px">
    <thead><tr><th>Name</th><th>Primary Email</th><th>Secondary Email</th>
      <th>Role</th><th>Password Reset</th><th>Signature</th></tr></thead>
    <tbody>{user_rows}</tbody>
  </table>
  </div>
  <h2 style="margin-top:20px">Add New User</h2>
  <form method="POST" style="margin-top:10px">
    <input type="hidden" name="action" value="add_user">
    <div class="grid3">
      <div class="form-group"><label>Full Name *</label>
        <input name="name" required></div>
      <div class="form-group"><label>Primary Email *</label>
        <input type="email" name="email" required></div>
      <div class="form-group"><label>Secondary Email</label>
        <input type="email" name="email2"></div>
      <div class="form-group"><label>Password *</label>
        <input type="password" name="password" required></div>
      <div class="form-group"><label>Role</label>
        <select name="role">
          <option value="editor">Editor</option>
          <option value="admin">Admin</option>
        </select></div>
    </div>
    <button type="submit" class="btn btn-primary">Create User</button>
  </form>
</div>

<div class="card">
  <h2>Document Number Counters</h2>
  <form method="POST">
    <input type="hidden" name="action" value="update_counters">
    <table style="max-width:500px">
      <thead><tr><th>Type</th><th>Current</th><th>Next</th><th>Override</th></tr></thead>
      <tbody>{counter_rows}</tbody>
    </table>
    <button type="submit" class="btn btn-warning"
            style="margin-top:10px;color:#fff">Update Counters</button>
  </form>
</div>'''
    return base_page(content, 'settings', 'Settings')


# ── Catalog (with cost_price + markup_pct) ────────────────────

@app.route('/catalog', methods=['GET','POST'])
@login_required
def catalog():
    if request.method == 'POST':
        action = request.form.get('action','add')
        if action == 'add':
            cost  = float(request.form.get('cost_price',0) or 0)
            mkup  = float(request.form.get('markup_pct',0) or 0)
            sell  = cost * (1 + mkup/100) if cost and mkup else float(request.form.get('last_price',0) or 0)
            db.session.add(CatalogItem(
                name=request.form.get('name',''),
                spec=request.form.get('spec',''),
                unit=request.form.get('unit','Pcs'),
                category=request.form.get('category',''),
                vendors=request.form.get('vendors',''),
                cost_price=cost,
                markup_pct=mkup,
                last_price=sell,
                added_by=session.get('user_name','')
            ))
            db.session.commit()
            flash('Item added.', 'success')
        elif action == 'edit':
            iid  = int(request.form.get('item_id',0))
            item = db.session.get(CatalogItem, iid)
            if item:
                cost = float(request.form.get('cost_price',0) or 0)
                mkup = float(request.form.get('markup_pct',0) or 0)
                sell = cost * (1 + mkup/100) if cost and mkup else float(request.form.get('last_price',0) or 0)
                item.name=request.form.get('name',item.name)
                item.spec=request.form.get('spec',item.spec)
                item.unit=request.form.get('unit',item.unit)
                item.category=request.form.get('category',item.category)
                item.vendors=request.form.get('vendors',item.vendors)
                item.cost_price=cost
                item.markup_pct=mkup
                item.last_price=sell
                db.session.commit()
                flash('Item updated.', 'success')
        elif action == 'toggle_active':
            iid  = int(request.form.get('item_id',0))
            item = db.session.get(CatalogItem, iid)
            if item:
                item.active = not item.active
                db.session.commit()
        elif action == 'import':
            f = request.files.get('import_file')
            if f and f.filename:
                try:
                    if f.filename.endswith('.csv'):
                        df = pd.read_csv(io.StringIO(f.read().decode('utf-8-sig')))
                    else:
                        df = pd.read_excel(f)
                    df.columns = [c.strip() for c in df.columns]
                    count = 0
                    for _, row in df.iterrows():
                        n = str(row.get('Name','')).strip()
                        if not n or n=='nan': continue
                        cost = float(row.get('Cost_Price',0) or 0)
                        mkup = float(row.get('Markup_Pct',0) or 0)
                        sell = cost*(1+mkup/100) if cost and mkup else float(row.get('Last_Price',0) or 0)
                        db.session.add(CatalogItem(
                            name=n,
                            spec=str(row.get('Spec','')).strip(),
                            unit=str(row.get('Unit','Pcs')).strip(),
                            category=str(row.get('Category','')).strip(),
                            vendors=str(row.get('Vendors','')).strip(),
                            cost_price=cost, markup_pct=mkup, last_price=sell,
                            added_by=session.get('user_name','')
                        ))
                        count += 1
                    db.session.commit()
                    flash(f'{count} items imported.', 'success')
                except Exception as e:
                    flash(f'Import failed: {e}', 'error')
        return redirect(url_for('catalog'))

    q_str    = request.args.get('q','').lower()
    per_page = int(request.args.get('per_page', 100))
    if per_page not in (50,100,200,500): per_page = 100
    page     = max(1, int(request.args.get('page', 1)))

    q_obj = CatalogItem.query.filter_by(active=True).order_by(CatalogItem.name)
    if q_str:
        q_obj = q_obj.filter(db.or_(
            CatalogItem.name.ilike(f'%{q_str}%'),
            CatalogItem.spec.ilike(f'%{q_str}%'),
            CatalogItem.category.ilike(f'%{q_str}%'),
        ))
    total_items = q_obj.count()
    total_pages = max(1, -(-total_items // per_page))
    page        = min(page, total_pages)
    items       = q_obj.offset((page-1)*per_page).limit(per_page).all()

    is_admin = session.get('user_role') == 'admin'
    rows = ''.join(f'''<tr>
      <td><strong>{i.name}</strong></td>
      <td style="font-size:11px;color:#555">{i.spec or "-"}</td>
      <td>{i.unit}</td>
      <td><span class="badge badge-grey">{i.category or "-"}</span></td>
      <td style="font-size:11px">{i.vendors or "-"}</td>
      <td style="color:#e74c3c;font-weight:bold">
        {"AED {:,.2f}".format(i.cost_price) if i.cost_price else "-"}</td>
      <td style="color:#888">{str(i.markup_pct)+"%" if i.markup_pct else "-"}</td>
      <td style="font-weight:bold;color:#1a3a6b">
        {"AED {:,.2f}".format(i.last_price) if i.last_price else "-"}</td>
      <td style="white-space:nowrap">
        <button onclick="document.getElementById('edit-item-{i.id}').style.display='block'"
                class="btn btn-sm btn-warning" style="color:#fff">Edit</button>
        <a href="/catalog/history/{i.id}" class="btn btn-sm btn-outline">History</a>
      </td></tr>
      <tr id="edit-item-{i.id}" style="display:none;background:#f8f9ff">
        <td colspan="9" style="padding:14px">
          <form method="POST">
            <input type="hidden" name="action" value="edit">
            <input type="hidden" name="item_id" value="{i.id}">
            <div class="grid4">
              <div class="form-group"><label>Item Name</label>
                <input name="name" value="{i.name}"></div>
              <div class="form-group"><label>Specification</label>
                <input name="spec" value="{i.spec or ''}"></div>
              <div class="form-group"><label>Unit</label>
                <input name="unit" value="{i.unit}"></div>
              <div class="form-group"><label>Category</label>
                <input name="category" value="{i.category or ''}"></div>
              <div class="form-group"><label>Linked Vendors</label>
                <input name="vendors" value="{i.vendors or ''}"></div>
              <div class="form-group"><label>Cost Price (AED) — internal</label>
                <input type="number" name="cost_price" value="{i.cost_price or 0}" step="0.01"></div>
              <div class="form-group"><label>Markup % (auto-calc sell price)</label>
                <input type="number" name="markup_pct" value="{i.markup_pct or 0}" step="0.1"></div>
              <div class="form-group"><label>Selling Price (AED) — override</label>
                <input type="number" name="last_price" value="{i.last_price or 0}" step="0.01"></div>
            </div>
            <button type="submit" class="btn btn-primary btn-sm">Save</button>
            <button type="button"
              onclick="document.getElementById('edit-item-{i.id}').style.display='none'"
              class="btn btn-outline btn-sm" style="margin-left:8px">Cancel</button>
          </form>
        </td>
      </tr>''' for i in items) or \
      '<tr><td colspan="9" style="text-align:center;color:#999;padding:20px">No items yet.</td></tr>'

    def _pg_url_cat(p, pp=per_page):
        return f"/catalog?q={request.args.get('q','')}&page={p}&per_page={pp}"

    pg_controls_cat = f'''
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
    <span style="font-size:12px;color:#666">Show:
      {''.join(f'<a href="{_pg_url_cat(1,n)}" class="btn btn-sm {"btn-primary" if per_page==n else "btn-outline"}">{n}</a>' for n in [50,100,200,500])}
    </span>
    <span style="font-size:12px;color:#666;margin-left:10px">
      Page {page} of {total_pages} &nbsp;
      {'<a href="'+_pg_url_cat(1)+'" class="btn btn-sm btn-outline">«</a>' if page>1 else ''}
      {'<a href="'+_pg_url_cat(page-1)+'" class="btn btn-sm btn-outline">‹</a>' if page>1 else ''}
      {''.join(f'<a href="{_pg_url_cat(p)}" class="btn btn-sm {"btn-primary" if p==page else "btn-outline"}">{p}</a>' for p in range(max(1,page-2), min(total_pages+1,page+3)))}
      {'<a href="'+_pg_url_cat(page+1)+'" class="btn btn-sm btn-outline">›</a>' if page<total_pages else ''}
      {'<a href="'+_pg_url_cat(total_pages)+'" class="btn btn-sm btn-outline">»</a>' if page<total_pages else ''}
    </span>
  </div>'''

    content = f'''<div class="card">
  <h2>Items Catalog ({total_items} items)</h2>
  <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center">
    <form method="GET" style="display:flex;gap:6px">
      <input type="text" name="q" value="{request.args.get('q','')}"
             placeholder="Search item, spec, category..."
             style="padding:7px 11px;border:1px solid #ddd;border-radius:6px;font-size:12px;width:220px">
      <input type="hidden" name="per_page" value="{per_page}">
      <button type="submit" class="btn btn-primary btn-sm">Search</button>
    </form>
    <button onclick="document.getElementById('add-item').style.display='block'"
            class="btn btn-success btn-sm">+ Add Item</button>
    <button onclick="document.getElementById('import-catalog').style.display='block'"
            class="btn btn-info btn-sm">⬆ Import CSV</button>
    <a href="/catalog/template" class="btn btn-outline btn-sm">⬇ Template</a>
  </div>
  {pg_controls_cat}

  <div id="add-item" style="display:none;background:#f8f9ff;border:1px solid #dce0f0;
       border-radius:8px;padding:16px;margin-bottom:14px">
    <form method="POST">
      <input type="hidden" name="action" value="add">
      <div class="grid4">
        <div class="form-group"><label>Item Name *</label>
          <input name="name" required placeholder="Ball Valve 2 inch"></div>
        <div class="form-group"><label>Specification</label>
          <input name="spec" placeholder="SS316, PN16"></div>
        <div class="form-group"><label>Unit</label>
          <input name="unit" value="Pcs"></div>
        <div class="form-group"><label>Category</label>
          <input name="category" placeholder="Valves, Pipes..."></div>
        <div class="form-group"><label>Linked Vendors</label>
          <input name="vendors" placeholder="Vendor1, Vendor2"></div>
        <div class="form-group"><label>Cost Price (AED) — internal only</label>
          <input type="number" name="cost_price" value="0" step="0.01"></div>
        <div class="form-group"><label>Default Markup %</label>
          <input type="number" name="markup_pct" value="0" step="0.1"
                 placeholder="e.g. 15 for 15%"></div>
        <div class="form-group"><label>Selling Price (AED) — overrides markup</label>
          <input type="number" name="last_price" value="0" step="0.01"></div>
      </div>
      <button type="submit" class="btn btn-primary">Add to Catalog</button>
      <button type="button"
        onclick="document.getElementById('add-item').style.display='none'"
        class="btn btn-outline" style="margin-left:8px">Cancel</button>
    </form>
  </div>

  <div id="import-catalog" style="display:none">
    <div class="import-box">
      <form method="POST" enctype="multipart/form-data"
            style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
        <input type="hidden" name="action" value="import">
        <input type="file" name="import_file" accept=".csv,.xlsx,.xls" style="font-size:12px">
        <button type="submit" class="btn btn-primary">Import</button>
        <button type="button"
          onclick="document.getElementById('import-catalog').style.display='none'"
          class="btn btn-outline">Cancel</button>
      </form>
    </div>
  </div>

  <table><thead><tr><th>Item Name</th><th>Specification</th><th>Unit</th>
    <th>Category</th><th>Vendors</th>
    <th>Cost Price</th><th>Markup</th><th>Sell Price</th><th>Actions</th>
  </tr></thead><tbody>{rows}</tbody></table>
  {pg_controls_cat}
</div>'''
    return base_page(content, 'catalog', 'Items Catalog')


@app.route('/catalog/search')
@login_required
def catalog_search():
    q = request.args.get('q','').strip()
    if len(q) < 2:
        return jsonify([])
    items = CatalogItem.query.filter(
        CatalogItem.active==True,
        db.or_(
            CatalogItem.name.ilike(f'%{q}%'),
            CatalogItem.spec.ilike(f'%{q}%'),
            CatalogItem.category.ilike(f'%{q}%')
        )
    ).limit(10).all()
    return jsonify([{
        'name': i.name, 'spec': i.spec or '',
        'unit': i.unit, 'last_price': i.last_price or 0,
        'category': i.category or ''
    } for i in items])


@app.route('/catalog/history/<int:item_id>')
@login_required
def catalog_history(item_id):
    item = CatalogItem.query.get_or_404(item_id)
    logs = ItemPriceLog.query.filter_by(item_id=item_id).order_by(ItemPriceLog.logged_at.desc()).all()
    rows = ''.join(f'''<tr>
      <td>{l.logged_at.strftime("%d/%m/%Y %H:%M") if l.logged_at else "-"}</td>
      <td style="font-weight:bold;color:#1a3a6b">AED {l.price:,.2f}</td>
      <td>{l.vendor or "-"}</td>
      <td style="font-family:monospace">{l.doc_ref or "-"}</td>
    </tr>''' for l in logs) or \
    '<tr><td colspan="4" style="text-align:center;color:#999;padding:20px">No price history yet.</td></tr>'

    content = f'''<div class="card">
  <h2>Price History — {item.name}</h2>
  <p style="color:#555;font-size:12px;margin-bottom:14px">
    Cost: AED {item.cost_price:,.2f} &bull; Markup: {item.markup_pct}% &bull;
    Current Sell: AED {item.last_price:,.2f}</p>
  <table><thead><tr><th>Date</th><th>Price (AED)</th><th>Vendor</th><th>Document Ref</th></tr></thead>
  <tbody>{rows}</tbody></table>
  <div style="margin-top:14px">
    <a href="/catalog" class="btn btn-outline btn-sm">← Back to Catalog</a>
  </div>
</div>'''
    return base_page(content, 'catalog', f'Price History — {item.name}')


@app.route('/catalog/template')
@login_required
def catalog_template():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Name','Spec','Unit','Category','Vendors','Cost_Price','Markup_Pct','Last_Price'])
    writer.writerow(['Ball Valve 2 inch','SS316 PN16','Pcs','Valves','SWBM Fittings LLC','100.00','25','125.00'])
    buf.seek(0)
    return send_file(io.BytesIO(buf.read().encode('utf-8-sig')),
                     download_name='NAT_Catalog_Template.csv',
                     as_attachment=True, mimetype='text/csv')


# ── All remaining routes (clients, vendors, contacts, logistics,
#    archive, receivables, vat, reports, audit, data export) ──
# These are identical to v3 with minor tweaks — pasting full versions

# ── Clients ──────────────────────────────────────────────────

CLIENT_TEMPLATE_HEADERS = ['Name','Contact','Phone','Email','Address','TRN','License_No','Notes']
VENDOR_TEMPLATE_HEADERS = ['Name','Contact','Phone','Email','Address','TRN','License_No','Products','Notes']

@app.route('/clients', methods=['GET','POST'])
@login_required
def clients():
    is_admin = session.get('user_role') == 'admin'
    if request.method == 'POST':
        action = request.form.get('action','add')
        if action == 'add':
            c = Client(
                name=request.form.get('name',''), contact=request.form.get('contact',''),
                phone=request.form.get('phone',''), email=request.form.get('email',''),
                address=request.form.get('address',''), trn=request.form.get('trn',''),
                license_no=request.form.get('license_no',''), notes=request.form.get('notes',''),
                added_by=session.get('user_name',''))
            db.session.add(c)
            db.session.flush()
            audit('ADD','client', c.id, c.name)
            db.session.commit()
            flash('Client added.', 'success')
        elif action == 'edit':
            cid = int(request.form.get('client_id',0))
            c   = db.session.get(Client, cid)
            if c:
                fields = ['name','contact','phone','email','address','trn','license_no','notes']
                new_data = {f: request.form.get(f, getattr(c,f) or '') for f in fields}
                changes  = diff_record(c, new_data, fields)
                for f in fields: setattr(c, f, new_data[f])
                if changes: audit('EDIT','client', c.id, c.name, changes)
                db.session.commit()
                flash('Client updated.', 'success')
        elif action == 'delete' and is_admin:
            cid = int(request.form.get('client_id',0))
            c   = db.session.get(Client, cid)
            if c:
                audit('DELETE','client', c.id, c.name)
                db.session.delete(c)
                db.session.commit()
                flash(f'Client "{c.name}" deleted.', 'success')
        elif action == 'clear_all' and is_admin:
            count = Client.query.count()
            audit('CLEAR','client', 0, f'All {count} clients deleted')
            Client.query.delete()
            db.session.commit()
            flash(f'All {count} clients deleted.', 'success')
        elif action == 'import' and HAS_PD:
            f = request.files.get('import_file')
            if f and f.filename:
                try:
                    if f.filename.endswith('.csv'):
                        df = pd.read_csv(io.StringIO(f.read().decode('utf-8-sig')))
                    else:
                        df = pd.read_excel(f)
                    df.columns = [col.strip() for col in df.columns]
                    count = 0
                    def safe(val):
                        if val is None: return ''
                        s = str(val).strip()
                        return '' if s.lower() in ('nan','none','n/a','#n/a') else s
                    for _, row in df.iterrows():
                        if not str(row.get('Name','')).strip() or str(row.get('Name',''))=='nan': continue
                        db.session.add(Client(
                            name=safe(row.get('Name','')), contact=safe(row.get('Contact','')),
                            phone=safe(row.get('Phone','')), email=safe(row.get('Email','')),
                            address=safe(row.get('Address','')), trn=safe(row.get('TRN','')),
                            license_no=safe(row.get('License_No','')), notes=safe(row.get('Notes','')),
                            added_by=session.get('user_name','')))
                        count += 1
                    db.session.commit()
                    flash(f'{count} clients imported.', 'success')
                except Exception as e:
                    flash(f'Import failed: {e}', 'error')
        return redirect(url_for('clients'))

    q_str = request.args.get('q','').lower()
    all_clients = Client.query.order_by(Client.name).all()
    if q_str:
        all_clients = [c for c in all_clients
                       if q_str in (c.name+(c.email or '')+(c.phone or '')+(c.trn or '')+'').lower()]

    def _dc(val):
        if not val: return '-'
        s = re.sub(r'\.0+$','',str(val).strip())
        return '-' if s.lower() in ('nan','none','n/a','null','') else s

    rows = ''
    for c in all_clients:
        pn = re.sub(r'[^0-9]','', str(c.phone or ''))
        wa = f'<a href="https://wa.me/{pn}" target="_blank" class="btn btn-sm btn-success">WA</a>' if pn else ''
        em = f'<a href="mailto:{c.email}" class="btn btn-sm btn-info">✉</a>' if c.email else ''
        rows += f'''<tr>
          <td><strong>{_dc(c.name)}</strong></td>
          <td>{_dc(c.contact)}</td>
          <td style="font-size:12px">{_dc(c.phone)}</td>
          <td style="font-size:11px">{_dc(c.email)}</td>
          <td style="font-family:monospace;font-size:11px">{_dc(c.trn)}</td>
          <td style="white-space:nowrap">
            {wa}{em}
            <a href="/clients/{c.id}" class="btn btn-sm btn-outline">View</a>
            <button onclick="document.getElementById('ec-{c.id}').style.display='block'"
                    class="btn btn-sm btn-warning" style="color:#fff">Edit</button>
            {"" if not is_admin else f'<form method="POST" style="display:inline"><input type="hidden" name="action" value="delete"><input type="hidden" name="client_id" value="{c.id}"><button type="submit" class="btn btn-sm btn-danger" onclick="return confirm(\'Delete?\')">Del</button></form>'}
          </td></tr>
          <tr id="ec-{c.id}" style="display:none;background:#f8f9ff">
            <td colspan="6" style="padding:12px">
              <form method="POST">
                <input type="hidden" name="action" value="edit">
                <input type="hidden" name="client_id" value="{c.id}">
                <div class="grid3">
                  <div class="form-group"><label>Name</label><input name="name" value="{c.name or ''}"></div>
                  <div class="form-group"><label>Contact</label><input name="contact" value="{c.contact or ''}"></div>
                  <div class="form-group"><label>Phone</label><input name="phone" value="{c.phone or ''}"></div>
                  <div class="form-group"><label>Email</label><input name="email" value="{c.email or ''}"></div>
                  <div class="form-group"><label>TRN</label><input name="trn" value="{c.trn or ''}"></div>
                  <div class="form-group"><label>License No</label><input name="license_no" value="{c.license_no or ''}"></div>
                </div>
                <div class="form-group"><label>Address</label><input name="address" value="{c.address or ''}"></div>
                <div class="form-group"><label>Notes</label><textarea name="notes">{c.notes or ''}</textarea></div>
                <button type="submit" class="btn btn-primary btn-sm">Save</button>
                <button type="button" onclick="document.getElementById('ec-{c.id}').style.display='none'"
                        class="btn btn-outline btn-sm" style="margin-left:8px">Cancel</button>
              </form>
            </td>
          </tr>'''

    if not all_clients:
        rows = '<tr><td colspan="6" style="text-align:center;color:#999;padding:20px">No clients found.</td></tr>'

    content = f'''<div class="card">
  <h2>Client Directory ({len(all_clients)} clients)</h2>
  <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center">
    <form method="GET" style="display:flex;gap:6px">
      <input type="text" name="q" value="{request.args.get('q','')}"
             placeholder="Search name, phone, TRN..."
             style="padding:7px 11px;border:1px solid #ddd;border-radius:6px;font-size:12px;width:220px">
      <button type="submit" class="btn btn-primary btn-sm">Search</button>
    </form>
    <button onclick="document.getElementById('add-client').style.display='block'"
            class="btn btn-success btn-sm">+ Add Client</button>
    <button onclick="document.getElementById('import-client').style.display='block'"
            class="btn btn-info btn-sm">⬆ Import</button>
    <a href="/clients/template" class="btn btn-outline btn-sm">⬇ Template</a>
    {f'<form method="POST" style="display:inline"><input type="hidden" name="action" value="clear_all"><button type="submit" class="btn btn-sm btn-danger" onclick="return confirm(\'Delete ALL {len(all_clients)} clients?\')">🗑 Clear All</button></form>' if is_admin and all_clients else ""}
  </div>
  <div id="add-client" style="display:none;background:#f8f9ff;border:1px solid #dce0f0;border-radius:8px;padding:16px;margin-bottom:14px">
    <form method="POST"><input type="hidden" name="action" value="add">
      <div class="grid3">
        <div class="form-group"><label>Company Name *</label><input name="name" required></div>
        <div class="form-group"><label>Contact Person</label><input name="contact"></div>
        <div class="form-group"><label>Phone</label><input name="phone"></div>
        <div class="form-group"><label>Email</label><input type="email" name="email"></div>
        <div class="form-group"><label>TRN / VAT Number</label><input name="trn" placeholder="100xxxxxxxxx00003"></div>
        <div class="form-group"><label>License No.</label><input name="license_no"></div>
      </div>
      <div class="form-group"><label>Address</label><input name="address"></div>
      <div class="form-group"><label>Notes</label><textarea name="notes"></textarea></div>
      <button type="submit" class="btn btn-primary">Save</button>
      <button type="button" onclick="document.getElementById('add-client').style.display='none'" class="btn btn-outline" style="margin-left:8px">Cancel</button>
    </form>
  </div>
  <div id="import-client" style="display:none"><div class="import-box">
    <form method="POST" enctype="multipart/form-data" style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
      <input type="hidden" name="action" value="import">
      <input type="file" name="import_file" accept=".csv,.xlsx,.xls" style="font-size:12px">
      <button type="submit" class="btn btn-primary">Import</button>
      <button type="button" onclick="document.getElementById('import-client').style.display='none'" class="btn btn-outline">Cancel</button>
    </form>
  </div></div>
  <table><thead><tr><th>Company</th><th>Contact</th><th>Phone</th>
    <th>Email</th><th>TRN</th><th>Actions</th></tr></thead>
  <tbody>{rows}</tbody></table>
</div>'''
    return base_page(content, 'clients', 'Clients')


@app.route('/clients/<int:client_id>')
@login_required
def client_view(client_id):
    c    = Client.query.get_or_404(client_id)
    docs = Document.query.filter_by(party_name=c.name).order_by(Document.created_at.desc()).limit(20).all()
    badge = {'Issued':'badge-purple','Paid':'badge-green','VOID':'badge-red','Overdue':'badge-red',
             'Open':'badge-grey','Raised':'badge-orange','Delivered':'badge-blue','Sent':'badge-blue'}
    doc_rows = ''.join(f'''<tr>
      <td><a href="/doc/{d.doc_type.lower()}/{d.id}" style="color:#1a3a6b;font-weight:bold">{d.ref}</a></td>
      <td><span class="badge badge-blue">{d.doc_type}</span></td>
      <td>{"AED {:,.2f}".format(d.total) if d.total else "-"}</td>
      <td><span class="badge {badge.get(d.status,'badge-grey')}">{d.status}</span></td>
      <td>{d.date}</td></tr>''' for d in docs) or \
    '<tr><td colspan="5" style="text-align:center;color:#999">No documents yet.</td></tr>'
    phone_clean = re.sub(r'[^0-9]','', c.phone or '')
    if phone_clean.startswith('0'): phone_clean = '971'+phone_clean[1:]
    content = f'''<div class="card">
  <h2>{c.name}</h2>
  <div class="grid3" style="margin-bottom:16px">
    <div><b>Contact:</b> {c.contact or "-"}</div>
    <div><b>Phone:</b> {c.phone or "-"}</div>
    <div><b>Email:</b> {c.email or "-"}</div>
    <div><b>TRN:</b> <span style="font-family:monospace">{c.trn or "-"}</span></div>
    <div><b>License:</b> {c.license_no or "-"}</div>
    <div><b>Address:</b> {c.address or "-"}</div>
  </div>
  <div style="display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap">
    {f'<a href="https://wa.me/{phone_clean}" target="_blank" class="btn btn-success">💬 WhatsApp</a>' if phone_clean else ""}
    {f'<a href="mailto:{c.email}" class="btn btn-info">✉ Email</a>' if c.email else ""}
    <a href="/doc/inv/new" class="btn btn-primary">+ New Invoice</a>
    <a href="/clients" class="btn btn-outline">← Back</a>
  </div>
  <h2>Documents ({len(docs)} recent)</h2>
  <table><thead><tr><th>Reference</th><th>Type</th><th>Value</th><th>Status</th><th>Date</th></tr></thead>
  <tbody>{doc_rows}</tbody></table>
</div>'''
    return base_page(content, 'clients', c.name)


@app.route('/clients/template')
@login_required
def clients_template():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CLIENT_TEMPLATE_HEADERS)
    writer.writerow(['Example Company LLC','Ahmed Al Rashid','+971501234567',
                     'ahmed@example.com','Sharjah Industrial Area',
                     '1040000000000003','CN-12345','Key account'])
    buf.seek(0)
    return send_file(io.BytesIO(buf.read().encode('utf-8-sig')),
                     download_name='NAT_Client_Import_Template.csv',
                     as_attachment=True, mimetype='text/csv')


# ── Vendors ──────────────────────────────────────────────────

@app.route('/vendors', methods=['GET','POST'])
@login_required
def vendors():
    is_admin = session.get('user_role') == 'admin'
    if request.method == 'POST':
        action = request.form.get('action','add')
        if action == 'add':
            v = Vendor(
                name=request.form.get('name',''), contact=request.form.get('contact',''),
                phone=request.form.get('phone',''), email=request.form.get('email',''),
                address=request.form.get('address',''), trn=request.form.get('trn',''),
                license_no=request.form.get('license_no',''),
                products=request.form.get('products',''), notes=request.form.get('notes',''),
                added_by=session.get('user_name',''))
            db.session.add(v); db.session.flush()
            audit('ADD','vendor', v.id, v.name)
            db.session.commit()
            flash('Vendor added.', 'success')
        elif action == 'edit':
            vid = int(request.form.get('vendor_id',0))
            v   = db.session.get(Vendor, vid)
            if v:
                fields = ['name','contact','phone','email','address','trn','license_no','products','notes']
                new_data = {f: request.form.get(f, getattr(v,f) or '') for f in fields}
                changes  = diff_record(v, new_data, fields)
                for f in fields: setattr(v, f, new_data[f])
                if changes: audit('EDIT','vendor', v.id, v.name, changes)
                db.session.commit()
                flash('Vendor updated.', 'success')
        elif action == 'delete' and is_admin:
            vid = int(request.form.get('vendor_id',0))
            v   = db.session.get(Vendor, vid)
            if v:
                audit('DELETE','vendor', v.id, v.name)
                db.session.delete(v); db.session.commit()
                flash(f'Vendor "{v.name}" deleted.', 'success')
        elif action == 'clear_all' and is_admin:
            count = Vendor.query.count()
            audit('CLEAR','vendor', 0, f'All {count} vendors deleted')
            Vendor.query.delete(); db.session.commit()
            flash(f'All {count} vendors deleted.', 'success')
        elif action == 'import' and HAS_PD:
            f = request.files.get('import_file')
            if f and f.filename:
                try:
                    if f.filename.endswith('.csv'):
                        df = pd.read_csv(io.StringIO(f.read().decode('utf-8-sig')))
                    else:
                        df = pd.read_excel(f)
                    df.columns = [col.strip() for col in df.columns]
                    count = 0
                    def safe(val):
                        if val is None: return ''
                        s = str(val).strip()
                        return '' if s.lower() in ('nan','none','n/a','#n/a') else s
                    for _, row in df.iterrows():
                        if not str(row.get('Name','')).strip() or str(row.get('Name',''))=='nan': continue
                        db.session.add(Vendor(
                            name=safe(row.get('Name','')), contact=safe(row.get('Contact','')),
                            phone=safe(row.get('Phone','')), email=safe(row.get('Email','')),
                            address=safe(row.get('Address','')), trn=safe(row.get('TRN','')),
                            license_no=safe(row.get('License_No','')),
                            products=safe(row.get('Products','')), notes=safe(row.get('Notes','')),
                            added_by=session.get('user_name','')))
                        count += 1
                    db.session.commit()
                    flash(f'{count} vendors imported.', 'success')
                except Exception as e:
                    flash(f'Import failed: {e}', 'error')
        return redirect(url_for('vendors'))

    q_str    = request.args.get('q','').lower()
    per_page = int(request.args.get('per_page', 100))
    if per_page not in (50,100,200,500): per_page = 100
    page     = max(1, int(request.args.get('page', 1)))

    q_obj = Vendor.query.order_by(Vendor.name)
    if q_str:
        q_obj = q_obj.filter(db.or_(
            Vendor.name.ilike(f'%{q_str}%'),
            Vendor.email.ilike(f'%{q_str}%'),
            Vendor.phone.ilike(f'%{q_str}%'),
            Vendor.products.ilike(f'%{q_str}%'),
        ))
    total_vendors = q_obj.count()
    total_pages   = max(1, -(-total_vendors // per_page))
    page          = min(page, total_pages)
    all_vendors   = q_obj.offset((page-1)*per_page).limit(per_page).all()

    def _d(val):
        if not val: return '-'
        s = re.sub(r'\.0+$','',str(val).strip())
        return '-' if s.lower() in ('nan','none','n/a','null','') else s

    rows = ''
    for v in all_vendors:
        pn = re.sub(r'[^0-9]','', str(v.phone or ''))
        wa = f'<a href="https://wa.me/{pn}" target="_blank" class="btn btn-sm btn-success">WA</a>' if pn else ''
        em = f'<a href="mailto:{v.email}" class="btn btn-sm btn-info">✉</a>' if v.email else ''
        rows += f'''<tr>
          <td><strong>{_d(v.name)}</strong></td>
          <td>{_d(v.contact)}</td>
          <td style="font-size:12px">{_d(v.phone)}</td>
          <td style="font-size:11px">{_d(v.email)}</td>
          <td style="font-size:11px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{_d(v.products)}</td>
          <td style="white-space:nowrap">
            {wa}{em}
            <a href="/vendors/{v.id}" class="btn btn-sm btn-outline">View</a>
            <button onclick="document.getElementById('ev-{v.id}').style.display='block'"
                    class="btn btn-sm btn-warning" style="color:#fff">Edit</button>
            {"" if not is_admin else f'<form method="POST" style="display:inline"><input type="hidden" name="action" value="delete"><input type="hidden" name="vendor_id" value="{v.id}"><button type="submit" class="btn btn-sm btn-danger" onclick="return confirm(\'Delete?\')">Del</button></form>'}
          </td></tr>
          <tr id="ev-{v.id}" style="display:none;background:#f8f9ff">
            <td colspan="6" style="padding:12px">
              <form method="POST">
                <input type="hidden" name="action" value="edit">
                <input type="hidden" name="vendor_id" value="{v.id}">
                <div class="grid3">
                  <div class="form-group"><label>Name</label><input name="name" value="{v.name or ''}"></div>
                  <div class="form-group"><label>Contact</label><input name="contact" value="{v.contact or ''}"></div>
                  <div class="form-group"><label>Phone</label><input name="phone" value="{v.phone or ''}"></div>
                  <div class="form-group"><label>Email</label><input name="email" value="{v.email or ''}"></div>
                  <div class="form-group"><label>TRN</label><input name="trn" value="{v.trn or ''}"></div>
                  <div class="form-group"><label>License No</label><input name="license_no" value="{v.license_no or ''}"></div>
                </div>
                <div class="form-group"><label>Products</label><textarea name="products">{v.products or ''}</textarea></div>
                <div class="form-group"><label>Notes</label><textarea name="notes">{v.notes or ''}</textarea></div>
                <button type="submit" class="btn btn-primary btn-sm">Save</button>
                <button type="button" onclick="document.getElementById('ev-{v.id}').style.display='none'" class="btn btn-outline btn-sm" style="margin-left:8px">Cancel</button>
              </form>
            </td>
          </tr>'''

    if not all_vendors:
        rows = '<tr><td colspan="6" style="text-align:center;color:#999;padding:20px">No vendors found.</td></tr>'

    def _pg_url_ven(p, pp=per_page):
        return f"/vendors?q={request.args.get('q','')}&page={p}&per_page={pp}"

    pg_controls_ven = f'''
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
    <span style="font-size:12px;color:#666">Show:
      {''.join(f'<a href="{_pg_url_ven(1,n)}" class="btn btn-sm {"btn-primary" if per_page==n else "btn-outline"}">{n}</a>' for n in [50,100,200,500])}
    </span>
    <span style="font-size:12px;color:#666;margin-left:10px">
      Page {page} of {total_pages} &nbsp;
      {'<a href="'+_pg_url_ven(1)+'" class="btn btn-sm btn-outline">«</a>' if page>1 else ''}
      {'<a href="'+_pg_url_ven(page-1)+'" class="btn btn-sm btn-outline">‹</a>' if page>1 else ''}
      {''.join(f'<a href="{_pg_url_ven(p)}" class="btn btn-sm {"btn-primary" if p==page else "btn-outline"}">{p}</a>' for p in range(max(1,page-2), min(total_pages+1,page+3)))}
      {'<a href="'+_pg_url_ven(page+1)+'" class="btn btn-sm btn-outline">›</a>' if page<total_pages else ''}
      {'<a href="'+_pg_url_ven(total_pages)+'" class="btn btn-sm btn-outline">»</a>' if page<total_pages else ''}
    </span>
  </div>'''

    content = f'''<div class="card">
  <h2>Vendor Directory ({total_vendors} vendors)</h2>
  <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center">
    <form method="GET" style="display:flex;gap:6px">
      <input type="text" name="q" value="{request.args.get('q','')}"
             placeholder="Search name, phone, products..."
             style="padding:7px 11px;border:1px solid #ddd;border-radius:6px;font-size:12px;width:220px">
      <input type="hidden" name="per_page" value="{per_page}">
      <button type="submit" class="btn btn-primary btn-sm">Search</button>
    </form>
    <button onclick="document.getElementById('add-vendor').style.display='block'"
            class="btn btn-success btn-sm">+ Add Vendor</button>
    <button onclick="document.getElementById('import-vendor').style.display='block'"
            class="btn btn-info btn-sm">⬆ Import</button>
    <a href="/vendors/template" class="btn btn-outline btn-sm">⬇ Template</a>
    {f'<form method="POST" style="display:inline"><input type="hidden" name="action" value="clear_all"><button type="submit" class="btn btn-sm btn-danger" onclick="return confirm(\'Delete ALL {total_vendors} vendors?\')">🗑 Clear All</button></form>' if is_admin and all_vendors else ""}
  </div>
  {pg_controls_ven}
  <div id="add-vendor" style="display:none;background:#f8f9ff;border:1px solid #dce0f0;border-radius:8px;padding:16px;margin-bottom:14px">
    <form method="POST"><input type="hidden" name="action" value="add">
      <div class="grid3">
        <div class="form-group"><label>Vendor Name *</label><input name="name" required></div>
        <div class="form-group"><label>Contact Person</label><input name="contact"></div>
        <div class="form-group"><label>Phone</label><input name="phone"></div>
        <div class="form-group"><label>Email</label><input type="email" name="email"></div>
        <div class="form-group"><label>TRN</label><input name="trn"></div>
        <div class="form-group"><label>License No.</label><input name="license_no"></div>
      </div>
      <div class="form-group"><label>Address</label><input name="address"></div>
      <div class="form-group"><label>Products / Materials</label>
        <textarea name="products" placeholder="Valves, flanges, bearings..."></textarea></div>
      <div class="form-group"><label>Notes</label><textarea name="notes"></textarea></div>
      <button type="submit" class="btn btn-primary">Save</button>
      <button type="button" onclick="document.getElementById('add-vendor').style.display='none'" class="btn btn-outline" style="margin-left:8px">Cancel</button>
    </form>
  </div>
  <div id="import-vendor" style="display:none"><div class="import-box">
    <form method="POST" enctype="multipart/form-data" style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
      <input type="hidden" name="action" value="import">
      <input type="file" name="import_file" accept=".csv,.xlsx,.xls" style="font-size:12px">
      <button type="submit" class="btn btn-primary">Import</button>
      <button type="button" onclick="document.getElementById('import-vendor').style.display='none'" class="btn btn-outline">Cancel</button>
    </form>
  </div></div>
  <table><thead><tr><th>Vendor</th><th>Contact</th><th>Phone</th>
    <th>Email</th><th>Products</th><th>Actions</th></tr></thead>
  <tbody>{rows}</tbody></table>
  {pg_controls_ven}
</div>'''
    return base_page(content, 'vendors', 'Vendors')


@app.route('/vendors/<int:vendor_id>')
@login_required
def vendor_view(vendor_id):
    v    = Vendor.query.get_or_404(vendor_id)
    docs = Document.query.filter_by(party_name=v.name).order_by(Document.created_at.desc()).limit(20).all()
    badge = {'Raised':'badge-orange','Delivered':'badge-blue','Paid':'badge-green','VOID':'badge-red'}
    doc_rows = ''.join(f'''<tr>
      <td><a href="/doc/{d.doc_type.lower()}/{d.id}" style="color:#1a3a6b;font-weight:bold">{d.ref}</a></td>
      <td><span class="badge badge-blue">{d.doc_type}</span></td>
      <td>{"AED {:,.2f}".format(d.total) if d.total else "-"}</td>
      <td><span class="badge {badge.get(d.status,'badge-grey')}">{d.status}</span></td>
      <td>{d.date}</td></tr>''' for d in docs) or \
    '<tr><td colspan="5" style="text-align:center;color:#999">No documents yet.</td></tr>'
    phone_clean = re.sub(r'[^0-9]','', v.phone or '')
    if phone_clean.startswith('0'): phone_clean = '971'+phone_clean[1:]
    content = f'''<div class="card">
  <h2>{v.name}</h2>
  <div class="grid3" style="margin-bottom:16px">
    <div><b>Contact:</b> {v.contact or "-"}</div>
    <div><b>Phone:</b> {v.phone or "-"}</div>
    <div><b>Email:</b> {v.email or "-"}</div>
    <div><b>TRN:</b> <span style="font-family:monospace">{v.trn or "-"}</span></div>
    <div><b>License:</b> {v.license_no or "-"}</div>
  </div>
  {"<p><b>Products:</b> "+v.products+"</p>" if v.products else ""}
  <div style="display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap">
    {f'<a href="https://wa.me/{phone_clean}" target="_blank" class="btn btn-success">💬 WhatsApp</a>' if phone_clean else ""}
    {f'<a href="mailto:{v.email}" class="btn btn-info">✉ Email</a>' if v.email else ""}
    <a href="/doc/lpo/new" class="btn btn-primary">+ New LPO</a>
    <a href="/vendors" class="btn btn-outline">← Back</a>
  </div>
  <h2>Documents ({len(docs)} recent)</h2>
  <table><thead><tr><th>Reference</th><th>Type</th><th>Value</th><th>Status</th><th>Date</th></tr></thead>
  <tbody>{doc_rows}</tbody></table>
</div>'''
    return base_page(content, 'vendors', v.name)


@app.route('/vendors/template')
@login_required
def vendors_template():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(VENDOR_TEMPLATE_HEADERS)
    writer.writerow(['SWBM Fittings LLC','Sales','+97150XXXXXXX','sales@swbm.com',
                     'Sharjah Industrial Area','','','Valves, flanges','Core supplier'])
    buf.seek(0)
    return send_file(io.BytesIO(buf.read().encode('utf-8-sig')),
                     download_name='NAT_Vendor_Import_Template.csv',
                     as_attachment=True, mimetype='text/csv')


# ── Contacts, Logistics, Archive, Receivables ─────────────────
# Ported directly from v3 with no functional changes

@app.route('/contacts', methods=['GET','POST'])
@login_required
def contacts():
    if request.method == 'POST':
        action = request.form.get('action','import')
        if action == 'import' and HAS_PD:
            f = request.files.get('import_file')
            if f and f.filename:
                try:
                    if f.filename.endswith('.csv'):
                        df = pd.read_csv(io.StringIO(f.read().decode('utf-8-sig')))
                    else:
                        df = pd.read_excel(f)
                    df.columns = [c.strip() for c in df.columns]
                    count = 0
                    for _, row in df.iterrows():
                        def _s(k, k2, default=''):
                            v = str(row.get(k, row.get(k2, default))).strip()
                            return '' if v == 'nan' else v
                        name = _s('Name','name')
                        if not name: continue
                        # Apollo export maps: First Name + Last Name → combine if no Name col
                        if not name and 'First Name' in row:
                            name = (str(row.get('First Name','')).strip() + ' ' + str(row.get('Last Name','')).strip()).strip()
                        if not name: continue
                        db.session.add(Contact(
                            name            = name,
                            phone           = _s('Phone','phone'),
                            email           = _s('Email','email'),
                            company         = _s('Company','company'),
                            source          = _s('Source','source') or 'Import',
                            count           = int(row.get('Count', row.get('count', 0)) or 0),
                            title           = _s('Title','title'),
                            linkedin        = _s('LinkedIn','linkedin'),
                            website         = _s('Website','website'),
                            email_quality   = _s('Quality','quality'),
                            icp_notes       = _s('ICP','icp_notes'),
                            outreach_status = _s('Status','status') or 'New',
                        ))
                        count += 1
                    db.session.commit()
                    flash(f'{count} contacts imported.', 'success')
                except Exception as e:
                    flash(f'Import failed: {e}', 'error')
        elif action == 'clear_all' and session.get('user_role') == 'admin':
            Contact.query.delete(); db.session.commit()
            flash('All contacts cleared.', 'success')
        elif action == 'add':
            db.session.add(Contact(
                name=request.form.get('name',''), phone=request.form.get('phone',''),
                email=request.form.get('email',''), company=request.form.get('company',''),
                source=request.form.get('source','Manual'),))
            db.session.commit()
            flash('Contact added.', 'success')
        return redirect(url_for('contacts'))

    q_str    = request.args.get('q','').lower()
    per_page = int(request.args.get('per_page', 100))
    if per_page not in (50,100,200,500): per_page = 100
    page     = max(1, int(request.args.get('page', 1)))

    q_obj = Contact.query.order_by(Contact.count.desc())
    if q_str:
        q_obj = q_obj.filter(db.or_(
            Contact.name.ilike(f'%{q_str}%'),
            Contact.email.ilike(f'%{q_str}%'),
            Contact.phone.ilike(f'%{q_str}%'),
            Contact.company.ilike(f'%{q_str}%'),
        ))
    total_count   = q_obj.count()
    total_pages   = max(1, -(-total_count // per_page))
    page          = min(page, total_pages)
    contacts_list = q_obj.offset((page-1)*per_page).limit(per_page).all()
    is_admin = session.get('user_role') == 'admin'
    _STATUS_CLR = {'New':'#6c757d','Emailed':'#0d6efd','Replied':'#fd7e14',
                   'Meeting Booked':'#198754','Converted':'#20c997','Not Interested':'#dc3545'}
    rows = ''.join(f'''<tr>
      <td><a href="/contacts/{c.id}" style="font-weight:600;color:#1a3a5c">{c.name}</a></td>
      <td style="font-size:11px">{c.company or "-"}</td>
      <td style="font-size:11px">{c.title or "-"}</td>
      <td>{c.phone or "-"}</td>
      <td style="font-size:11px">{c.email or "-"}</td>
      <td><span style="background:{_STATUS_CLR.get(c.outreach_status or 'New','#6c757d')};color:#fff;padding:2px 8px;border-radius:10px;font-size:11px">{c.outreach_status or 'New'}</span></td>
      <td><span class="badge badge-grey">{c.source}</span></td>
      <td style="white-space:nowrap">
        {f'<a href="https://wa.me/{re.sub(chr(91)+chr(94)+"0-9"+chr(93),"",c.phone or "")}" target="_blank" class="btn btn-sm btn-success">WA</a>' if c.phone else ""}
        {f'<a href="mailto:{c.email}" class="btn btn-sm btn-info">Email</a>' if c.email else ""}
        {f'<a href="{c.linkedin}" target="_blank" class="btn btn-sm btn-outline" style="font-size:10px">LI</a>' if c.linkedin else ""}
      </td></tr>''' for c in contacts_list) or \
      '<tr><td colspan="8" style="text-align:center;color:#999;padding:20px">No contacts loaded yet.</td></tr>'

    def _pg_url_con(p, pp=per_page):
        return f"/contacts?q={request.args.get('q','')}&page={p}&per_page={pp}"

    pg_controls_con = f'''
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
    <span style="font-size:12px;color:#666">Show:
      {''.join(f'<a href="{_pg_url_con(1,n)}" class="btn btn-sm {"btn-primary" if per_page==n else "btn-outline"}">{n}</a>' for n in [50,100,200,500])}
    </span>
    <span style="font-size:12px;color:#666;margin-left:10px">
      Page {page} of {total_pages} &nbsp;
      {'<a href="'+_pg_url_con(1)+'" class="btn btn-sm btn-outline">«</a>' if page>1 else ''}
      {'<a href="'+_pg_url_con(page-1)+'" class="btn btn-sm btn-outline">‹</a>' if page>1 else ''}
      {''.join(f'<a href="{_pg_url_con(p)}" class="btn btn-sm {"btn-primary" if p==page else "btn-outline"}">{p}</a>' for p in range(max(1,page-2), min(total_pages+1,page+3)))}
      {'<a href="'+_pg_url_con(page+1)+'" class="btn btn-sm btn-outline">›</a>' if page<total_pages else ''}
      {'<a href="'+_pg_url_con(total_pages)+'" class="btn btn-sm btn-outline">»</a>' if page<total_pages else ''}
    </span>
  </div>'''

    content = f'''<div class="card">
  <h2>Contacts ({total_count} total)</h2>
  <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center">
    <form method="GET" style="display:flex;gap:6px">
      <input type="text" name="q" value="{request.args.get('q','')}"
             placeholder="Search name, email, phone..."
             style="max-width:300px;padding:7px 11px;border:1px solid #ddd;border-radius:6px;font-size:12px">
      <input type="hidden" name="per_page" value="{per_page}">
      <button type="submit" class="btn btn-primary btn-sm">Search</button>
    </form>
    <button onclick="document.getElementById('add-contact').style.display='block'"
            class="btn btn-success btn-sm">+ Add</button>
    <button onclick="document.getElementById('import-contacts').style.display='block'"
            class="btn btn-info btn-sm">⬆ Import</button>
    {f'<form method="POST" style="display:inline"><input type="hidden" name="action" value="clear_all"><button type="submit" class="btn btn-sm btn-danger" onclick="return confirm(\'Delete ALL {total_count} contacts?\')">🗑 Clear All</button></form>' if is_admin and total_count > 0 else ""}
  </div>
  {pg_controls_con}
  <div id="add-contact" style="display:none;background:#f8f9ff;border:1px solid #dce0f0;border-radius:8px;padding:16px;margin-bottom:14px">
    <form method="POST"><input type="hidden" name="action" value="add">
      <div class="grid3">
        <div class="form-group"><label>Name *</label><input name="name" required></div>
        <div class="form-group"><label>Company</label><input name="company"></div>
        <div class="form-group"><label>Phone</label><input name="phone"></div>
        <div class="form-group"><label>Email</label><input type="email" name="email"></div>
        <div class="form-group"><label>Source</label>
          <select name="source"><option>Manual</option><option>Brevo</option>
            <option>WhatsApp</option><option>PST Export</option>
            <option>Google Maps</option><option>Referral</option></select></div>
      </div>
      <button type="submit" class="btn btn-primary">Save</button>
      <button type="button" onclick="document.getElementById('add-contact').style.display='none'" class="btn btn-outline" style="margin-left:8px">Cancel</button>
    </form>
  </div>
  <div id="import-contacts" style="display:none"><div class="import-box">
    <form method="POST" enctype="multipart/form-data" style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
      <input type="hidden" name="action" value="import">
      <input type="file" name="import_file" accept=".csv,.xlsx,.xls" style="font-size:12px">
      <button type="submit" class="btn btn-primary">Import</button>
      <button type="button" onclick="document.getElementById('import-contacts').style.display='none'" class="btn btn-outline">Cancel</button>
    </form>
  </div></div>
  <table><thead><tr><th>Name</th><th>Company</th><th>Title</th><th>Phone</th><th>Email</th>
    <th>Status</th><th>Source</th><th>Actions</th></tr></thead>
  <tbody>{rows}</tbody></table>
  {pg_controls_con}
</div>'''
    return base_page(content, 'contacts', 'Contacts')


@app.route('/contacts/<int:cid>', methods=['GET','POST'])
@login_required
def contact_detail(cid):
    c = Contact.query.get_or_404(cid)
    if request.method == 'POST':
        action = request.form.get('action','')
        if action == 'update_status':
            c.outreach_status = request.form.get('outreach_status', c.outreach_status)
            c.meeting_date    = request.form.get('meeting_date', c.meeting_date)
            c.meeting_notes   = request.form.get('meeting_notes', c.meeting_notes)
            c.icp_notes       = request.form.get('icp_notes', c.icp_notes)
            db.session.commit()
            flash('Contact updated.', 'success')
        elif action == 'add_log':
            db.session.add(OutreachLog(
                contact_id = cid,
                date       = request.form.get('date', datetime.now().strftime('%Y-%m-%d')),
                channel    = request.form.get('channel','Email'),
                subject    = request.form.get('subject',''),
                notes      = request.form.get('notes',''),
                added_by   = session.get('username',''),
            ))
            db.session.commit()
            flash('Log added.', 'success')
        elif action == 'delete_log':
            log = OutreachLog.query.get(int(request.form.get('log_id',0)))
            if log and log.contact_id == cid:
                db.session.delete(log)
                db.session.commit()
        return redirect(url_for('contact_detail', cid=cid))

    STATUS_COLORS = {
        'New':'#6c757d','Emailed':'#0d6efd','Replied':'#fd7e14',
        'Meeting Booked':'#198754','Converted':'#20c997','Not Interested':'#dc3545'
    }
    status_opts = ''.join(
        f'<option value="{s}" {"selected" if c.outreach_status==s else ""}>{s}</option>'
        for s in STATUS_COLORS
    )
    logs_html = ''.join(f"""
        <tr>
          <td style="font-size:11px">{l.date or "-"}</td>
          <td><span class="badge badge-grey">{l.channel}</span></td>
          <td style="font-size:12px">{l.subject or "-"}</td>
          <td style="font-size:12px">{l.notes or "-"}</td>
          <td style="font-size:11px">{l.added_by or "-"}</td>
          <td><form method="POST" style="display:inline">
            <input type="hidden" name="action" value="delete_log">
            <input type="hidden" name="log_id" value="{l.id}">
            <button class="btn btn-sm btn-danger" onclick="return confirm('Delete?')">✕</button>
          </form></td>
        </tr>""" for l in sorted(c.logs, key=lambda x: x.added_at, reverse=True)
    ) or '<tr><td colspan="6" style="text-align:center;color:#999;padding:16px">No outreach logged yet.</td></tr>'

    sc = STATUS_COLORS.get(c.outreach_status or 'New','#6c757d')
    today = datetime.now().strftime('%Y-%m-%d')
    li_btn = f'<a href="{c.linkedin}" target="_blank" class="btn btn-sm btn-outline">LinkedIn ↗</a>' if c.linkedin else ""
    web_btn = f'<a href="{c.website}" target="_blank" class="btn btn-sm btn-outline">Website ↗</a>' if c.website else ""

    content = f"""<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;margin-bottom:4px">
    <div>
      <h2 style="margin:0">{c.name}</h2>
      <p style="color:#666;margin:4px 0 0">{c.company or ""}{(" &bull; " + c.title) if c.title else ""}</p>
    </div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <span style="background:{sc};color:#fff;padding:5px 14px;border-radius:12px;font-size:12px;font-weight:600">{c.outreach_status or "New"}</span>
      {li_btn}{web_btn}
      <a href="/contacts" class="btn btn-outline btn-sm">← Back</a>
    </div>
  </div>
  <hr style="margin:14px 0">

  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px;font-size:13px">
    <div><span style="color:#888;font-size:11px;text-transform:uppercase">Email</span><br>
      {"<a href='mailto:" + c.email + "'>" + c.email + "</a>" if c.email else "-"}</div>
    <div><span style="color:#888;font-size:11px;text-transform:uppercase">Phone</span><br>{c.phone or "-"}</div>
    <div><span style="color:#888;font-size:11px;text-transform:uppercase">Email Quality</span><br>
      <span style="color:{"#198754" if c.email_quality=="good" else "#dc3545" if c.email_quality=="risky" else "#666"}">{c.email_quality or "-"}</span></div>
    <div><span style="color:#888;font-size:11px;text-transform:uppercase">Source</span><br>{c.source or "-"}</div>
    <div><span style="color:#888;font-size:11px;text-transform:uppercase">Meeting Date</span><br>{c.meeting_date or "Not booked"}</div>
    <div><span style="color:#888;font-size:11px;text-transform:uppercase">Added</span><br>{c.added_at.strftime("%d/%m/%Y") if c.added_at else "-"}</div>
  </div>

  <form method="POST">
    <input type="hidden" name="action" value="update_status">
    <div class="grid3">
      <div class="form-group"><label>Outreach Status</label>
        <select name="outreach_status">{status_opts}</select></div>
      <div class="form-group"><label>Meeting Date</label>
        <input type="date" name="meeting_date" value="{c.meeting_date or ""}"></div>
    </div>
    <div class="form-group"><label>ICP / Research Notes</label>
      <textarea name="icp_notes" rows="3" style="width:100%;box-sizing:border-box">{c.icp_notes or ""}</textarea></div>
    <div class="form-group"><label>Meeting Notes</label>
      <textarea name="meeting_notes" rows="2" style="width:100%;box-sizing:border-box">{c.meeting_notes or ""}</textarea></div>
    <button type="submit" class="btn btn-primary">Save Changes</button>
  </form>

  <hr style="margin:24px 0">
  <h3 style="margin-bottom:12px">Outreach Log</h3>
  <div style="background:#f8f9ff;border:1px solid #dce0f0;border-radius:8px;padding:14px;margin-bottom:16px">
    <form method="POST">
      <input type="hidden" name="action" value="add_log">
      <div class="grid3">
        <div class="form-group"><label>Date</label>
          <input type="date" name="date" value="{today}"></div>
        <div class="form-group"><label>Channel</label>
          <select name="channel">
            <option>Email</option><option>LinkedIn</option>
            <option>WhatsApp</option><option>Call</option>
          </select></div>
        <div class="form-group"><label>Subject / Topic</label>
          <input name="subject" placeholder="e.g. Cold email #1, Follow-up"></div>
      </div>
      <div class="form-group"><label>Notes</label>
        <textarea name="notes" rows="2" style="width:100%;box-sizing:border-box"
                  placeholder="What happened? Response received? Next step?"></textarea></div>
      <button type="submit" class="btn btn-success">+ Log Outreach</button>
    </form>
  </div>
  <table><thead><tr>
    <th>Date</th><th>Channel</th><th>Subject</th><th>Notes</th><th>By</th><th></th>
  </tr></thead>
  <tbody>{logs_html}</tbody></table>
</div>"""
    return base_page(content, 'contacts', f'Contact — {c.name}')


@app.route('/logistics', methods=['GET','POST'])
@login_required
def logistics():
    if request.method == 'POST':
        date_raw = request.form.get('date', datetime.now().strftime('%Y-%m-%d'))
        try: date_fmt = datetime.strptime(date_raw,'%Y-%m-%d').strftime('%d/%m/%Y')
        except: date_fmt = date_raw
        db.session.add(Logistics(
            date=date_fmt, courier=request.form.get('courier',''),
            tracking_no=request.form.get('tracking_no',''),
            linked_ref=request.form.get('linked_ref',''),
            origin=request.form.get('origin',''), destination=request.form.get('destination',''),
            charge_aed=float(request.form.get('charge_aed',0) or 0),
            status=request.form.get('status','Booked'),
            weight_kg=request.form.get('weight_kg',''), dimensions=request.form.get('dimensions',''),
            vehicle_plate=request.form.get('vehicle_plate',''), driver_name=request.form.get('driver_name',''),
            notes=request.form.get('notes',''), added_by=session.get('user_name','')))
        db.session.commit()
        flash('Logistics entry added.', 'success')
        return redirect(url_for('logistics'))

    entries = Logistics.query.order_by(Logistics.added_at.desc()).all()
    badge_map = {'Booked':'badge-orange','In Transit':'badge-blue','Delivered':'badge-green'}
    rows = ''.join(f'''<tr>
      <td>{e.date}</td><td><strong>{e.courier}</strong></td>
      <td style="font-family:monospace;font-size:11px">{e.tracking_no or "-"}</td>
      <td>{e.linked_ref or "-"}</td>
      <td style="font-size:11px">{e.origin or "-"} → {e.destination or "-"}</td>
      <td>{"AED {:,.2f}".format(e.charge_aed) if e.charge_aed else "-"}</td>
      <td><span class="badge {badge_map.get(e.status,'badge-grey')}">{e.status}</span></td>
      <td>{e.added_by}</td></tr>''' for e in entries) or \
      '<tr><td colspan="8" style="text-align:center;color:#999;padding:20px">No logistics entries yet.</td></tr>'

    content = f'''<div class="card">
  <h2>Logistics & Freight</h2>
  <button onclick="document.getElementById('add-log').style.display='block'"
          class="btn btn-success btn-sm" style="margin-bottom:14px">+ Add Entry</button>
  <div id="add-log" style="display:none;background:#f8f9ff;border:1px solid #dce0f0;border-radius:8px;padding:16px;margin-bottom:14px">
    <form method="POST">
      <div class="grid3">
        <div class="form-group"><label>Date</label><input type="date" name="date" value="{datetime.now().strftime('%Y-%m-%d')}"></div>
        <div class="form-group"><label>Courier *</label><input name="courier" required placeholder="Aramex, DHL..."></div>
        <div class="form-group"><label>Tracking No.</label><input name="tracking_no"></div>
        <div class="form-group"><label>Linked Ref (LPO/DO)</label><input name="linked_ref"></div>
        <div class="form-group"><label>Origin</label><input name="origin"></div>
        <div class="form-group"><label>Destination</label><input name="destination"></div>
        <div class="form-group"><label>Charge (AED)</label><input type="number" name="charge_aed" value="0" step="0.01"></div>
        <div class="form-group"><label>Weight (kg)</label><input name="weight_kg"></div>
        <div class="form-group"><label>Status</label>
          <select name="status"><option>Booked</option><option>In Transit</option><option>Delivered</option></select></div>
      </div>
      <button type="submit" class="btn btn-primary">Save</button>
      <button type="button" onclick="document.getElementById('add-log').style.display='none'" class="btn btn-outline" style="margin-left:8px">Cancel</button>
    </form>
  </div>
  <table><thead><tr><th>Date</th><th>Courier</th><th>Tracking</th><th>Linked Ref</th>
    <th>Route</th><th>Charge</th><th>Status</th><th>By</th></tr></thead>
  <tbody>{rows}</tbody></table>
</div>'''
    return base_page(content, 'logistics', 'Logistics')


@app.route('/archive', methods=['GET','POST'])
@login_required
def archive():
    if request.method == 'POST':
        date_raw = request.form.get('date','')
        try: date_fmt = datetime.strptime(date_raw,'%Y-%m-%d').strftime('%d/%m/%Y')
        except: date_fmt = date_raw
        db.session.add(CloudArchive(
            doc_type=request.form.get('doc_type',''), doc_number=request.form.get('doc_number',''),
            date=date_fmt, party_name=request.form.get('party_name',''),
            amount_aed=float(request.form.get('amount_aed',0) or 0),
            cloud_url=request.form.get('cloud_url',''), description=request.form.get('description',''),
            upload_date=datetime.now().strftime('%d/%m/%Y'), added_by=session.get('user_name','')))
        db.session.commit()
        flash('Document archived.', 'success')
        return redirect(url_for('archive'))

    docs = CloudArchive.query.order_by(CloudArchive.added_at.desc()).all()
    rows = ''.join(f'''<tr>
      <td><span class="badge badge-blue">{d.doc_type}</span></td>
      <td style="font-family:monospace">{d.doc_number}</td>
      <td>{d.date}</td><td>{d.party_name}</td>
      <td>{"AED {:,.2f}".format(d.amount_aed) if d.amount_aed else "-"}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        {f'<a href="{d.cloud_url}" target="_blank" style="color:#1a3a6b">{d.description or d.cloud_url[:40]}</a>'
         if d.cloud_url else d.description or "-"}</td>
      <td style="font-size:11px">{d.added_by}</td></tr>''' for d in docs) or \
      '<tr><td colspan="7" style="text-align:center;color:#999;padding:20px">No archived documents yet.</td></tr>'

    content = f'''<div class="card">
  <h2>Cloud Document Archive ({len(docs)} documents)</h2>
  <button onclick="document.getElementById('add-arc').style.display='block'"
          class="btn btn-success btn-sm" style="margin-bottom:14px">+ Add Document</button>
  <div id="add-arc" style="display:none;background:#f8f9ff;border:1px solid #dce0f0;border-radius:8px;padding:16px;margin-bottom:14px">
    <form method="POST">
      <div class="grid3">
        <div class="form-group"><label>Document Type</label>
          <select name="doc_type"><option>Invoice</option><option>LPO</option>
            <option>Delivery Note</option><option>Quotation</option><option>Other</option></select></div>
        <div class="form-group"><label>Document Number</label><input name="doc_number"></div>
        <div class="form-group"><label>Date</label><input type="date" name="date"></div>
        <div class="form-group"><label>Party Name</label><input name="party_name"></div>
        <div class="form-group"><label>Amount (AED)</label><input type="number" name="amount_aed" value="0" step="0.01"></div>
        <div class="form-group"><label>Cloud URL</label><input name="cloud_url" placeholder="https://drive.google.com/..."></div>
      </div>
      <div class="form-group"><label>Description</label><textarea name="description"></textarea></div>
      <button type="submit" class="btn btn-primary">Save</button>
      <button type="button" onclick="document.getElementById('add-arc').style.display='none'" class="btn btn-outline" style="margin-left:8px">Cancel</button>
    </form>
  </div>
  <table><thead><tr><th>Type</th><th>Doc Number</th><th>Date</th>
    <th>Party</th><th>Amount</th><th>Description / Link</th><th>By</th></tr></thead>
  <tbody>{rows}</tbody></table>
</div>'''
    return base_page(content, 'archive', 'Archive')


@app.route('/receivables')
@admin_required
def receivables():
    today    = datetime.utcnow().date()
    invoices = Document.query.filter(
        Document.doc_type=='INV',
        Document.status.in_(['Issued','Overdue'])
    ).order_by(Document.created_at.asc()).all()

    buckets = {'current':[],'1_30':[],'31_60':[],'61_90':[],'over_90':[]}
    for inv in invoices:
        due = None
        if inv.due_date:
            for fmt in ('%Y-%m-%d','%d/%m/%Y'):
                try: due = datetime.strptime(inv.due_date, fmt).date(); break
                except: pass
        if not due:
            due = inv.created_at.date() + timedelta(days=30)
        days = (today - due).days
        if days <= 0: buckets['current'].append((inv, days))
        elif days <= 30: buckets['1_30'].append((inv, days))
        elif days <= 60: buckets['31_60'].append((inv, days))
        elif days <= 90: buckets['61_90'].append((inv, days))
        else: buckets['over_90'].append((inv, days))
        if days > 0 and inv.status == 'Issued':
            inv.status = 'Overdue'
    db.session.commit()

    def bt(b): return sum(i.total or 0 for i,_ in b)
    def br(b, css):
        if not b: return '<tr><td colspan="6" style="text-align:center;color:#999;padding:10px">None</td></tr>'
        return ''.join(f'''<tr>
          <td><a href="/doc/inv/{i.id}" style="color:#1a3a6b;font-weight:bold">{i.ref}</a></td>
          <td>{i.party_name}</td><td>AED {i.total:,.2f}</td>
          <td>{i.due_date or "-"}</td>
          <td class="{css}">{abs(d)} {"days left" if d<=0 else "days overdue"}</td>
          <td><form method="POST" action="/doc/status/{i.id}" style="display:inline">
            <input type="hidden" name="status" value="Paid">
            <button type="submit" class="btn btn-sm btn-success"
                    onclick="return confirm('Mark {i.ref} as Paid?')">✓ Paid</button>
          </form></td></tr>''' for i,d in b)

    totals   = {k: bt(v) for k,v in buckets.items()}
    grand    = sum(totals.values())

    content = f'''<div class="card"><h2>Aged Receivables Dashboard</h2>
<div class="stats">
  <div class="stat" style="border-left:4px solid #27ae60">
    <div class="stat-label">Current</div><div class="stat-value green">AED {totals["current"]:,.0f}</div>
    <div class="stat-sub">{len(buckets["current"])} invoices</div></div>
  <div class="stat" style="border-left:4px solid #f39c12">
    <div class="stat-label">1–30 Days Overdue</div><div class="stat-value orange">AED {totals["1_30"]:,.0f}</div>
    <div class="stat-sub">{len(buckets["1_30"])} invoices</div></div>
  <div class="stat" style="border-left:4px solid #e67e22">
    <div class="stat-label">31–60 Days</div><div class="stat-value orange">AED {totals["31_60"]:,.0f}</div>
    <div class="stat-sub">{len(buckets["31_60"])} invoices</div></div>
  <div class="stat" style="border-left:4px solid #e74c3c">
    <div class="stat-label">61–90 Days</div><div class="stat-value red">AED {totals["61_90"]:,.0f}</div>
    <div class="stat-sub">{len(buckets["61_90"])} invoices</div></div>
  <div class="stat" style="border-left:4px solid #922b21;background:#fff5f5">
    <div class="stat-label">90+ Days</div><div class="stat-value red">AED {totals["over_90"]:,.0f}</div>
    <div class="stat-sub">{len(buckets["over_90"])} invoices</div></div>
  <div class="stat" style="border:2px solid #1a3a6b">
    <div class="stat-label">Total Outstanding</div><div class="stat-value">AED {grand:,.0f}</div>
    <div class="stat-sub blue">{len(invoices)} unpaid</div></div>
</div>
{"".join(f'''<div class="card" style="border-left:4px solid {clr}"><h2>{lbl}</h2>
<table><thead><tr><th>Invoice</th><th>Client</th><th>Amount</th><th>Due</th><th>Status</th><th>Action</th></tr></thead>
<tbody>{br(buckets[key], css)}</tbody></table></div>'''
for key, lbl, clr, css in [
  ('current','Current','#27ae60','aged-current'),
  ('1_30','1–30 Days Overdue','#f39c12','aged-30'),
  ('31_60','31–60 Days Overdue','#e67e22','aged-60'),
  ('61_90','61–90 Days Overdue','#e74c3c','aged-90'),
  ('over_90','90+ Days — Action Required','#922b21','aged-90'),
])}
</div>'''
    return base_page(content, 'receivables', 'Aged Receivables')


# ── VAT / FTA ─────────────────────────────────────────────────

@app.route('/vat', methods=['GET','POST'])
@admin_required
def vat():
    now     = datetime.now()
    quarter = request.args.get('quarter', str((now.month-1)//3 + 1))
    year    = request.args.get('year', str(now.year))
    try:
        q_int, y_int = int(quarter), int(year)
    except:
        q_int, y_int = 2, 2026

    q_months = [(q_int-1)*3+1, (q_int-1)*3+2, (q_int-1)*3+3]

    def in_quarter(date_str):
        if not date_str: return False
        for sep in ('/', '-'):
            parts = date_str.split(sep)
            if len(parts) == 3:
                try:
                    m = int(parts[1] if sep=='/' else parts[1])
                    yi = int(parts[2] if sep=='/' else parts[0])
                    return m in q_months and yi == y_int
                except: pass
        return False

    inv_docs = Document.query.filter(Document.doc_type=='INV', Document.status!='VOID').all()
    lpo_docs = Document.query.filter(Document.doc_type=='LPO', Document.status!='VOID').all()

    inv_q = [d for d in inv_docs if in_quarter(d.date)]
    lpo_q = [d for d in lpo_docs if in_quarter(d.date)]

    output_tax = sum(d.vat or 0 for d in inv_q)
    input_tax  = sum(d.vat or 0 for d in lpo_q)
    net_vat    = output_tax - input_tax

    inv_rows = ''.join(f'''<tr>
      <td><a href="/doc/inv/{d.id}" style="color:#1a3a6b">{d.ref}</a></td>
      <td>{d.date}</td><td>{d.party_name}</td>
      <td>AED {d.subtotal:,.2f}</td><td>AED {d.vat:,.2f}</td>
      <td>AED {d.total:,.2f}</td></tr>''' for d in inv_q) or \
      '<tr><td colspan="6" style="text-align:center;color:#999">No invoices in this quarter.</td></tr>'
    lpo_rows = ''.join(f'''<tr>
      <td><a href="/doc/lpo/{d.id}" style="color:#1a3a6b">{d.ref}</a></td>
      <td>{d.date}</td><td>{d.party_name}</td>
      <td>AED {d.subtotal:,.2f}</td><td>AED {d.vat:,.2f}</td>
      <td>AED {d.total:,.2f}</td></tr>''' for d in lpo_q) or \
      '<tr><td colspan="6" style="text-align:center;color:#999">No LPOs in this quarter.</td></tr>'

    q_opts = ''.join(
        f'<option value="{q}" {"selected" if str(q)==str(q_int) else ""}>Q{q}</option>'
        for q in range(1,5))
    y_opts = ''.join(
        f'<option value="{y}" {"selected" if str(y)==str(y_int) else ""}>{y}</option>'
        for y in [2024,2025,2026,2027])

    vat_color = 'red' if net_vat > 0 else 'green'

    content = f'''<div class="card">
  <h2>VAT / FTA Reporting</h2>
  <form method="GET" style="display:flex;gap:8px;align-items:center;margin-bottom:16px">
    <label style="font-size:12px;font-weight:bold;color:#555">Quarter:</label>
    <select name="quarter" style="padding:7px;border:1px solid #ddd;border-radius:6px;font-size:12px">{q_opts}</select>
    <select name="year" style="padding:7px;border:1px solid #ddd;border-radius:6px;font-size:12px">{y_opts}</select>
    <button type="submit" class="btn btn-primary btn-sm">Load</button>
    <a href="/vat/pdf?quarter={q_int}&year={y_int}" class="btn btn-outline btn-sm"
       target="_blank">⬇ Download PDF</a>
  </form>

  <div class="stats">
    <div class="stat" style="border-left:4px solid #2980b9">
      <div class="stat-label">Output VAT (from Invoices)</div>
      <div class="stat-value blue">AED {output_tax:,.2f}</div>
      <div class="stat-sub">{len(inv_q)} invoices in Q{q_int} {y_int}</div>
    </div>
    <div class="stat" style="border-left:4px solid #27ae60">
      <div class="stat-label">Input VAT (from LPOs)</div>
      <div class="stat-value green">AED {input_tax:,.2f}</div>
      <div class="stat-sub">{len(lpo_q)} LPOs in Q{q_int} {y_int}</div>
    </div>
    <div class="stat" style="border:2px solid #1a3a6b">
      <div class="stat-label">Net VAT Payable to FTA</div>
      <div class="stat-value {vat_color}">AED {net_vat:,.2f}</div>
      <div class="stat-sub">{"Payable" if net_vat>0 else "Refundable"}</div>
    </div>
  </div>

  <div class="card">
    <h2>Tax Invoices — Q{q_int} {y_int}</h2>
    <table><thead><tr><th>Invoice</th><th>Date</th><th>Client</th>
      <th>Subtotal</th><th>VAT</th><th>Total</th></tr></thead>
    <tbody>{inv_rows}</tbody></table>
    <div style="text-align:right;padding:8px 12px;font-weight:bold;font-size:12px">
      Output VAT Total: AED {output_tax:,.2f}</div>
  </div>

  <div class="card">
    <h2>Purchase LPOs — Q{q_int} {y_int}</h2>
    <table><thead><tr><th>LPO</th><th>Date</th><th>Vendor</th>
      <th>Subtotal</th><th>VAT</th><th>Total</th></tr></thead>
    <tbody>{lpo_rows}</tbody></table>
    <div style="text-align:right;padding:8px 12px;font-weight:bold;font-size:12px">
      Input VAT Total: AED {input_tax:,.2f}</div>
  </div>

  <div style="background:#1a3a6b;color:#fff;border-radius:10px;padding:16px;margin-top:4px">
    <div style="font-size:11px;opacity:0.7;margin-bottom:4px">FTA RETURN SUMMARY — Q{q_int} {y_int}</div>
    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:8px">
      <span>Box 1a — Standard-Rated Supplies:</span>
      <strong>AED {sum(d.subtotal or 0 for d in inv_q):,.2f}</strong>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:8px">
      <span>Box 1b — VAT on Supplies (Output Tax):</span>
      <strong>AED {output_tax:,.2f}</strong>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:8px">
      <span>Box 9 — Input Tax Recoverable:</span>
      <strong>AED {input_tax:,.2f}</strong>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:16px;
                border-top:1px solid rgba(255,255,255,0.3);padding-top:8px">
      <span>Tax Payable / (Refundable):</span>
      <strong>AED {net_vat:,.2f}</strong>
    </div>
  </div>
</div>'''
    return base_page(content, 'vat', f'VAT / FTA — Q{q_int} {y_int}')


@app.route('/vat/pdf')
@admin_required
def vat_pdf():
    q_int = int(request.args.get('quarter', 2))
    y_int = int(request.args.get('year', 2026))
    q_months = [(q_int-1)*3+1, (q_int-1)*3+2, (q_int-1)*3+3]

    def in_quarter(date_str):
        if not date_str: return False
        for sep in ('/', '-'):
            parts = date_str.split(sep)
            if len(parts) == 3:
                try:
                    m = int(parts[1] if sep=='/' else parts[1])
                    yi = int(parts[2] if sep=='/' else parts[0])
                    return m in q_months and yi == y_int
                except: pass
        return False

    inv_q = [d for d in Document.query.filter(
        Document.doc_type=='INV', Document.status!='VOID').all() if in_quarter(d.date)]
    lpo_q = [d for d in Document.query.filter(
        Document.doc_type=='LPO', Document.status!='VOID').all() if in_quarter(d.date)]
    output_tax = sum(d.vat or 0 for d in inv_q)
    input_tax  = sum(d.vat or 0 for d in lpo_q)
    net_vat    = output_tax - input_tax

    buf = generate_vat_pdf(q_int, y_int, output_tax, input_tax, net_vat, inv_q, lpo_q)
    if not buf:
        flash('PDF generation failed.', 'error')
        return redirect(url_for('vat'))
    return send_file(buf, download_name=f'NAT_VAT_Q{q_int}_{y_int}.pdf',
                     as_attachment=True, mimetype='application/pdf')


# ── Reports ───────────────────────────────────────────────────

@app.route('/reports')
@admin_required
def reports():
    now = datetime.now()
    year = int(request.args.get('year', now.year))

    monthly_rev = {}
    monthly_cost = {}
    for m in range(1, 13):
        docs = Document.query.filter(
            Document.doc_type=='INV', Document.status!='VOID',
            Document.date.like(f'%-{str(m).zfill(2)}-%') if '-' in (Document.query.first().date if Document.query.first() else '') else
            Document.date.like(f'{str(m).zfill(2)}/%')
        ).all() if Document.query.first() else []
        # Simpler: filter client-side
        all_inv = Document.query.filter(Document.doc_type=='INV', Document.status!='VOID').all()
        m_inv = [d for d in all_inv if d.date and (
            (('-' in d.date) and d.date.split('-')[1] == str(m).zfill(2) and d.date.split('-')[0] == str(year)) or
            (('/' in d.date) and d.date.split('/')[1] == str(m).zfill(2) and d.date.split('/')[2] == str(year))
        )]
        monthly_rev[m] = sum(d.subtotal or 0 for d in m_inv)

    month_names = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    chart_data = [(month_names[m-1], monthly_rev.get(m,0)) for m in range(1,13)]
    max_val = max((v for _, v in chart_data), default=1) or 1

    bars = ''.join(f'''<div style="display:flex;flex-direction:column;align-items:center;flex:1">
      <div style="font-size:9px;color:#888;margin-bottom:2px">
        {"AED {:,.0f}".format(v) if v else ""}</div>
      <div style="background:#1a3a6b;width:100%;height:{int(v/max_val*120)}px;
                  border-radius:4px 4px 0 0;min-height:{2 if v>0 else 0}px"></div>
      <div style="font-size:10px;color:#555;margin-top:4px">{m}</div>
    </div>''' for m,v in chart_data)

    annual_rev   = sum(monthly_rev.values())
    annual_inv   = Document.query.filter(Document.doc_type=='INV', Document.status!='VOID').count()
    top_clients  = {}
    for d in Document.query.filter(Document.doc_type=='INV', Document.status!='VOID').all():
        top_clients[d.party_name] = top_clients.get(d.party_name, 0) + (d.total or 0)
    top5 = sorted(top_clients.items(), key=lambda x: -x[1])[:5]
    top5_rows = ''.join(f'<tr><td><strong>{c}</strong></td><td>AED {v:,.2f}</td></tr>'
                        for c, v in top5) or '<tr><td colspan="2" style="color:#999">No data</td></tr>'

    content = f'''<div class="card">
  <h2>Business Reports & Analytics</h2>
  <form method="GET" style="display:flex;gap:8px;align-items:center;margin-bottom:14px">
    <select name="year" style="padding:7px;border:1px solid #ddd;border-radius:6px;font-size:12px">
      <option {"selected" if year==2025 else ""}>2025</option>
      <option {"selected" if year==2026 else ""}>2026</option>
      <option {"selected" if year==2027 else ""}>2027</option>
    </select>
    <button type="submit" class="btn btn-primary btn-sm">Load</button>
  </form>
  <div class="stats">
    <div class="stat"><div class="stat-label">Total Revenue {year}</div>
      <div class="stat-value">AED {annual_rev:,.0f}</div></div>
    <div class="stat"><div class="stat-label">Total Invoices</div>
      <div class="stat-value">{annual_inv}</div></div>
    <div class="stat"><div class="stat-label">Avg Invoice Value</div>
      <div class="stat-value">AED {(annual_rev/annual_inv if annual_inv else 0):,.0f}</div></div>
  </div>

  <h2>Monthly Revenue {year}</h2>
  <div style="display:flex;align-items:flex-end;gap:4px;height:160px;
              border-bottom:2px solid #eee;padding-bottom:4px;margin-bottom:14px">
    {bars}
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Top Clients by Revenue (All Time)</h2>
      <table><thead><tr><th>Client</th><th>Revenue AED</th></tr></thead>
      <tbody>{top5_rows}</tbody></table>
    </div>
    <div class="card">
      <h2>Quick Exports</h2>
      <div style="display:flex;flex-direction:column;gap:8px">
        <a href="/export/invoices" class="btn btn-outline">⬇ Export All Invoices CSV</a>
        <a href="/export/lpos" class="btn btn-outline">⬇ Export All LPOs CSV</a>
        <a href="/export/clients" class="btn btn-outline">⬇ Export Clients CSV</a>
        <a href="/export/vendors" class="btn btn-outline">⬇ Export Vendors CSV</a>
        <a href="/export/catalog" class="btn btn-outline">⬇ Export Catalog CSV</a>
        <a href="/export/cheques" class="btn btn-outline">⬇ Export Cheque Register CSV</a>
      </div>
    </div>
  </div>
</div>'''
    return base_page(content, 'reports', 'Reports')


# ── Data Exports ──────────────────────────────────────────────

@app.route('/export/<data_type>')
@admin_required
def export_data(data_type):
    buf = io.StringIO()
    writer = csv.writer(buf)

    if data_type == 'invoices':
        writer.writerow(['Ref','Date','Client','Client TRN','Subtotal','VAT','Total','Status','By'])
        for d in Document.query.filter_by(doc_type='INV').order_by(Document.created_at.desc()).all():
            writer.writerow([d.ref, d.date, d.party_name, d.party_trn or '',
                             d.subtotal, d.vat, d.total, d.status, d.created_by])
        fname = 'NAT_Invoices.csv'

    elif data_type == 'lpos':
        writer.writerow(['Ref','Date','Vendor','Subtotal','VAT','Total','Status','By'])
        for d in Document.query.filter_by(doc_type='LPO').order_by(Document.created_at.desc()).all():
            writer.writerow([d.ref, d.date, d.party_name, d.subtotal, d.vat, d.total, d.status, d.created_by])
        fname = 'NAT_LPOs.csv'

    elif data_type == 'clients':
        writer.writerow(CLIENT_TEMPLATE_HEADERS)
        for c in Client.query.order_by(Client.name).all():
            writer.writerow([c.name, c.contact, c.phone, c.email,
                             c.address, c.trn, c.license_no, c.notes])
        fname = 'NAT_Clients.csv'

    elif data_type == 'vendors':
        writer.writerow(VENDOR_TEMPLATE_HEADERS)
        for v in Vendor.query.order_by(Vendor.name).all():
            writer.writerow([v.name, v.contact, v.phone, v.email,
                             v.address, v.trn, v.license_no, v.products, v.notes])
        fname = 'NAT_Vendors.csv'

    elif data_type == 'catalog':
        writer.writerow(['Name','Spec','Unit','Category','Vendors',
                         'Cost_Price','Markup_Pct','Last_Price'])
        for i in CatalogItem.query.order_by(CatalogItem.name).all():
            writer.writerow([i.name, i.spec, i.unit, i.category, i.vendors,
                             i.cost_price, i.markup_pct, i.last_price])
        fname = 'NAT_Catalog.csv'

    elif data_type == 'cheques':
        writer.writerow(['Company','TRN','Invoice Ref','DO No.','Cheque No.',
                         'Cheque Date','Bank','Clearance Date','Signatory',
                         'Amount AED','Status','Notes'])
        for r in ChequeRecord.query.order_by(ChequeRecord.cheque_date.desc()).all():
            writer.writerow([r.company_name, r.party_trn, r.invoice_ref,
                             r.delivery_note_no, r.cheque_number, r.cheque_date,
                             r.bank_name, r.clearance_date, r.signatory,
                             r.total_amount, r.status, r.notes])
        fname = 'NAT_Cheque_Register.csv'

    else:
        abort(404)

    buf.seek(0)
    return send_file(
        io.BytesIO(buf.read().encode('utf-8-sig')),
        download_name=fname, as_attachment=True, mimetype='text/csv')


# ── Audit Log ─────────────────────────────────────────────────

def get_action_badge(action):
    m = {'ADD':'badge-green','EDIT':'badge-blue','DELETE':'badge-red',
         'CLEAR':'badge-red','VOID':'badge-red'}
    return m.get(action, 'badge-grey')

@app.route('/audit')
@admin_required
def audit_log():
    logs = AuditLog.query.order_by(AuditLog.logged_at.desc()).limit(500).all()
    rows = ''.join(f'''<tr>
      <td style="font-size:11px">{l.logged_at.strftime("%d/%m/%Y %H:%M") if l.logged_at else "-"}</td>
      <td>{l.user_name}</td>
      <td><span class="badge {get_action_badge(l.action)}">{l.action}</span></td>
      <td>{l.table_name}</td>
      <td>{l.record_name}</td>
      <td style="font-size:10px;max-width:300px">
        {("".join(f'<span style="color:#888">{c["field"]}</span>: '
                   f'<del style="color:#e74c3c">{c["old"][:30]}</del> → '
                   f'<span style="color:#27ae60">{c["new"][:30]}</span><br/>'
                   for c in json.loads(l.changes or '[]'))) if l.changes and l.changes != '[]' else ""}
      </td>
    </tr>''' for l in logs) or \
    '<tr><td colspan="6" style="text-align:center;color:#999;padding:20px">No audit logs yet.</td></tr>'

    content = f'''<div class="card">
  <h2>Audit Log (last 500 entries)</h2>
  <table><thead><tr>
    <th>Timestamp</th><th>User</th><th>Action</th><th>Table</th><th>Record</th><th>Changes</th>
  </tr></thead><tbody>{rows}</tbody></table>
</div>'''
    return base_page(content, 'audit', 'Audit Log')


# ── DB Init + Startup ──────────────────────────────────────────

def create_default_users():
    """Create default admin users if none exist."""
    if User.query.count() > 0:
        return
    default_users = [
        {'name':'Rameez', 'email':'rameez@newasiantrd.com',
         'email2':'rameez.fayyaz@gmail.com', 'password':'NAT@2026', 'role':'admin'},
        {'name':'Muhammad', 'email':'newasiantrd@emirates.net.ae',
         'email2':'info@newasiantrading.com', 'password':'NAT@2026', 'role':'admin'},
        {'name':'Nabil', 'email':'sales@newasiantrd.com',
         'password':'NAT@2026', 'role':'admin'},
    ]
    for u in default_users:
        db.session.add(User(
            name=u['name'], email=u['email'],
            email2=u.get('email2'),
            password=generate_password_hash(u['password']),
            role=u['role']
        ))
    db.session.commit()
    print("Default users created.")

def init_counters():
    """Initialize document counters if not present."""
    prefixes = {'LPO':'LPO-2026-','INV':'INV-2026-','DO':'DO-2026-',
                'QUO':'QUO-2026-','ENQ':'ENQ-2026-','RCV':'RCV-2026-'}
    for dt, pf in prefixes.items():
        if not DocCounter.query.filter_by(doc_type=dt).first():
            db.session.add(DocCounter(doc_type=dt, prefix=pf, last_num=0))
    db.session.commit()

def seed_logo_to_db():
    """Seed the Logo.jpeg file into DB on startup if not already there."""
    if CompanySetting.get('logo_b64'):
        return  # Already in DB
    if os.path.exists(LOGO_PATH):
        with open(LOGO_PATH, 'rb') as f:
            raw = f.read()
        b64 = 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()
        CompanySetting.set('logo_b64', b64)
        print("Logo seeded to DB from file.")

def run_migrations():
    """Add any missing columns from v3 → v4 migration."""
    engine = db.engine
    with engine.connect() as conn:
        # Check if party_trn column exists on Document
        try:
            conn.execute(db.text("SELECT party_trn FROM document LIMIT 1"))
        except:
            try:
                conn.execute(db.text("ALTER TABLE document ADD COLUMN party_trn VARCHAR(50)"))
                conn.commit()
                print("Added party_trn to document")
            except Exception as e:
                print(f"Migration note: {e}")

        # Check gdrive_url on Document
        try:
            conn.execute(db.text("SELECT gdrive_url FROM document LIMIT 1"))
        except:
            try:
                conn.execute(db.text("ALTER TABLE document ADD COLUMN gdrive_url VARCHAR(500)"))
                conn.commit()
                print("Added gdrive_url to document")
            except Exception as e:
                print(f"Migration note: {e}")

        # Check cost_price on catalog_item
        try:
            conn.execute(db.text("SELECT cost_price FROM catalog_item LIMIT 1"))
        except:
            try:
                conn.execute(db.text("ALTER TABLE catalog_item ADD COLUMN cost_price FLOAT DEFAULT 0"))
                conn.execute(db.text("ALTER TABLE catalog_item ADD COLUMN markup_pct FLOAT DEFAULT 0"))
                conn.commit()
                print("Added cost_price, markup_pct to catalog_item")
            except Exception as e:
                print(f"Migration note: {e}")


with app.app_context():
    db.create_all()
    run_migrations()
    create_default_users()
    init_counters()
    seed_logo_to_db()
    print("NAT Web Operations System v4 — Ready")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)

