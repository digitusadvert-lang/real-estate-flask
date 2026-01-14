import sqlite3
import json
import smtplib
import csv
from flask import (
    Flask,
    render_template,
    render_template_string,
    request,
    redirect,
    session,
    jsonify,
    flash,
    send_file,
    url_for,
)
from io import StringIO
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import random
import string
import os


def get_db_connection(timeout=30):
    """Get a database connection with timeout handling"""
    import time

    max_retries = 3

    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect("real_estate.db", timeout=timeout)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")  # 30 second timeout
            return conn
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                print(f" Database locked, retrying... ({attempt + 1}/{max_retries})")
                time.sleep(1)
                continue
            else:
                raise e


app = Flask(__name__)
import secrets

app.secret_key = secrets.token_hex(32)  # Generate secure random key
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB max file size
app.config["UPLOAD_FOLDER"] = "uploads"

# ============ HELPER FUNCTIONS ============

def render_error_page(error_message, error_details=None):
    """Render error page with consistent styling"""
    return render_template(
        "error.html",
        error_message=error_message,
        error_details=error_details,
        support_email="support@realestate.com"
    )

@app.template_filter('format_currency')
def format_currency_filter(value):
    """Format numbers as currency with 2 decimal places"""
    try:
        return "{:,.2f}".format(float(value))
    except (ValueError, TypeError):
        return value

def update_pending_commissions(agent_id, downline_id, commission_type, amount, submission_id):
    """Update pending commissions when a downline submits a commission request"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    
    try:
        # Insert into upline_pending_commissions table
        cursor.execute(
            """
            INSERT INTO upline_pending_commissions 
            (submission_id, downline_agent_id, upline_agent_id, commission_type, amount, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (submission_id, downline_id, agent_id, commission_type, amount)
        )
        
        conn.commit()
        return True
    except Exception as e:
        print(f"Error updating pending commissions: {e}")
        return False
    finally:
        conn.close()

# ============ ADD SECURITY CONFIGURATION HERE ============
from datetime import timedelta

# Session security settings
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=2)
app.config["SESSION_COOKIE_SECURE"] = False  # Set to True when using HTTPS
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# File upload security
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def allowed_file(filename):
    """Check if file extension is allowed"""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def validate_file_size(file_storage):
    """Validate file size before saving"""
    # Get file size without reading entire file
    if hasattr(file_storage, "content_length"):
        return file_storage.content_length <= MAX_FILE_SIZE
    return True  # If we can't check, proceed with caution


@app.context_processor
def utility_processor():
    """Make helper functions available to all templates"""

    def format_currency(value):
        try:
            if value is None:
                return "RM0.00"
            return "RM{:,.2f}".format(float(value))
        except (ValueError, TypeError):
            return "RM0.00"

    def format_number(value):
        try:
            if value is None:
                return "0"
            return "{:,.0f}".format(float(value))
        except (ValueError, TypeError):
            return "0"

    return dict(format_currency=format_currency, format_number=format_number)


# ============ UTILITY FUNCTIONS ============
def get_file_icon(file_type):
    """Get appropriate icon for file type"""
    if file_type in ["pdf"]:
        return "📄"
    elif file_type in ["jpg", "jpeg", "png", "gif", "bmp"]:
        return "🖼️"
    elif file_type in ["doc", "docx"]:
        return "📝"
    elif file_type in ["xls", "xlsx"]:
        return "📊"
    elif file_type in ["txt"]:
        return "📋"
    else:
        return "📎"


# ===================== MULTI-LEVEL COMMISSION HELPERS =====================
def get_agent_with_upline_info(agent_id):
    """Get agent information with upline details"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get agent with upline names - UPDATED for users table
    cursor.execute(
        """
    SELECT 
        u.*,
        u1.name as upline_name,
        u1.email as upline_email,
        u2.name as upline2_name,
        u2.email as upline2_email
    FROM users u
    LEFT JOIN users u1 ON u.upline_id = u1.id
    LEFT JOIN users u2 ON u.upline2_id = u2.id
    WHERE u.id = ? AND u.role = "agent"
    """,
        (agent_id,),
    )

    agent = cursor.fetchone()
    conn.close()

    if agent:
        # Convert to dictionary with proper column mapping
        columns = [
            "id",
            "email",
            "password",
            "name",
            "role",
            "upline_id",
            "upline_commission_rate",
            "created_at",
            "upline2_id",
            "upline2_commission_rate",
            "commission_rate",
            "total_listings",
            "total_commission",
            "joined_date",
            "upline_name",
            "upline_email",
            "upline2_name",
            "upline2_email",
        ]

        # Fill missing columns with None (adjust based on your actual table structure)
        agent_data = dict(zip(columns[: len(agent)], agent))
        return agent_data
    return None


# ===================== COMMISSION CALCULATION =====================
def calculate_multi_level_commission(sale_amount, agent_id, calculation_method="auto", listing_id=None):
    """
    Calculate commissions with two possible methods
    
    Parameters:
    - sale_amount: Property sale price
    - agent_id: ID of selling agent
    - calculation_method: "auto", "legacy", or "fund_based"
    - listing_id: Optional listing ID (can be None for tests)
    """
    import json
    from datetime import datetime
    
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    
    # Get agent with all commission fields
    cursor.execute("""
        SELECT 
            upline_id, upline2_id, 
            total_commission_fund_pct, agent_fund_pct,
            upline_fund_pct, upline2_fund_pct, company_fund_pct,
            commission_structure
        FROM users WHERE id = ? AND role = "agent"
        """,
        (agent_id,)
    )
    
    agent = cursor.fetchone()
    commissions = []
    
    if agent:
        (upline_id, upline2_id, total_fund_pct, agent_fund_pct,
         upline_fund_pct, upline2_fund_pct, company_fund_pct, structure) = agent
        
        # For legacy calculation, use default rates
        legacy_agent_rate = 0.025  # Default 2.5%
        legacy_upline_rate = 0.20  # Default 20%
        
        # Determine calculation method
        if calculation_method == "auto":
            use_fund_method = (structure == "fund_based" or 
                              total_fund_pct is not None)
        else:
            use_fund_method = (calculation_method == "fund_based")
        
        if use_fund_method:
            # ============ NEW FUND-BASED CALCULATION ============
            # 1. Calculate total commission fund (2% of sale)
            total_fund_percentage = total_fund_pct if total_fund_pct is not None else 2.00
            total_commission_fund = sale_amount * (total_fund_percentage / 100)
            
            # 2. Get percentages (use defaults if NULL)
            agent_pct = agent_fund_pct if agent_fund_pct is not None else 80.00
            upline_pct = upline_fund_pct if upline_fund_pct is not None else 10.00
            upline2_pct = upline2_fund_pct if upline2_fund_pct is not None else 5.00
            company_pct = company_fund_pct if company_fund_pct is not None else 5.00
            
            # 3. Calculate amounts from fund
            # Agent: 80% of fund
            agent_amount = total_commission_fund * (agent_pct / 100)
            
            # Direct upline: 10% of fund
            upline_amount = 0
            if upline_id and upline_pct > 0:
                upline_amount = total_commission_fund * (upline_pct / 100)
            
            # Indirect upline: 5% of fund
            upline2_amount = 0
            if upline2_id and upline2_pct > 0:
                upline2_amount = total_commission_fund * (upline2_pct / 100)
            
            # Company balance: 5% of fund
            company_amount = total_commission_fund * (company_pct / 100)
            
            # Adjust for any rounding errors
            total_distributed = agent_amount + upline_amount + upline2_amount + company_amount
            variance = total_commission_fund - total_distributed
            if abs(variance) > 0.01:
                company_amount += variance
            
            # 4. Create commission records
            commissions.append({
                "agent_id": agent_id,
                "amount": agent_amount,
                "rate": agent_pct,
                "level": 0,
                "type": "agent_fund",
                "calculation_method": "fund_based",
                "calculation_base": total_commission_fund,
                "formula": f"{total_fund_percentage}% of sale = fund, then {agent_pct}% of fund"
            })
            
            # Update agent's total commission
            cursor.execute(
                """
                UPDATE users 
                SET total_commission = COALESCE(total_commission, 0) + ? 
                WHERE id = ?
                """,
                (agent_amount, agent_id)
            )
            
            # Direct upline
            if upline_id and upline_amount > 0:
                commissions.append({
                    "agent_id": upline_id,
                    "amount": upline_amount,
                    "rate": upline_pct,
                    "level": 1,
                    "type": "direct_upline_fund",
                    "calculation_method": "fund_based",
                    "calculation_base": total_commission_fund,
                    "formula": f"{upline_pct}% of commission fund"
                })
                
                cursor.execute(
                    """
                    UPDATE users 
                    SET total_commission = COALESCE(total_commission, 0) + ? 
                    WHERE id = ?
                    """,
                    (upline_amount, upline_id)
                )
            
            # Indirect upline
            if upline2_id and upline2_amount > 0:
                commissions.append({
                    "agent_id": upline2_id,
                    "amount": upline2_amount,
                    "rate": upline2_pct,
                    "level": 2,
                    "type": "indirect_upline_fund",
                    "calculation_method": "fund_based",
                    "calculation_base": total_commission_fund,
                    "formula": f"{upline2_pct}% of commission fund"
                })
                
                cursor.execute(
                    """
                    UPDATE users 
                    SET total_commission = COALESCE(total_commission, 0) + ? 
                    WHERE id = ?
                    """,
                    (upline2_amount, upline2_id)
                )
            
            # Company balance
            commissions.append({
                "agent_id": 0,
                "amount": company_amount,
                "rate": company_pct,
                "level": 3,
                "type": "company_balance",
                "calculation_method": "fund_based",
                "calculation_base": total_commission_fund,
                "formula": f"Company balance: {company_pct}% of fund"
            })
            
            # Store calculation details
            calculation_details = json.dumps({
                'method': 'fund_based',
                'total_fund_percentage': total_fund_percentage,
                'total_commission_fund': total_commission_fund,
                'distributions': commissions,
                'timestamp': datetime.now().isoformat()
            })
            
            # INSERT with listing_id (can be NULL)
            cursor.execute(
                """
                INSERT INTO commission_calculations 
                (listing_id, agent_id, sale_price, base_rate, commission, calculation_details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (listing_id, agent_id, sale_amount, total_fund_percentage, agent_amount, calculation_details)
            )
            
        else:
            # ============ LEGACY CALCULATION ============
            agent_amount = sale_amount * legacy_agent_rate
            commissions.append({
                "agent_id": agent_id,
                "amount": agent_amount,
                "rate": legacy_agent_rate * 100,
                "level": 0,
                "type": "self",
                "calculation_method": "legacy"
            })
            
            cursor.execute(
                """
                UPDATE users 
                SET total_commission = COALESCE(total_commission, 0) + ? 
                WHERE id = ?
                """,
                (agent_amount, agent_id)
            )
            
            # Calculate for direct upline
            if upline_id and legacy_upline_rate > 0:
                upline_amount = agent_amount * legacy_upline_rate
                commissions.append({
                    "agent_id": upline_id,
                    "amount": upline_amount,
                    "rate": legacy_upline_rate * 100,
                    "level": 1,
                    "type": "direct_upline",
                    "calculation_method": "legacy"
                })
                
                cursor.execute(
                    """
                    UPDATE users 
                    SET total_commission = COALESCE(total_commission, 0) + ? 
                    WHERE id = ?
                    """,
                    (upline_amount, upline_id)
                )
            
            # Store legacy calculation
            calculation_details = json.dumps({
                'method': 'legacy',
                'agent_rate': float(legacy_agent_rate),
                'upline_rate': float(legacy_upline_rate),
                'distributions': commissions,
                'timestamp': datetime.now().isoformat()
            })
            
            # INSERT with listing_id (can be NULL)
            cursor.execute(
                """
                INSERT INTO commission_calculations 
                (listing_id, agent_id, sale_price, base_rate, commission, calculation_details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (listing_id, agent_id, sale_amount, legacy_agent_rate, commissions[0]['amount'] if commissions else 0, calculation_details)
            )
        
        # Update total sales for agent
        cursor.execute(
            """
            UPDATE users 
            SET total_sales = COALESCE(total_sales, 0) + ? 
            WHERE id = ?
            """,
            (sale_amount, agent_id)
        )
        
        conn.commit()
    
    conn.close()
    return commissions

# ===================== COMMISSION HELPER FUNCTIONS =====================
def get_commission_breakdown(sale_amount, agent_id=None, method="fund_based"):
    """
    Get detailed commission breakdown for preview or display
    """
    if method == "fund_based":
        total_fund = sale_amount * 0.02  # 2% of sale
        
        breakdown = {
            "sale_amount": sale_amount,
            "commission_fund_percentage": 2.0,
            "total_commission_fund": total_fund,
            "distribution": [
                {
                    "party": "Agent",
                    "percentage": 80.0,
                    "amount": total_fund * 0.80,
                    "calculation": f"RM {total_fund:,.2f} × 80%",
                    "description": "Agent gets 80% of commission fund"
                },
                {
                    "party": "Direct Upline",
                    "percentage": 10.0,
                    "amount": total_fund * 0.10,
                    "calculation": f"RM {total_fund:,.2f} × 10%",
                    "description": "Direct upline gets 10% of commission fund"
                },
                {
                    "party": "Indirect Upline",
                    "percentage": 5.0,
                    "amount": total_fund * 0.05,
                    "calculation": f"RM {total_fund:,.2f} × 5%",
                    "description": "Indirect upline gets 5% of commission fund"
                },
                {
                    "party": "Company Balance",
                    "percentage": 5.0,
                    "amount": total_fund * 0.05,
                    "calculation": f"RM {total_fund:,.2f} × 5%",
                    "description": "Company keeps 5% as balance"
                }
            ],
            "total_distributed": total_fund,
            "method": "fund_based"
        }
        
        # If agent_id provided, fetch agent-specific rates
        if agent_id:
            conn = sqlite3.connect("real_estate.db")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT total_commission_fund_pct, agent_fund_pct, 
                       upline_fund_pct, upline2_fund_pct, company_fund_pct
                FROM users WHERE id = ?
            """, (agent_id,))
            
            agent_rates = cursor.fetchone()
            conn.close()
            
            if agent_rates and any(r is not None for r in agent_rates):
                total_pct, agent_pct, upline_pct, upline2_pct, company_pct = agent_rates
                
                if total_pct is not None:
                    breakdown["commission_fund_percentage"] = float(total_pct)
                    breakdown["total_commission_fund"] = sale_amount * (float(total_pct) / 100)
                
                # Update percentages if custom
                if agent_pct is not None:
                    breakdown["distribution"][0]["percentage"] = float(agent_pct)
                if upline_pct is not None:
                    breakdown["distribution"][1]["percentage"] = float(upline_pct)
                if upline2_pct is not None:
                    breakdown["distribution"][2]["percentage"] = float(upline2_pct)
                if company_pct is not None:
                    breakdown["distribution"][3]["percentage"] = float(company_pct)
                
                # Recalculate amounts with custom percentages
                total_fund = breakdown["total_commission_fund"]
                breakdown["distribution"][0]["amount"] = total_fund * (breakdown["distribution"][0]["percentage"] / 100)
                breakdown["distribution"][1]["amount"] = total_fund * (breakdown["distribution"][1]["percentage"] / 100)
                breakdown["distribution"][2]["amount"] = total_fund * (breakdown["distribution"][2]["percentage"] / 100)
                breakdown["distribution"][3]["amount"] = total_fund * (breakdown["distribution"][3]["percentage"] / 100)
                
                breakdown["has_custom_rates"] = True
            else:
                breakdown["has_custom_rates"] = False
    
    else:
        # Legacy breakdown
        breakdown = {
            "sale_amount": sale_amount,
            "distribution": [
                {
                    "party": "Agent",
                    "percentage": 2.5,
                    "amount": sale_amount * 0.025,
                    "calculation": f"RM {sale_amount:,.2f} × 2.5%",
                    "description": "Agent gets 2.5% of sale price"
                },
                {
                    "party": "Direct Upline",
                    "percentage": 0.5,  # 20% of 2.5%
                    "amount": sale_amount * 0.025 * 0.20,
                    "calculation": f"RM {sale_amount * 0.025:,.2f} × 20%",
                    "description": "Upline gets 20% of agent's commission"
                }
            ],
            "total_distributed": sale_amount * 0.025 * 1.20,
            "method": "legacy"
        }
    
    return breakdown

def update_agent_commission_structure(agent_id, structure_type, rates=None):
    """
    Update an agent's commission structure
    """
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    
    try:
        if structure_type == "fund_based":
            # Update to fund-based structure
            cursor.execute("""
                UPDATE users 
                SET commission_structure = 'fund_based',
                    total_commission_fund_pct = COALESCE(?, total_commission_fund_pct),
                    agent_fund_pct = COALESCE(?, agent_fund_pct),
                    upline_fund_pct = COALESCE(?, upline_fund_pct),
                    upline2_fund_pct = COALESCE(?, upline2_fund_pct),
                    company_fund_pct = COALESCE(?, company_fund_pct)
                WHERE id = ?
            """, (
                rates.get('total_fund_pct') if rates else None,
                rates.get('agent_pct') if rates else None,
                rates.get('upline_pct') if rates else None,
                rates.get('upline2_pct') if rates else None,
                rates.get('company_pct') if rates else None,
                agent_id
            ))
        else:
            # Update to legacy structure
            cursor.execute("""
                UPDATE users 
                SET commission_structure = 'legacy'
                WHERE id = ?
            """, (agent_id,))
        
        conn.commit()
        success = True
    except Exception as e:
        print(f"Error updating commission structure: {e}")
        success = False
    
    conn.close()
    return success

def migrate_agent_to_fund_based(agent_id, custom_rates=None):
    """
    Migrate an agent from legacy to fund-based commission structure
    """
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    
    # Get current agent rates
    cursor.execute("""
        SELECT agent_commission_rate, upline_commission_rate 
        FROM users WHERE id = ?
    """, (agent_id,))
    
    result = cursor.fetchone()
    
    if result:
        agent_rate, upline_rate = result
        
        # Convert legacy rates to fund-based equivalents
        # Legacy: agent gets 2.5% of sale, upline gets 20% of that
        # Equivalent: agent gets 80% of 2% fund, upline gets 10% of fund
        
        # Keep track of migration
        cursor.execute("""
            UPDATE users 
            SET commission_structure = 'fund_based',
                total_commission_fund_pct = ?,
                agent_fund_pct = ?,
                upline_fund_pct = ?,
                company_fund_pct = 5.00,
                upline2_fund_pct = 5.00
            WHERE id = ?
        """, (
            custom_rates.get('total_fund_pct', 2.00) if custom_rates else 2.00,
            custom_rates.get('agent_pct', 80.00) if custom_rates else 80.00,
            custom_rates.get('upline_pct', 10.00) if custom_rates else 10.00,
            agent_id
        ))
        
        # Log the migration
        cursor.execute("""
            INSERT INTO system_settings (setting_type, setting_key, setting_value)
            VALUES ('migration', 'agent_migration', ?)
        """, (json.dumps({
            'agent_id': agent_id,
            'from_structure': 'legacy',
            'to_structure': 'fund_based',
            'legacy_rates': {'agent': float(agent_rate), 'upline': float(upline_rate)},
            'new_rates': {
                'total_fund_pct': custom_rates.get('total_fund_pct', 2.00) if custom_rates else 2.00,
                'agent_pct': custom_rates.get('agent_pct', 80.00) if custom_rates else 80.00,
                'upline_pct': custom_rates.get('upline_pct', 10.00) if custom_rates else 10.00
            },
            'migrated_at': datetime.now().isoformat()
        }),))
        
        conn.commit()
        success = True
    else:
        success = False
    
    conn.close()
    return success

def update_upline_chain(agent_id, upline_id):
    """Update an agent's upline and automatically set upline2"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get the new upline's upline (for upline2)
    cursor.execute(
        "SELECT upline_id FROM users WHERE id = ? AND role = 'agent'", (upline_id,)
    )
    upline_result = cursor.fetchone()
    upline2_id = upline_result[0] if upline_result and upline_result[0] else None

    conn.close()
    return upline2_id


def format_file_size(size_in_bytes):
    """Format file size to human readable format"""
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    elif size_in_bytes < 1024 * 1024:
        return f"{size_in_bytes / 1024:.1f} KB"
    else:
        return f"{size_in_bytes / (1024 * 1024):.1f} MB"


def can_preview_in_browser(file_type):
    """Check if file can be previewed in browser"""
    previewable_types = ["pdf", "jpg", "jpeg", "png", "gif", "txt"]
    return file_type.lower() in previewable_types


def check_and_notify_incomplete_docs(listing_id, agent_id, customer_name):
    """Check if submission has insufficient documents and notify agent immediately"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Count documents for this listing
    cursor.execute("SELECT COUNT(*) FROM documents WHERE listing_id = ?", (listing_id,))
    doc_count = cursor.fetchone()[0]

    conn.close()

    # Create notification based on document count
    if doc_count == 0:
        create_agent_notification(
            agent_id=agent_id,
            notification_type="incomplete_docs",
            title="🚨 CRITICAL: No Documents Uploaded",
            message=f"Submission #{listing_id} ({customer_name}) has NO documents uploaded. This cannot be submitted.",
            related_id=listing_id,
            related_type="listing",
            priority="urgent",
        )
    elif doc_count == 1:
        create_agent_notification(
            agent_id=agent_id,
            notification_type="incomplete_docs",
            title=" Very Incomplete Documents",
            message=f"Submission #{listing_id} ({customer_name}) has only 1/3 documents. Minimum 3 documents required.",
            related_id=listing_id,
            related_type="listing",
            priority="high",
        )
    elif doc_count == 2:
        create_agent_notification(
            agent_id=agent_id,
            notification_type="incomplete_docs",
            title="📎 Missing Documents",
            message=f"Submission #{listing_id} ({customer_name}) has {doc_count}/3 documents. One more document needed.",
            related_id=listing_id,
            related_type="listing",
            priority="normal",
        )


# ============ DATABASE SETUP ============
def init_database():
    """Create all necessary tables"""
    print("🔧 Starting database initialization...")
    
    conn = None
    try:
        conn = sqlite3.connect("real_estate.db", timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        
        # Users Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT DEFAULT 'agent',
                upline_id INTEGER NULL,
                upline2_id INTEGER NULL,
                agent_commission_rate DECIMAL(5,4) DEFAULT 0.025,
                upline_commission_rate DECIMAL(5,4) DEFAULT 0.20,
                total_commission_fund_pct DECIMAL(5,2) DEFAULT 2.00,
                agent_fund_pct DECIMAL(5,2) DEFAULT 80.00,
                upline_fund_pct DECIMAL(5,2) DEFAULT 10.00,
                upline2_fund_pct DECIMAL(5,2) DEFAULT 5.00,
                company_fund_pct DECIMAL(5,2) DEFAULT 5.00,
                commission_structure TEXT DEFAULT 'legacy',
                total_commission DECIMAL(12,2) DEFAULT 0.00,
                total_sales DECIMAL(12,2) DEFAULT 0.00,
                is_deleted BOOLEAN DEFAULT 0,
                deleted_at TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Property Listings Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS property_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL,
                status TEXT DEFAULT 'draft',
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                customer_phone TEXT,
                property_address TEXT NOT NULL,
                property_type TEXT DEFAULT 'residential',
                sale_price DECIMAL(12,2) NOT NULL,
                closing_date DATE,
                commission_amount DECIMAL(10,2),
                upline_commission_amount DECIMAL(10,2) DEFAULT 0.00,
                net_commission_amount DECIMAL(10,2),
                commission_status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                submitted_at TIMESTAMP NULL,
                approved_at TIMESTAMP NULL,
                approved_by INTEGER NULL,
                notes TEXT,
                metadata TEXT DEFAULT '{}',
                rejection_reason TEXT NULL,
                project_id INTEGER NULL,
                unit_id INTEGER NULL,
                FOREIGN KEY (agent_id) REFERENCES users(id)
            )
        """)
        
        # Commission Distributions Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS commission_distributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                upline_id INTEGER NULL,
                level INTEGER DEFAULT 1,
                sale_price DECIMAL(12,2) NOT NULL,
                agent_commission_rate DECIMAL(5,4) NOT NULL,
                agent_gross_commission DECIMAL(10,2) NOT NULL,
                upline_commission_rate DECIMAL(5,4) DEFAULT 0.00,
                upline_commission DECIMAL(10,2) DEFAULT 0.00,
                agent_net_commission DECIMAL(10,2) NOT NULL,
                distribution_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                payment_status TEXT DEFAULT 'pending',
                paid_date DATE NULL,
                FOREIGN KEY (listing_id) REFERENCES property_listings(id),
                FOREIGN KEY (agent_id) REFERENCES users(id),
                FOREIGN KEY (upline_id) REFERENCES users(id)
            )
        """)
        
        # Commission Calculations Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS commission_calculations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NULL,
                agent_id INTEGER NOT NULL,
                property_type TEXT,
                sale_price DECIMAL(12,2),
                base_rate DECIMAL(5,4),
                commission DECIMAL(10,2),
                calculation_details TEXT,
                calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Documents Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                file_type TEXT,
                file_size INTEGER,
                uploaded_by INTEGER,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending',
                notes TEXT,
                FOREIGN KEY (listing_id) REFERENCES property_listings(id) ON DELETE CASCADE
            )
        """)
        
        # Commission Payments Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS commission_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                commission_amount DECIMAL(10,2),
                payment_status TEXT DEFAULT 'pending',
                payment_date DATE,
                payment_method TEXT,
                transaction_id TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_by INTEGER NULL,
                FOREIGN KEY (listing_id) REFERENCES property_listings(id) ON DELETE CASCADE,
                FOREIGN KEY (agent_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # Projects Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT NOT NULL,
                category TEXT NOT NULL,
                project_type TEXT NOT NULL,
                project_sale_type TEXT,
                location TEXT,
                description TEXT,
                status TEXT DEFAULT 'active',
                commission_rate DECIMAL(5,2),
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Project Units Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                unit_type TEXT NOT NULL,
                square_feet INTEGER,
                base_price DECIMAL(12,2),
                rental_price DECIMAL(12,2),
                commission_rate DECIMAL(5,2),
                quantity INTEGER DEFAULT 1,
                status TEXT DEFAULT 'available',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        
        # System Settings Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_type TEXT NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(setting_type, setting_key)
            )
        """)
        
        # Payment Vouchers Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payment_vouchers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_number TEXT UNIQUE NOT NULL,
                payment_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                amount DECIMAL(10,2) NOT NULL,
                payment_date DATE NOT NULL,
                payment_method TEXT,
                status TEXT DEFAULT 'pending',
                email_sent_at TIMESTAMP,
                email_status TEXT,
                pdf_path TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (payment_id) REFERENCES commission_payments(id) ON DELETE CASCADE,
                FOREIGN KEY (agent_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        # Email Logs Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS email_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_email TEXT NOT NULL,
                recipient_name TEXT,
                subject TEXT NOT NULL,
                email_type TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                sent_at TIMESTAMP,
                error_message TEXT,
                related_id INTEGER,
                related_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Deletion Logs Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deletion_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                deleted_by INTEGER NOT NULL,
                reason TEXT,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (deleted_by) REFERENCES users(id)
            )
        """)
        
        # ============ INITIALIZE DEFAULT SETTINGS ============
        default_settings = [
            ("commission", "default_total_fund_pct", "2.00"),
            ("commission", "default_agent_fund_pct", "80.00"),
            ("commission", "default_upline_fund_pct", "10.00"),
            ("commission", "default_upline2_fund_pct", "5.00"),
            ("commission", "default_company_fund_pct", "5.00"),
            ("payment", "processing_days", "14"),
            ("payment", "min_payout", "100"),
            ("payment", "payout_schedule", "monthly"),
            ("payment", "auto_generate_voucher", "yes"),
            ("payment", "voucher_template", "detailed"),
            ("payment", "voucher_prefix", "PAY"),
            ("payment", "payment_methods", "bank_transfer,check"),
            ("notification", "notifications", "submission_received,submission_approved,payment_processed,reminders"),
            ("notification", "auto_approve_threshold", "0"),
            ("notification", "reminder_days", "3"),
            ("notification", "admin_email", "admin@example.com"),
            ("notification", "system_from_email", "noreply@realestate.com"),
            ("notification", "smtp_server", ""),
            ("notification", "smtp_port", ""),
            ("notification", "smtp_username", ""),
            ("notification", "smtp_password", ""),
            ("notification", "email_footer", "© 2024 Real Estate System. All rights reserved.")
        ]
        
        for setting_type, setting_key, default_value in default_settings:
            cursor.execute(
                """
                INSERT OR IGNORE INTO system_settings (setting_type, setting_key, setting_value)
                VALUES (?, ?, ?)
                """,
                (setting_type, setting_key, default_value),
            )
        
        conn.commit()
        
        # ============ CREATE SAMPLE USERS ============
        print("\n👤 Checking for sample users...")
        
        # Check for specific users BEFORE creating them
        cursor.execute(
            "SELECT email FROM users WHERE email IN ('admin@example.com', 'agent@example.com', 'john_agent@yahoo.com', 'erwin@yahoo.com')"
        )
        existing_emails = [row[0] for row in cursor.fetchall()]
        print(f"📊 Existing sample users: {len(existing_emails)}")
        
        # Only create users that don't exist
        try:
            if "admin@example.com" not in existing_emails:
                print("➕ Creating admin user...")
                from werkzeug.security import generate_password_hash
                
                admin_password = generate_password_hash("admin456***")
                cursor.execute(
                    "INSERT INTO users (email, password, name, role) VALUES (?, ?, ?, ?)",
                    ("admin@example.com", admin_password, "Admin User", "admin"),
                )
                print("   ✅ Admin user created")
            
            if "agent@example.com" not in existing_emails:
                print("➕ Creating agent user...")
                from werkzeug.security import generate_password_hash
                
                agent_password = generate_password_hash("agent123")
                cursor.execute(
                    "INSERT INTO users (email, password, name, role, agent_commission_rate) VALUES (?, ?, ?, ?, ?)",
                    ("agent@example.com", agent_password, "John Agent", "agent", 0.025),
                )
                print("   ✅ Agent user created")
            
            # Get John's ID for upline reference
            cursor.execute("SELECT id FROM users WHERE email = 'agent@example.com'")
            john_result = cursor.fetchone()
            john_id = john_result[0] if john_result else None
            
            if "erwin@yahoo.com" not in existing_emails and john_id:
                print("➕ Creating Erwin user (John's downline)...")
                from werkzeug.security import generate_password_hash
                
                erwin_password = generate_password_hash("erwin123")
                cursor.execute(
                    """INSERT INTO users (email, password, name, role, upline_id, 
                                         agent_commission_rate, upline_commission_rate) 
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "erwin@yahoo.com",
                        erwin_password,
                        "Erwin",
                        "agent",
                        john_id,
                        0.025,
                        0.20,
                    ),
                )
                print("   ✅ Erwin user created as John's downline")
            
            conn.commit()
            print("✅ Sample users created successfully")
            
        except Exception as e:
            print(f" User creation warning: {e}")
            conn.rollback()
        
        # Create uploads folder if it doesn't exist
        if not os.path.exists("uploads"):
            os.makedirs("uploads")
            print("✅ Uploads folder created")
        
        conn.commit()
        print("✅ Database initialized successfully!")
        
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            print("❌ Database is locked by another process")
            print("💡 Please close any programs that might be using the database")
            print("💡 Or wait a few moments and try again")
        else:
            print(f"❌ Database error: {e}")
        raise e
        
    except Exception as e:
        print(f"❌ Critical error: {e}")
        import traceback
        traceback.print_exc()
        raise e
        
    finally:
        if conn:
            conn.close()


def calculate_commission_for_listing(listing_id):
    """Calculate commission for a specific listing"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        # Get listing with agent and upline info
        cursor.execute(
            """
            SELECT pl.sale_price, u.name as agent_name, u.agent_commission_rate,
                   u.upline_id, upline.name as upline_name, u.upline_commission_rate
            FROM property_listings pl
            JOIN users u ON pl.agent_id = u.id
            LEFT JOIN users upline ON u.upline_id = upline.id
            WHERE pl.id = ?
        """,
            (listing_id,),
        )

        result = cursor.fetchone()
        if not result:
            return {"error": "Listing not found"}

        sale_price, agent_name, agent_rate, upline_id, upline_name, upline_rate = result

        # Calculate commissions
        agent_gross = sale_price * agent_rate
        upline_commission = agent_gross * upline_rate if upline_id else 0
        agent_net = agent_gross - upline_commission

        return {
            "sale_price": float(sale_price),
            "agent": {
                "name": agent_name,
                "rate": f"{agent_rate * 100}%",
                "gross_commission": float(agent_gross),
                "net_commission": float(agent_net),
            },
            "upline": (
                {
                    "name": upline_name if upline_id else None,
                    "rate": f"{upline_rate * 100}%" if upline_id else "0%",
                    "commission": float(upline_commission),
                }
                if upline_id
                else None
            ),
        }

    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


def get_agent_commission_summary(agent_id):
    """Get commission summary for an agent"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        # Get agent's own commissions
        cursor.execute(
            """
            SELECT COUNT(*) as total_sales,
                   SUM(sale_price) as total_sales_value,
                   SUM(agent_net_commission) as total_net_commission,
                   SUM(upline_commission) as total_upline_commission
            FROM commission_distributions
            WHERE agent_id = ? AND payment_status = 'paid'
        """,
            (agent_id,),
        )

        agent_stats = cursor.fetchone()

        # Get upline commissions (commissions from downlines)
        cursor.execute(
            """
            SELECT COUNT(*) as downline_sales,
                   SUM(upline_commission) as total_upline_earnings
            FROM commission_distributions
            WHERE upline_id = ? AND payment_status = 'paid'
        """,
            (agent_id,),
        )

        upline_stats = cursor.fetchone()

        return {
            "agent_id": agent_id,
            "own_sales": {
                "count": agent_stats[0] or 0,
                "total_value": float(agent_stats[1] or 0),
                "net_commission": float(agent_stats[2] or 0),
                "upline_paid": float(agent_stats[3] or 0),
            },
            "upline_earnings": {
                "downline_sales_count": upline_stats[0] or 0,
                "total_earnings": float(upline_stats[1] or 0),
            },
        }

    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


def cleanup_tier_data():
    """Clean up tier-related data from the database"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        # Remove tier references from existing commission calculations
        cursor.execute("SELECT id, calculation_details FROM commission_calculations")
        calculations = cursor.fetchall()

        for calc_id, details_json in calculations:
            if details_json:
                try:
                    details = json.loads(details_json)
                    # Remove tier-related fields
                    if "tier_multiplier" in details:
                        del details["tier_multiplier"]
                    if "agent_tier" in details:
                        del details["agent_tier"]

                    # Update the record
                    cursor.execute(
                        """
                        UPDATE commission_calculations 
                        SET calculation_details = ?
                        WHERE id = ?
                    """,
                        (json.dumps(details), calc_id),
                    )
                except:
                    pass  # Skip if JSON is invalid

        print("✅ Cleaned up tier data from commission calculations")
        conn.commit()

    except Exception as e:
        print(f"❌ Error cleaning tier data: {e}")
        conn.rollback()

    conn.close()


def update_database():
    """Update database schema if needed"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        # Check if project_id column exists in property_listings
        cursor.execute("PRAGMA table_info(property_listings)")
        columns = [col[1] for col in cursor.fetchall()]

        if "project_id" not in columns:
            print("🔄 Adding project_id column to property_listings table...")
            cursor.execute(
                "ALTER TABLE property_listings ADD COLUMN project_id INTEGER NULL"
            )
            conn.commit()
            print("✅ project_id column added!")

        if "unit_id" not in columns:
            print("🔄 Adding unit_id column to property_listings table...")
            cursor.execute(
                "ALTER TABLE property_listings ADD COLUMN unit_id INTEGER NULL"
            )
            conn.commit()
            print("✅ unit_id column added!")

        # Check if upline columns exist in users table
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]

        if "upline_id" not in columns:
            print("🔄 Adding upline_id column to users table...")
            cursor.execute("ALTER TABLE users ADD COLUMN upline_id INTEGER NULL")
            conn.commit()
            print("✅ upline_id column added!")

        if "upline_commission_rate" not in columns:
            print("🔄 Adding upline_commission_rate column to users table...")
            cursor.execute(
                "ALTER TABLE users ADD COLUMN upline_commission_rate DECIMAL(5,2) DEFAULT 0.00"
            )
            conn.commit()
            print("✅ upline_commission_rate column added!")

        # ============ REMOVE TIER SYSTEM ============
        # Remove agent_tier from users table
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]

        if "agent_tier" in columns:
            print("🔄 Removing agent_tier column from users table...")

            # Create temporary table without agent_tier
            cursor.execute(
                """
                CREATE TABLE users_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT DEFAULT 'agent',
                    upline_id INTEGER NULL,
                    upline_commission_rate DECIMAL(5,2) DEFAULT 0.00,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (upline_id) REFERENCES users(id)
                )
            """
            )

            # Copy data (excluding agent_tier)
            cursor.execute(
                """
                INSERT INTO users_new (id, email, password, name, role, upline_id, upline_commission_rate, created_at)
                SELECT id, email, password, name, role, upline_id, upline_commission_rate, created_at
                FROM users
            """
            )

            # Drop old table and rename new one
            cursor.execute("DROP TABLE users")
            cursor.execute("ALTER TABLE users_new RENAME TO users")

            print("✅ agent_tier column removed from users table!")

        # Update commission_calculations table to remove tier columns
        cursor.execute("PRAGMA table_info(commission_calculations)")
        columns = [col[1] for col in cursor.fetchall()]

        # Check if agent_tier column exists
        if "agent_tier" in columns:
            print("🔄 Removing tier columns from commission_calculations table...")

            # Create temporary table without tier columns
            cursor.execute(
                """
                CREATE TABLE commission_calculations_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id INTEGER NOT NULL,
                    agent_id INTEGER NOT NULL,
                    property_type TEXT,
                    sale_price DECIMAL(12,2),
                    base_rate DECIMAL(5,4),
                    commission DECIMAL(10,2),
                    calculation_details TEXT,
                    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Copy data (excluding tier columns)
            cursor.execute(
                """
                INSERT INTO commission_calculations_new 
                (id, listing_id, agent_id, property_type, sale_price, base_rate, commission, calculation_details, calculated_at)
                SELECT id, listing_id, agent_id, property_type, sale_price, base_rate, commission, calculation_details, calculated_at
                FROM commission_calculations
            """
            )

            # Drop old table and rename new one
            cursor.execute("DROP TABLE commission_calculations")
            cursor.execute(
                "ALTER TABLE commission_calculations_new RENAME TO commission_calculations"
            )

            print("✅ Tier columns removed from commission_calculations!")

        # Remove tier_multiplier column if it exists
        cursor.execute("PRAGMA table_info(commission_calculations)")
        columns = [col[1] for col in cursor.fetchall()]

        if "tier_multiplier" in columns:
            print("🔄 Removing tier_multiplier column from commission_calculations...")

            # Create another temporary table without tier_multiplier
            cursor.execute(
                """
                CREATE TABLE commission_calculations_final (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id INTEGER NOT NULL,
                    agent_id INTEGER NOT NULL,
                    property_type TEXT,
                    sale_price DECIMAL(12,2),
                    base_rate DECIMAL(5,4),
                    commission DECIMAL(10,2),
                    calculation_details TEXT,
                    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Copy data
            cursor.execute(
                """
                INSERT INTO commission_calculations_final 
                (id, listing_id, agent_id, property_type, sale_price, base_rate, commission, calculation_details, calculated_at)
                SELECT id, listing_id, agent_id, property_type, sale_price, base_rate, commission, calculation_details, calculated_at
                FROM commission_calculations
            """
            )

            # Drop and rename
            cursor.execute("DROP TABLE commission_calculations")
            cursor.execute(
                "ALTER TABLE commission_calculations_final RENAME TO commission_calculations"
            )

            print("✅ tier_multiplier column removed!")

        # Drop project_commissions table (tier-specific commissions)
        cursor.execute("DROP TABLE IF EXISTS project_commissions")
        print("✅ project_commissions table removed!")

        # ============ REMOVE PROPERTY TYPE SYSTEM ============
        # Remove property_type from property_listings table
        cursor.execute("PRAGMA table_info(property_listings)")
        columns = [col[1] for col in cursor.fetchall()]

        if "property_type" in columns:
            print("🔄 Removing property_type column from property_listings table...")

            # Create temporary table without property_type
            cursor.execute(
                """
                CREATE TABLE property_listings_temp (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id INTEGER NOT NULL,
                    status TEXT DEFAULT 'draft',
                    customer_name TEXT NOT NULL,
                    customer_email TEXT NOT NULL,
                    customer_phone TEXT,
                    property_address TEXT NOT NULL,
                    sale_price DECIMAL(12,2) NOT NULL,
                    closing_date DATE,
                    commission_amount DECIMAL(10,2),
                    commission_status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    submitted_at TIMESTAMP NULL,
                    approved_at TIMESTAMP NULL,
                    approved_by INTEGER NULL,
                    notes TEXT,
                    metadata TEXT DEFAULT '{}',
                    rejection_reason TEXT NULL,
                    project_id INTEGER NULL,
                    unit_id INTEGER NULL
                )
            """
            )

            # Copy data (excluding property_type)
            cursor.execute(
                """
                INSERT INTO property_listings_temp 
                (id, agent_id, status, customer_name, customer_email, customer_phone,
                 property_address, sale_price, closing_date, commission_amount,
                 commission_status, created_at, submitted_at, approved_at,
                 approved_by, notes, metadata, rejection_reason, project_id, unit_id)
                SELECT id, agent_id, status, customer_name, customer_email, customer_phone,
                       property_address, sale_price, closing_date, commission_amount,
                       commission_status, created_at, submitted_at, approved_at,
                       approved_by, notes, metadata, rejection_reason, project_id, unit_id
                FROM property_listings
            """
            )

            # Drop old table and rename new one
            cursor.execute("DROP TABLE property_listings")
            cursor.execute(
                "ALTER TABLE property_listings_temp RENAME TO property_listings"
            )

            print("✅ property_type column removed from property_listings table!")

        # Remove property_type from commission_calculations table
        cursor.execute("PRAGMA table_info(commission_calculations)")
        columns = [col[1] for col in cursor.fetchall()]

        if "property_type" in columns:
            print(
                "🔄 Removing property_type column from commission_calculations table..."
            )

            # Create temporary table without property_type
            cursor.execute(
                """
                CREATE TABLE commission_calculations_temp (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id INTEGER NOT NULL,
                    agent_id INTEGER NOT NULL,
                    sale_price DECIMAL(12,2),
                    base_rate DECIMAL(5,4),
                    commission DECIMAL(10,2),
                    calculation_details TEXT,
                    calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Copy data (excluding property_type)
            cursor.execute(
                """
                INSERT INTO commission_calculations_temp 
                (id, listing_id, agent_id, sale_price, base_rate, commission, calculation_details, calculated_at)
                SELECT id, listing_id, agent_id, sale_price, base_rate, commission, calculation_details, calculated_at
                FROM commission_calculations
            """
            )

            # Drop old table and rename new one
            cursor.execute("DROP TABLE commission_calculations")
            cursor.execute(
                "ALTER TABLE commission_calculations_temp RENAME TO commission_calculations"
            )

            print("✅ property_type column removed from commission_calculations table!")

        conn.commit()
        print("✅ Database schema is up to date.")

    except Exception as e:
        print(f"❌ Database update error: {e}")
        import traceback

        traceback.print_exc()
        conn.rollback()

    # ============ CREATE NOTIFICATIONS TABLE ============
    try:
        # Check if notifications table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_notifications'"
        )
        if not cursor.fetchone():
            print("🔄 Creating agent_notifications table...")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id INTEGER NOT NULL,
                    notification_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    related_id INTEGER,
                    related_type TEXT,
                    is_read INTEGER DEFAULT 0,
                    priority TEXT DEFAULT 'normal',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    read_at TIMESTAMP NULL,
                    expires_at TIMESTAMP NULL,
                    FOREIGN KEY (agent_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """
            )

            # Add index for faster queries
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_agent ON agent_notifications(agent_id, is_read)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_expires ON agent_notifications(expires_at)"
            )

            print("✅ agent_notifications table created!")
        else:
            print("✅ agent_notifications table already exists")

        conn.commit()

    except Exception as e:
        print(f"❌ Error creating notifications table: {e}")
        conn.rollback()

    # ============ CREATE EMAIL LOGS TABLE ============
    try:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='email_logs'"
        )
        if not cursor.fetchone():
            print("🔄 Creating email_logs table...")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS email_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient_email TEXT NOT NULL,
                    recipient_name TEXT,
                    subject TEXT NOT NULL,
                    email_type TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    sent_at TIMESTAMP,
                    error_message TEXT,
                    related_id INTEGER,
                    related_type TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
            print("✅ email_logs table created!")

        conn.commit()

    except Exception as e:
        print(f"❌ Error creating email_logs table: {e}")
        conn.rollback()

    # ============ CREATE PAYMENT VOUCHERS TABLE ============
    try:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='payment_vouchers'"
        )
        if not cursor.fetchone():
            print("🔄 Creating payment_vouchers table...")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_vouchers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    voucher_number TEXT UNIQUE NOT NULL,
                    payment_id INTEGER NOT NULL,
                    agent_id INTEGER NOT NULL,
                    amount DECIMAL(10,2) NOT NULL,
                    payment_date DATE NOT NULL,
                    payment_method TEXT,
                    status TEXT DEFAULT 'pending',
                    email_sent_at TIMESTAMP,
                    email_status TEXT,
                    pdf_path TEXT,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (payment_id) REFERENCES commission_payments(id) ON DELETE CASCADE,
                    FOREIGN KEY (agent_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """
            )
            print("✅ payment_vouchers table created!")

        conn.commit()

    except Exception as e:
        print(f"❌ Error creating payment_vouchers table: {e}")
        conn.rollback()

    # ============ CREATE SYSTEM SETTINGS TABLE ============
    try:
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='system_settings'"
        )
        if not cursor.fetchone():
            print("🔄 Creating system_settings table...")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS system_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    setting_type TEXT NOT NULL,
                    setting_key TEXT NOT NULL,
                    setting_value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(setting_type, setting_key)
                )
            """
            )
            print("✅ system_settings table created!")

            # Insert default settings
            default_settings = [
                ("payment", "processing_days", "14"),
                ("payment", "min_payout", "100"),
                ("payment", "payout_schedule", "monthly"),
                ("payment", "auto_generate_voucher", "yes"),
                ("payment", "voucher_template", "detailed"),
                ("payment", "voucher_prefix", "PAY"),
                ("payment", "payment_methods", "bank_transfer,check"),
                (
                    "notification",
                    "notifications",
                    "submission_received,submission_approved,payment_processed,reminders",
                ),
                ("notification", "auto_approve_threshold", "0"),
                ("notification", "reminder_days", "3"),
                ("notification", "admin_email", "admin@example.com"),
                ("notification", "system_from_email", "noreply@realestate.com"),
                ("notification", "smtp_server", ""),
                ("notification", "smtp_port", ""),
                ("notification", "smtp_username", ""),
                ("notification", "smtp_password", ""),
                (
                    "notification",
                    "email_footer",
                    "© 2024 Real Estate System. All rights reserved.",
                ),
            ]

            for setting_type, setting_key, default_value in default_settings:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO system_settings (setting_type, setting_key, setting_value)
                    VALUES (?, ?, ?)
                """,
                    (setting_type, setting_key, default_value),
                )

            print("✅ Default settings added!")

        conn.commit()

    except Exception as e:
        print(f"❌ Error creating system_settings table: {e}")
        conn.rollback()

    # ============ CLEANUP EXPIRED NOTIFICATIONS ============
    try:
        cleanup_expired_notifications()
        print("✅ Cleaned up expired notifications")
    except Exception as e:
        print(f" Error cleaning up notifications: {e}")

    conn.close()
    print("✅ Database initialization complete!")


# ============ HTML TEMPLATES ============
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Login - Real Estate System</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 400px; margin: 50px auto; padding: 20px; }
        .login-box { border: 1px solid #ddd; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        h2 { text-align: center; color: #333; }
        input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #ddd; border-radius: 5px; }
        button { width: 100%; padding: 12px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; }
        button:hover { background: #0056b3; }
        .error { color: red; text-align: center; margin: 10px 0; }
        .test-accounts { margin-top: 20px; padding: 15px; background: #f8f9fa; border-radius: 5px; }
    
        /* ============ SALES/RENTAL SELECTION STYLES ============ */
        .transaction-type-selector {
            display: flex;
            gap: 15px;
            margin-top: 10px;
        }
        .transaction-type-option input {
            display: none;
        }
        .type-card {
            padding: 15px;
            border: 2px solid #ddd;
            border-radius: 8px;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s;
            min-width: 100px;
        }
        .type-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
        }
        .transaction-type-option input:checked + .type-card {
            border-color: #007bff;
            background: #e8f4ff;
        }
        .sales-card {
            border-color: #f8d7da;
            background: #f8d7da20;
        }
        .rental-card {
            border-color: #d1ecf1;
            background: #d1ecf120;
        }
        .sales-card .type-icon { 
            color: #721c24; 
            font-size: 24px;
            margin-bottom: 5px;
        }
        .rental-card .type-icon { 
            color: #0c5460; 
            font-size: 24px;
            margin-bottom: 5px;
        }
        .type-label {
            font-weight: bold;
            margin-bottom: 5px;
            color: #333;
        }
        .type-desc {
            font-size: 12px;
            color: #666;
        }
        </style>
</head>
<body>
    <div class="login-box">
        <h2>🏠 WTP - Real Estate System</h2>
        <form method="POST">
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
        
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
        
        <div class="test-accounts">
            <strong>Test Accounts:</strong><br>
            Admin: admin@xxxxx.com<br>
            Admin will create agent login
        </div>
    </div>
</body>
</html>
"""

# ============ NOTIFICATION FUNCTIONS ============
def create_agent_notification(
    agent_id,
    notification_type,
    title,
    message,
    related_id=None,
    related_type=None,
    priority="normal",
    expires_in_days=7,
):
    """Create a notification for an agent"""
    conn = get_db_connection()
    cursor = conn.cursor()

    expires_at = None
    if expires_in_days:
        from datetime import timedelta

        expires_at = (datetime.now() + timedelta(days=expires_in_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    cursor.execute(
        """
        INSERT INTO agent_notifications 
        (agent_id, notification_type, title, message, related_id, related_type, priority, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            agent_id,
            notification_type,
            title,
            message,
            related_id,
            related_type,
            priority,
            expires_at,
        ),
    )

    conn.commit()
    conn.close()

    return cursor.lastrowid


def get_agent_notifications(agent_id, unread_only=True, limit=20):
    """Get notifications for an agent - WITH DEBUG"""
    print(
        f"DEBUG get_agent_notifications: agent_id={agent_id}, unread_only={unread_only}"
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    # Build query
    query = """
        SELECT * FROM agent_notifications 
        WHERE agent_id = ? AND (expires_at IS NULL OR expires_at > datetime('now'))
    """

    if unread_only:
        query += " AND is_read = 0"

    query += ' ORDER BY CASE priority WHEN "urgent" THEN 1 WHEN "high" THEN 2 WHEN "normal" THEN 3 ELSE 4 END, created_at DESC'

    if limit:
        query += f" LIMIT {limit}"

    print(f"DEBUG SQL: {query}")
    cursor.execute(query, (agent_id,))
    notifications = cursor.fetchall()

    print(f"DEBUG: Found {len(notifications)} notifications")

    conn.close()

    # Format notifications - ENHANCED with time_ago
    formatted_notifications = []
    for notif in notifications:
        # Calculate time_ago
        created_at = notif[9]  # created_at field
        time_ago = (
            get_time_ago(created_at)
            if "get_time_ago" in globals()
            else created_at[:10] if created_at else ""
        )

        formatted_notifications.append(
            {
                "id": notif[0],
                "agent_id": notif[1],
                "type": notif[2],
                "title": notif[3],
                "message": notif[4],
                "related_id": notif[5],
                "related_type": notif[6],
                "is_read": notif[7],
                "priority": notif[8],
                "created_at": notif[9],
                "read_at": notif[10],
                "expires_at": notif[11],
                "time_ago": time_ago,  # ADDED: For display
                "unread": not bool(notif[7]),  # ADDED: For compatibility with frontend
            }
        )

    return formatted_notifications


def get_time_ago(created_at):
    """Convert datetime to 'time ago' string"""
    from datetime import datetime

    if not created_at:
        return "Recently"

    try:
        if isinstance(created_at, str):
            # Try different datetime formats
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(created_at, fmt)
                    break
                except:
                    continue
            else:
                return created_at[:10] if len(created_at) >= 10 else created_at
        else:
            dt = created_at

        now = datetime.now()
        diff = now - dt

        if diff.days > 365:
            years = diff.days // 365
            return f'{years} year{"s" if years > 1 else ""} ago'
        elif diff.days > 30:
            months = diff.days // 30
            return f'{months} month{"s" if months > 1 else ""} ago'
        elif diff.days > 0:
            return f'{diff.days} day{"s" if diff.days > 1 else ""} ago'
        elif diff.seconds > 3600:
            hours = diff.seconds // 3600
            return f'{hours} hour{"s" if hours > 1 else ""} ago'
        elif diff.seconds > 60:
            minutes = diff.seconds // 60
            return f'{minutes} minute{"s" if minutes > 1 else ""} ago'
        else:
            return "Just now"
    except Exception as e:
        print(f"Error calculating time_ago for {created_at}: {e}")
        return created_at[:10] if created_at and len(created_at) >= 10 else "Recently"


def get_unread_notification_count(agent_id):
    """Count unread notifications for an agent - WITH DEBUG"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT COUNT(*) FROM agent_notifications 
        WHERE agent_id = ? AND is_read = 0 
        AND (expires_at IS NULL OR expires_at > datetime('now'))
    """,
        (agent_id,),
    )

    count = cursor.fetchone()[0]
    conn.close()

    print(f"DEBUG get_unread_notification_count: agent_id={agent_id}, count={count}")

    return count


def mark_notification_read(notification_id):
    """Mark a notification as read - WITH DEBUG"""
    print(
        f"🔔 DEBUG mark_notification_read: Starting for notification #{notification_id}"
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    # Check current status BEFORE update
    cursor.execute(
        "SELECT id, agent_id, is_read FROM agent_notifications WHERE id = ?",
        (notification_id,),
    )
    before = cursor.fetchone()

    if before:
        print(
            f"🔔 DEBUG: Before update - ID: {before[0]}, Agent: {before[1]}, Is Read: {before[2]}"
        )
    else:
        print(f"🔔 DEBUG: Notification #{notification_id} not found!")
        conn.close()
        return False

    # Update the notification
    read_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        """
        UPDATE agent_notifications 
        SET is_read = 1, read_at = ?
        WHERE id = ?
    """,
        (read_time, notification_id),
    )

    rows_updated = cursor.rowcount
    print(f"🔔 DEBUG: Rows updated: {rows_updated}")

    # Check status AFTER update
    cursor.execute(
        "SELECT is_read, read_at FROM agent_notifications WHERE id = ?",
        (notification_id,),
    )
    after = cursor.fetchone()

    if after:
        print(f"🔔 DEBUG: After update - Is Read: {after[0]}, Read At: {after[1]}")

    conn.commit()
    print(f"🔔 DEBUG: Changes committed")
    conn.close()

    return rows_updated > 0


def mark_all_notifications_read(agent_id):
    """Mark all notifications as read for an agent"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE agent_notifications 
        SET is_read = 1, read_at = ?
        WHERE agent_id = ? AND is_read = 0
    """,
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), agent_id),
    )

    conn.commit()
    conn.close()


def check_agent_pending_tasks(agent_id):
    """Check for pending tasks and create notifications - ENHANCED VERSION"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Check for incomplete documents in pending submissions
    cursor.execute(
        """
        SELECT pl.id, pl.customer_name, pl.status,
               (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) as doc_count
        FROM property_listings pl
        WHERE pl.agent_id = ? 
          AND pl.status IN ('draft', 'rejected')
          AND (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) < 3
        ORDER BY pl.created_at DESC
    """,
        (agent_id,),
    )

    incomplete_listings = cursor.fetchall()

    # Create notifications for incomplete submissions
    for listing in incomplete_listings:
        listing_id = listing[0]
        customer_name = listing[1]
        status = listing[2]
        doc_count = listing[3]

        # Check if notification already exists
        cursor.execute(
            """
            SELECT id FROM agent_notifications 
            WHERE agent_id = ? AND related_id = ? AND related_type = 'listing' 
            AND is_read = 0 AND notification_type = 'incomplete_docs'
        """,
            (agent_id, listing_id),
        )

        existing = cursor.fetchone()

        if not existing:
            # Determine priority based on document count
            if doc_count == 0:
                priority = "urgent"
                title = "🚨 CRITICAL: No Documents Uploaded"
                message = f"Submission #{listing_id} ({customer_name}) has NO documents uploaded. This cannot be submitted."
            elif doc_count == 1:
                priority = "high"
                title = " Very Incomplete Documents"
                message = f"Submission #{listing_id} ({customer_name}) has only 1/3 documents. Minimum 3 documents required."
            else:
                priority = "normal"
                title = "📎 Missing Documents"
                message = f"Submission #{listing_id} ({customer_name}) has {doc_count}/3 documents. One more document needed."

            create_agent_notification(
                agent_id=agent_id,
                notification_type="incomplete_docs",
                title=title,
                message=message,
                related_id=listing_id,
                related_type="listing",
                priority=priority,
                expires_in_days=7,
            )

    # Check for rejected submissions that need resubmission
    cursor.execute(
        """
        SELECT id, customer_name FROM property_listings 
        WHERE agent_id = ? AND status = 'rejected'
    """,
        (agent_id,),
    )

    rejected_listings = cursor.fetchall()

    for listing in rejected_listings:
        listing_id = listing[0]
        customer_name = listing[1]

        # Check if notification already exists
        cursor.execute(
            """
            SELECT id FROM agent_notifications 
            WHERE agent_id = ? AND related_id = ? AND related_type = 'listing' 
            AND is_read = 0 AND notification_type = 'rejected_submission'
        """,
            (agent_id, listing_id),
        )

        existing = cursor.fetchone()

        if not existing:
            # Create notification
            create_agent_notification(
                agent_id=agent_id,
                notification_type="rejected_submission",
                title="❌ Submission Rejected",
                message=f"Submission #{listing_id} ({customer_name}) was rejected. Please review and resubmit.",
                related_id=listing_id,
                related_type="listing",
                priority="high",
            )

    # Get count of incomplete submissions for dashboard display
    incomplete_count = len(incomplete_listings)

    conn.close()

    return incomplete_count


def cleanup_expired_notifications():
    """Remove expired notifications"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM agent_notifications 
        WHERE expires_at IS NOT NULL AND expires_at < datetime('now')
    """
    )

    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted > 0:
        print(f"🧹 Cleaned up {deleted} expired notifications")

    return deleted


# ============ ROUTES ============
@app.route("/")
def home():
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = sqlite3.connect("real_estate.db")
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            # -------------------------------
            # Store session info
            # -------------------------------
            session["user_id"] = user[0]
            session["user_email"] = user[1]
            session["user_name"] = user[3]
            session["user_role"] = user[4]

            # Make session permanent so PERMANENT_SESSION_LIFETIME is used
            session.permanent = True  # <-- IMPORTANT

            # Redirect based on role
            if user[4] == "admin":
                return redirect("/admin/dashboard")
            else:
                return redirect("/agent/dashboard")
        else:
            return render_template_string(
                LOGIN_TEMPLATE, error="Invalid email or password"
            )

    return render_template_string(LOGIN_TEMPLATE)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/new-listing")
def new_listing():
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get transaction type from URL
    transaction_type = request.args.get("type", "sales")

    print("\n" + "=" * 60)
    print(f"DEBUG: URL parameter 'type' = '{transaction_type}'")

    # Build the SQL query
    if transaction_type == "all":
        sql_query = """
            SELECT p.id, p.project_name, p.category, p.project_type, 
                   p.location, p.description, p.status, p.commission_rate,
                   p.project_sale_type
            FROM projects p
            WHERE p.status = 'active' AND p.is_active = 1
            ORDER BY p.project_name
        """
        params = ()
    else:
        sql_query = """
            SELECT p.id, p.project_name, p.category, p.project_type, 
                   p.location, p.description, p.status, p.commission_rate,
                   p.project_sale_type
            FROM projects p
            WHERE p.status = 'active' AND p.is_active = 1 AND p.project_sale_type = ?
            ORDER BY p.project_name
        """
        params = (transaction_type,)

    cursor.execute(sql_query, params)
    projects_raw = cursor.fetchall()

    projects = []
    for project in projects_raw:
        cursor.execute(
            """
            SELECT id, unit_type, square_feet, base_price, rental_price, 
                   commission_rate, quantity, status
            FROM project_units 
            WHERE project_id = ? AND status = 'available'
            ORDER BY unit_type
        """,
            (project[0],),
        )

        units = cursor.fetchall()

        # Format units data
        unit_list = []
        for unit in units:
            unit_list.append(
                {
                    "id": unit[0],
                    "unit_type": unit[1],
                    "square_feet": unit[2],
                    "base_price": unit[3],
                    "rental_price": unit[4],
                    "commission_rate": unit[5],
                    "quantity": unit[6],
                    "status": unit[7],
                }
            )

        projects.append(
            {
                "id": project[0],
                "project_name": project[1],
                "category": project[2],
                "project_type": project[3],
                "location": project[4],
                "description": project[5],
                "status": project[6],
                "commission_rate": float(project[7]) if project[7] else 0.0,
                "project_sale_type": project[8],
                "units": unit_list,
            }
        )

    conn.close()

    # Use Flask's render_template function instead of render_template_string
    return render_template(
        "agent/new-listing.html",  # Path to template file
        agent_name=session.get("user_name", "Agent"),
        agent_id=session.get("user_id"),
        agent_tier="standard",
        projects=projects,
        transaction_type=transaction_type,
        projects_json=json.dumps(projects),
    )

@app.route("/agent/dashboard")
def agent_dashboard():
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    user_id = session["user_id"]

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # ============ 1. GET BASIC AGENT STATS ============
    cursor.execute(
        """
        SELECT 
            COUNT(*) as total_sales,
            COALESCE(SUM(commission_amount), 0) as total_commission,
            SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) as drafts,
            SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected
        FROM property_listings 
        WHERE agent_id = ?
    """,
        (user_id,),
    )

    stats = cursor.fetchone()
    total_sales = stats[0] if stats else 0
    total_commission = stats[1] if stats and stats[1] else 0
    pending_count = stats[2] if stats else 0
    draft_count = stats[3] if stats else 0
    rejected_count = stats[4] if stats else 0

    # ============ 2. GET UPLINE EARNINGS ============
    cursor.execute(
        """
        SELECT 
            COALESCE(SUM(cp.commission_amount), 0) as upline_earnings,
            COUNT(cp.id) as upline_payments_count
        FROM commission_payments cp
        JOIN property_listings pl ON cp.listing_id = pl.id
        WHERE cp.agent_id = ?
        AND pl.agent_id != ?
        AND cp.payment_status != 'rejected'
    """,
        (user_id, user_id),
    )

    upline_result = cursor.fetchone()
    upline_earnings = upline_result[0] if upline_result else 0
    upline_payments_count = upline_result[1] if upline_result else 0

    # ============ 3. GET PAID COMMISSIONS ============
    cursor.execute(
        """
        SELECT 
            COALESCE(SUM(commission_amount), 0) as total_paid,
            COUNT(*) as total_payments
        FROM commission_payments 
        WHERE agent_id = ? AND payment_status = 'paid'
    """,
        (user_id,),
    )

    paid_result = cursor.fetchone()
    total_paid = paid_result[0] if paid_result else 0
    total_payments = paid_result[1] if paid_result else 0

    # ============ 4. GET UPLINE INFO ============
    cursor.execute(
        """
        SELECT 
            upline.name,
            upline.email,
            users.upline_commission_rate
        FROM users
        LEFT JOIN users upline ON users.upline_id = upline.id
        WHERE users.id = ?
    """,
        (user_id,),
    )

    upline_info_result = cursor.fetchone()
    upline_info = None
    if upline_info_result and upline_info_result[0]:
        upline_info = {
            "name": upline_info_result[0],
            "email": upline_info_result[1],
            "direct_rate": 10,  # ← FIXED: 10% for direct upline in fund-based
            "indirect_rate": 5,  # ← FIXED: 5% for indirect upline in fund-based
            # Note: We removed "commission_rate" and added "direct_rate"/"indirect_rate"
        }

    # ============ 5. GET DOWNLINE AGENTS ============
    cursor.execute(
        """
        SELECT 
            id,
            name,
            email,
            upline_commission_rate,
            created_at
        FROM users 
        WHERE upline_id = ? AND role = 'agent'
        ORDER BY created_at DESC
    """,
        (user_id,),
    )

    downline_rows = cursor.fetchall()
    downline_agents = []
    for row in downline_rows:
        downline_agents.append({
            "id": row[0],
            "name": row[1],
            "email": row[2],
            "direct_rate": 10,  # ← FIXED: You earn 10% as their direct upline
            "indirect_rate": 5,  # ← FIXED: You earn 5% as their indirect upline
            "join_date": row[4][:10] if row[4] else "",
            # Note: We removed "commission_rate" and added "direct_rate"/"indirect_rate"
        })

    # ============ 6. GET RECENT SALES ============
    cursor.execute(
        """
        SELECT 
            pl.id,
            pl.customer_name,
            pl.sale_price,
            pl.commission_amount,
            pl.status,
            pl.created_at,
            COALESCE(p.project_name, '') as project_name
        FROM property_listings pl
        LEFT JOIN projects p ON pl.project_id = p.id
        WHERE pl.agent_id = ?
        ORDER BY pl.created_at DESC 
        LIMIT 10
    """,
        (user_id,),
    )

    recent_sales_rows = cursor.fetchall()
    recent_sales = []
    project_sales_count = 0
    unique_projects = set()
    
    for row in recent_sales_rows:
        project_name = row[6]
        recent_sales.append({
            "id": row[0],
            "customer_name": row[1],
            "sale_price": float(row[2]) if row[2] else 0,
            "commission_amount": float(row[3]) if row[3] else 0,
            "status": row[4],
            "created_at": row[5],
            "project_name": project_name,
        })
        
        if project_name:
            project_sales_count += 1
            unique_projects.add(project_name)

    unique_projects_count = len(unique_projects)

    # ============ 7. GET RECENT PAYMENTS (FIXED VERSION) ============
    recent_payments = []  # Initialize empty list

    try:
        # Get agent's own paid commissions (UNCHANGED)
        cursor.execute(
            """
            SELECT 
                cp.payment_date,
                cp.commission_amount,
                'Own' as payment_type,
                cp.payment_status,
                COALESCE(cp.transaction_id, 'N/A') as transaction_id,
                COALESCE(p.project_name, '') as project_name,
                cp.created_at
            FROM commission_payments cp
            LEFT JOIN property_listings pl ON cp.listing_id = pl.id
            LEFT JOIN projects p ON pl.project_id = p.id
            WHERE cp.agent_id = ? AND cp.payment_status = 'paid'
            ORDER BY cp.payment_date DESC
            LIMIT 5
            """,
            (user_id,),
        )
        
        own_payments = cursor.fetchall()
        
        # === FIXED UPLINE PAYMENTS QUERY ===
        cursor.execute(
            """
            SELECT 
                uc.paid_at as payment_date,
                uc.amount as commission_amount,
                'Upline' as payment_type,
                uc.status as payment_status,
                COALESCE(uc.transaction_id, 'N/A') as transaction_id,
                COALESCE(p.project_name, '') as project_name,
                uc.created_at,
                COALESCE(selling_agent.name, '') as selling_agent_name,
                selling_agent.upline_id
            FROM upline_commissions uc
            LEFT JOIN property_listings pl ON uc.listing_id = pl.id
            LEFT JOIN projects p ON pl.project_id = p.id
            LEFT JOIN users selling_agent ON pl.agent_id = selling_agent.id
            WHERE uc.upline_id = ? AND uc.status = 'paid'
            ORDER BY uc.paid_at DESC
            LIMIT 5
            """,
            (user_id,),
        )
        
        upline_payments = cursor.fetchall()
        
        # Combine both lists
        all_payments = []
        
        # Process own payments (UNCHANGED)
        for row in own_payments:
            all_payments.append({
                "payment_date": row[0],
                "commission_amount": float(row[1]) if row[1] else 0,
                "payment_type": row[2],
                "payment_status": row[3],
                "transaction_id": row[4] if row[4] != 'N/A' else None,
                "project_name": row[5] if row[5] else None,
                "created_at": row[6],
                "is_upline_payment": False,
                "selling_agent_name": None,
                "is_direct_upline": False  # Own payments are never direct upline
            })
        
        # Process upline payments (UPDATED)
        for row in upline_payments:
            # Check if this is a direct upline payment by comparing IDs
            selling_agent_upline_id = row[8] if len(row) > 8 else None
            is_direct = (selling_agent_upline_id == user_id) if selling_agent_upline_id else False
            
            all_payments.append({
                "payment_date": row[0],
                "commission_amount": float(row[1]) if row[1] else 0,
                "payment_type": row[2],
                "payment_status": row[3],
                "transaction_id": row[4] if row[4] != 'N/A' else None,
                "project_name": row[5] if row[5] else None,
                "created_at": row[6],
                "is_upline_payment": True,
                "selling_agent_name": row[7] if row[7] else None,
                "is_direct_upline": is_direct  # Calculated from upline_id comparison
            })
        
        # Sort by payment_date (most recent first)
        all_payments.sort(key=lambda x: x["payment_date"] or "", reverse=True)
        
        # Take only top 10
        recent_payments = all_payments[:10]
        
    except Exception as e:
        print(f"Error in recent payments query: {e}")
        # Keep recent_payments as empty list if query fails

    # ============ 8. GET NOTIFICATIONS ============
    notifications = []
    unread_count = 0

    # ============ 9. GET INCOMPLETE SUBMISSIONS ============
    cursor.execute(
        """
        SELECT 
            id,
            customer_name,
            property_address,
            status,
            created_at
        FROM property_listings 
        WHERE agent_id = ? AND (status = 'draft' OR status IS NULL)
        ORDER BY created_at DESC
        LIMIT 5
    """,
        (user_id,),
    )

    incomplete_rows = cursor.fetchall()
    incomplete_submissions = []
    
    for row in incomplete_rows:
        incomplete_submissions.append({
            "id": row[0],
            "customer_name": row[1],
            "property_address": row[2],
            "status": row[3],
            "created_at": row[4],
        })

    incomplete_count = len(incomplete_submissions)

    conn.close()

    # ============ 10. CALCULATE ADDITIONAL STATS ============
    downline_stats = {
        "count": len(downline_agents),
        "avg_direct_rate": 10.0 if downline_agents else 0.0,  # ← ADDED
        "avg_indirect_rate": 5.0 if downline_agents else 0.0,  # ← ADDED
        "total_commission_rate": 0,  # ← Not used anymore
        "upline_earnings": upline_earnings,
        "upline_payments_count": upline_payments_count,
    }

    # ============ 11. RENDER TEMPLATE ============
    return render_template(
        "agent/dashboard.html",
        user_name=session.get("user_name", "Agent"),
        total_sales=total_sales,
        total_commission=total_commission,  # Already formatted earlier if needed
        pending_count=pending_count,
        draft_count=draft_count,
        rejected_count=rejected_count,
        recent_sales=recent_sales,
        recent_payments=recent_payments,
        project_sales_count=project_sales_count,
        unique_projects_count=unique_projects_count,
        upline_info=upline_info,
        downline_agents=downline_agents,
        downline_stats=downline_stats,
        notifications=notifications,
        unread_count=unread_count,
        incomplete_submissions=incomplete_submissions,
        incomplete_count=incomplete_count,
        upline_earnings=upline_earnings,  # PASS THE NUMBER, NOT FORMATTED STRING
        upline_payments_count=upline_payments_count,
        total_paid=total_paid,  # PASS THE NUMBER, NOT FORMATTED STRING
        total_payments=total_payments,
        agent_commission_rate=80,
    )

@app.route("/agent/my-downline")
def agent_downline():
    """Agent view of their downline network - FIXED PENDING COMMISSIONS"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    agent_id = session["user_id"]

    # ========== GET CURRENT AGENT'S COMMISSION STRUCTURE ==========
    cursor.execute(
        """
        SELECT 
            commission_structure,
            upline_fund_pct,
            upline2_fund_pct,
            total_commission_fund_pct
        FROM users 
        WHERE id = ?
    """,
        (agent_id,),
    )
    
    agent_info = cursor.fetchone()
    commission_structure = agent_info[0] if agent_info else 'fund_based'
    
    if commission_structure == 'fund_based':
        direct_rate = agent_info[1] if agent_info and agent_info[1] is not None else 10.0
        indirect_rate = agent_info[2] if agent_info and agent_info[2] is not None else 5.0
        total_fund_pct = agent_info[3] if agent_info and agent_info[3] is not None else 2.0
    else:
        direct_rate = 5.0
        indirect_rate = 2.5
        total_fund_pct = None

    # ========== GET DOWNLINES ==========
    # Direct downlines
    cursor.execute(
        """
        SELECT id, name, email, created_at, commission_structure
        FROM users 
        WHERE upline_id = ? AND role = 'agent'
        ORDER BY created_at DESC
    """,
        (agent_id,),
    )
    direct_downlines = cursor.fetchall()

    # Indirect downlines
    cursor.execute(
        """
        SELECT 
            u2.id,
            u2.name,
            u2.email,
            u2.created_at,
            u2.commission_structure,
            u1.name as direct_upline_name
        FROM users u1
        JOIN users u2 ON u1.id = u2.upline_id
        WHERE u1.upline_id = ? 
        AND u2.role = 'agent'
        AND u1.role = 'agent'
        ORDER BY u2.created_at DESC
    """,
        (agent_id,),
    )
    indirect_downlines = cursor.fetchall()

    # ========== COMMISSION CALCULATION ==========
    # First check what tables exist
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    
    total_direct_earnings = 0
    total_indirect_earnings = 0
    total_direct_pending = 0
    total_indirect_pending = 0
    
    direct_downline_list = []
    indirect_downline_list = []

    # Process direct downlines
    for agent in direct_downlines:
        agent_id_val = agent[0]
        
        # Calculate EARNED commissions from upline_commissions (status = 'paid' or 'approved')
        earned = 0
        earned_count = 0
        if 'upline_commissions' in tables:
            cursor.execute(
                """
                SELECT SUM(amount), COUNT(*)
                FROM upline_commissions 
                WHERE upline_id = ? 
                AND agent_id = ? 
                AND commission_type = 'direct'
                AND status IN ('paid', 'approved', 'completed')
                """,
                (agent_id, agent_id_val)
            )
            result = cursor.fetchone()
            earned = result[0] or 0
            earned_count = result[1] or 0
        
        # Calculate PENDING commissions from upline_commissions (status = 'pending')
        pending = 0
        pending_count = 0
        if 'upline_commissions' in tables:
            cursor.execute(
                """
                SELECT SUM(amount), COUNT(*)
                FROM upline_commissions 
                WHERE upline_id = ? 
                AND agent_id = ? 
                AND commission_type = 'direct'
                AND status = 'pending'
                """,
                (agent_id, agent_id_val)
            )
            result = cursor.fetchone()
            pending = result[0] or 0
            pending_count = result[1] or 0
        
        # If no results in upline_commissions, check property_listings as fallback
        if pending == 0 and 'property_listings' in tables:
            cursor.execute(
                """
                SELECT SUM(commission_amount), COUNT(*)
                FROM property_listings 
                WHERE agent_id = ? AND status IN ('sold', 'pending') 
                AND (commission_status IS NULL OR commission_status IN ('pending', 'unpaid'))
                """,
                (agent_id_val,)
            )
            result = cursor.fetchone()
            pending_total = result[0] or 0
            pending_count = result[1] or 0
            pending = pending_total * direct_rate / 100
        
        direct_downline_list.append({
            "id": agent_id_val,
            "name": agent[1],
            "email": agent[2],
            "commission_rate": direct_rate,
            "join_date": agent[3][:10] if agent[3] else "",
            "commission_percentage": f"{direct_rate}%",
            "relationship": "direct",
            "earned_from_agent": earned,
            "earned_count": earned_count,
            "pending_from_agent": pending,
            "pending_count": pending_count,
            "commission_structure": agent[4] if len(agent) > 4 else 'fund_based',
        })
        
        total_direct_earnings += earned
        total_direct_pending += pending

    # Process indirect downlines
    for agent in indirect_downlines:
        agent_id_val = agent[0]
        
        # Calculate EARNED commissions
        earned = 0
        earned_count = 0
        if 'upline_commissions' in tables:
            cursor.execute(
                """
                SELECT SUM(amount), COUNT(*)
                FROM upline_commissions 
                WHERE upline_id = ? 
                AND agent_id = ? 
                AND commission_type = 'indirect'
                AND status IN ('paid', 'approved', 'completed')
                """,
                (agent_id, agent_id_val)
            )
            result = cursor.fetchone()
            earned = result[0] or 0
            earned_count = result[1] or 0
        
        # Calculate PENDING commissions
        pending = 0
        pending_count = 0
        if 'upline_commissions' in tables:
            cursor.execute(
                """
                SELECT SUM(amount), COUNT(*)
                FROM upline_commissions 
                WHERE upline_id = ? 
                AND agent_id = ? 
                AND commission_type = 'indirect'
                AND status = 'pending'
                """,
                (agent_id, agent_id_val)
            )
            result = cursor.fetchone()
            pending = result[0] or 0
            pending_count = result[1] or 0
        
        # Fallback to property_listings
        if pending == 0 and 'property_listings' in tables:
            cursor.execute(
                """
                SELECT SUM(commission_amount), COUNT(*)
                FROM property_listings 
                WHERE agent_id = ? AND status IN ('sold', 'pending') 
                AND (commission_status IS NULL OR commission_status IN ('pending', 'unpaid'))
                """,
                (agent_id_val,)
            )
            result = cursor.fetchone()
            pending_total = result[0] or 0
            pending_count = result[1] or 0
            pending = pending_total * indirect_rate / 100
        
        indirect_downline_list.append({
            "id": agent_id_val,
            "name": agent[1],
            "email": agent[2],
            "commission_rate": indirect_rate,
            "join_date": agent[3][:10] if agent[3] else "",
            "commission_percentage": f"{indirect_rate}%",
            "relationship": "indirect",
            "direct_upline_name": agent[5] if len(agent) > 5 else "Direct Upline",
            "earned_from_agent": earned,
            "earned_count": earned_count,
            "pending_from_agent": pending,
            "pending_count": pending_count,
            "commission_structure": agent[4] if len(agent) > 4 else 'fund_based',
        })
        
        total_indirect_earnings += earned
        total_indirect_pending += pending

    # Debug logging
    print(f"DEBUG: Total direct downlines: {len(direct_downline_list)}")
    print(f"DEBUG: Total direct earnings: {total_direct_earnings}")
    print(f"DEBUG: Total direct pending: {total_direct_pending}")
    print(f"DEBUG: Total indirect downlines: {len(indirect_downline_list)}")
    print(f"DEBUG: Total indirect earnings: {total_indirect_earnings}")
    print(f"DEBUG: Total indirect pending: {total_indirect_pending}")
    
    # Stats
    total_pending = total_direct_pending + total_indirect_pending
    total_earnings = total_direct_earnings + total_indirect_earnings
    
    stats_dict = {
        "total_downline": len(direct_downline_list) + len(indirect_downline_list),
        "direct_downline_count": len(direct_downline_list),
        "indirect_downline_count": len(indirect_downline_list),
        "total_direct_earnings": total_direct_earnings,
        "total_indirect_earnings": total_indirect_earnings,
        "total_direct_pending": total_direct_pending,
        "total_indirect_pending": total_indirect_pending,
        "total_your_earnings": total_earnings,
        "total_your_pending": total_pending,
        "commission_structure": commission_structure,
        "direct_rate": direct_rate,
        "indirect_rate": indirect_rate,
        "total_fund_pct": total_fund_pct,
    }

    conn.close()
    
    return render_template(
        "agent/downline.html",
        direct_downline_agents=direct_downline_list,
        indirect_downline_agents=indirect_downline_list,
        stats=stats_dict,
    )

@app.route("/agent/downline-performance/<int:agent_id>")
def agent_downline_performance(agent_id):
    """Agent view of a specific downline agent's performance"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    # Verify this agent is actually in the current user's downline
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    cursor.execute("SELECT upline_id FROM users WHERE id = ?", (agent_id,))
    result = cursor.fetchone()

    if not result or result[0] != session["user_id"]:
        conn.close()
        return "Access denied - This agent is not in your downline", 403

    # Get downline agent details with commission structure
    cursor.execute("""
        SELECT name, email, upline_id, upline2_id, 
               total_commission_fund_pct, agent_fund_pct,
               upline_fund_pct, upline2_fund_pct, company_fund_pct,
               commission_structure, created_at 
        FROM users WHERE id = ?
    """, (agent_id,))
    agent_info = cursor.fetchone()

    # Get approved listings with sale price
    sql = """SELECT 
    COUNT(*) as total_listings,
    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved_listings,
    SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected_listings,
    SUM(sale_price) as total_sales,
    SUM(CASE WHEN status = 'approved' THEN sale_price ELSE 0 END) as approved_sales,
    AVG(sale_price) as avg_sale_price
    FROM property_listings 
    WHERE agent_id = ? AND status IN ('approved', 'rejected', 'pending')"""
    
    cursor.execute(sql, (agent_id,))
    performance = cursor.fetchone()
    
    # Get commission calculations ONLY for listings where agent is the selling agent
    commission_sql = """
    SELECT 
        SUM(pl.commission_amount) as total_agent_commission,
        AVG(pl.commission_amount) as avg_agent_commission,
        COUNT(pl.id) as total_listings
    FROM property_listings pl
    WHERE pl.agent_id = ? AND pl.status = 'approved'
    """
    cursor.execute(commission_sql, (agent_id,))
    commission_data = cursor.fetchone()
    
    # Get monthly performance with correct commission calculation
    monthly_sql = """
    SELECT 
        strftime('%Y-%m', pl.created_at) as month,
        COUNT(pl.id) as listings,
        SUM(pl.sale_price) as sales_value,
        SUM(CASE WHEN pl.status = 'approved' THEN pl.sale_price ELSE 0 END) as approved_sales,
        SUM(CASE WHEN pl.status = 'approved' THEN pl.commission_amount ELSE 0 END) as agent_commission,
        SUM(CASE WHEN pl.status = 'approved' THEN 1 ELSE 0 END) as approved_count
    FROM property_listings pl
    WHERE pl.agent_id = ? 
    GROUP BY strftime('%Y-%m', pl.created_at)
    ORDER BY month DESC
    """
    cursor.execute(monthly_sql, (agent_id,))
    monthly_raw = cursor.fetchall()
    
    conn.close()

    # Process agent data
    if agent_info:
        agent_data = {
            "id": agent_id,
            "name": agent_info[0],
            "email": agent_info[1],
            "upline_id": agent_info[2],
            "upline2_id": agent_info[3],
            "total_fund_pct": float(agent_info[4]) if agent_info[4] else 2.0,
            "agent_fund_pct": float(agent_info[5]) if agent_info[5] else 80.0,
            "upline_fund_pct": float(agent_info[6]) if agent_info[6] else 10.0,
            "upline2_fund_pct": float(agent_info[7]) if agent_info[7] else 5.0,
            "company_fund_pct": float(agent_info[8]) if agent_info[8] else 5.0,
            "commission_structure": agent_info[9] or "fund_based",
            "created_at": agent_info[10][:10] if agent_info[10] else "",
            "commission_percentage": f"{float(agent_info[6]) if agent_info[6] else 10.0}%",
        }
    else:
        agent_data = None

    # Process performance data
    if performance:
        perf_data = {
            "total_listings": performance[0] or 0,
            "approved_listings": performance[1] or 0,
            "rejected_listings": performance[2] or 0,
            "total_sales": float(performance[3] or 0),
            "approved_sales": float(performance[4] or 0),
            "avg_sale_price": float(performance[5] or 0),
        }
    else:
        perf_data = None
    
    # Process commission data
    if commission_data:
        perf_data["total_commission"] = float(commission_data[0] or 0)
        perf_data["avg_commission"] = float(commission_data[1] or 0)
        perf_data["total_calculations"] = commission_data[2] or 0
    elif perf_data:
        perf_data["total_commission"] = 0
        perf_data["avg_commission"] = 0
        perf_data["total_calculations"] = 0

    # Calculate conversion rates
    if perf_data and perf_data["total_listings"] > 0:
        approval_rate = (
            perf_data["approved_listings"] / perf_data["total_listings"]
        ) * 100
        rejection_rate = (
            perf_data["rejected_listings"] / perf_data["total_listings"]
        ) * 100
    else:
        approval_rate = 0
        rejection_rate = 0

    # CORRECT FUND-BASED CALCULATION
    funds = []
    total_fund_allocation = 0
    agent_gets = 0
    your_earnings = 0  # Current user's earnings as upline
    upline_commission_amount = 0
    
    if perf_data and perf_data["approved_sales"] > 0 and agent_data:
        # Get the correct fund percentages from agent data
        total_fund_pct = agent_data["total_fund_pct"]  # Usually 2%
        agent_fund_pct = agent_data["agent_fund_pct"]  # Usually 80%
        upline_fund_pct = agent_data["upline_fund_pct"]  # Usually 10%
        upline2_fund_pct = agent_data["upline2_fund_pct"]  # Usually 5%
        company_fund_pct = agent_data["company_fund_pct"]  # Usually 5%
        
        # Calculate based on APPROVED sales only (according to your function)
        approved_sales = perf_data["approved_sales"]
        
        # Step 1: Total commission fund (2% of approved sales)
        total_commission_fund = approved_sales * (total_fund_pct / 100)
        
        # Step 2: Distribute according to fund percentages
        # Agent gets their share from the fund
        agent_from_fund = total_commission_fund * (agent_fund_pct / 100)
        
        # Current user (as direct upline) gets their share
        if agent_data["upline_id"] == session["user_id"]:
            your_share_from_fund = total_commission_fund * (upline_fund_pct / 100)
            your_earnings = your_share_from_fund
            upline_commission_amount = your_share_from_fund
        
        # Check if current user is indirect upline (upline2)
        elif agent_data["upline2_id"] == session["user_id"]:
            your_share_from_fund = total_commission_fund * (upline2_fund_pct / 100)
            your_earnings = your_share_from_fund
        
        # Company gets balance
        company_from_fund = total_commission_fund * (company_fund_pct / 100)
        
        # What agent actually gets (their share from fund)
        agent_gets = agent_from_fund
        
        # Fund breakdown for display
        funds = [
            {"fund_name": "Total Commission Fund", "percentage": total_fund_pct, "amount": total_commission_fund},
            {"fund_name": "Agent Fund", "percentage": agent_fund_pct, "amount": agent_from_fund},
            {"fund_name": "Direct Upline Fund", "percentage": upline_fund_pct, "amount": total_commission_fund * (upline_fund_pct / 100)},
            {"fund_name": "Indirect Upline Fund", "percentage": upline2_fund_pct, "amount": total_commission_fund * (upline2_fund_pct / 100)},
            {"fund_name": "Company Fund", "percentage": company_fund_pct, "amount": company_from_fund}
        ]
    
    # Process monthly data with correct fund calculation
    monthly_data = []
    for month in monthly_raw:
        month_sales = float(month[3] or 0)  # Approved sales for the month
        agent_commission = float(month[4] or 0)  # This is still position 4
        # month[5] is now approved_count instead of commission_count
        
        if month_sales > 0 and agent_data:
            # Calculate monthly fund
            total_fund_pct = agent_data["total_fund_pct"]
            total_commission_fund = month_sales * (total_fund_pct / 100)
            
            # Calculate your share based on relationship
            if agent_data["upline_id"] == session["user_id"]:
                upline_fund_pct = agent_data["upline_fund_pct"]
                your_share_month = total_commission_fund * (upline_fund_pct / 100)
            elif agent_data["upline2_id"] == session["user_id"]:
                upline2_fund_pct = agent_data["upline2_fund_pct"]
                your_share_month = total_commission_fund * (upline2_fund_pct / 100)
            else:
                your_share_month = 0
        else:
            your_share_month = 0
        
        monthly_data.append({
            "month": month[0],
            "listings": month[1] or 0,
            "sales_value": float(month[2] or 0),
            "approved_sales": month_sales,
            "commission": agent_commission,
            "your_share": your_share_month
        })

    return render_template(
        "agent/downline-performance.html",
        agent=agent_data,
        performance=perf_data,
        your_earnings=your_earnings,
        approval_rate=approval_rate,
        rejection_rate=rejection_rate,
        funds=funds,
        upline_commission_amount=upline_commission_amount,
        agent_gets=agent_gets,
        monthly_data=monthly_data
    )


# ============ NOTIFICATION MANAGEMENT ROUTES ============
@app.route("/agent/mark-notification-read/<int:notification_id>")
def mark_notification_read_route(notification_id):
    """Mark a notification as read"""
    if "user_id" not in session:
        return redirect("/login")

    mark_notification_read(notification_id)
    return redirect("/agent/dashboard")


@app.route("/agent/mark-all-read")
def mark_all_notifications_read_route():
    """Mark all notifications as read"""
    if "user_id" not in session:
        return redirect("/login")

    mark_all_notifications_read(session["user_id"])
    return redirect("/agent/dashboard")


@app.route("/agent/notifications")
def agent_notifications_page():
    """Agent notifications page"""
    if "user_id" not in session:
        return redirect("/login")

    # Direct database query
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT id, title, message, created_at, is_read, 
                   COALESCE(notification_type, 'system') as type
            FROM agent_notifications 
            WHERE agent_id = ? 
            ORDER BY created_at DESC
            LIMIT 50
        """,
            (session["user_id"],),
        )
    except sqlite3.OperationalError as e:
        print(f"Query error: {e}")
        # Fallback query
        cursor.execute(
            """
            SELECT id, title, message, created_at, is_read
            FROM agent_notifications 
            WHERE agent_id = ? 
            ORDER BY created_at DESC
            LIMIT 50
        """,
            (session["user_id"],),
        )

    rows = cursor.fetchall()
    conn.close()

    # Convert to list of dictionaries
    notifications = []
    for row in rows:
        notification = {
            "id": row[0],
            "title": row[1],
            "message": row[2],
            "created_at": row[3],
            "is_read": bool(row[4]),
        }
        if len(row) > 5:
            notification["type"] = row[5]
        else:
            notification["type"] = "system"
        notifications.append(notification)

    # DEBUG: Print what we found
    print(
        f"📢 DEBUG: Found {len(notifications)} notifications for agent {session['user_id']}"
    )

    # DEFINE THE TEMPLATE HERE (it was missing!)
    notification_template = """<!DOCTYPE html>
<html>
<head>
    <title>My Notifications</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .notifications-container { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .notification-item { padding: 15px; margin-bottom: 10px; border-radius: 8px; border: 1px solid #e0e0e0; }
        .notification-item.read { background: #f8f9fa; opacity: 0.7; }
        .notification-header { display: flex; justify-content: space-between; margin-bottom: 10px; }
        .notification-title { font-weight: bold; color: #333; }
        .notification-date { color: #666; font-size: 12px; }
        .notification-message { color: #444; line-height: 1.5; }
        .notification-type { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-right: 8px; }
        .type-system { background: #e3f2fd; color: #1565c0; }
        .type-payment { background: #d4edda; color: #155724; }
        .type-commission_paid { background: #d4edda; color: #155724; }  /* ADDED THIS */
        .type-listing { background: #fff3cd; color: #856404; }
        .type-listing_approved { background: #c3e6cb; color: #155724; }
        .type-submission_success { background: #d1ecf1; color: #0c5460; }
        .btn { padding: 8px 16px; border-radius: 5px; text-decoration: none; display: inline-block; }
        .btn-back { background: #6c757d; color: white; }
        .btn-mark-read { background: #17a2b8; color: white; font-size: 12px; padding: 4px 8px; }
        .empty-state { text-align: center; padding: 50px 20px; color: #666; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🔔 My Notifications</h1>
        <div style="margin-top: 15px;">
            <a href="/agent/dashboard" class="btn btn-back">← Back to Dashboard</a>
            <a href="/agent/mark-all-read" class="btn" style="background: #28a745; color: white; margin-left: 10px;">✓ Mark All as Read</a>
        </div>
    </div>
    
    <div class="notifications-container">
        <h3>📋 Recent Notifications</h3>
        
        {% if notifications %}
            {% for notification in notifications %}
            <div class="notification-item {% if notification.is_read %}read{% endif %}">
                <div class="notification-header">
                    <div>
                        <!-- FIXED TYPE DISPLAY -->
                        <span class="notification-type type-{{ notification.type }}">{{ notification.type.replace('_', ' ').title() }}</span>
                        <span class="notification-title">{{ notification.title }}</span>
                    </div>
                    <div class="notification-date">{{ notification.created_at[:19] if notification.created_at else '' }}</div>
                </div>
                <div class="notification-message">{{ notification.message }}</div>
                {% if not notification.is_read %}
                <div style="margin-top: 10px; text-align: right;">
                    <a href="/agent/mark-notification-read/{{ notification.id }}" class="btn-mark-read">Mark as Read</a>
                </div>
                {% endif %}
            </div>
            {% endfor %}
        {% else %}
            <div class="empty-state">
                <h3>No notifications yet</h3>
                <p>You don't have any notifications at the moment.</p>
            </div>
        {% endif %}
    </div>
    
    <div style="margin-top: 20px; text-align: center;">
        <a href="/agent/dashboard" class="btn btn-back">← Back to Dashboard</a>
    </div>
</body>
</html>"""

    return render_template_string(notification_template, notifications=notifications)


# Add this temporary debug route to your app
@app.route("/debug/table-structure")
def debug_table_structure():
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Check agent_notifications table
    cursor.execute("PRAGMA table_info(agent_notifications)")
    columns = cursor.fetchall()

    result = "<h1>agent_notifications Table Structure</h1>"
    for col in columns:
        result += f"<p>Column {col[0]}: {col[1]} (Type: {col[2]})</p>"

    # Also check what notification types exist
    cursor.execute("SELECT DISTINCT notification_type FROM agent_notifications")
    types = cursor.fetchall()

    result += "<h2>Existing Notification Types:</h2>"
    for t in types:
        result += f"<p>{t[0]}</p>"

    conn.close()
    return result


# ============ BELL NOTIFICATION API ENDPOINTS ============


@app.route("/api/agent/notifications")
def api_get_agent_notifications():
    """API endpoint for bell notifications (returns JSON)"""
    if "user_id" not in session or session["user_role"] != "agent":
        return jsonify({"error": "Not authenticated"}), 401

    agent_id = session["user_id"]

    # Get notifications using your existing function
    notifications = get_agent_notifications(agent_id, unread_only=False, limit=10)

    # Get unread count using your existing function
    unread_count = get_unread_notification_count(agent_id)

    return jsonify({"notifications": notifications, "unread_count": unread_count})


@app.route("/api/agent/notifications/<int:notification_id>/read", methods=["POST"])
def api_mark_notification_read(notification_id):
    """API endpoint to mark notification as read"""
    if "user_id" not in session or session["user_role"] != "agent":
        return jsonify({"error": "Not authenticated"}), 401

    # Use your existing database function
    mark_notification_read(notification_id)

    return jsonify({"success": True})


@app.route("/api/agent/notifications/mark-all-read", methods=["POST"])
def api_mark_all_notifications_read():
    """API endpoint to mark all notifications as read"""
    if "user_id" not in session or session["user_role"] != "agent":
        return jsonify({"error": "Not authenticated"}), 401

    agent_id = session["user_id"]

    # Use your existing database function
    mark_all_notifications_read(agent_id)

    return jsonify({"success": True})


@app.route("/debug-notification/<int:notification_id>")
def debug_notification(notification_id):
    """Debug a specific notification"""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, agent_id, title, is_read, read_at, expires_at, created_at
        FROM agent_notifications WHERE id = ?
    """,
        (notification_id,),
    )

    notif = cursor.fetchone()
    conn.close()

    if notif:
        return f"""
        <h3>Notification #{notif[0]} Details:</h3>
        <pre>
        Agent ID: {notif[1]}
        Title: {notif[2]}
        Is Read: {notif[3]} (1 = read, 0 = unread)
        Read At: {notif[4]}
        Expires At: {notif[5]}
        Created At: {notif[6]}
        </pre>
        <a href="/agent/dashboard">Back to Dashboard</a>
        """
    else:
        return "Notification not found"


@app.route("/debug-notification-status")
def debug_notification_status():
    """Debug notification status"""
    if "user_id" not in session:
        return redirect("/login")

    agent_id = session["user_id"]

    # Get counts
    total_count = len(get_agent_notifications(agent_id, unread_only=False, limit=100))
    unread_count = get_unread_notification_count(agent_id)
    read_count = total_count - unread_count

    # Get sample notifications
    notifications = get_agent_notifications(agent_id, unread_only=False, limit=5)

    html = f"""
    <h3>🔍 Notification Debug</h3>
    <p>Agent ID: {agent_id}</p>
    <p>Total Notifications: {total_count}</p>
    <p>Unread Notifications: {unread_count}</p>
    <p>Read Notifications: {read_count}</p>
    
    <h4>Sample Notifications (5):</h4>
    <table border="1" cellpadding="5">
        <tr>
            <th>ID</th>
            <th>Title</th>
            <th>Is Read</th>
            <th>Unread Flag</th>
            <th>Created</th>
        </tr>
    """

    for notif in notifications:
        html += f"""
        <tr>
            <td>{notif['id']}</td>
            <td>{notif['title'][:30]}...</td>
            <td>{'✅' if notif['is_read'] else '❌'}</td>
            <td>{'✅' if notif['unread'] else '❌'}</td>
            <td>{notif['created_at'][:10]}</td>
        </tr>
        """

    html += """
    </table>
    
    <h4>Actions:</h4>
    <ul>
        <li><a href="/reset-notifications">Reset All to Unread</a></li>
        <li><a href="/agent/dashboard">Go to Dashboard</a></li>
        <li><a href="/api/agent/notifications">View API Response</a></li>
    </ul>
    """

    return html


@app.route("/check-dashboard-notifications")
def check_dashboard_notifications():
    """Check what notifications are being shown on dashboard"""
    if "user_id" not in session:
        return redirect("/login")

    user_id = session["user_id"]

    # Get what the dashboard is showing
    notifications = get_agent_notifications(user_id, unread_only=False, limit=10)
    unread_count = get_unread_notification_count(user_id)

    result = f"""
    <h3>Dashboard Notification Data</h3>
    <p>Unread Count: {unread_count}</p>
    <p>Total Notifications Returned: {len(notifications)}</p>
    
    <h4>Notifications List:</h4>
    <ol>
    """

    for notif in notifications:
        result += f"""
        <li>
            <strong>{notif['title']}</strong><br>
            ID: {notif['id']}, 
            Is Read: {notif['is_read']}, 
            Unread Flag: {notif['unread']}<br>
            Message: {notif['message'][:50]}...
        </li>
        """

    result += """
    </ol>
    <p><a href="/agent/dashboard">Back to Dashboard</a></p>
    """

    return result


@app.route("/reset-notifications")
def reset_notifications():
    """Reset all notifications to unread (for testing)"""
    if "user_id" not in session:
        return redirect("/login")

    agent_id = session["user_id"]

    conn = get_db_connection()
    cursor = conn.cursor()

    # Reset all notifications for this agent to unread
    cursor.execute(
        """
        UPDATE agent_notifications 
        SET is_read = 0, read_at = NULL 
        WHERE agent_id = ?
    """,
        (agent_id,),
    )

    rows_affected = cursor.rowcount
    conn.commit()
    conn.close()

    return f'Reset {rows_affected} notifications to unread. <a href="/agent/dashboard">Go to Dashboard</a>'


@app.route("/create-test-notification")
def create_test_notification():
    """Create a test notification"""
    if "user_id" not in session:
        return redirect("/login")

    create_agent_notification(
        agent_id=session["user_id"],
        notification_type="test",
        title="🔔 Test Notification",
        message="This is a test notification for the bell system.",
        priority="normal",
    )

    return redirect("/agent/dashboard")


@app.route("/agent/submissions")
def agent_submissions():
    """Agent view all their submissions - TEMPLATE VERSION"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get filter parameters
    status_filter = request.args.get("status", "all")
    search_query = request.args.get("search", "")

    # Build query based on filters
    query = """
        SELECT p.id, p.status, p.customer_name, p.property_address, 
               p.sale_price, p.commission_amount, p.created_at, 
               p.submitted_at, p.approved_at,
               (SELECT COUNT(*) FROM documents WHERE listing_id = p.id) as doc_count
        FROM property_listings p
        WHERE p.agent_id = ?
    """
    params = [session["user_id"]]

    if status_filter == "incomplete":
        query += " AND (SELECT COUNT(*) FROM documents d WHERE d.listing_id = p.id) < 3"
    elif status_filter != "all":
        query += " AND p.status = ?"
        params.append(status_filter)

    if search_query:
        query += " AND (p.customer_name LIKE ? OR p.property_address LIKE ?)"
        params.extend([f"%{search_query}%", f"%{search_query}%"])

    query += " ORDER BY p.created_at DESC"

    cursor.execute(query, params)
    submissions = cursor.fetchall()

    # Get counts for each status
    cursor.execute(
        """
        SELECT status, COUNT(*) as count 
        FROM property_listings 
        WHERE agent_id = ? 
        GROUP BY status
    """,
        (session["user_id"],),
    )
    status_counts_raw = cursor.fetchall()

    # Get incomplete count
    cursor.execute(
        """
        SELECT COUNT(*) as incomplete_count
        FROM property_listings p
        WHERE p.agent_id = ? 
        AND (SELECT COUNT(*) FROM documents WHERE listing_id = p.id) < 3
    """,
        (session["user_id"],),
    )
    incomplete_count = cursor.fetchone()[0] or 0

    # Get total count
    cursor.execute(
        "SELECT COUNT(*) FROM property_listings WHERE agent_id = ?",
        (session["user_id"],),
    )
    total_count = cursor.fetchone()[0] or 0

    conn.close()

    # Convert status_counts to dictionary for easier access
    status_counts = {}
    for status, count in status_counts_raw:
        status_key = status if status else "draft"
        status_counts[status_key] = count

    # Build empty state message
    if not submissions:
        if status_filter != "all":
            if status_filter == "incomplete":
                empty_message = "No incomplete submissions found. All submissions have sufficient documents!"
            else:
                empty_message = f"No {status_filter} submissions found."
        else:
            empty_message = "You haven't created any submissions yet."
    else:
        empty_message = ""

    return render_template(
        "agent/submissions.html",
        submissions=submissions,
        status_filter=status_filter,
        search_query=search_query,
        status_counts=status_counts,
        incomplete_count=incomplete_count,
        total_count=total_count,
        empty_message=empty_message
    )


@app.route("/agent/submission/<int:listing_id>")
def agent_view_submission(listing_id):
    """Agent view a single submission"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    # Verify the listing belongs to this agent
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    cursor.execute("SELECT agent_id FROM property_listings WHERE id = ?", (listing_id,))
    listing = cursor.fetchone()

    if not listing or listing[0] != session["user_id"]:
        conn.close()
        return "Access denied or listing not found", 403

    # Get submission details
    cursor.execute(
        """
        SELECT 
            pl.*,
            p.project_name,
            pu.unit_type,
            u.name as agent_name,
            (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) as doc_count
        FROM property_listings pl
        LEFT JOIN projects p ON pl.project_id = p.id
        LEFT JOIN project_units pu ON pl.unit_id = pu.id
        LEFT JOIN users u ON pl.agent_id = u.id
        WHERE pl.id = ?
    """,
        (listing_id,),
    )

    submission = cursor.fetchone()

    if not submission:
        conn.close()
        return "Submission not found", 404

    # Get uploaded documents
    cursor.execute(
        "SELECT * FROM documents WHERE listing_id = ? ORDER BY uploaded_at",
        (listing_id,),
    )
    documents = cursor.fetchall()

    conn.close()

    # Format the data for the template
    sub_data = {
        "id": submission[0],
        "agent_id": submission[1],
        "status": submission[2],
        "customer_name": submission[3],
        "customer_email": submission[4],
        "customer_phone": submission[5],
        "property_address": submission[6],
        "sale_price": submission[7],
        "closing_date": submission[8],
        "commission_amount": submission[9],
        "commission_status": submission[10],
        "created_at": submission[11],
        "submitted_at": submission[12],
        "approved_at": submission[13],
        "approved_by": submission[14],
        "notes": submission[15],
        "rejection_reason": submission[17],
        "project_name": submission[18],
        "unit_type": submission[19],
        "agent_name": submission[20],
        "doc_count": submission[21],
    }

    # Create the template HTML using proper Jinja2 syntax
    template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Submission #{listing_id}</title>
        <style>
            body {{ 
                font-family: Arial, sans-serif; 
                margin: 0; 
                padding: 20px; 
                background: #f5f5f5; 
                min-height: 100vh;
            }}
            
            .container {{
                max-width: 800px;
                margin: 0 auto;
            }}
            
            .header {{ 
                background: white;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 20px;
                box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            }}
            
            .status-badge {{ 
                padding: 8px 16px; 
                border-radius: 20px; 
                font-weight: bold; 
                font-size: 16px; 
                display: inline-block; 
            }}
            
            .status-draft {{ background: #fff3cd; color: #856404; }}
            .status-submitted {{ background: #cce5ff; color: #004085; }}
            .status-approved {{ background: #d4edda; color: #155724; }}
            .status-rejected {{ background: #f8d7da; color: #721c24; }}
            
            .info-card {{ 
                background: white; 
                padding: 20px; 
                border-radius: 10px; 
                margin-bottom: 20px; 
                box-shadow: 0 2px 5px rgba(0,0,0,0.1); 
            }}
            
            .info-grid {{ 
                display: grid; 
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
                gap: 15px; 
                margin: 15px 0; 
            }}
            
            .info-item {{ 
                padding: 10px; 
                background: #f8f9fa; 
                border-radius: 5px; 
            }}
            
            .info-label {{ 
                font-weight: bold; 
                color: #555; 
                font-size: 14px; 
                margin-bottom: 5px; 
            }}
            
            .info-value {{ 
                font-size: 16px; 
            }}
            
            .btn {{ 
                padding: 10px 20px; 
                border-radius: 5px; 
                text-decoration: none; 
                display: inline-block; 
                margin-right: 10px; 
                margin-bottom: 10px; 
            }}
            
            .btn-primary {{ background: #007bff; color: white; }}
            .btn-secondary {{ background: #6c757d; color: white; }}
            .btn-success {{ background: #28a745; color: white; }}
            .btn-danger {{ background: #dc3545; color: white; }}
            
            .rejection-box {{
                background: #fff3cd;
                border: 1px solid #ffeaa7;
                color: #856404;
                padding: 15px;
                border-radius: 5px;
                margin: 15px 0;
            }}
            
            .commission-box {{
                background: #d4edda;
                border: 1px solid #c3e6cb;
                color: #155724;
                padding: 15px;
                border-radius: 5px;
                margin: 15px 0;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📄 Submission #{listing_id}</h1>
                <div style="margin: 15px 0;">
                    <span class="status-badge status-{sub_data['status']}">
                        {sub_data['status'].upper()}
                    </span>
                    <span style="margin-left: 15px; color: #666;">
                        Created: {sub_data['created_at'][:10] if sub_data['created_at'] else 'N/A'}
                    </span>
                </div>
                <div>
                    <a href="/agent/submissions" class="btn btn-secondary">← Back to My Submissions</a>
                    <a href="/agent/dashboard" class="btn btn-secondary">📊 Dashboard</a>
                </div>
            </div>
            
            <!-- Status-specific actions -->
            <div class="info-card">
                <h3>📋 Actions</h3>
                <div style="display: flex; flex-wrap: wrap; gap: 10px;">
    """

    # Add dynamic buttons based on status
    if sub_data["status"] in ["draft", "rejected"]:
        template += f'<a href="/agent/reupload-documents/{listing_id}" class="btn btn-primary">📤 Add/Replace Documents</a>'

    if sub_data["status"] == "rejected":
        template += f'<a href="/agent/resubmit/{listing_id}" class="btn btn-success">✅ Resubmit for Approval</a>'

    template += f"""
                    <a href="/agent/documents/{listing_id}" class="btn btn-primary">📎 View Documents ({sub_data['doc_count']})</a>
                    <a href="/new-listing" class="btn btn-success">➕ Create New Sale</a>
                </div>
            </div>
    """

    # Add rejection reason if rejected
    if sub_data["status"] == "rejected" and sub_data["rejection_reason"]:
        template += f"""
            <div class="rejection-box">
                <strong>❌ Rejection Reason:</strong>
                <p>{sub_data['rejection_reason']}</p>
            </div>
        """

    # Add commission info if approved
    if sub_data["status"] == "approved" and sub_data["commission_amount"]:
        template += f"""
            <div class="commission-box">
                <strong>💰 Commission Amount:</strong> RM{"{:,.2f}".format(sub_data["commission_amount"])}
            </div>
        """

    # Continue with the rest of the template
    template += f"""
            <!-- Customer Information -->
            <div class="info-card">
                <h3>👤 Customer Information</h3>
                <div class="info-grid">
                    <div class="info-item">
                        <div class="info-label">Customer Name</div>
                        <div class="info-value">{sub_data['customer_name']}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Email</div>
                        <div class="info-value">{sub_data['customer_email']}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Phone</div>
                        <div class="info-value">{sub_data['customer_phone'] or 'Not provided'}</div>
                    </div>
                </div>
            </div>
            
            <!-- Property Details -->
            <div class="info-card">
                <h3>🏠 Property Details</h3>
                <div class="info-grid">
                    <div class="info-item">
                        <div class="info-label">Property Address</div>
                        <div class="info-value">{sub_data['property_address']}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Sale Price</div>
                        <div class="info-value">RM{"{:,.2f}".format(sub_data['sale_price'])}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Closing Date</div>
                        <div class="info-value">{sub_data['closing_date'] or 'Not set'}</div>
                    </div>
                </div>
    """

    # Add project info if any
    if sub_data["project_name"]:
        template += f"""
                <div style="margin-top: 15px;">
                    <div class="info-label">Project</div>
                    <div class="info-value">{sub_data['project_name']}</div>
                </div>
        """

    if sub_data["unit_type"]:
        template += f"""
                <div style="margin-top: 10px;">
                    <div class="info-label">Unit Type</div>
                    <div class="info-value">{sub_data['unit_type']}</div>
                </div>
        """

    template += f"""
            </div>
            
            <!-- Commission Details -->
            <div class="info-card">
                <h3>💰 Commission Details</h3>
                <div class="info-grid">
                    <div class="info-item">
                        <div class="info-label">Commission Amount</div>
                        <div class="info-value">RM{"{:,.2f}".format(sub_data['commission_amount'] or 0)}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Commission Status</div>
                        <div class="info-value">{sub_data['commission_status'] or 'Not calculated'}</div>
                    </div>
                </div>
            </div>
            
            <!-- Timeline -->
            <div class="info-card">
                <h3>📅 Timeline</h3>
                <div class="info-grid">
                    <div class="info-item">
                        <div class="info-label">Created</div>
                        <div class="info-value">{sub_data['created_at'][:19] if sub_data['created_at'] else 'N/A'}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Submitted</div>
                        <div class="info-value">{sub_data['submitted_at'][:19] if sub_data['submitted_at'] else 'Not submitted'}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Approved</div>
                        <div class="info-value">{sub_data['approved_at'][:19] if sub_data['approved_at'] else 'Not approved'}</div>
                    </div>
                </div>
            </div>
    """

    # Add notes if any
    if sub_data["notes"]:
        template += f"""
            <div class="info-card">
                <h3>📝 Notes</h3>
                <div style="padding: 15px; background: #f8f9fa; border-radius: 5px;">
                    {sub_data['notes']}
                </div>
            </div>
        """

    # Add documents preview
    if documents:
        template += f"""
            <div class="info-card">
                <h3>📎 Documents ({len(documents)})</h3>
                <p><a href="/agent/documents/{listing_id}" class="btn btn-primary">View All Documents →</a></p>
            </div>
        """
    else:
        template += f"""
            <div class="info-card">
                <h3>📎 Documents</h3>
                <p>No documents uploaded yet. <a href="/agent/reupload-documents/{listing_id}" class="btn btn-primary">Upload Documents</a></p>
            </div>
        """

    # Add navigation footer
    template += f"""
            <!-- Navigation -->
            <div style="text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd;">
                <a href="/agent/submissions" class="btn btn-secondary">← Back to My Submissions</a>
                <a href="/new-listing" class="btn btn-success">➕ Create New Sale</a>
                <a href="/agent/dashboard" class="btn btn-primary">📊 Dashboard</a>
            </div>
        </div>
    </body>
    </html>
    """

    return template


@app.route("/view-document/<int:doc_id>")
def view_document(doc_id):
    """View/download a specific document"""
    if "user_id" not in session:
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    if session["user_role"] == "admin":
        cursor.execute(
            """
            SELECT d.*, pl.agent_id, u.name as agent_name, pl.customer_name
            FROM documents d
            JOIN property_listings pl ON d.listing_id = pl.id
            JOIN users u ON pl.agent_id = u.id
            WHERE d.id = ?
        """,
            (doc_id,),
        )
    else:
        cursor.execute(
            """
            SELECT d.*, pl.agent_id, u.name as agent_name, pl.customer_name
            FROM documents d
            JOIN property_listings pl ON d.listing_id = pl.id
            JOIN users u ON pl.agent_id = u.id
            WHERE d.id = ? AND pl.agent_id = ?
        """,
            (doc_id, session["user_id"]),
        )

    document = cursor.fetchone()
    conn.close()

    if not document:
        return "Document not found or access denied", 404

    # -----------------------------
    # FIX: normalize Windows paths for Linux
    # -----------------------------
    filepath_db = document[3].replace("\\", "/")
    app_root = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(app_root, filepath_db)
    filepath = os.path.normpath(filepath)
    filename = os.path.basename(filepath)

    if not os.path.exists(filepath):
        return f"File not found: {filename}", 404

    # Content type
    content_type = "application/octet-stream"
    ext = filename.lower().split(".")[-1] if "." in filename else ""
    types = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
    }
    if ext in types:
        content_type = types[ext]

    as_attachment = request.args.get("download", "0") == "1"

    return send_file(
        filepath,
        mimetype=content_type,
        as_attachment=as_attachment,
        download_name=filename,
    )


@app.route("/agent/documents/<int:listing_id>")
def agent_view_documents(listing_id):
    """Agent view all documents for a listing"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    # Verify the listing belongs to this agent
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    cursor.execute(
        "SELECT agent_id, customer_name FROM property_listings WHERE id = ?",
        (listing_id,),
    )
    listing = cursor.fetchone()

    if not listing or listing[0] != session["user_id"]:
        conn.close()
        return "Access denied", 403

    # Get all documents for this listing
    cursor.execute(
        """
        SELECT d.*, u.name as uploader_name
        FROM documents d
        LEFT JOIN users u ON d.uploaded_by = u.id
        WHERE d.listing_id = ?
        ORDER BY d.uploaded_at DESC
    """,
        (listing_id,),
    )

    documents = cursor.fetchall()
    conn.close()

    # Create HTML for documents list
    docs_html = ""
    if documents:
        for doc in documents:
            doc_id = doc[0]
            filename = doc[2]
            file_type = doc[4]
            file_size = doc[5]
            uploaded_at = doc[7]
            uploader = doc[10] if doc[10] else "Agent"

            # Format file size
            size_str = format_file_size(file_size) if file_size else "Unknown"

            # Get file icon
            icon = get_file_icon(file_type)

            docs_html += f"""
            <div style="padding: 10px; border: 1px solid #ddd; margin-bottom: 10px; border-radius: 5px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <span style="font-size: 20px;">{icon}</span>
                        <strong>{filename}</strong>
                        <small style="color: #666; margin-left: 10px;">{size_str}</small>
                    </div>
                    <div>
                        <a href="/view-document/{doc_id}" target="_blank" 
                           style="background: #007bff; color: white; padding: 5px 10px; border-radius: 3px; text-decoration: none;">
                           👁️ View
                        </a>
                        <a href="/view-document/{doc_id}?download=1" 
                           style="background: #28a745; color: white; padding: 5px 10px; border-radius: 3px; text-decoration: none; margin-left: 5px;">
                           📥 Download
                        </a>
                    </div>
                </div>
                <div style="color: #666; font-size: 12px; margin-top: 5px;">
                    Uploaded by {uploader} on {uploaded_at[:10]}
                </div>
            </div>
            """
    else:
        docs_html = f"""
        <div style="padding: 40px; text-align: center; color: #666; background: #f8f9fa; border-radius: 5px;">
            <h3>No documents uploaded yet</h3>
            <p>Upload documents using the button below</p>
            <a href="/agent/reupload-documents/{listing_id}" class="btn" style="background: #28a745; color: white; padding: 10px 20px; border-radius: 5px; text-decoration: none;">
                📤 Upload Documents
            </a>
        </div>
        """

    # Create the full page
    template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Documents - Submission #{listing_id}</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
            .header {{ background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .documents-container {{ background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .btn {{ padding: 8px 16px; border-radius: 5px; text-decoration: none; display: inline-block; }}
            .btn-back {{ background: #6c757d; color: white; }}
            .btn-upload {{ background: #28a745; color: white; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>📎 Documents - Submission #{listing_id}</h1>
            <p>Customer: {listing[1]}</p>
            <div>
                <a href="/agent/submission/{listing_id}" class="btn btn-back">← Back to Submission</a>
                <a href="/agent/reupload-documents/{listing_id}" class="btn btn-upload">📤 Add More Documents</a>
                <a href="/agent/submissions" class="btn" style="background: #007bff; color: white;">📋 All Submissions</a>
            </div>
        </div>
        
        <div class="documents-container">
            <h3>Uploaded Documents ({len(documents)})</h3>
            {docs_html}
        </div>
        
        <div style="margin-top: 20px;">
            <a href="/agent/submission/{listing_id}" class="btn btn-back">← Back to Submission</a>
        </div>
    </body>
    </html>
    """

    return template


@app.route("/agent/reupload-documents/<int:listing_id>", methods=["GET", "POST"])
def agent_reupload_documents(listing_id):
    """Agent reupload documents to existing listing - TEMPLATE VERSION"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    # Verify the listing belongs to this agent
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    cursor.execute(
        "SELECT agent_id, status FROM property_listings WHERE id = ?", (listing_id,)
    )
    listing = cursor.fetchone()

    if not listing or listing[0] != session["user_id"]:
        conn.close()
        return "Access denied or listing not found", 403

    status = listing[1]

    # Check if listing status allows reupload
    allowed_statuses = ["draft", "rejected"]
    if status not in allowed_statuses:
        conn.close()
        return render_template(
            "agent/reupload_not_allowed.html",
            listing_id=listing_id,
            status=status
        )

    # Get existing documents
    cursor.execute(
        "SELECT filename, uploaded_at FROM documents WHERE listing_id = ? ORDER BY uploaded_at DESC",
        (listing_id,),
    )
    existing_docs = cursor.fetchall()
    conn.close()

    if request.method == "POST":
        try:
            conn = sqlite3.connect("real_estate.db")
            cursor = conn.cursor()

            # Get listing details for folder structure
            cursor.execute(
                "SELECT agent_id FROM property_listings WHERE id = ?", (listing_id,)
            )
            listing_info = cursor.fetchone()
            agent_id = listing_info[0]

            # Find existing upload folder
            cursor.execute(
                "SELECT filepath FROM documents WHERE listing_id = ? LIMIT 1",
                (listing_id,),
            )
            doc = cursor.fetchone()

            if doc:
                # Use existing folder
                filepath = doc[0]
                upload_folder = os.path.dirname(filepath)
            else:
                # Create new folder structure
                current_date = datetime.now().strftime("%Y-%m-%d")
                upload_folder = (
                    f"uploads/agent_{agent_id}/{current_date}/listing_{listing_id}"
                )

            # Create folder if it doesn't exist
            if not os.path.exists(upload_folder):
                os.makedirs(upload_folder)

            uploaded_files = []
            ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}

            def allowed_file(filename):
                return (
                    "." in filename
                    and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
                )

            # Handle file uploads
            for field_name in request.files:
                files = request.files.getlist(field_name)
                for file in files:
                    if file and file.filename and allowed_file(file.filename):
                        filename = secure_filename(file.filename)
                        filepath = os.path.join(upload_folder, filename)
                        file.save(filepath)

                        # Check if document already exists
                        cursor.execute(
                            "SELECT id FROM documents WHERE listing_id = ? AND filename = ?",
                            (listing_id, filename),
                        )
                        existing = cursor.fetchone()

                        if existing:
                            # Update existing document
                            cursor.execute(
                                """
                                UPDATE documents 
                                SET filepath = ?, uploaded_at = ?, notes = ?
                                WHERE id = ?
                            """,
                                (
                                    filepath,
                                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    f"Reuploaded by {session['user_name']} on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                                    existing[0],
                                ),
                            )
                            uploaded_files.append(f"📄 Updated: {filename}")
                        else:
                            # Add new document
                            cursor.execute(
                                """
                                INSERT INTO documents 
                                (listing_id, filename, filepath, file_type, file_size, uploaded_by, notes)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                                (
                                    listing_id,
                                    filename,
                                    filepath,
                                    filename.rsplit(".", 1)[1].lower(),
                                    os.path.getsize(filepath),
                                    session["user_id"],
                                    f"Uploaded by {session['user_name']} on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                                ),
                            )
                            uploaded_files.append(f"📄 Added: {filename}")

            # If status was 'rejected', change it back to 'draft' after adding documents
            if status == "rejected":
                cursor.execute(
                    """
                    UPDATE property_listings 
                    SET status = 'draft'
                    WHERE id = ?
                """,
                    (listing_id,),
                )

            # Get customer name for notifications BEFORE closing connection
            cursor.execute(
                "SELECT customer_name FROM property_listings WHERE id = ?",
                (listing_id,),
            )
            customer_result = cursor.fetchone()
            customer_name = customer_result[0] if customer_result else "Unknown"

            conn.commit()
            conn.close()

            # ===== FIX: RESUBMIT LISTING FOR ADMIN REVIEW =====
            conn = sqlite3.connect("real_estate.db")
            cursor = conn.cursor()

            cursor.execute(
                """
                UPDATE property_listings
                SET status = 'submitted',
                    submitted_at = CURRENT_TIMESTAMP,
                    rejection_reason = NULL
                WHERE id = ?
            """,
                (listing_id,),
            )

            conn.commit()
            conn.close()
            # ================================================

            # ============ CREATE NOTIFICATION ============
            create_agent_notification(
                agent_id=session["user_id"],
                notification_type="documents_uploaded",
                title="📎 Documents Uploaded",
                message=f"Documents uploaded for submission #{listing_id}",
                related_id=listing_id,
                related_type="listing",
                priority="normal",
            )

            # Re-check document completeness after upload
            check_and_notify_incomplete_docs(
                listing_id=listing_id,
                agent_id=session["user_id"],
                customer_name=customer_name,
            )

            # Success message using template
            return render_template(
                "agent/reupload_success.html",
                listing_id=listing_id,
                uploaded_files=uploaded_files,
                was_rejected=(status == "rejected")
            )

        except Exception as e:
            # Safely handle errors
            error_msg = str(e)

            # Try to rollback if connection is still open
            try:
                if "conn" in locals() and conn:
                    conn.rollback()
                    conn.close()
            except:
                pass  # Ignore rollback errors

            return render_template(
                "agent/reupload_error.html",
                listing_id=listing_id,
                error_message=error_msg
            )

    # GET request - show reupload form
    return render_template(
        "agent/reupload_documents.html",
        listing_id=listing_id,
        status=status,
        existing_docs=existing_docs
    )

@app.route("/submit-listing", methods=["POST"])
def submit_listing():
    """Submit a new property listing"""
    if "user_id" not in session:
        return redirect("/login")

    # Get action type from form (draft or submit)
    action = request.form.get("action", "submit")  # Default to submit
    status = "submitted" if action == "submit" else "draft"
    submitted_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if action == "submit" else None

    # Initialize variables
    conn = None
    cursor = None
    listing_id = None

    try:
        data = request.form
        sale_type = data.get("sale_type", "sales")  # Default to sales

        # Get project and unit info
        project_id = data.get("project_id")
        unit_id = data.get("unit_id")

        # Calculate commission
        sale_price = float(data["sale_price"])

        # Initialize commission calculation variables
        commission_rate = None
        project_commission_rate = None
        unit_commission_rate = None
        commission_source = "default"

        # OPEN SINGLE DATABASE CONNECTION WITH TIMEOUT
        conn = sqlite3.connect("real_estate.db", timeout=30.0)
        cursor = conn.cursor()

        # Check for project-specific commission
        if project_id:
            # Get project commission rate
            cursor.execute(
                "SELECT commission_rate FROM projects WHERE id = ?", (project_id,)
            )
            project = cursor.fetchone()
            if project and project[0]:
                project_commission_rate = float(project[0])
                commission_rate = project_commission_rate / 100
                commission_source = "project"

            # Check for unit-specific commission
            if unit_id:
                cursor.execute(
                    "SELECT commission_rate FROM project_units WHERE id = ?", (unit_id,)
                )
                unit = cursor.fetchone()
                if unit and unit[0]:
                    unit_commission_rate = float(unit[0])
                    commission_rate = unit_commission_rate / 100
                    commission_source = "unit"

        # If no project commission, use default rate
        if commission_rate is None:
            commission_rate = 0.02  # Default 2% commission (CHANGED FROM 3% TO 2%)
            total_commission = sale_price * commission_rate
            commission_source = "default"
        else:
            # Use project/unit commission rate
            total_commission = sale_price * commission_rate

        # Apply caps (RM1,000 - RM50,000) to total commission
        total_commission = max(1000, min(total_commission, 50000))

        # ============ APPLY FUND-BASED ALLOCATION ============
        # Agent gets 80% of total commission under fund-based system
        agent_commission = total_commission * 0.80  # 80% to agent
        
        # For commission_amount field, store agent's share (80%)
        commission_to_store = agent_commission

        # Save to database
        cursor.execute(
            """
            INSERT INTO property_listings
            (agent_id, customer_name, customer_email, customer_phone,
            property_address, sale_type, sale_price, closing_date,
            commission_amount, status, submitted_at, notes,
            project_id, unit_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                session["user_id"],
                data["customer_name"],
                data["customer_email"],
                data.get("customer_phone"),
                data["property_address"],
                sale_type,
                sale_price,
                data.get("closing_date"),
                round(commission_to_store, 2),  # Store agent's 80% share
                status,
                submitted_time,
                data.get("notes", ""),
                project_id if project_id else None,
                unit_id if unit_id else None,
            ),
        )

        listing_id = cursor.lastrowid
        agent_id = session["user_id"]
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ============ CREATE NOTIFICATIONS FOR AGENT ============
        notification_title = "✅ Submission Created" if action == "submit" else "💾 Draft Saved"
        notification_message = (
            f"Submission #{listing_id} has been submitted for approval."
            if action == "submit"
            else f"Draft #{listing_id} has been saved."
        )
        
        cursor.execute(
            """
            INSERT INTO agent_notifications 
            (agent_id, notification_type, title, message, related_id, related_type, priority, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                session["user_id"],
                "submission_success" if action == "submit" else "draft_saved",
                notification_title,
                notification_message,
                listing_id,
                "listing",
                "normal",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

        # ============ ENHANCED FILE UPLOAD HANDLING ============
        uploaded_files = []
        processed_filenames = set()
        ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}

        def allowed_file(filename):
            if not filename or "." not in filename:
                return False
            return filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

        def is_valid_file(file):
            """Check if file is actually uploaded (not empty/placeholder)"""
            if not file:
                return False
            if not hasattr(file, 'filename'):
                return False
            if not file.filename or file.filename.strip() == "":
                return False
            
            # Additional check for file content
            try:
                current_pos = file.tell()
                file.seek(0)
                content = file.read(1024)
                file.seek(current_pos)
                
                if len(content) == 0:
                    print(f"DEBUG: Empty file detected: {file.filename}")
                    return False
                    
            except Exception as e:
                print(f"DEBUG: Error checking file {file.filename}: {e}")
                return False
                
            return True

        # Debug logging
        print(f"DEBUG [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]: Agent {agent_id} submitting listing {listing_id}")
        print(f"DEBUG: Received form fields: {list(request.form.keys())}")
        print(f"DEBUG: Received file fields: {list(request.files.keys())}")

        for field_name, file_obj in request.files.items():
            if hasattr(file_obj, 'filename'):
                print(f"DEBUG: Field '{field_name}' - filename: '{file_obj.filename}', content_length: {getattr(file_obj, 'content_length', 'N/A')}")
            elif isinstance(file_obj, list):
                for idx, f in enumerate(file_obj):
                    if hasattr(f, 'filename'):
                        print(f"DEBUG: Field '{field_name}[{idx}]' - filename: '{f.filename}', content_length: {getattr(f, 'content_length', 'N/A')}")

        # Create structured folder
        current_date_folder = datetime.now().strftime("%Y-%m-%d")
        listing_folder = f"uploads/agent_{agent_id}/{current_date_folder}/listing_{listing_id}"
        os.makedirs(listing_folder, exist_ok=True)

        # ============ PROCESS MAIN DOCUMENT (REQUIRED) ============
        if "main_document" in request.files:
            file = request.files["main_document"]
            if is_valid_file(file):
                file_content = file.read()
                file.seek(0)
                
                if len(file_content) > 0 and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    
                    if filename not in processed_filenames:
                        filepath = os.path.join(listing_folder, filename)
                        
                        # Check if file already exists
                        cursor.execute(
                            "SELECT id FROM documents WHERE listing_id = ? AND filename = ?",
                            (listing_id, filename)
                        )
                        existing_file = cursor.fetchone()
                        
                        if not existing_file:
                            file.save(filepath)
                            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                                cursor.execute(
                                    """
                                    INSERT INTO documents 
                                    (listing_id, filename, filepath, file_type, file_size, uploaded_by, notes)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                    (
                                        listing_id,
                                        filename,
                                        filepath,
                                        filename.rsplit(".", 1)[1].lower(),
                                        os.path.getsize(filepath),
                                        session["user_id"],
                                        f"Main document uploaded by {session.get('user_name', 'Agent')}",
                                    ),
                                )
                                uploaded_files.append(filename)
                                processed_filenames.add(filename)
                                print(f"DEBUG: Uploaded main document: {filename} ({os.path.getsize(filepath)} bytes)")
                            else:
                                print(f"DEBUG: Main document save failed: {filename}")
                        else:
                            print(f"DEBUG: Main document already exists: {filename}")
            else:
                print(f"DEBUG: Invalid main document file")

        # ============ PROCESS ADDITIONAL DOCUMENTS (OPTIONAL) ============
        if "additional_docs" in request.files:
            files = request.files.getlist("additional_docs")
            print(f"DEBUG: Found {len(files)} additional files")
            
            for index, file in enumerate(files):
                if is_valid_file(file):
                    file_content = file.read()
                    file.seek(0)
                    
                    if len(file_content) > 0 and allowed_file(file.filename):
                        filename = secure_filename(file.filename)
                        if filename in processed_filenames:
                            print(f"DEBUG: Skipping duplicate: {filename}")
                            continue
                        
                        # Check if file already exists
                        cursor.execute(
                            "SELECT id FROM documents WHERE listing_id = ? AND filename = ?",
                            (listing_id, filename)
                        )
                        existing_file = cursor.fetchone()
                        
                        if not existing_file:
                            filepath = os.path.join(listing_folder, filename)
                            file.save(filepath)
                            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                                cursor.execute(
                                    """
                                    INSERT INTO documents 
                                    (listing_id, filename, filepath, file_type, file_size, uploaded_by, notes)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                    (
                                        listing_id,
                                        filename,
                                        filepath,
                                        filename.rsplit(".", 1)[1].lower(),
                                        os.path.getsize(filepath),
                                        session["user_id"],
                                        f"Additional document #{index+1}",
                                    ),
                                )
                                uploaded_files.append(filename)
                                processed_filenames.add(filename)
                                print(f"DEBUG: Uploaded additional document: {filename}")
                            else:
                                print(f"DEBUG: Additional document save failed: {filename}")
                        else:
                            print(f"DEBUG: Additional document already exists: {filename}")
                else:
                    print(f"DEBUG: Invalid additional file #{index}")

        # Log summary
        if uploaded_files:
            print(f"DEBUG: Successfully uploaded {len(uploaded_files)} file(s): {uploaded_files}")
        else:
            print(f"DEBUG: No valid files uploaded")

        # ============ CLEANUP DUPLICATE DOCUMENTS ============

        # Handle multiple additional files
        if "additional_docs" in request.files:
            files = request.files.getlist("additional_docs")
            print(f"DEBUG: Found {len(files)} additional files")
            
            for index, file in enumerate(files):
                # Add validation check
                if is_valid_file(file):
                    file_content = file.read()
                    file.seek(0)
                    
                    if len(file_content) > 0 and allowed_file(file.filename):
                        filename = secure_filename(file.filename)
                        if filename in processed_filenames:
                            print(f"DEBUG: Skipping duplicate filename in additional docs: {filename}")
                            continue
                        
                        # Check if file already exists in this listing
                        cursor.execute(
                            "SELECT id FROM documents WHERE listing_id = ? AND filename = ?",
                            (listing_id, filename)
                        )
                        existing_file = cursor.fetchone()
                        
                        if not existing_file:
                            filepath = os.path.join(listing_folder, filename)
                            file.save(filepath)
                            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                                cursor.execute(
                                    """
                                    INSERT INTO documents 
                                    (listing_id, filename, filepath, file_type, file_size, uploaded_by, notes)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                    (
                                        listing_id,
                                        filename,
                                        filepath,
                                        filename.rsplit(".", 1)[1].lower(),
                                        os.path.getsize(filepath),
                                        session["user_id"],
                                        f"Additional document #{index+1}",
                                    ),
                                )
                                uploaded_files.append(filename)
                                processed_filenames.add(filename)
                                print(f"DEBUG: Uploaded additional file: {filename} ({os.path.getsize(filepath)} bytes)")
                            else:
                                print(f"DEBUG: Additional file save failed or empty: {filename}")
                        else:
                            print(f"DEBUG: Additional file already exists in database: {filename}")
                else:
                    print(f"DEBUG: Invalid additional file #{index}: filename={getattr(file, 'filename', 'N/A')}")

        # Log upload activity summary
        if uploaded_files:
            print(f"DEBUG: Successfully uploaded {len(uploaded_files)} file(s) for listing {listing_id}: {uploaded_files}")
        else:
            print(f"DEBUG: No valid files uploaded for listing {listing_id}")

        # ============ CLEANUP DUPLICATE DOCUMENTS ============
        def cleanup_duplicate_documents(listing_id, cursor):
            """Remove duplicate documents for a listing"""
            # Find duplicate filenames
            cursor.execute("""
                SELECT filename, COUNT(*) as count
                FROM documents 
                WHERE listing_id = ?
                GROUP BY filename 
                HAVING count > 1
            """, (listing_id,))
            
            duplicates = cursor.fetchall()
            
            for filename, count in duplicates:
                print(f"DEBUG: Found {count} duplicates for {filename} in listing {listing_id}")
                # Keep the first one (lowest id), delete others
                cursor.execute("""
                    DELETE FROM documents 
                    WHERE id NOT IN (
                        SELECT MIN(id)
                        FROM documents 
                        WHERE listing_id = ? AND filename = ?
                        GROUP BY filename
                    ) AND listing_id = ? AND filename = ?
                """, (listing_id, filename, listing_id, filename))
                
            if duplicates:
                print(f"DEBUG: Cleaned up {len(duplicates)} duplicate document(s)")
                
        cleanup_duplicate_documents(listing_id, cursor)

        # ============ UPDATE COMMISSION CALCULATION DETAILS ============
        calculation_details = {
            "commission_source": commission_source,
            "base_rate": commission_rate * 100,
            "project_commission_rate": project_commission_rate,
            "unit_commission_rate": unit_commission_rate,
            "total_commission": float(total_commission),  # Total before allocation
            "agent_share_percentage": 80,
            "agent_commission": round(commission_to_store, 2),  # Agent's 80% share
            "fund_allocation": {
                "agent": 80,
                "direct_upline": 10,
                "indirect_upline": 5,
                "company_fund": 5
            }
        }

        # Save commission calculation
        cursor.execute(
            """
            INSERT INTO commission_calculations 
            (listing_id, agent_id, sale_price,
             base_rate, commission, calculation_details)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                listing_id,
                session["user_id"],
                sale_price,
                commission_rate * 100,
                round(commission_to_store, 2),  # Store agent's commission
                json.dumps(calculation_details),
            ),
        )

        # Commit all changes at once
        conn.commit()

        # ============ RENDER TEMPLATE ============
        # Prepare upload message
        upload_message = ""
        if uploaded_files:
            upload_message = f"<br>📎 Uploaded {len(uploaded_files)} document(s): {', '.join(uploaded_files[:3])}"
            if len(uploaded_files) > 3:
                upload_message += f" and {len(uploaded_files)-3} more"

        # Render the template
        return render_template(
            "agent/submission_success.html",
            is_draft=(action == "draft"),
            listing_id=listing_id,
            agent_id=agent_id,
            current_date=current_date,
            current_date_folder=current_date_folder,
            customer_name=data["customer_name"],
            property_address=data["property_address"],
            sale_price=sale_price,
            commission=commission_to_store,  # Show agent's commission
            upload_message=upload_message
        )

    except sqlite3.OperationalError as e:
        if conn:
            conn.rollback()
        if "locked" in str(e).lower():
            error_msg = "Database is temporarily busy. Please wait a moment and try again."
        else:
            error_msg = f"Database error: {str(e)}"
        return render_error_page(error_msg)

    except ValueError as e:
        if conn:
            conn.rollback()
        return render_error_page(f"Invalid input data: {str(e)}")

    except Exception as e:
        if conn:
            conn.rollback()
        import traceback
        error_details = traceback.format_exc()
        print(f"ERROR in submit-listing: {error_details}")
        return render_error_page(f"Unexpected error: {str(e)}")

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route("/agent/commissions")
def agent_commissions():
    """Agent commission tracking with filters"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    user_id = session["user_id"]
    
    # Get filter parameters from request
    status_filter = request.args.get('status', 'all')
    payment_type_filter = request.args.get('payment_type', 'all')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    search_query = request.args.get('search', '')
    sort_by = request.args.get('sort_by', 'date_desc')
    page = int(request.args.get('page', 1))
    items_per_page = 10

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # ===== BASE QUERIES WITH FILTERS =====
    
    # Build WHERE clauses dynamically - FIX: Start with approved only
    where_clauses = ["pl.agent_id = ?", "pl.status = 'approved'"]  # Only approved commissions
    params = [user_id]
    
    # Additional status filter (if user wants to see other statuses)
    if status_filter != 'all' and status_filter != 'approved':
        # If user selects other status, override the default 'approved'
        where_clauses = ["pl.agent_id = ?", "pl.status = ?"]
        params = [user_id, status_filter]
    # If status_filter is 'approved' or 'all', keep the default 'approved' filter
    
    # Date filters for approved_at date
    if date_from:
        where_clauses.append("pl.approved_at >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("pl.approved_at <= ?")
        params.append(f"{date_to} 23:59:59")
    
    # Search filter
    if search_query:
        where_clauses.append("(pl.customer_name LIKE ? OR pl.customer_email LIKE ? OR pl.property_address LIKE ?)")
        params.extend([f"%{search_query}%", f"%{search_query}%", f"%{search_query}%"])
    
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    
    # ===== 1. GET FILTERED APPROVED COMMISSIONS =====
    # First get total count
    cursor.execute(f"""
        SELECT COUNT(*) 
        FROM property_listings pl
        LEFT JOIN projects p ON pl.project_id = p.id
        WHERE {where_sql}
    """, params)
    total_commissions = cursor.fetchone()[0]
    
    # Calculate pagination
    total_pages = (total_commissions + items_per_page - 1) // items_per_page
    offset = (page - 1) * items_per_page
    
    # Build ORDER BY based on sort parameter
    order_by = {
        'date_desc': 'pl.approved_at DESC',
        'date_asc': 'pl.approved_at ASC',
        'amount_desc': 'pl.commission_amount DESC',
        'amount_asc': 'pl.commission_amount ASC',
        'customer_asc': 'pl.customer_name ASC',
        'customer_desc': 'pl.customer_name DESC'
    }.get(sort_by, 'pl.approved_at DESC')
    
    # Get paginated commissions
    cursor.execute(f"""
        SELECT 
            pl.id,
            pl.customer_name,
            pl.sale_price,
            pl.commission_amount,
            pl.status,
            pl.approved_at,
            pl.created_at,
            COALESCE(p.project_name, '') as project_name
        FROM property_listings pl
        LEFT JOIN projects p ON pl.project_id = p.id
        WHERE {where_sql}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """, params + [items_per_page, offset])
    
    commissions = cursor.fetchall()

    # Calculate totals for current filter
    cursor.execute(f"""
        SELECT 
            COALESCE(SUM(commission_amount), 0) as total_approved,
            COUNT(*) as total_count,
            COALESCE(AVG(commission_amount), 0) as avg_commission
        FROM property_listings pl
        WHERE {where_sql}
    """, params)
    totals = cursor.fetchone()

    # ===== 2. GET ALL STATUSES FOR STATS =====
    # Get counts for all statuses (for stats display)
    cursor.execute("""
        SELECT 
            SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved_count,
            SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) as submitted_count,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_count,
            SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected_count,
            SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) as draft_count
        FROM property_listings 
        WHERE agent_id = ?
    """, (user_id,))
    
    status_counts = cursor.fetchone()
    
    # ===== 3. GET FILTERED PAYMENTS =====
    # Get own paid commissions
    own_payments_query = """
        SELECT 
            cp.payment_date,
            cp.commission_amount,
            'Own' as payment_type,
            cp.payment_status,
            COALESCE(cp.transaction_id, 'N/A') as transaction_id,
            COALESCE(p.project_name, '') as project_name,
            pl.customer_name,
            cp.created_at
        FROM commission_payments cp
        LEFT JOIN property_listings pl ON cp.listing_id = pl.id
        LEFT JOIN projects p ON pl.project_id = p.id
        WHERE cp.agent_id = ? AND cp.payment_status = 'paid'
    """
    
    # Get upline commissions
    upline_payments_query = """
        SELECT 
            uc.paid_at as payment_date,
            uc.amount as commission_amount,
            'Upline' as payment_type,
            uc.status as payment_status,
            COALESCE(uc.transaction_id, 'N/A') as transaction_id,
            COALESCE(p.project_name, '') as project_name,
            pl.customer_name,
            selling_agent.name as selling_agent_name,
            uc.created_at
        FROM upline_commissions uc
        LEFT JOIN property_listings pl ON uc.listing_id = pl.id
        LEFT JOIN projects p ON pl.project_id = p.id
        LEFT JOIN users selling_agent ON pl.agent_id = selling_agent.id
        WHERE uc.upline_id = ? AND uc.status = 'paid'
    """
    
    # Combine payments based on filter
    recent_payments_list = []
    
    if payment_type_filter in ['all', 'own']:
        cursor.execute(own_payments_query + " ORDER BY cp.payment_date DESC LIMIT 10", (user_id,))
        for payment in cursor.fetchall():
            recent_payments_list.append({
                "payment_date": payment[0],
                "amount": float(payment[1]) if payment[1] else 0,
                "payment_type": payment[2],
                "payment_status": payment[3],
                "reference": payment[4] if payment[4] != 'N/A' else None,
                "project_name": payment[5] if payment[5] else None,
                "customer_name": payment[6],
                "created_at": payment[7],
                "is_upline_payment": False,
                "selling_agent_name": None
            })
    
    if payment_type_filter in ['all', 'upline']:
        cursor.execute(upline_payments_query + " ORDER BY uc.paid_at DESC LIMIT 10", (user_id,))
        for payment in cursor.fetchall():
            recent_payments_list.append({
                "payment_date": payment[0],
                "amount": float(payment[1]) if payment[1] else 0,
                "payment_type": payment[2],
                "payment_status": payment[3],
                "reference": payment[4] if payment[4] != 'N/A' else None,
                "project_name": payment[5] if payment[5] else None,
                "customer_name": payment[6],
                "selling_agent_name": payment[7],
                "created_at": payment[8],
                "is_upline_payment": True
            })
    
    # Sort and limit payments
    recent_payments_list.sort(key=lambda x: x["payment_date"] or "", reverse=True)
    recent_payments_list = recent_payments_list[:10]

    # ===== 4. GET RECENT SALES (all statuses, last 10) =====
    cursor.execute("""
        SELECT 
            pl.id,
            pl.customer_name,
            pl.sale_price,
            pl.commission_amount,
            pl.status,
            pl.created_at,
            COALESCE(p.project_name, '') as project_name
        FROM property_listings pl
        LEFT JOIN projects p ON pl.project_id = p.id
        WHERE pl.agent_id = ?
        ORDER BY pl.created_at DESC
        LIMIT 10
    """, (user_id,))
    recent_sales = cursor.fetchall()

    # ===== 5. GET TOTAL STATS (unfiltered) =====
    # Total approved commissions (for stats card)
    cursor.execute("""
        SELECT 
            COALESCE(SUM(commission_amount), 0) as total_approved_amount,
            COUNT(*) as total_approved_count
        FROM property_listings 
        WHERE agent_id = ? AND status = 'approved'
    """, (user_id,))
    approved_stats = cursor.fetchone()
    total_approved_amount = float(approved_stats[0]) if approved_stats and approved_stats[0] else 0
    total_approved_count = approved_stats[1] if approved_stats else 0

    # Total paid commissions
    cursor.execute("""
        SELECT 
            COALESCE(SUM(commission_amount), 0) as total_own_paid
        FROM commission_payments 
        WHERE agent_id = ? AND payment_status = 'paid'
    """, (user_id,))
    total_own_paid_result = cursor.fetchone()
    total_own_paid = float(total_own_paid_result[0]) if total_own_paid_result and total_own_paid_result[0] else 0

    # Total upline commissions
    cursor.execute("""
        SELECT 
            COALESCE(SUM(amount), 0) as total_upline_paid
        FROM upline_commissions 
        WHERE upline_id = ? AND status = 'paid'
    """, (user_id,))
    total_upline_paid_result = cursor.fetchone()
    total_upline_paid = float(total_upline_paid_result[0]) if total_upline_paid_result and total_upline_paid_result[0] else 0

    # Total pending (approved but not paid)
    cursor.execute("""
        SELECT 
            COALESCE(SUM(commission_amount), 0) as total_pending
        FROM property_listings 
        WHERE agent_id = ? AND status = 'approved' 
        AND id NOT IN (
            SELECT listing_id FROM commission_payments WHERE agent_id = ? AND payment_status = 'paid'
        )
    """, (user_id, user_id))
    total_pending_result = cursor.fetchone()
    total_pending = float(total_pending_result[0]) if total_pending_result and total_pending_result[0] else 0

    conn.close()

    # ===== PROCESS DATA =====
    commissions_list = []
    for comm in commissions:
        commissions_list.append({
            "id": comm[0],
            "customer_name": comm[1],
            "sale_price": float(comm[2]) if comm[2] else 0,
            "commission_amount": float(comm[3]) if comm[3] else 0,
            "status": comm[4],
            "approved_at": comm[5],
            "created_at": comm[6],
            "project_name": comm[7]
        })

    recent_sales_list = []
    for sale in recent_sales:
        recent_sales_list.append({
            "id": sale[0],
            "customer_name": sale[1],
            "sale_price": float(sale[2]) if sale[2] else 0,
            "commission_amount": float(sale[3]) if sale[3] else 0,
            "status": sale[4],
            "created_at": sale[5],
            "project_name": sale[6]
        })

    total_earnings = total_own_paid + total_upline_paid
    
    # Count payments by type
    own_payments_count = sum(1 for p in recent_payments_list if p['payment_type'] == 'Own')
    upline_payments_count = sum(1 for p in recent_payments_list if p['payment_type'] == 'Upline')
    total_payments_count = len(recent_payments_list)

    return render_template(
        "agent/commissions.html",
        commissions_list=commissions_list,
        recent_sales=recent_sales_list,
        recent_payments=recent_payments_list,
        
        # Stats for cards
        total_approved=total_approved_amount,
        total_count=total_approved_count,
        total_earnings=total_earnings,
        total_own_paid=total_own_paid,
        total_upline_paid=total_upline_paid,
        total_pending=total_pending,
        
        # Status counts for filter display
        submitted_count=status_counts[1] if status_counts else 0,
        pending_count=status_counts[2] if status_counts else 0,
        rejected_count=status_counts[3] if status_counts else 0,
        draft_count=status_counts[4] if status_counts else 0,
        
        total_payments_count=total_payments_count,
        own_payments_count=own_payments_count,
        upline_payments_count=upline_payments_count,
        total_pages=total_pages,
        current_page=page,
        items_per_page=items_per_page,
        total_commissions=total_commissions,
        
        # Filter parameters to pass back to template
        filter_status=status_filter,
        filter_payment_type=payment_type_filter,
        filter_date_from=date_from,
        filter_date_to=date_to,
        filter_search=search_query,
        filter_sort_by=sort_by,
    )

# ===================== FLASK ROUTES =====================

@app.route('/api/commission/preview', methods=['POST'])
def api_commission_preview():
    """Preview commission calculation before actual sale"""
    data = request.json
    
    sale_amount = float(data.get('sale_amount', 0))
    agent_id = data.get('agent_id')
    method = data.get('method', 'fund_based')
    
    if not sale_amount:
        return jsonify({'error': 'Sale amount required'})
    
    # Get breakdown
    breakdown = get_commission_breakdown(sale_amount, agent_id, method)
    
    # If agent_id provided, show comparison
    if agent_id:
        # Calculate actual commission for comparison
        preview_commissions = calculate_multi_level_commission(sale_amount, agent_id, method)
        breakdown['preview_calculation'] = preview_commissions
    
    return jsonify(breakdown)

@app.route('/admin/commission/migrate_agent/<int:agent_id>', methods=['POST'])
def admin_migrate_agent(agent_id):
    """Migrate an agent to fund-based commission system"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'})
    
    data = request.json
    custom_rates = data.get('custom_rates')
    
    success = migrate_agent_to_fund_based(agent_id, custom_rates)
    
    return jsonify({'success': success})

@app.route('/admin/commission/structure', methods=['GET'])
def admin_commission_structure():
    """View all agents' commission structures"""
    if session.get('role') not in ['admin', 'manager']:
        return redirect(url_for('login'))
    
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, name, email, commission_structure,
               total_commission_fund_pct, agent_fund_pct,
               upline_fund_pct, upline2_fund_pct,
               agent_commission_rate, upline_commission_rate
        FROM users WHERE role = 'agent'
        ORDER BY name
    """)
    
    agents = []
    for row in cursor.fetchall():
        agents.append({
            'id': row[0],
            'name': row[1],
            'email': row[2],
            'structure': row[3],
            'fund_total_pct': row[4],
            'fund_agent_pct': row[5],
            'fund_upline_pct': row[6],
            'fund_upline2_pct': row[7],
            'legacy_agent_rate': row[8],
            'legacy_upline_rate': row[9]
        })
    
    conn.close()
    
    return render_template('admin/commission_structures.html', agents=agents)


@app.route("/agent/projects")
def agent_projects():
    """Agent view of projects they've worked on"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get projects the agent has worked on
    cursor.execute(
        """
        SELECT 
            p.id,
            p.project_name,
            p.category,
            p.project_type,
            p.location,
            p.commission_rate,
            COUNT(pl.id) as total_sales,
            SUM(pl.sale_price) as total_sales_value,
            SUM(pl.commission_amount) as total_commission,
            MAX(pl.created_at) as last_sale_date
        FROM projects p
        JOIN property_listings pl ON p.id = pl.project_id
        WHERE pl.agent_id = ?
        GROUP BY p.id
        ORDER BY last_sale_date DESC
    """,
        (session["user_id"],),
    )

    projects = cursor.fetchall()

    conn.close()

    # Process projects data for template
    processed_projects = []
    for project in projects:
        processed_projects.append({
            'id': project[0],
            'name': project[1],
            'category': project[2],
            'type': project[3],
            'location': project[4],
            'commission_rate': project[5],
            'total_sales': project[6],
            'total_value': project[7] if project[7] else 0,
            'total_commission': project[8] if project[8] else 0,
            'last_sale_date': project[9][:10] if project[9] else 'Never'
        })

    return render_template("agent/projects.html", projects=processed_projects)


@app.route("/agent/project-sales/<int:project_id>")
def agent_project_sales(project_id):
    """View sales for a specific project"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Verify agent has access to this project
    cursor.execute(
        """
        SELECT p.project_name, p.category, p.project_type, p.location
        FROM projects p
        JOIN property_listings pl ON p.id = pl.project_id
        WHERE p.id = ? AND pl.agent_id = ?
        LIMIT 1
    """,
        (project_id, session["user_id"]),
    )

    project = cursor.fetchone()

    if not project:
        conn.close()
        return "Project not found or access denied", 404

    # Get all sales for this project by this agent
    cursor.execute(
        """
        SELECT pl.*, pu.unit_type
        FROM property_listings pl
        LEFT JOIN project_units pu ON pl.unit_id = pu.id
        WHERE pl.project_id = ? AND pl.agent_id = ?
        ORDER BY pl.created_at DESC
    """,
        (project_id, session["user_id"]),
    )

    sales = cursor.fetchall()

    conn.close()

    # Build the sales rows HTML - Adjusted indices
    sales_rows = ""
    if sales:
        for sale in sales:
            unit_type = (
                sale[19] if len(sale) > 19 and sale[19] else "N/A"
            )  # Changed from 20 to 19
            status = sale[2] if sale[2] else "draft"
            created_at = sale[12][:10] if sale[12] else ""  # Changed from 13 to 12

            sales_rows += f"""
                <tr>
                    <td>#{sale[0]}</td>
                    <td>{sale[3]}</td>
                    <td>{unit_type}</td>
                    <td>RM{sale[7]:,.2f}</td>  <!-- Changed from 8 to 7 -->
                    <td><strong style="color: #28a745;">RM{sale[9]:,.2f}</strong></td>  <!-- Changed from 10 to 9 -->
                    <td><span class="status-badge status-{status}">{status.title()}</span></td>
                    <td>{created_at}</td>
                    <td>
                        <a href="/agent/submission/{sale[0]}" class="btn" style="padding: 5px 10px; font-size: 12px; background: #17a2b8;">View</a>
                    </td>
                </tr>
            """

    # Create the template
    project_sales_template = f"""<!DOCTYPE html>
<html>
<head>
    <title>Sales - {project[0]}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .header {{ background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        table {{ width: 100%; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin: 20px 0; }}
        th, td {{ padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #2c3e50; color: white; }}
        .status-badge {{ padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }}
        .status-draft {{ background: #fff3cd; color: #856404; }}
        .status-submitted {{ background: #cce5ff; color: #004085; }}
        .status-approved {{ background: #d4edda; color: #155724; }}
        .btn {{ padding: 8px 16px; border-radius: 5px; text-decoration: none; display: inline-block; margin-right: 10px; }}
        .btn-back {{ background: #6c757d; color: white; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>📊 Sales for {project[0]}</h1>
        <div style="margin-top: 10px;">
            <span style="background: #d4edda; color: #155724; padding: 5px 10px; border-radius: 3px; margin-right: 10px;">
                {project[1].upper()}
            </span>
            <span style="background: #cce5ff; color: #004085; padding: 5px 10px; border-radius: 3px;">
                {project[2].upper()}
            </span>
            <span style="margin-left: 20px; color: #666;">{project[3] or ''}</span>
        </div>
        <div style="margin-top: 15px;">
            <a href="/agent/projects" class="btn btn-back">← Back to Projects</a>
            <a href="/new-listing?project_id={project_id}" class="btn" style="background: #28a745; color: white;">➕ New Sale for This Project</a>
        </div>
    </div>
    
    <h2>Sales History ({len(sales)} sales)</h2>
"""

    # Add table or empty state
    if sales:
        project_sales_template += f"""
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Customer</th>
                <th>Unit Type</th>
                <th>Sale Price</th>
                <th>Commission</th>
                <th>Status</th>
                <th>Date</th>
                <th>Actions</th>
            </tr>
        </thead>
        <tbody>
            {sales_rows}
        </tbody>
    </table>
"""
    else:
        project_sales_template += f"""
    <div style="padding: 40px; text-align: center; background: white; border-radius: 10px;">
        <h3>No sales yet for this project</h3>
        <p>You haven't made any sales for this project yet.</p>
        <a href="/new-listing?project_id={project_id}" class="btn" style="background: #28a745; color: white; margin-top: 15px;">Make Your First Sale</a>
    </div>
"""

    # Close the HTML
    project_sales_template += """
</body>
</html>"""

    return project_sales_template


@app.route("/agent/performance")
def agent_performance():
    """Agent performance analytics"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Monthly performance
    cursor.execute(
        """
        SELECT 
            strftime('%Y-%m', submitted_at) as month,
            COUNT(*) as submissions,
            SUM(sale_price) as total_sales,
            SUM(commission_amount) as total_commission
        FROM property_listings 
        WHERE agent_id = ? AND status = 'approved'
        GROUP BY strftime('%Y-%m', submitted_at)
        ORDER BY month DESC
        LIMIT 12
    """,
        (session["user_id"],),
    )

    monthly_stats = cursor.fetchall()

    # Property type breakdown
    cursor.execute(
        """
        SELECT 
            property_type,
            COUNT(*) as count,
            AVG(sale_price) as avg_price,
            SUM(commission_amount) as total_commission
        FROM property_listings 
        WHERE agent_id = ? AND status = 'approved'
        GROUP BY property_type
    """,
        (session["user_id"],),
    )

    property_breakdown = cursor.fetchall()

    conn.close()

    # Return performance dashboard
    performance_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>My Performance</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }
            .stat-card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center; }
            .stat-value { font-size: 1.8em; font-weight: bold; }
            .chart-container { background: white; padding: 25px; border-radius: 10px; margin: 20px 0; }
            table { width: 100%; background: white; border-radius: 10px; margin: 20px 0; }
            th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }
            th { background: #2c3e50; color: white; }
        </style>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body>
        <div class="header">
            <h1>📊 My Performance Analytics</h1>
            <div>
                <a href="/agent/dashboard">← Dashboard</a>
                <a href="/agent/commissions">💰 Commissions</a>
                <a href="/agent/submissions">📋 Submissions</a>
            </div>
        </div>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div style="color: #666; font-size: 14px;">Monthly Avg. Commission</div>
                <div class="stat-value" style="color: #28a745;">RM{{ "{:,.2f}".format(avg_monthly) }}</div>
            </div>
            <div class="stat-card">
                <div style="color: #666; font-size: 14px;">Success Rate</div>
                <div class="stat-value" style="color: #007bff;">{{ success_rate }}%</div>
            </div>
            <div class="stat-card">
                <div style="color: #666; font-size: 14px;">Avg. Sale Price</div>
                <div class="stat-value" style="color: #6f42c1;">RM{{ "{:,.2f}".format(avg_sale_price) }}</div>
            </div>
            <div class="stat-card">
                <div style="color: #666; font-size: 14px;">Top Property Type</div>
                <div class="stat-value" style="color: #fd7e14;">{{ top_property_type }}</div>
            </div>
        </div>
        
        <div class="chart-container">
            <h3>Monthly Performance</h3>
            <canvas id="monthlyChart" height="100"></canvas>
        </div>
        
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
            <div>
                <h3>Property Type Breakdown</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Type</th>
                            <th>Count</th>
                            <th>Avg. Price</th>
                            <th>Total Commission</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for prop in property_breakdown %}
                        <tr>
                            <td>{{ prop[0]|title }}</td>
                            <td>{{ prop[1] }}</td>
                            <td>RM{{ "{:,.2f}".format(prop[2] or 0) }}</td>
                            <td>RM{{ "{:,.2f}".format(prop[3] or 0) }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            
            <div>
                <h3>Recent Months</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Month</th>
                            <th>Submissions</th>
                            <th>Total Sales</th>
                            <th>Commission</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for month in monthly_stats %}
                        <tr>
                            <td>{{ month[0] }}</td>
                            <td>{{ month[1] }}</td>
                            <td>RM{{ "{:,.2f}".format(month[2] or 0) }}</td>
                            <td><strong>RM{{ "{:,.2f}".format(month[3] or 0) }}</strong></td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        
        <script>
            const monthlyData = {
                labels: {{ monthly_labels|safe }},
                datasets: [{
                    label: 'Commission (RM)',
                    data: {{ monthly_commissions|safe }},
                    borderColor: '#28a745',
                    backgroundColor: 'rgba(40, 167, 69, 0.1)',
                    fill: true
                }]
            };
            
            const ctx = document.getElementById('monthlyChart').getContext('2d');
            new Chart(ctx, {
                type: 'line',
                data: monthlyData,
                options: {
                    responsive: true,
                    plugins: {
                        legend: { display: true }
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            ticks: {
                                callback: function(value) {
                                    return 'RM' + value.toLocaleString();
                                }
                            }
                        }
                    }
                }
            });
        </script>
    </body>
    </html>
    """

    # Calculate stats
    total_submissions = len(monthly_stats)
    total_commission = sum([m[3] or 0 for m in monthly_stats])
    avg_monthly = total_commission / max(total_submissions, 1)

    # Prepare chart data
    monthly_labels = [m[0] for m in monthly_stats][::-1]
    monthly_commissions = [m[3] or 0 for m in monthly_stats][::-1]

    return render_template_string(
        performance_template,
        monthly_stats=monthly_stats,
        property_breakdown=property_breakdown,
        avg_monthly=avg_monthly,
        success_rate=75,  # Calculate this from your data
        avg_sale_price=500000,  # Calculate this
        top_property_type="Residential",
        monthly_labels=json.dumps(monthly_labels),
        monthly_commissions=json.dumps(monthly_commissions),
    )


# ============ COMPLETE ADMIN SYSTEM ============
@app.route("/admin/dashboard")
def admin_dashboard():
    """Admin dashboard - shows all submissions with filtering"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get filter parameters
    status_filter = request.args.get("status", "all")
    type_filter = request.args.get("type", "all")
    search_query = request.args.get("search", "")

    # Build query based on filters
    query = """
        SELECT pl.id, pl.agent_id, pl.status, pl.customer_name, pl.customer_email, 
               pl.customer_phone, pl.property_address, pl.sale_price, pl.closing_date,
               pl.commission_amount, pl.commission_status, pl.created_at, pl.submitted_at,
               pl.approved_at, pl.approved_by, pl.notes, pl.metadata, pl.rejection_reason,
               pl.project_id, pl.unit_id,
               u.name as agent_name,
               (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) as document_count
        FROM property_listings pl
        LEFT JOIN users u ON pl.agent_id = u.id
        WHERE 1=1
    """

    params = []

    # Apply status filter
    if status_filter == "submitted":
        query += " AND pl.status = ?"
        params.append("submitted")
    elif status_filter == "approved":
        query += " AND pl.status = ?"
        params.append("approved")
    elif status_filter == "rejected":
        query += " AND pl.status = ?"
        params.append("rejected")
    elif status_filter == "draft":
        query += " AND (pl.status = ? OR pl.status IS NULL)"
        params.append("draft")
    # 'all' shows everything

    # Note: Type filter is disabled in template since sale_type column doesn't exist
    
    # Apply search filter
    if search_query:
        query += " AND (pl.customer_name LIKE ? OR pl.property_address LIKE ? OR u.name LIKE ?)"
        search_term = f"%{search_query}%"
        params.extend([search_term, search_term, search_term])

    query += " ORDER BY pl.created_at DESC"

    cursor.execute(query, params)
    all_submissions = cursor.fetchall()

    # Get pending submissions count (for separate display)
    cursor.execute(
        """
        SELECT COUNT(*) FROM property_listings WHERE status = 'submitted'
    """
    )
    pending_count = cursor.fetchone()[0] or 0

    # Get all listings for stats
    cursor.execute(
        """
        SELECT 
            COUNT(*) as total_listings,
            COALESCE(SUM(sale_price), 0) as total_sales,
            COALESCE(SUM(commission_amount), 0) as total_commissions,
            SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved,
            SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected,
            SUM(CASE WHEN status = 'draft' OR status IS NULL THEN 1 ELSE 0 END) as draft
        FROM property_listings
    """
    )
    stats = cursor.fetchone()

    # Get total agents
    cursor.execute('SELECT COUNT(*) FROM users WHERE role = "agent"')
    total_agents = cursor.fetchone()[0] or 0

    # Get today's submissions
    cursor.execute(
        """
        SELECT COUNT(*) FROM property_listings 
        WHERE DATE(created_at) = DATE('now')
    """
    )
    todays_submissions = cursor.fetchone()[0] or 0

    # Commission calculations
    total_commissions = stats[2] if stats and stats[2] else 0

    # Calculate commissions - check if upline_commissions table exists
    upline_commissions = 0
    try:
        cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM upline_commissions")
        actual_upline_result = cursor.fetchone()
        upline_commissions = actual_upline_result[0] if actual_upline_result else 0
    except sqlite3.OperationalError:
        # Table doesn't exist, set to 0
        upline_commissions = 0

    # Agent commissions = total - actual upline
    agent_commissions = max(0, total_commissions - upline_commissions)

    # Get total paid from commission_payments table (check if exists)
    total_paid = 0
    try:
        cursor.execute(
            'SELECT COALESCE(SUM(commission_amount), 0) FROM commission_payments WHERE payment_status = "paid"'
        )
        paid_result = cursor.fetchone()
        total_paid = paid_result[0] if paid_result else 0
    except sqlite3.OperationalError:
        # Table doesn't exist, set to 0
        total_paid = 0

    # Calculate balance (what's still unpaid)
    balance = max(0, total_commissions - total_paid)

    conn.close()

    # Prepare data for template
    submissions_list = []
    for sub in all_submissions:
        submissions_list.append(
            {
                "id": sub[0],
                "agent_id": sub[1],
                "status": sub[2] or "draft",
                "customer_name": sub[3] or "",
                "customer_email": sub[4] or "",
                "customer_phone": sub[5] or "",
                "property_address": sub[6] or "",
                "sale_price": float(sub[7]) if sub[7] else 0,
                "closing_date": sub[8],
                "commission_amount": float(sub[9]) if sub[9] else 0,
                "commission_status": sub[10],
                "created_at": sub[11],
                "submitted_at": sub[12],
                "approved_at": sub[13],
                "approved_by": sub[14],
                "notes": sub[15],
                "metadata": sub[16],
                "rejection_reason": sub[17],
                "project_id": sub[18],
                "unit_id": sub[19],
                "sale_type": "sales",  # Default value since column doesn't exist
                "agent_name": sub[20] or "Unknown Agent",
                "document_count": sub[21] or 0,
            }
        )

    # Calculate stats - all listings are treated as sales for now
    total_listings = stats[0] if stats else 0
    
    stats_dict = {
        "total_listings": total_listings,
        "total_sales": stats[1] if stats and stats[1] else 0,
        "total_rentals": 0,  # Set to 0 until you add rental functionality
        "total_commissions": total_commissions,
        "agent_commissions": agent_commissions,
        "upline_commissions": upline_commissions,
        "total_paid": total_paid,
        "balance": balance,
        "approved": stats[3] if stats else 0,
        "pending": stats[4] if stats else 0,
        "rejected": stats[5] if stats else 0,
        "draft": stats[6] if stats else 0,
        "sales_count": total_listings,  # All are sales for now
        "rentals_count": 0,  # Set to 0
    }

    return render_template(
        "admin/dashboard.html",
        admin_name=session.get("user_name"),
        submissions_list=submissions_list,
        pending_count=pending_count,
        stats=stats_dict,
        status_filter=status_filter,
        type_filter=type_filter,
        search_query=search_query,
        total_agents=total_agents,
        todays_submissions=todays_submissions,
    )


@app.route("/admin/move-to-draft/<int:listing_id>")
def move_to_draft(listing_id):
    """Admin move submission back to draft so agent can reupload documents"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        # Get listing details for notification
        cursor.execute(
            "SELECT agent_id FROM property_listings WHERE id = ?", (listing_id,)
        )
        listing = cursor.fetchone()

        if not listing:
            conn.close()
            return redirect(f"/admin/documents/{listing_id}?error=Listing+not+found")

        agent_id = listing[0]

        # Update status to draft
        cursor.execute(
            """
            UPDATE property_listings 
            SET status = 'draft'
            WHERE id = ?
        """,
            (listing_id,),
        )

        conn.commit()
        conn.close()

        # Send notification to agent
        admin_name = session.get("user_name", "Admin")
        notify_agent_status_change(listing_id, agent_id, "draft", admin_name)

        return redirect(
            f"/admin/documents/{listing_id}?success=Submission+moved+to+draft.+Agent+can+now+reupload+documents."
        )

    except Exception as e:
        conn.rollback()
        conn.close()
        return redirect(f"/admin/documents/{listing_id}?error=Error:+{str(e)}")


# ============ ENHANCED DOCUMENT VIEW PAGE WITH ADMIN ACTIONS ============


@app.route("/admin/documents/<int:listing_id>")
def view_documents(listing_id):
    """Admin view documents with status change option"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get listing details with agent name
    cursor.execute(
        """
        SELECT pl.*, u.name as agent_name, u.email as agent_email
        FROM property_listings pl
        LEFT JOIN users u ON pl.agent_id = u.id
        WHERE pl.id = ?
    """,
        (listing_id,),
    )
    listing = cursor.fetchone()

    # Get uploaded documents
    cursor.execute(
        """
        SELECT * FROM documents 
        WHERE listing_id = ? 
        ORDER BY uploaded_at DESC
    """,
        (listing_id,),
    )
    documents = cursor.fetchall()

    conn.close()

    if not listing:
        return "Listing not found", 404

    # Get success/error messages
    success_msg = request.args.get("success")
    error_msg = request.args.get("error")

    # Prepare document data
    docs_list = []
    for doc in documents:
        docs_list.append(
            {
                "id": doc[0],
                "filename": doc[2],
                "filepath": doc[3],
                "file_type": doc[4].lower() if doc[4] else "unknown",
                "file_size": doc[5],
                "uploaded_at": doc[7],
                "notes": doc[9],
            }
        )

    # Create enhanced document view template with admin actions
    enhanced_doc_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Documents - Listing #{{ listing_id }}</title>
        <style>
            body { 
                font-family: Arial, sans-serif; 
                margin: 0; 
                padding: 20px; 
                background: #f5f5f5; 
                min-height: 100vh;
            }
            
            .container {
                max-width: 1200px;
                margin: 0 auto;
            }
            
            .header { 
                background: white;
                padding: 25px;
                border-radius: 10px;
                margin-bottom: 20px;
                box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            }
            
            .status-badge {
                padding: 8px 16px;
                border-radius: 20px;
                font-weight: bold;
                font-size: 14px;
                margin-right: 10px;
            }
            
            .status-draft { background: #fff3cd; color: #856404; }
            .status-submitted { background: #cce5ff; color: #004085; }
            .status-approved { background: #d4edda; color: #155724; }
            .status-rejected { background: #f8d7da; color: #721c24; }
            
            .admin-actions {
                background: white;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 20px;
                box-shadow: 0 2px 5px rgba(0,0,0,0.1);
            }
            
            .btn {
                padding: 10px 20px;
                border-radius: 5px;
                text-decoration: none;
                display: inline-block;
                margin: 5px;
                border: none;
                cursor: pointer;
                font-size: 14px;
                font-weight: bold;
            }
            
            .btn-draft {
                background: #ffc107;
                color: #000;
            }
            
            .btn-draft:hover {
                background: #e0a800;
            }
            
            .btn-approve {
                background: #28a745;
                color: white;
            }
            
            .btn-approve:hover {
                background: #218838;
            }
            
            .btn-reject {
                background: #dc3545;
                color: white;
            }
            
            .btn-reject:hover {
                background: #c82333;
            }
            
            .btn-back {
                background: #6c757d;
                color: white;
            }
            
            .btn-back:hover {
                background: #545b62;
            }
            
            .message-box {
                padding: 15px;
                border-radius: 5px;
                margin: 15px 0;
            }
            
            .success-message {
                background: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }
            
            .error-message {
                background: #f8d7da;
                color: #721c24;
                border: 1px solid #f5c6cb;
            }
            
            .info-box {
                background: #e8f4ff;
                padding: 15px;
                border-radius: 5px;
                margin: 15px 0;
                border-left: 4px solid #007bff;
            }
            
            .document-list {
                background: white;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 20px;
            }
            
            .doc-grid {
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
                gap: 15px;
                margin-top: 20px;
            }
            
            .doc-item {
                background: #f8f9fa;
                padding: 15px;
                border-radius: 5px;
                border: 1px solid #ddd;
            }
            
            .file-badge {
                display: inline-block;
                padding: 3px 8px;
                border-radius: 12px;
                font-size: 11px;
                font-weight: 600;
                color: white;
            }
            
            .badge-pdf { background: #ff6b6b; }
            .badge-image { background: #4ecdc4; }
            .badge-doc { background: #45b7d1; }
            .badge-other { background: #96a6b2; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📎 Documents - Listing #{{ listing_id }}</h1>
                <div style="margin: 15px 0;">
                    <span class="status-badge status-{{ status }}">
                        {{ status|upper }}
                    </span>
                    <span style="color: #666;">
                        Agent: {{ agent_name }} ({{ agent_email }}) | 
                        Customer: {{ customer_name }} | 
                        Created: {{ created_at[:10] }}
                    </span>
                </div>
                <div>
                    <a href="/admin/dashboard" class="btn btn-back">← Back to Dashboard</a>
                    <a href="/admin/approve/{{ listing_id }}" class="btn btn-approve" style="{% if status == 'approved' %}display: none;{% endif %}">✅ Approve</a>
                    <a href="/admin/reject/{{ listing_id }}" class="btn btn-reject" style="{% if status == 'rejected' %}display: none;{% endif %}">❌ Reject</a>
                </div>
            </div>
            
            {% if success_msg %}
            <div class="message-box success-message">
                ✅ {{ success_msg }}
            </div>
            {% endif %}
            
            {% if error_msg %}
            <div class="message-box error-message">
                ❌ {{ error_msg }}
            </div>
            {% endif %}
            
            <!-- ADMIN ACTIONS FOR DOCUMENT REUPLOAD -->
            {% if status in ['submitted', 'approved'] %}
            <div class="admin-actions">
                <h3> Document Reupload Status</h3>
                <div class="info-box">
                    <p><strong>Current Status:</strong> {{ status|upper }}</p>
                    <p><strong>Document Upload Rules:</strong></p>
                    <ul>
                        <li><strong>✅ Draft/Rejected:</strong> Agent can add/replace documents freely</li>
                        <li><strong>⏳ Submitted:</strong> Under admin review - cannot modify documents</li>
                        <li><strong>✅ Approved:</strong> Completed - cannot modify documents</li>
                    </ul>
                    
                    <p style="margin-top: 15px;">
                        <strong>Action Required:</strong> To allow the agent to reupload documents, 
                        you need to change the status back to "draft".
                    </p>
                </div>
                
                <div style="margin-top: 20px;">
                    <form method="GET" action="/admin/move-to-draft/{{ listing_id }}" onsubmit="return confirm('Are you sure you want to move this submission back to draft?\\n\\nAgent will be able to reupload documents.')">
                        <button type="submit" class="btn btn-draft">
                            📝 Move to Draft (Allow Reupload)
                        </button>
                        <small style="display: block; margin-top: 10px; color: #666;">
                            This will change status to "draft" and notify the agent they can reupload documents.
                        </small>
                    </form>
                </div>
            </div>
            {% endif %}
            
            <!-- DOCUMENT LIST -->
            <div class="document-list">
                <h2>📁 Uploaded Documents ({{ document_count }})</h2>
                
                {% if documents %}
                <div class="doc-grid">
                    {% for doc in documents %}
                    <div class="doc-item">
                        <div style="font-size: 32px; margin-bottom: 10px;">
                            {{ get_file_icon(doc.file_type) }}
                        </div>
                        
                        <div style="font-weight: bold; margin: 10px 0; word-break: break-all;">
                            {{ doc.filename }}
                        </div>
                        
                        <div style="color: #666; font-size: 13px; margin: 8px 0;">
                            {% if doc.file_type == 'pdf' %}
                                <span class="file-badge badge-pdf">PDF</span>
                            {% elif doc.file_type in ['jpg', 'jpeg', 'png', 'gif'] %}
                                <span class="file-badge badge-image">IMAGE</span>
                            {% elif doc.file_type in ['doc', 'docx'] %}
                                <span class="file-badge badge-doc">DOCUMENT</span>
                            {% else %}
                                <span class="file-badge badge-other">{{ doc.file_type|upper }}</span>
                            {% endif %}
                            
                            <span> • {{ format_file_size(doc.file_size) }}</span>
                            <br>
                            <span>📅 {{ doc.uploaded_at[:19] if doc.uploaded_at else 'Unknown' }}</span>
                            {% if doc.notes %}
                            <br>
                            <span>📝 {{ doc.notes }}</span>
                            {% endif %}
                        </div>
                        
                        <div style="display: flex; gap: 10px; margin-top: 15px;">
                            {% if can_preview_in_browser(doc.file_type) %}
                            <a href="/view-document/{{ doc.id }}" class="btn" style="background: #17a2b8; color: white;" target="_blank">
                                👁️ View
                            </a>
                            {% endif %}
                            
                            <a href="/download/{{ doc.id }}" class="btn" style="background: #007bff; color: white;" download>
                                ⬇️ Download
                            </a>
                        </div>
                    </div>
                    {% endfor %}
                </div>
                {% else %}
                <div style="text-align: center; padding: 50px 20px; color: #666;">
                    <h3>📭 No documents uploaded</h3>
                    <p>The agent has not uploaded any documents for this listing.</p>
                    
                    {% if status in ['draft', 'rejected'] %}
                    <div style="margin-top: 20px; padding: 15px; background: #fff3cd; border-radius: 5px; display: inline-block;">
                        <p style="margin: 0;">
                            <strong>Note:</strong> Agent can upload documents because status is "{{ status }}"
                        </p>
                    </div>
                    {% endif %}
                </div>
                {% endif %}
            </div>
            
            <!-- SUBMISSION DETAILS -->
            <div class="admin-actions">
                <h3>📋 Submission Details</h3>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 15px; margin-top: 15px;">
                    <div style="padding: 15px; background: #f8f9fa; border-radius: 5px;">
                        <strong>Customer Information</strong><br>
                        <small>{{ customer_name }}</small><br>
                        <small>{{ customer_email }}</small><br>
                        <small>{{ customer_phone or 'No phone' }}</small>
                    </div>
                    
                    <div style="padding: 15px; background: #f8f9fa; border-radius: 5px;">
                        <strong>Property Details</strong><br>
                        <small>{{ property_address[:50] }}{% if property_address|length > 50 %}...{% endif %}</small><br>
                        <small>Sale Price: RM{{ "{:,.2f}".format(sale_price) }}</small><br>
                        <small>Commission: RM{{ "{:,.2f}".format(commission_amount or 0) }}</small>
                    </div>
                    
                    <div style="padding: 15px; background: #f8f9fa; border-radius: 5px;">
                        <strong>Timeline</strong><br>
                        <small>Created: {{ created_at[:19] }}</small><br>
                        {% if submitted_at %}
                        <small>Submitted: {{ submitted_at[:19] }}</small><br>
                        {% endif %}
                        {% if approved_at %}
                        <small>Approved: {{ approved_at[:19] }}</small><br>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    return render_template_string(
        enhanced_doc_template,
        listing_id=listing_id,
        customer_name=listing[3] if listing else "Unknown",
        customer_email=listing[4] if listing else "Unknown",
        customer_phone=listing[5] if listing else "",
        agent_name=listing[18] if listing and len(listing) > 18 else "Unknown",
        agent_email=listing[19] if listing and len(listing) > 19 else "Unknown",
        property_address=listing[6] if listing else "Unknown",
        status=listing[2] if listing else "draft",
        sale_price=listing[7] if listing else 0,
        commission_amount=listing[9] if listing else 0,
        created_at=listing[11] if listing else "",
        submitted_at=listing[12] if listing else "",
        approved_at=listing[13] if listing else "",
        documents=docs_list,
        document_count=len(docs_list),
        get_file_icon=get_file_icon,
        format_file_size=format_file_size,
        can_preview_in_browser=can_preview_in_browser,
        success_msg=success_msg,
        error_msg=error_msg,
    )


# ============ UPDATED PENDING SUBMISSIONS TABLE WITH DOCUMENT STATUS ============

admin_dashboard_table_section = """
        <h2>📋 Pending Submissions ({{ pending_count }})</h2>
        
        {% if pending_submissions %}
        <div style="margin-bottom: 20px; display: flex; gap: 10px; align-items: center;">
            <div style="display: flex; align-items: center; gap: 5px;">
                <div style="width: 12px; height: 12px; background: #dc3545; border-radius: 50%;"></div>
                <small>Incomplete Documents (0-2)</small>
            </div>
            <div style="display: flex; align-items: center; gap: 5px;">
                <div style="width: 12px; height: 12px; background: #ffc107; border-radius: 50%;"></div>
                <small>Minimum Documents (3)</small>
            </div>
            <div style="display: flex; align-items: center; gap: 5px;">
                <div style="width: 12px; height: 12px; background: #28a745; border-radius: 50%;"></div>
                <small>Complete Documents (4+)</small>
            </div>
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Agent</th>
                    <th>Customer</th>
                    <th>Property</th>
                    <th>Sale Price</th>
                    <th>Commission</th>
                    <th>Documents</th>
                    <th>Status</th>
                    <th>Submitted</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for sub in pending_submissions %}
                <tr {% if sub.document_count <= 2 %}style="background: #fff5f5; border-left: 4px solid #dc3545;"{% elif sub.document_count == 3 %}style="background: #fff9e6; border-left: 4px solid #ffc107;"{% else %}style="background: #f0fff4; border-left: 4px solid #28a745;"{% endif %}>
                    <td>#{{ sub.id }}</td>
                    <td>
                        {{ sub.agent_name }}
                        <br>
                        <small style="color: #666;">ID: {{ sub.agent_id }}</small>
                    </td>
                    <td>
                        {{ sub.customer_name }}
                        <br>
                        <small style="color: #666;">{{ sub.customer_email }}</small>
                        {% if sub.customer_phone %}
                        <br>
                        <small style="color: #666;">📱 {{ sub.customer_phone }}</small>
                        {% endif %}
                    </td>
                    <td>
                        {{ sub.property_address[:25] }}{% if sub.property_address|length > 25 %}...{% endif %}
                        {% if sub.project_name %}
                        <br>
                        <small class="project-badge">{{ sub.project_name }}</small>
                        {% endif %}
                    </td>
                    <td>RM{{ "{:,.2f}".format(sub.sale_price) }}</td>
                    <td>RM{{ "{:,.2f}".format(sub.commission_amount or 0) }}</td>
                    <td>
                        {% if sub.document_count == 0 %}
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span style="color: #dc3545; font-size: 20px;">❌</span>
                                <div>
                                    <strong style="color: #dc3545;">No Documents</strong>
                                    <div style="font-size: 11px; color: #dc3545;">
                                        Critical: Agent must upload documents
                                    </div>
                                </div>
                            </div>
                        {% elif sub.document_count == 1 %}
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span style="color: #dc3545; font-size: 20px;"></span>
                                <div>
                                    <strong style="color: #dc3545;">1/4 Documents</strong>
                                    <div style="font-size: 11px; color: #dc3545;">
                                        Very Incomplete
                                    </div>
                                </div>
                            </div>
                        {% elif sub.document_count == 2 %}
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span style="color: #ffc107; font-size: 20px;"></span>
                                <div>
                                    <strong style="color: #ffc107;">2/4 Documents</strong>
                                    <div style="font-size: 11px; color: #ffc107;">
                                        Missing documents
                                    </div>
                                </div>
                            </div>
                        {% elif sub.document_count == 3 %}
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span style="color: #28a745; font-size: 20px;"></span>
                                <div>
                                    <strong style="color: #28a745;">3/4 Documents</strong>
                                    <div style="font-size: 11px; color: #28a745;">
                                        Minimum complete
                                    </div>
                                </div>
                            </div>
                        {% else %}
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span style="color: #28a745; font-size: 20px;">✅</span>
                                <div>
                                    <strong style="color: #28a745;">{{ sub.document_count }} Documents</strong>
                                    <div style="font-size: 11px; color: #28a745;">
                                        Complete submission
                                    </div>
                                </div>
                            </div>
                        {% endif %}
                        
                        {% if sub.document_count <= 2 %}
                        <div style="margin-top: 5px; padding: 5px 10px; background: #ffeaea; border-radius: 3px;">
                            <small style="color: #dc3545; font-weight: bold;">
                                ❌ DO NOT APPROVE - Incomplete
                            </small>
                        </div>
                        {% elif sub.document_count == 3 %}
                        <div style="margin-top: 5px; padding: 5px 10px; background: #fff3cd; border-radius: 3px;">
                            <small style="color: #856404; font-weight: bold;">
                                 Review Carefully - Minimum documents
                            </small>
                        </div>
                        {% endif %}
                    </td>
                    <td>
                        {% if sub.document_count <= 2 %}
                        <span style="background: #dc3545; color: white; padding: 4px 8px; border-radius: 12px; font-size: 12px; font-weight: bold;">
                            INCOMPLETE
                        </span>
                        <div style="margin-top: 5px; font-size: 11px; color: #dc3545;">
                            ❌ Missing documents
                        </div>
                        {% elif sub.document_count == 3 %}
                        <span style="background: #ffc107; color: #000; padding: 4px 8px; border-radius: 12px; font-size: 12px; font-weight: bold;">
                            MINIMUM
                        </span>
                        <div style="margin-top: 5px; font-size: 11px; color: #856404;">
                             Basic documents only
                        </div>
                        {% else %}
                        <span style="background: #28a745; color: white; padding: 4px 8px; border-radius: 12px; font-size: 12px; font-weight: bold;">
                            READY
                        </span>
                        <div style="margin-top: 5px; font-size: 11px; color: #155724;">
                            ✅ Ready for review
                        </div>
                        {% endif %}
                    </td>
                    <td>{{ sub.submitted_at[:10] if sub.submitted_at else 'N/A' }}</td>
                    <td>
                        <div style="display: flex; flex-direction: column; gap: 5px;">
                            <a href="/admin/documents/{{ sub.id }}" class="btn" style="padding: 6px 12px; background: #6f42c1; color: white; text-decoration: none; border-radius: 4px; font-size: 12px; text-align: center;">
                                📎 View Docs ({{ sub.document_count }})
                            </a>
                            
                            {% if sub.document_count >= 3 %}
                            <a href="/admin/approve/{{ sub.id }}" class="btn" style="padding: 6px 12px; background: #28a745; color: white; text-decoration: none; border-radius: 4px; font-size: 12px; text-align: center;">
                                ✅ Approve
                            </a>
                            {% else %}
                            <button style="padding: 6px 12px; background: #6c757d; color: white; border: none; border-radius: 4px; font-size: 12px; cursor: not-allowed; opacity: 0.5;" disabled>
                                ❌ Approve (Incomplete)
                            </button>
                            {% endif %}
                            
                            <a href="/admin/reject/{{ sub.id }}" class="btn" style="padding: 6px 12px; background: #dc3545; color: white; text-decoration: none; border-radius: 4px; font-size: 12px; text-align: center;">
                                ❌ Reject
                            </a>
                        </div>
                        
                        {% if sub.document_count <= 2 %}
                        <div style="margin-top: 5px;">
                            <a href="/admin/move-to-draft/{{ sub.id }}" class="btn" style="padding: 4px 8px; background: #ffc107; color: #000; text-decoration: none; border-radius: 3px; font-size: 11px; width: 100%; text-align: center;">
                                📝 Return for Upload
                            </a>
                        </div>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        
        <div style="margin-top: 30px; padding: 20px; background: #f8f9fa; border-radius: 10px;">
            <h3>📋 Document Requirement Guidelines</h3>
            <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin-top: 15px;">
                <div>
                    <h4 style="margin-top: 0; color: #dc3545;">❌ INCOMPLETE (0-2 docs)</h4>
                    <ul style="margin: 10px 0; padding-left: 20px; font-size: 14px;">
                        <li>Cannot be approved</li>
                        <li>Return to agent for upload</li>
                        <li>Critical missing documents</li>
                    </ul>
                </div>
                <div>
                    <h4 style="margin-top: 0; color: #ffc107;"> MINIMUM (3 docs)</h4>
                    <ul style="margin: 10px 0; padding-left: 20px; font-size: 14px;">
                        <li>Can be approved with caution</li>
                        <li>Basic requirements met</li>
                        <li>Review carefully</li>
                    </ul>
                </div>
                <div>
                    <h4 style="margin-top: 0; color: #28a745;">✅ READY (4+ docs)</h4>
                    <ul style="margin: 10px 0; padding-left: 20px; font-size: 14px;">
                        <li>Ready for approval</li>
                        <li>All documents complete</li>
                        <li>Fast-track approval possible</li>
                    </ul>
                </div>
            </div>
        </div>
        {% else %}
        <div style="padding: 40px; text-align: center; background: white; border-radius: 10px;">
            <h3>🎉 No pending submissions!</h3>
            <p>All submissions have been processed.</p>
        </div>
        {% endif %}
"""

# ============ UPDATE THE QUERY IN admin_dashboard() ============

# Update the pending submissions query in admin_dashboard() function:

updated_pending_query = """
        SELECT pl.id, pl.agent_id, pl.status, pl.customer_name, pl.customer_email, 
               pl.customer_phone, pl.property_address, pl.sale_price, pl.closing_date,
               pl.commission_amount, pl.commission_status, pl.created_at, pl.submitted_at,
               pl.approved_at, pl.approved_by, pl.notes, pl.metadata, pl.rejection_reason,
               pl.project_id, pl.unit_id, u.name as agent_name,
               (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) as document_count,
               p.project_name
        FROM property_listings pl
        JOIN users u ON pl.agent_id = u.id
        LEFT JOIN projects p ON pl.project_id = p.id
        WHERE pl.status = 'submitted'
        ORDER BY 
            CASE 
                WHEN (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) <= 2 THEN 1
                WHEN (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) = 3 THEN 2
                ELSE 3
            END,
            pl.submitted_at DESC
        """

# ============ ADD WORKING ADMIN FEATURES ============
@app.route("/admin/agents")
def manage_agents():
    """Display all agents with their commission structures"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")
    
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    
    # UPDATED QUERY: Get all agents with their NEW commission fields
    cursor.execute("""
        SELECT 
            u.id,
            u.email,
            u.name,
            u.role,
            u.upline_id,
            u.upline_commission_rate,
            u.created_at,
            u.upline2_id,
            u.upline2_commission_rate,
            u.commission_rate,
            u.total_listings,
            u.total_commission,
            -- NEW FIELDS:
            u.commission_structure,
            u.total_commission_fund_pct,
            u.agent_fund_pct,
            u.upline_fund_pct,
            u.upline2_fund_pct,
            u.company_fund_pct,
            -- Upline details
            ul.name as upline_name,
            ul.email as upline_email,
            ul2.name as upline2_name,
            ul2.email as upline2_email
        FROM users u
        LEFT JOIN users ul ON u.upline_id = ul.id
        LEFT JOIN users ul2 ON u.upline2_id = ul2.id
        WHERE u.role = 'agent'
        ORDER BY u.created_at DESC
    """)
    
    agents_data = cursor.fetchall()
    conn.close()
    
    # Convert to list of dictionaries for easier template access
    agents = []
    for agent in agents_data:
        agents.append({
            'id': agent[0],
            'email': agent[1],
            'name': agent[2],
            'role': agent[3],
            'upline_id': agent[4],
            'upline_commission_rate': agent[5],
            'created_at': agent[6],
            'upline2_id': agent[7],
            'upline2_commission_rate': agent[8],
            'commission_rate': agent[9],
            'total_listings': agent[10],
            'total_commission': agent[11],
            'commission_structure': agent[12],
            'total_commission_fund_pct': agent[13],
            'agent_fund_pct': agent[14],
            'upline_fund_pct': agent[15],
            'upline2_fund_pct': agent[16],
            'company_fund_pct': agent[17],
            'upline_name': agent[18],
            'upline_email': agent[19],
            'upline2_name': agent[20],
            'upline2_email': agent[21]
        })
    
    return render_template("admin/manage_agents.html", agents=agents)

@app.route("/admin/agent-hierarchy")
def agent_hierarchy():
    """View agent hierarchy tree with improved design - UPDATED FOR FUND-BASED COMMISSIONS"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # UPDATED QUERY: Get all agents with NEW fund-based commission fields
    cursor.execute(
        """
        SELECT 
            u1.id,
            u1.name,
            u1.email,
            u1.upline_id,
            u1.upline2_id,
            u2.name as upline_name,
            u2.email as upline_email,
            u3.name as upline2_name,
            u3.email as upline2_email,
            u1.created_at,
            -- NEW FIELDS:
            u1.commission_structure,
            u1.total_commission_fund_pct,
            u1.agent_fund_pct,
            u1.upline_fund_pct,
            u1.upline2_fund_pct,
            u1.company_fund_pct,
            -- Legacy fields (keep for backward compatibility)
            u1.upline_commission_rate,
            u1.upline2_commission_rate,
            u1.commission_rate,
            -- Statistics
            (SELECT COUNT(*) FROM users u4 WHERE u4.upline_id = u1.id AND u4.role = 'agent') as downline_count,
            (SELECT COUNT(*) FROM property_listings pl WHERE pl.agent_id = u1.id) as total_listings,
            (SELECT SUM(pl.commission_amount) FROM property_listings pl WHERE pl.agent_id = u1.id AND pl.status = 'approved') as total_commission
        FROM users u1
        LEFT JOIN users u2 ON u1.upline_id = u2.id
        LEFT JOIN users u3 ON u1.upline2_id = u3.id
        WHERE u1.role = 'agent'
        ORDER BY u1.upline_id IS NULL DESC, u1.name
    """
    )

    agents = cursor.fetchall()

    # Get all downline relationships
    cursor.execute(
        """
        SELECT upline_id, GROUP_CONCAT(name) as downline_names
        FROM users 
        WHERE role = 'agent' AND upline_id IS NOT NULL 
        GROUP BY upline_id
    """
    )
    downline_groups = {row[0]: row[1] for row in cursor.fetchall()}

    conn.close()

    # Build hierarchy tree
    def build_hierarchy_tree():
        """Build hierarchical tree structure"""
        # Create agent nodes
        nodes = {}
        for agent in agents:
            agent_id = agent[0]
            nodes[agent_id] = {
                "id": agent[0],
                "name": agent[1],
                "email": agent[2],
                "upline_id": agent[3],
                "upline2_id": agent[4],
                "upline_name": agent[5],
                "upline_email": agent[6],
                "upline2_name": agent[7],
                "upline2_email": agent[8],
                "join_date": agent[9],
                # NEW FIELDS:
                "commission_structure": agent[10],
                "total_commission_fund_pct": agent[11],
                "agent_fund_pct": agent[12],
                "upline_fund_pct": agent[13],
                "upline2_fund_pct": agent[14],
                "company_fund_pct": agent[15],
                # Legacy fields:
                "upline_commission_rate": agent[16],
                "upline2_commission_rate": agent[17],
                "commission_rate": agent[18],
                # Statistics
                "downline_count": agent[19],
                "total_listings": agent[20],
                "total_commission": agent[21] or 0,
                "downlines": [],  # Will be filled with child nodes
            }

        # Build tree by connecting downlines
        for agent_id, node in nodes.items():
            upline_id = node["upline_id"]
            if upline_id and upline_id in nodes:
                nodes[upline_id]["downlines"].append(node)

        # Return top-level nodes (no upline)
        top_level = [node for node in nodes.values() if not node["upline_id"]]

        # Sort by name
        top_level.sort(key=lambda x: x["name"])

        # Sort downlines recursively
        def sort_downlines(node):
            node["downlines"].sort(key=lambda x: x["name"])
            for downline in node["downlines"]:
                sort_downlines(downline)

        for node in top_level:
            sort_downlines(node)

        return top_level

    hierarchy_tree = build_hierarchy_tree()

    # Render HTML tree - UPDATED FOR FUND-BASED COMMISSIONS
    def render_tree_html(agents_list, level=0, parent_id=None):
        html = ""
        for agent in agents_list:
            # Determine level-specific styling
            level_class = f"level-{min(level, 3)}"
            padding_left = level * 40  # Indent based on level

            # Calculate statistics
            total_downlines = agent["downline_count"]
            total_commission = agent["total_commission"] or 0

            # Determine if this agent has downlines
            has_downlines = len(agent["downlines"]) > 0
            
            # Determine commission structure to display
            is_fund_based = agent.get("commission_structure") == "fund_based"
            
            # Commission display based on structure
            commission_display = ""
            if is_fund_based:
                commission_display = f"""
                    <div class="detail-row">
                        <div class="detail-item">
                            <span class="detail-label">Fund:</span>
                            <span class="detail-value">{agent.get('total_commission_fund_pct') or 2.0}% of sale</span>
                        </div>
                        <div class="detail-item">
                            <span class="detail-label">Agent Share:</span>
                            <span class="detail-value" style="color: #28a745;">{agent.get('agent_fund_pct') or 80.0}%</span>
                        </div>
                    </div>
                    
                    <div class="detail-row">
                        <div class="detail-item">
                            <span class="detail-label">Direct Upline:</span>
                            <span class="detail-value">{agent.get('upline_fund_pct') or 10.0}% of fund</span>
                        </div>
                        <div class="detail-item">
                            <span class="detail-label">Indirect Upline:</span>
                            <span class="detail-value">{agent.get('upline2_fund_pct') or 5.0}% of fund</span>
                        </div>
                    </div>
                    
                    <div class="detail-row">
                        <div class="detail-item">
                            <span class="detail-label">Company:</span>
                            <span class="detail-value">{agent.get('company_fund_pct') or 5.0}% of fund</span>
                        </div>
                    </div>
                """
            else:
                commission_display = f"""
                    <div class="detail-row">
                        <div class="detail-item">
                            <span class="detail-label">Upline Rate:</span>
                            <span class="detail-value">{agent.get('upline_commission_rate') or 0}%</span>
                        </div>
                        <div class="detail-item">
                            <span class="detail-label">Upline2 Rate:</span>
                            <span class="detail-value">{agent.get('upline2_commission_rate') or 0}%</span>
                        </div>
                    </div>
                    
                    <div class="detail-row">
                        <div class="detail-item">
                            <span class="detail-label">Own Rate:</span>
                            <span class="detail-value">{agent.get('commission_rate') or 0}%</span>
                        </div>
                    </div>
                """

            html += f"""
            <div class="hierarchy-item {level_class}" style="margin-left: {padding_left}px;">
                <div class="agent-card">
                    <div class="agent-header">
                        <div class="agent-avatar">
                            <span class="avatar-icon">👤</span>
                            <span class="level-badge">L{level + 1}</span>
                        </div>
                        <div class="agent-info">
                            <h3>{agent['name']}</h3>
                            <p class="agent-email">{agent['email']}</p>
                            <div class="agent-id">ID: #{agent['id']}</div>
                            <div style="font-size: 10px; margin-top: 3px;">
                                <span class="commission-structure" style="background: {'#28a745' if is_fund_based else '#6c757d'}; color: white; padding: 2px 6px; border-radius: 3px;">
                                    {'💰 Fund-Based' if is_fund_based else '⚡ Legacy'}
                                </span>
                            </div>
                        </div>
                        <div class="agent-actions">
                            <a href="/admin/edit-agent/{agent['id']}" class="btn-edit">✏️ Edit</a>
                            <a href="/admin/agents?view={agent['id']}" class="btn-view">👁️ View</a>
                        </div>
                    </div>
        
                    <div class="agent-details">
                        <div class="detail-row">
                            <div class="detail-item">
                                <span class="detail-label">Direct Upline:</span>
                                <span class="detail-value">{agent['upline_name'] or 'TOP LEVEL'}</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Indirect Upline:</span>
                                <span class="detail-value">{agent['upline2_name'] or 'None'}</span>
                            </div>
                        </div>
            
                        {commission_display}
            
                        <div class="detail-row">
                            <div class="detail-item">
                                <span class="detail-label">Downlines:</span>
                                <span class="detail-value badge-downline">{total_downlines} agent(s)</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Listings:</span>
                                <span class="detail-value badge-listings">{agent['total_listings'] or 0}</span>
                            </div>
                        </div>
            
                        <div class="detail-row">
                            <div class="detail-item">
                                <span class="detail-label">Total Commission:</span>
                                <span class="detail-value" style="color: #28a745; font-weight: bold;">RM{float(total_commission):,.2f}</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Joined:</span>
                                <span class="detail-value">{agent['join_date'][:10] if agent['join_date'] else 'N/A'}</span>
                            </div>
                        </div>
            
                        {f'<div class="downline-preview" style="background: #e7f3ff;"><strong>Fund-Based Commission:</strong> Agent gets {agent.get("agent_fund_pct") or 80.0}% of {agent.get("total_commission_fund_pct") or 2.0}% fund</div>' if is_fund_based else ''}
            
                        {f'<div class="downline-preview"><strong>Direct Downlines:</strong> {downline_groups.get(agent["id"], "None")}</div>' if downline_groups.get(agent["id"]) else ''}
                   </div>
        
                   {f'<div class="connector-line" style="left: {padding_left + 15}px;"></div>' if has_downlines else ''}
               </div>
            """

            # Recursively render downlines
            if agent["downlines"]:
                html += f'<div class="downline-container">'
                html += render_tree_html(agent["downlines"], level + 1, agent["id"])
                html += "</div>"

            html += "</div>"

        return html

    hierarchy_html = render_tree_html(hierarchy_tree) if hierarchy_tree else ""

    # Calculate statistics
    total_agents = len(agents)
    top_level_count = sum(1 for agent in agents if agent[3] is None or agent[3] == "")
    with_downlines = sum(1 for agent in agents if agent[19] and int(agent[19]) > 0)
    total_commission = sum(float(agent[21] or 0) for agent in agents)
    
    # Count fund-based vs legacy agents
    fund_based_count = sum(1 for agent in agents if agent[10] == 'fund_based')
    legacy_count = total_agents - fund_based_count

    return render_template(
        "admin/agent_hierarchy.html",
        hierarchy_tree=hierarchy_tree,
        hierarchy_html=hierarchy_html,
        total_agents=total_agents,
        top_level_count=top_level_count,
        with_downlines=with_downlines,
        total_commission=total_commission,
        fund_based_count=fund_based_count,
        legacy_count=legacy_count
    )


@app.route("/admin/add-agent", methods=["GET", "POST"])
def add_agent():
    """Add new agent with upline structure"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get all existing agents for upline selection
    cursor.execute(
        """
        SELECT id, name, email 
        FROM users 
        WHERE role = 'agent' 
        ORDER BY name
    """
    )
    existing_agents = cursor.fetchall()
    conn.close()

    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        password = request.form["password"]
        upline_id = request.form.get("upline_id", None)

        # Set upline commission rate to 0 (admin will set later)
        upline_commission_rate = 0.00

        hashed_pw = generate_password_hash(password)

        conn = sqlite3.connect("real_estate.db")
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO users (email, password, name, role, upline_id, upline_commission_rate)
                VALUES (?, ?, ?, 'agent', ?, ?)
            """,
                (email, hashed_pw, name, upline_id, upline_commission_rate),
            )

            # Get the new agent's ID
            new_agent_id = cursor.lastrowid

            # If upline is specified, update the hierarchy
            if upline_id:
                # You can add hierarchy tracking here if needed
                pass

            conn.commit()
            conn.close()
            return redirect("/admin/agents?success=Agent added successfully!")
        except Exception as e:
            conn.rollback()
            conn.close()
            return f"Error: {str(e)}"

    # GET request - show form
    add_agent_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Add New Agent</title>
        <style>
            body { 
                font-family: Arial, sans-serif; 
                max-width: 600px; 
                margin: 50px auto; 
                padding: 20px; 
                background: #f5f5f5;
            }
            .form-box { 
                background: white; 
                padding: 30px; 
                border-radius: 10px; 
                box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
            }
            h2 { 
                margin-top: 0; 
                color: #333;
                border-bottom: 2px solid #007bff;
                padding-bottom: 10px;
            }
            .form-group {
                margin-bottom: 20px;
            }
            label { 
                display: block; 
                margin-bottom: 8px; 
                font-weight: bold; 
                color: #555;
            }
            input, select { 
                width: 100%; 
                padding: 12px; 
                border: 1px solid #ddd; 
                border-radius: 5px; 
                box-sizing: border-box;
                font-size: 16px;
            }
            input:focus, select:focus {
                border-color: #007bff;
                outline: none;
                box-shadow: 0 0 5px rgba(0,123,255,0.3);
            }
            button { 
                width: 100%; 
                padding: 14px; 
                background: #28a745; 
                color: white; 
                border: none; 
                border-radius: 5px; 
                cursor: pointer; 
                font-size: 16px;
                font-weight: bold;
                margin-top: 10px;
            }
            button:hover { 
                background: #218838; 
            }
            .back-link { 
                display: block; 
                margin-top: 20px; 
                text-align: center; 
                color: #007bff; 
                text-decoration: none;
            }
            .back-link:hover {
                text-decoration: underline;
            }
            .info-box {
                background: #e8f4ff;
                padding: 15px;
                border-radius: 5px;
                margin: 15px 0;
                border-left: 4px solid #007bff;
            }
            .hierarchy-example {
                background: #f0f9ff;
                padding: 15px;
                border-radius: 5px;
                margin: 15px 0;
                font-size: 14px;
                color: #666;
            }
            .hierarchy-example h4 {
                margin-top: 0;
                color: #333;
            }
        </style>
    </head>
    <body>
        <div class="form-box">
            <h2>➕ Add New Agent</h2>
            
            <div class="info-box">
                <strong>📋 Upline System:</strong>
                <p>Each agent can be assigned to an upline (supervising agent). This creates a hierarchy for commission tracking.</p>
            </div>
            
            <div class="hierarchy-example">
                <h4>📊 Example Hierarchy:</h4>
                <ul>
                    <li>Level 1: Eunice (Top Level)</li>
                    <li>Level 2: Erwin (Upline of Derrick)</li>
                    <li>Level 3: Derrick (New agent under Erwin)</li>
                </ul>
                <p><em>Note: Upline commission rate will be set by admin separately.</em></p>
            </div>
            
            <form method="POST">
                <div class="form-group">
                    <label>Full Name *</label>
                    <input type="text" name="name" placeholder="Enter agent's full name" required>
                </div>
                
                <div class="form-group">
                    <label>Email Address *</label>
                    <input type="email" name="email" placeholder="Enter email address" required>
                </div>
                
                <div class="form-group">
                    <label>Password *</label>
                    <input type="password" name="password" placeholder="Create a password" required minlength="6">
                </div>
                
                <div class="form-group">
                    <label>Upline (Optional)</label>
                    <select name="upline_id">
                        <option value="">-- No Upline (Top Level) --</option>
                        {% for agent in existing_agents %}
                        <option value="{{ agent[0] }}">{{ agent[1] }} ({{ agent[2] }})</option>
                        {% endfor %}
                    </select>
                    <small style="color: #666;">Select the supervising agent for this new agent. Leave blank if top level.</small>
                </div>
                
                <div style="background: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <strong> Note:</strong> Upline commission rate will be set to 0% initially. Admin can adjust it later in agent settings.
                </div>
                
                <button type="submit">✅ Create Agent Account</button>
            </form>
            
            <a href="/admin/agents" class="back-link">← Back to Agents List</a>
        </div>
    </body>
    </html>
    """

    return render_template_string(add_agent_template, existing_agents=existing_agents)


@app.route("/admin/edit-agent/<int:agent_id>", methods=["GET", "POST"])
def edit_agent(agent_id):
    """Edit agent details with upline system - UPDATED FOR FUND-BASED COMMISSIONS"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # FIXED QUERY: Removed the # comment which was causing SQL syntax error
    cursor.execute(
        """
        SELECT 
            u.id,
            u.email,
            u.password,
            u.name,
            u.role,
            u.upline_id,
            u.created_at,
            u.upline2_id,
            u.total_listings,
            u.total_commission,
            u.commission_structure,
            u.total_commission_fund_pct,
            u.agent_fund_pct,
            u.upline_fund_pct,
            u.upline2_fund_pct,
            u.company_fund_pct
        FROM users u
        WHERE u.id = ? AND u.role = "agent"
    """,
        (agent_id,),
    )

    agent = cursor.fetchone()

    if not agent:
        conn.close()
        return "Agent not found", 404

    # Get all agents except current one for upline selection
    cursor.execute(
        'SELECT id, name, email FROM users WHERE role = "agent" AND id != ? ORDER BY name',
        (agent_id,),
    )
    existing_agents = cursor.fetchall()

    # Get upline details
    upline_name = "None"
    if agent[5]:  # upline_id (index 5)
        cursor.execute("SELECT name FROM users WHERE id = ?", (agent[5],))
        upline_result = cursor.fetchone()
        upline_name = upline_result[0] if upline_result else "None"

    # Get upline2 details
    upline2_name = "None"
    if agent[7]:  # upline2_id (index 7)
        cursor.execute("SELECT name FROM users WHERE id = ?", (agent[7],))
        upline2_result = cursor.fetchone()
        upline2_name = upline2_result[0] if upline2_result else "None"

    if request.method == "POST":
        try:
            name = request.form["name"]
            email = request.form["email"]
            upline_id = request.form.get("upline_id", None)
            password = request.form.get("password", "")
            
            # NEW: Fund-based commission fields
            commission_structure = request.form.get("commission_structure", "fund_based")
            total_fund_pct = float(request.form.get("total_fund_pct", 2.0))
            agent_fund_pct = float(request.form.get("agent_fund_pct", 80.0))
            upline_fund_pct = float(request.form.get("upline_fund_pct", 10.0))
            upline2_fund_pct = float(request.form.get("upline2_fund_pct", 5.0))
            company_fund_pct = float(request.form.get("company_fund_pct", 5.0))
            
            # Auto-set upline2 based on upline's upline
            upline2_id = None
            if upline_id:
                from app import update_upline_chain
                upline2_id = update_upline_chain(agent_id, upline_id)
            
            # Build update query with NEW commission fields
            if password:
                hashed_pw = generate_password_hash(password)
                cursor.execute(
                    """
                    UPDATE users 
                    SET name = ?, email = ?, 
                        upline_id = ?, upline2_id = ?,
                        commission_structure = ?,
                        total_commission_fund_pct = ?,
                        agent_fund_pct = ?,
                        upline_fund_pct = ?,
                        upline2_fund_pct = ?,
                        company_fund_pct = ?,
                        password = ?
                    WHERE id = ?
                """,
                    (
                        name,
                        email,
                        upline_id,
                        upline2_id,
                        commission_structure,
                        total_fund_pct,
                        agent_fund_pct,
                        upline_fund_pct,
                        upline2_fund_pct,
                        company_fund_pct,
                        hashed_pw,
                        agent_id,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE users 
                    SET name = ?, email = ?, 
                        upline_id = ?, upline2_id = ?,
                        commission_structure = ?,
                        total_commission_fund_pct = ?,
                        agent_fund_pct = ?,
                        upline_fund_pct = ?,
                        upline2_fund_pct = ?,
                        company_fund_pct = ?
                    WHERE id = ?
                """,
                    (
                        name,
                        email,
                        upline_id,
                        upline2_id,
                        commission_structure,
                        total_fund_pct,
                        agent_fund_pct,
                        upline_fund_pct,
                        upline2_fund_pct,
                        company_fund_pct,
                        agent_id,
                    ),
                )
            
            conn.commit()
            conn.close()
            return redirect("/admin/agents?success=Agent updated successfully!")
        
        except Exception as e:
            conn.rollback()
            conn.close()
            return f"Error updating agent: {str(e)}"
    
    # GET request - show edit form
    conn.close()
    
    # Extract values from query result
    # Index mapping based on updated SELECT query:
    # 0: id, 1: email, 2: password, 3: name, 4: role, 5: upline_id, 6: created_at,
    # 7: upline2_id, 8: total_listings, 9: total_commission, 10: commission_structure,
    # 11: total_commission_fund_pct, 12: agent_fund_pct, 13: upline_fund_pct,
    # 14: upline2_fund_pct, 15: company_fund_pct
    
    # Use the updated template with fund-based commissions
    edit_agent_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Edit Agent</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; background: #f5f5f5; }
            .form-box { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            h2 { margin-top: 0; color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }
            .info-box { background: #e8f4ff; padding: 15px; border-radius: 5px; margin: 15px 0; }
            .form-group { margin-bottom: 15px; }
            label { display: block; margin-bottom: 5px; font-weight: bold; color: #555; }
            input, select { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
            button { padding: 12px 25px; background: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; margin-right: 10px; }
            button:hover { background: #0056b3; }
            .btn-secondary { background: #6c757d; }
            .btn-secondary:hover { background: #545b62; }
            .commission-section { 
                background: #f8f9fa; 
                padding: 20px; 
                border-radius: 5px; 
                margin: 20px 0; 
                border: 1px solid #dee2e6;
            }
            .commission-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 15px;
                margin-top: 10px;
            }
            .commission-box {
                background: white;
                padding: 15px;
                border-radius: 5px;
                border-left: 4px solid #007bff;
            }
            .commission-box:nth-child(2) {
                border-left-color: #28a745;
            }
            .commission-box:nth-child(3) {
                border-left-color: #ffc107;
            }
            .commission-box:nth-child(4) {
                border-left-color: #dc3545;
            }
            .commission-box:nth-child(5) {
                border-left-color: #6f42c1;
            }
            small { color: #666; font-size: 13px; display: block; margin-top: 5px; }
            .total-check {
                background: #d1ecf1;
                padding: 10px;
                border-radius: 5px;
                margin-top: 10px;
                font-weight: bold;
            }
        </style>
    </head>
    <body>
        <div class="form-box">
            <h2>✏️ Edit Agent: {{ agent_name }}</h2>
            
            <div class="info-box">
                <p><strong>Agent ID:</strong> #{{ agent_id }}</p>
                <p><strong>Current Email:</strong> {{ agent_email }}</p>
                <p><strong>Current Direct Upline:</strong> {{ upline_name }}</p>
                <p><strong>Current Indirect Upline:</strong> {{ upline2_name }}</p>
                <p><strong>Joined:</strong> {{ join_date }}</p>
                <p><strong>Commission Structure:</strong> {{ commission_structure|upper }}</p>
            </div>
            
            <form method="POST" onsubmit="return validateCommissionTotal()">
                <div class="form-group">
                    <label>Full Name</label>
                    <input type="text" name="name" value="{{ agent_name }}" required>
                </div>
                
                <div class="form-group">
                    <label>Email Address</label>
                    <input type="email" name="email" value="{{ agent_email }}" required>
                </div>
                
                <div class="form-group">
                    <label>Direct Upline</label>
                    <select name="upline_id">
                        <option value="">-- No Direct Upline --</option>
                        {% for agent in existing_agents %}
                        <option value="{{ agent[0] }}" {% if upline_id == agent[0] %}selected{% endif %}>
                            {{ agent[1] }} ({{ agent[2] }})
                        </option>
                        {% endfor %}
                    </select>
                    <small>Indirect upline (Upline 2) will be set automatically based on this selection</small>
                </div>
                
                <div class="commission-section">
                    <h4 style="margin-top: 0; color: #333;">💰 Fund-Based Commission Settings</h4>
                    
                    <div class="form-group">
                        <label>Commission Structure</label>
                        <select name="commission_structure" id="commission_structure" onchange="toggleCommissionType()">
                            <option value="fund_based" {% if commission_structure == 'fund_based' %}selected{% endif %}>
                                Fund-Based (Recommended)
                            </option>
                            <option value="legacy" {% if commission_structure == 'legacy' %}selected{% endif %}>
                                Legacy (Percentage-based)
                            </option>
                        </select>
                        <small>Fund-based: Percentage of sale creates commission fund, then split percentages</small>
                    </div>
                    
                    <div id="fund_based_settings">
                        <div class="commission-grid">
                            <div class="commission-box">
                                <label>Total Fund Percentage (%)</label>
                                <input type="number" name="total_fund_pct" id="total_fund_pct"
                                       value="{{ total_fund_pct|default('2.0') }}" 
                                       min="0.1" max="10" step="0.1" required>
                                <small>Percentage of sale that creates commission fund</small>
                            </div>
                            
                            <div class="commission-box">
                                <label>Agent's Fund Share (%)</label>
                                <input type="number" name="agent_fund_pct" id="agent_fund_pct"
                                       value="{{ agent_fund_pct|default('80.0') }}" 
                                       min="0" max="100" step="0.1" required>
                                <small>Agent's percentage of the commission fund</small>
                            </div>
                            
                            <div class="commission-box">
                                <label>Direct Upline Share (%)</label>
                                <input type="number" name="upline_fund_pct" id="upline_fund_pct"
                                       value="{{ upline_fund_pct|default('10.0') }}" 
                                       min="0" max="100" step="0.1" required>
                                <small>Direct upline's percentage of the commission fund</small>
                            </div>
                            
                            <div class="commission-box">
                                <label>Indirect Upline Share (%)</label>
                                <input type="number" name="upline2_fund_pct" id="upline2_fund_pct"
                                       value="{{ upline2_fund_pct|default('5.0') }}" 
                                       min="0" max="100" step="0.1" required>
                                <small>Indirect upline's percentage of the commission fund</small>
                            </div>
                            
                            <div class="commission-box">
                                <label>Company Balance (%)</label>
                                <input type="number" name="company_fund_pct" id="company_fund_pct"
                                       value="{{ company_fund_pct|default('5.0') }}" 
                                       min="0" max="100" step="0.1" required>
                                <small>Company's percentage of the commission fund</small>
                            </div>
                        </div>
                        
                        <div id="total_check" class="total-check">
                            Total Percentage: <span id="total_percentage">100.0</span>%
                        </div>
                        
                        <div style="margin-top: 15px; padding: 10px; background: #fff3cd; border-radius: 5px;">
                            <strong>💡 Example for RM1,000,000 sale:</strong><br>
                            <span id="example_text">
                                Calculating...
                            </span>
                        </div>
                    </div>
                </div>
                
                <div class="form-group">
                    <label>Password (Leave blank to keep current)</label>
                    <input type="password" name="password" placeholder="Enter new password">
                    <div style="color: #666; font-size: 14px; margin-top: 5px;">
                        Only fill this if you want to change the agent's password
                    </div>
                </div>
                
                <div style="margin-top: 25px;">
                    <button type="submit">💾 Save Changes</button>
                    <a href="/admin/agents" class="btn-secondary" style="padding: 12px 25px; background: #6c757d; color: white; text-decoration: none; border-radius: 5px;">Cancel</a>
                </div>
            </form>
        </div>
        
        <script>
        document.addEventListener('DOMContentLoaded', function() {
            updateCommissionTotal();
            updateExample();
        });
        
        function toggleCommissionType() {
            const structure = document.getElementById('commission_structure').value;
            const fundSettings = document.getElementById('fund_based_settings');
            
            if (structure === 'fund_based') {
                fundSettings.style.display = 'block';
            } else {
                fundSettings.style.display = 'none';
            }
        }
        
        function updateCommissionTotal() {
            const agentPct = parseFloat(document.getElementById('agent_fund_pct').value) || 0;
            const uplinePct = parseFloat(document.getElementById('upline_fund_pct').value) || 0;
            const upline2Pct = parseFloat(document.getElementById('upline2_fund_pct').value) || 0;
            const companyPct = parseFloat(document.getElementById('company_fund_pct').value) || 0;
            
            const total = agentPct + uplinePct + upline2Pct + companyPct;
            const totalElement = document.getElementById('total_percentage');
            
            totalElement.textContent = total.toFixed(1);
            
            if (Math.abs(total - 100.0) > 0.1) {
                totalElement.style.color = '#dc3545';
                totalElement.parentElement.style.background = '#f8d7da';
            } else {
                totalElement.style.color = '#28a745';
                totalElement.parentElement.style.background = '#d1ecf1';
            }
        }
        
        function updateExample() {
            const totalFundPct = parseFloat(document.getElementById('total_fund_pct').value) || 2.0;
            const agentPct = parseFloat(document.getElementById('agent_fund_pct').value) || 80.0;
            const uplinePct = parseFloat(document.getElementById('upline_fund_pct').value) || 10.0;
            const upline2Pct = parseFloat(document.getElementById('upline2_fund_pct').value) || 5.0;
            const companyPct = parseFloat(document.getElementById('company_fund_pct').value) || 5.0;
            
            const saleAmount = 1000000;
            const totalFund = saleAmount * (totalFundPct / 100);
            
            const exampleText = `
                • Total commission fund: RM${saleAmount.toLocaleString()} × ${totalFundPct}% = RM${totalFund.toFixed(2).toLocaleString()}<br>
                • Agent gets: RM${totalFund.toFixed(2).toLocaleString()} × ${agentPct}% = RM${(totalFund * agentPct/100).toFixed(2).toLocaleString()}<br>
                • Direct upline gets: RM${totalFund.toFixed(2).toLocaleString()} × ${uplinePct}% = RM${(totalFund * uplinePct/100).toFixed(2).toLocaleString()}<br>
                • Indirect upline gets: RM${totalFund.toFixed(2).toLocaleString()} × ${upline2Pct}% = RM${(totalFund * upline2Pct/100).toFixed(2).toLocaleString()}<br>
                • Company keeps: RM${totalFund.toFixed(2).toLocaleString()} × ${companyPct}% = RM${(totalFund * companyPct/100).toFixed(2).toLocaleString()}
            `;
            
            document.getElementById('example_text').innerHTML = exampleText;
        }
        
        function validateCommissionTotal() {
            const structure = document.getElementById('commission_structure').value;
            
            if (structure === 'fund_based') {
                const agentPct = parseFloat(document.getElementById('agent_fund_pct').value) || 0;
                const uplinePct = parseFloat(document.getElementById('upline_fund_pct').value) || 0;
                const upline2Pct = parseFloat(document.getElementById('upline2_fund_pct').value) || 0;
                const companyPct = parseFloat(document.getElementById('company_fund_pct').value) || 0;
                
                const total = agentPct + uplinePct + upline2Pct + companyPct;
                
                if (Math.abs(total - 100.0) > 0.1) {
                    if (!confirm(`Commission percentages total ${total.toFixed(1)}%, not 100%. Are you sure you want to save?`)) {
                        return false;
                    }
                }
            }
            
            return true;
        }
        
        // Attach event listeners
        const commissionInputs = [
            'agent_fund_pct', 'upline_fund_pct', 'upline2_fund_pct', 'company_fund_pct', 'total_fund_pct'
        ];
        
        commissionInputs.forEach(id => {
            document.getElementById(id).addEventListener('input', function() {
                updateCommissionTotal();
                updateExample();
            });
        });
        </script>
    </body>
    </html>
    """
    
    return render_template_string(
        edit_agent_template,
        agent_id=agent[0],
        agent_name=agent[3],
        agent_email=agent[1],
        upline_id=agent[5],
        upline_name=upline_name,
        upline2_name=upline2_name,
        commission_structure=agent[10] if len(agent) > 10 and agent[10] else 'fund_based',
        total_fund_pct=agent[11] if len(agent) > 11 and agent[11] else 2.0,
        agent_fund_pct=agent[12] if len(agent) > 12 and agent[12] else 80.0,
        upline_fund_pct=agent[13] if len(agent) > 13 and agent[13] else 10.0,
        upline2_fund_pct=agent[14] if len(agent) > 14 and agent[14] else 5.0,
        company_fund_pct=agent[15] if len(agent) > 15 and agent[15] else 5.0,
        join_date=agent[6][:10] if agent[6] else "Unknown",
        existing_agents=existing_agents,
    )


# Also add the delete agent route (optional but recommended)
@app.route("/admin/delete-agent/<int:agent_id>")
def delete_agent(agent_id):
    """Delete agent (with confirmation)"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Check if agent has any listings
    cursor.execute(
        "SELECT COUNT(*) FROM property_listings WHERE agent_id = ?", (agent_id,)
    )
    listing_count = cursor.fetchone()[0]

    if listing_count > 0:
        conn.close()
        return redirect(
            "/admin/agents?error=Cannot delete agent with existing listings. Reassign listings first."
        )

    try:
        cursor.execute('DELETE FROM users WHERE id = ? AND role = "agent"', (agent_id,))
        conn.commit()
        conn.close()
        return redirect("/admin/agents?success=Agent deleted successfully!")
    except Exception as e:
        conn.rollback()
        conn.close()
        return redirect(f"/admin/agents?error=Error deleting agent: {str(e)}")


@app.route("/admin/commissions")
def commission_report():
    """Commission report page - FIXED VERSION"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get commission data - REMOVED property_type
    cursor.execute(
        """
        SELECT 
            pl.id,
            pl.customer_name,
            u.name as agent_name,
            pl.sale_price,
            pl.commission_amount,
            pl.status,
            pl.approved_at,
            cc.calculation_details
        FROM property_listings pl
        JOIN users u ON pl.agent_id = u.id
        JOIN commission_calculations cc ON pl.id = cc.listing_id
        WHERE pl.status = 'approved'
        ORDER BY pl.approved_at DESC
    """
    )
    commissions = cursor.fetchall()

    # Calculate totals
    cursor.execute(
        """
        SELECT 
            SUM(commission_amount) as total_paid,
            COUNT(*) as total_approved
        FROM property_listings 
        WHERE status = 'approved'
    """
    )
    totals = cursor.fetchone()

    conn.close()

    # Create a properly formatted commissions list - REMOVED property_type
    commissions_list = []
    for comm in commissions:
        commissions_list.append(
            {
                "id": comm[0],
                "customer_name": comm[1],
                "agent_name": comm[2],
                "sale_price": float(comm[3]) if comm[3] else 0,
                "commission_amount": float(comm[4]) if comm[4] else 0,
                "status": comm[5],
                "approved_at": comm[6],
            }
        )

    # Calculate totals safely
    total_paid = float(totals[0]) if totals and totals[0] else 0
    total_approved = totals[1] if totals and totals[1] else 0

    commission_template = """<!DOCTYPE html>
<html>
<head>
    <title>Commission Report</title>
    <style>
        body { 
            font-family: Arial, sans-serif; 
            margin: 20px; 
            background: #f5f5f5; 
        }
        .header { 
            background: white; 
            padding: 20px; 
            border-radius: 10px; 
            margin-bottom: 20px; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
        }
        .stats { 
            display: flex; 
            gap: 15px; 
            margin: 20px 0; 
        }
        .stat-card { 
            background: white; 
            padding: 15px; 
            border-radius: 8px; 
            flex: 1; 
            box-shadow: 0 2px 5px rgba(0,0,0,0.1); 
            text-align: center;
        }
        .stat-card h3 { 
            margin-top: 0; 
            color: #555; 
            font-size: 14px; 
        }
        .stat-value { 
            font-size: 1.8em; 
            font-weight: bold; 
            color: #28a745; 
        }
        table { 
            width: 100%; 
            background: white; 
            border-radius: 10px; 
            overflow: hidden; 
            margin: 20px 0; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
        }
        th, td { 
            padding: 12px 15px; 
            text-align: left; 
            border-bottom: 1px solid #eee; 
        }
        th { 
            background: #2c3e50; 
            color: white; 
        }
        .btn { 
            padding: 8px 16px; 
            background: #007bff; 
            color: white; 
            text-decoration: none; 
            border-radius: 5px; 
            display: inline-block;
        }
        .btn:hover {
            background: #0056b3;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>💰 Commission Report</h1>
        <div>
            <a href="/admin/dashboard" class="btn">← Dashboard</a> | 
            <a href="/admin/export-data?type=commissions" class="btn">📤 Export to CSV</a>
        </div>
    </div>
    
    <div class="stats">
        <div class="stat-card">
            <h3>Total Commission Paid</h3>
            <div class="stat-value">RM{{ "%.2f"|format(total_paid) }}</div>
        </div>
        <div class="stat-card">
            <h3>Approved Transactions</h3>
            <div class="stat-value">{{ total_approved }}</div>
        </div>
    </div>
    
    <h2>Approved Commissions</h2>
    {% if commissions_list %}
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Customer</th>
                <th>Agent</th>
                <th>Sale Price</th>
                <th>Commission</th>
                <th>Approved Date</th>
            </tr>
        </thead>
        <tbody>
            {% for comm in commissions_list %}
            <tr>
                <td>#{{ comm.id }}</td>
                <td>{{ comm.customer_name }}</td>
                <td>{{ comm.agent_name }}</td>
                <td>RM{{ "%.2f"|format(comm.sale_price) }}</td>
                <td><strong>RM{{ "%.2f"|format(comm.commission_amount) }}</strong></td>
                <td>{{ comm.approved_at[:10] if comm.approved_at else '' }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div style="padding: 40px; text-align: center; background: white; border-radius: 10px;">
        <h3>No approved commissions yet</h3>
        <p>No commissions have been approved yet. Once agents submit sales and they are approved, they will appear here.</p>
        <a href="/admin/dashboard" class="btn" style="margin-top: 15px;">Check Pending Submissions</a>
    </div>
    {% endif %}
</body>
</html>"""

    return render_template_string(
        commission_template,
        commissions_list=commissions_list,
        total_paid=total_paid,
        total_approved=total_approved,
    )


def get_indirect_upline_rate(direct_upline_id):
    """Get commission rate for indirect upline"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Default indirect rate is 50% of direct rate
    cursor.execute(
        "SELECT upline_commission_rate FROM users WHERE id = ?", (direct_upline_id,)
    )
    direct_rate = cursor.fetchone()

    conn.close()

    if direct_rate and direct_rate[0]:
        # Indirect gets half of direct rate (e.g., 2.5% if direct is 5%)
        return direct_rate[0] / 2
    else:
        return 2.5  # Default 2.5%


def get_total_commissions():
    """Get total commissions including upline commissions"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        # 1. Get total commissions from property_listings (agent commissions)
        cursor.execute(
            """
            SELECT SUM(commission_amount) 
            FROM property_listings 
            WHERE status = 'approved'
        """
        )
        agent_commissions = cursor.fetchone()[0] or 0

        # 2. Get total upline commissions from commission_payments
        # Note: These are commissions that uplines earn from their downlines
        cursor.execute(
            """
            SELECT SUM(commission_amount) 
            FROM commission_payments 
            WHERE payment_status != 'rejected'
        """
        )
        all_commissions = cursor.fetchone()[0] or 0

        # Total = Agent commissions + Upline commissions
        # But careful: commission_payments includes BOTH agent and upline payments
        # We need to separate them

        # 3. Better approach: Get distinct totals
        # Agent's own commissions from their sales
        cursor.execute(
            """
            SELECT SUM(cp.commission_amount) 
            FROM commission_payments cp
            JOIN property_listings pl ON cp.listing_id = pl.id
            WHERE pl.agent_id = cp.agent_id  # Agent's own commissions
            AND cp.payment_status != 'rejected'
        """
        )
        agent_own_commissions = cursor.fetchone()[0] or 0

        # Upline commissions (where payment is to upline, not the selling agent)
        cursor.execute(
            """
            SELECT SUM(cp.commission_amount) 
            FROM commission_payments cp
            JOIN property_listings pl ON cp.listing_id = pl.id
            WHERE cp.agent_id != pl.agent_id  # Upline commissions
            AND cp.payment_status != 'rejected'
        """
        )
        upline_commissions = cursor.fetchone()[0] or 0

        return {
            "total_all_commissions": agent_own_commissions + upline_commissions,
            "agent_own_commissions": agent_own_commissions,
            "upline_commissions": upline_commissions,
        }

    except Exception as e:
        print(f"Error calculating total commissions: {e}")
        return {
            "total_all_commissions": 0,
            "agent_own_commissions": 0,
            "upline_commissions": 0,
        }
    finally:
        conn.close()


@app.route("/admin/reports")
def reports_dashboard():
    """Reports dashboard"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    reports_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Reports</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
            .report-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 20px; margin: 20px 0; }
            .report-card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center; }
            .report-card:hover { transform: translateY(-5px); transition: 0.3s; }
            .report-icon { font-size: 40px; margin-bottom: 10px; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>📈 Reports & Analytics</h1>
            <div>
                <a href="/admin/dashboard">← Dashboard</a>
            </div>
        </div>
        
        <div class="report-cards">
            <a href="/admin/commissions" style="text-decoration: none; color: inherit;">
                <div class="report-card">
                    <div class="report-icon">💰</div>
                    <h3>Commission Report</h3>
                    <p>View all commission payments</p>
                </div>
            </a>
            
            <a href="/admin/sales-report" style="text-decoration: none; color: inherit;">
                <div class="report-card">
                    <div class="report-icon">📊</div>
                    <h3>Sales Report</h3>
                    <p>Monthly sales analytics</p>
                </div>
            </a>
            
            <a href="/admin/agent-performance" style="text-decoration: none; color: inherit;">
                <div class="report-card">
                    <div class="report-icon">👥</div>
                    <h3>Agent Performance</h3>
                    <p>Agent rankings and stats</p>
                </div>
            </a>
            
            <a href="/admin/export-data" style="text-decoration: none; color: inherit;">
                <div class="report-card">
                    <div class="report-icon">📤</div>
                    <h3>Data Export</h3>
                    <p>Export to Excel/CSV</p>
                </div>
            </a>
        </div>
    </body>
    </html>
    """

    return render_template_string(reports_template)


@app.route("/admin/settings")
def admin_settings():
    """System settings page"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    # Get current settings
    payment_settings = get_payment_settings()
    notification_settings = get_notification_settings()

    settings_template = (
        """
    <!DOCTYPE html>
    <html>
    <head>
        <title>System Settings</title>
        <style>
            body { 
                font-family: Arial, sans-serif; 
                margin: 20px; 
                background: #f5f5f5; 
                max-width: 1000px; 
            }
            .header { 
                background: white; 
                padding: 20px; 
                border-radius: 10px; 
                margin-bottom: 20px; 
                box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
            }
            .settings-section { 
                background: white; 
                padding: 25px; 
                border-radius: 10px; 
                margin: 20px 0; 
                box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
            }
            .form-group { 
                margin-bottom: 15px; 
            }
            label { 
                display: block; 
                margin-bottom: 5px; 
                font-weight: bold; 
                color: #555;
            }
            input, select, textarea { 
                width: 100%; 
                padding: 10px; 
                border: 1px solid #ddd; 
                border-radius: 5px; 
                box-sizing: border-box;
            }
            button { 
                padding: 10px 20px; 
                background: #007bff; 
                color: white; 
                border: none; 
                border-radius: 5px; 
                cursor: pointer; 
                margin-top: 10px;
            }
            .btn { 
                padding: 10px 20px; 
                background: #007bff; 
                color: white; 
                border: none; 
                border-radius: 5px; 
                cursor: pointer; 
                text-decoration: none;
                display: inline-block;
            }
            .checkbox-group {
                margin: 10px 0;
            }
            .checkbox-group label {
                display: flex;
                align-items: center;
                margin-bottom: 8px;
                font-weight: normal;
            }
            .checkbox-group input[type="checkbox"] {
                width: auto;
                margin-right: 10px;
            }
            .setting-note {
                font-size: 12px;
                color: #666;
                margin-top: 5px;
                display: block;
            }
            .success-message {
                background: #d4edda;
                color: #155724;
                padding: 10px 15px;
                border-radius: 5px;
                margin-bottom: 15px;
                border: 1px solid #c3e6cb;
            }
            .error-message {
                background: #f8d7da;
                color: #721c24;
                padding: 10px 15px;
                border-radius: 5px;
                margin-bottom: 15px;
                border: 1px solid #f5c6cb;
            }
            .nav {
                margin-top: 10px;
            }
            .nav a {
                margin-right: 15px;
                color: #007bff;
                text-decoration: none;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>⚙️ System Settings</h1>
            <div class="nav">
                <a href="/admin/dashboard">← Dashboard</a>
            </div>
        </div>
        
        <!-- Display success/error messages -->
        """
        + """
        {% if success %}
        <div class="success-message">✅ {{ success }}</div>
        {% endif %}
        
        {% if error %}
        <div class="error-message">❌ {{ error }}</div>
        {% endif %}
        """
        + '''
        
        <!-- ============ PAYMENT SETTINGS ============ -->
        <div class="settings-section">
            <h2>💰 Payment & Payout Settings</h2>
            <form method="POST" action="/admin/update-payment-settings">
                <div class="form-group">
                    <label>Payment Processing Days</label>
                    <input type="number" name="processing_days" value="'''
        + str(payment_settings["processing_days"])
        + '''" 
                           min="1" max="60" required>
                    <span class="setting-note">Days until commission is paid after approval</span>
                </div>
                
                <div class="form-group">
                    <label>Minimum Payout Amount (RM)</label>
                    <input type="number" name="min_payout" value="'''
        + str(payment_settings["min_payout"])
        + """" 
                           step="10" min="0" required>
                    <span class="setting-note">Minimum commission balance for payout</span>
                </div>
                
                <div class="form-group">
                    <label>Payout Schedule</label>
                    <select name="payout_schedule" required>
                        <option value="weekly" """
        + ("selected" if payment_settings["payout_schedule"] == "weekly" else "")
        + """>
                            Weekly (Every Friday)
                        </option>
                        <option value="biweekly" """
        + ("selected" if payment_settings["payout_schedule"] == "biweekly" else "")
        + """>
                            Bi-weekly
                        </option>
                        <option value="monthly" """
        + ("selected" if payment_settings["payout_schedule"] == "monthly" else "")
        + """>
                            Monthly (End of month)
                        </option>
                        <option value="immediate" """
        + ("selected" if payment_settings["payout_schedule"] == "immediate" else "")
        + """>
                            Immediate (After approval)
                        </option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Auto-Generate Payment Voucher</label>
                    <select name="auto_generate_voucher" required>
                        <option value="yes" """
        + ("selected" if payment_settings["auto_generate_voucher"] == "yes" else "")
        + """>
                            Yes, auto-generate when marked paid
                        </option>
                        <option value="no" """
        + ("selected" if payment_settings["auto_generate_voucher"] == "no" else "")
        + """>
                            No, generate manually
                        </option>
                    </select>
                    <span class="setting-note">Automatically generate and email payment voucher when commission is marked as paid</span>
                </div>
                
                <div class="form-group">
                    <label>Voucher Email Template</label>
                    <select name="voucher_template" required>
                        <option value="simple" """
        + ("selected" if payment_settings["voucher_template"] == "simple" else "")
        + """>
                            Simple Text
                        </option>
                        <option value="detailed" """
        + ("selected" if payment_settings["voucher_template"] == "detailed" else "")
        + """>
                            Detailed HTML
                        </option>
                        <option value="receipt" """
        + ("selected" if payment_settings["voucher_template"] == "receipt" else "")
        + '''>
                            Official Receipt
                        </option>
                    </select>
                    <span class="setting-note">Template for payment voucher emails</span>
                </div>
                
                <div class="form-group">
                    <label>Payment Voucher Prefix</label>
                    <input type="text" name="voucher_prefix" value="'''
        + payment_settings["voucher_prefix"]
        + """" 
                           maxlength="10">
                    <span class="setting-note">Prefix for voucher numbers (e.g., PAY-2024-001)</span>
                </div>
                
                <div class="form-group">
                    <label>Payment Methods Allowed</label>
                    <div class="checkbox-group">
                        <label>
                            <input type="checkbox" name="payment_methods" value="bank_transfer" 
                                   """
        + ("checked" if "bank_transfer" in payment_settings["payment_methods"] else "")
        + """>
                            Bank Transfer
                        </label>
                        <label>
                            <input type="checkbox" name="payment_methods" value="check" 
                                   """
        + ("checked" if "check" in payment_settings["payment_methods"] else "")
        + """>
                            Check
                        </label>
                        <label>
                            <input type="checkbox" name="payment_methods" value="paypal" 
                                   """
        + ("checked" if "paypal" in payment_settings["payment_methods"] else "")
        + """>
                            PayPal
                        </label>
                        <label>
                            <input type="checkbox" name="payment_methods" value="cash" 
                                   """
        + ("checked" if "cash" in payment_settings["payment_methods"] else "")
        + """>
                            Cash
                        </label>
                    </div>
                </div>
                
                <button type="submit">💾 Save Payment Settings</button>
            </form>
        </div>
        
        <!-- ============ NOTIFICATION SETTINGS ============ -->
        <div class="settings-section">
            <h2>📧 Notification & Email Settings</h2>
            <form method="POST" action="/admin/update-notification-settings">
                <div class="form-group">
                    <label>Email Notifications</label>
                    <div class="checkbox-group">
                        <label>
                            <input type="checkbox" name="notifications" value="submission_received" 
                                   """
        + (
            "checked"
            if "submission_received" in notification_settings["notifications"]
            else ""
        )
        + """>
                            New submission received (Admin)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="submission_approved" 
                                   """
        + (
            "checked"
            if "submission_approved" in notification_settings["notifications"]
            else ""
        )
        + """>
                            Submission approved (Agent)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="payment_processed" 
                                   """
        + (
            "checked"
            if "payment_processed" in notification_settings["notifications"]
            else ""
        )
        + """>
                            Payment processed with voucher (Agent)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="monthly_report" 
                                   """
        + (
            "checked"
            if "monthly_report" in notification_settings["notifications"]
            else ""
        )
        + """>
                            Monthly performance report (Agent)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="upline_earnings" 
                                   """
        + (
            "checked"
            if "upline_earnings" in notification_settings["notifications"]
            else ""
        )
        + """>
                            Upline commission earned (Upline Agent)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="reminders" 
                                   """
        + ("checked" if "reminders" in notification_settings["notifications"] else "")
        + '''>
                            Pending submission reminders (Agent)
                        </label>
                    </div>
                </div>
                
                <div class="form-group">
                    <label>Auto-Approval Threshold (RM)</label>
                    <input type="number" name="auto_approve_threshold" 
                           value="'''
        + str(notification_settings["auto_approve_threshold"])
        + '''" 
                           step="100" min="0">
                    <span class="setting-note">Submissions below this amount auto-approve (0 = disabled)</span>
                </div>
                
                <div class="form-group">
                    <label>Reminder Days</label>
                    <input type="number" name="reminder_days" 
                           value="'''
        + str(notification_settings["reminder_days"])
        + '''" 
                           min="1" max="14">
                    <span class="setting-note">Days before sending reminder for pending submissions</span>
                </div>
                
                <div class="form-group">
                    <label>Admin Notification Email</label>
                    <input type="email" name="admin_email" 
                           value="'''
        + notification_settings["admin_email"]
        + '''" 
                           required>
                    <span class="setting-note">Email for receiving system notifications</span>
                </div>
                
                <div class="form-group">
                    <label>System From Email</label>
                    <input type="email" name="system_from_email" 
                           value="'''
        + notification_settings["system_from_email"]
        + '''" 
                           required>
                    <span class="setting-note">Email address shown as sender</span>
                </div>
                
                <div class="form-group">
                    <label>SMTP Server Configuration</label>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 5px;">
                        <input type="text" name="smtp_server" placeholder="SMTP Server" 
                               value="'''
        + notification_settings["smtp_server"]
        + '''">
                        <input type="number" name="smtp_port" placeholder="Port" 
                               value="'''
        + notification_settings["smtp_port"]
        + '''">
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px;">
                        <input type="text" name="smtp_username" placeholder="Username" 
                               value="'''
        + notification_settings["smtp_username"]
        + '''">
                        <input type="password" name="smtp_password" placeholder="Password" 
                               value="'''
        + notification_settings["smtp_password"]
        + """">
                    </div>
                    <span class="setting-note">Leave blank to use default system mail</span>
                </div>
                
                <div class="form-group">
                    <label>Email Footer Text</label>
                    <textarea name="email_footer" rows="3" placeholder="Email footer text...">"""
        + notification_settings["email_footer"]
        + """</textarea>
                </div>
                
                <button type="submit">💾 Save Notification Settings</button>
            </form>
        </div>
        
        <!-- ============ SYSTEM MAINTENANCE ============ -->
        <div class="settings-section">
            <h2> System Maintenance</h2>
            <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                <a href="/admin/backup-database" class="btn" style="background: #28a745;">💾 Backup Database</a>
                <a href="/admin/clear-cache" class="btn" style="background: #ffc107;">🧹 Clear Cache</a>
                <a href="/admin/system-logs" class="btn" style="background: #17a2b8;">📋 View Logs</a>
                <a href="/admin/test-email" class="btn" style="background: #6f42c1;">📧 Test Email System</a>
                <a href="/admin/send-test-voucher" class="btn" style="background: #fd7e14;">🧾 Test Payment Voucher</a>
            </div>
        </div>
    </body>
    </html>
    """
    )

    # Check for success/error messages in URL parameters
    success_msg = request.args.get("success")
    error_msg = request.args.get("error")

    return render_template_string(
        settings_template, success=success_msg, error=error_msg
    )


# ============ SETTINGS MANAGEMENT FUNCTIONS ============
def get_system_setting(setting_type, setting_key, default=None):
    """Get system setting from database"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT setting_value FROM system_settings WHERE setting_type = ? AND setting_key = ?",
        (setting_type, setting_key),
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else default


def save_system_setting(setting_type, setting_key, value):
    """Save system setting to database"""
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO system_settings (setting_type, setting_key, setting_value, updated_at)
        VALUES (?, ?, ?, ?)
    """,
        (
            setting_type,
            setting_key,
            value,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()


def get_payment_settings():
    """Get all payment settings as dictionary"""
    return {
        "processing_days": int(get_system_setting("payment", "processing_days", 14)),
        "min_payout": float(get_system_setting("payment", "min_payout", 100)),
        "payout_schedule": get_system_setting("payment", "payout_schedule", "monthly"),
        "auto_generate_voucher": get_system_setting(
            "payment", "auto_generate_voucher", "yes"
        ),
        "voucher_template": get_system_setting(
            "payment", "voucher_template", "detailed"
        ),
        "voucher_prefix": get_system_setting("payment", "voucher_prefix", "PAY"),
        "payment_methods": get_system_setting(
            "payment", "payment_methods", "bank_transfer,check"
        ).split(","),
    }


def get_notification_settings():
    """Get all notification settings as dictionary"""
    return {
        "notifications": get_system_setting(
            "notification",
            "notifications",
            "submission_received,submission_approved,payment_processed,reminders",
        ).split(","),
        "auto_approve_threshold": float(
            get_system_setting("notification", "auto_approve_threshold", 0)
        ),
        "reminder_days": int(get_system_setting("notification", "reminder_days", 3)),
        "admin_email": get_system_setting(
            "notification", "admin_email", "admin@example.com"
        ),
        "system_from_email": get_system_setting(
            "notification", "system_from_email", "noreply@realestate.com"
        ),
        "smtp_server": get_system_setting("notification", "smtp_server", ""),
        "smtp_port": get_system_setting("notification", "smtp_port", ""),
        "smtp_username": get_system_setting("notification", "smtp_username", ""),
        "smtp_password": get_system_setting("notification", "smtp_password", ""),
        "email_footer": get_system_setting(
            "notification",
            "email_footer",
            "© 2024 Real Estate System. All rights reserved.",
        ),
    }


@app.route("/admin/update-payment-settings", methods=["POST"])
def update_payment_settings():
    """Update payment settings"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    try:
        data = request.form
        sale_type = data.get("sale_type", "sales")  # Default to sales

        # Save payment settings
        save_system_setting("payment", "processing_days", data["processing_days"])
        save_system_setting("payment", "min_payout", data["min_payout"])
        save_system_setting("payment", "payout_schedule", data["payout_schedule"])
        save_system_setting(
            "payment", "auto_generate_voucher", data["auto_generate_voucher"]
        )
        save_system_setting("payment", "voucher_template", data["voucher_template"])
        save_system_setting("payment", "voucher_prefix", data["voucher_prefix"])

        # Handle checkboxes for payment methods
        payment_methods = request.form.getlist("payment_methods")
        save_system_setting("payment", "payment_methods", ",".join(payment_methods))

        return redirect("/admin/settings?success=Payment+settings+updated+successfully")

    except Exception as e:
        return redirect(f"/admin/settings?error={str(e)}")


@app.route("/admin/update-notification-settings", methods=["POST"])
def update_notification_settings():
    """Update notification settings"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    try:
        data = request.form
        sale_type = data.get("sale_type", "sales")  # Default to sales

        # Save notification settings
        notifications = request.form.getlist("notifications")
        save_system_setting("notification", "notifications", ",".join(notifications))

        save_system_setting(
            "notification", "auto_approve_threshold", data["auto_approve_threshold"]
        )
        save_system_setting("notification", "reminder_days", data["reminder_days"])
        save_system_setting("notification", "admin_email", data["admin_email"])
        save_system_setting(
            "notification", "system_from_email", data["system_from_email"]
        )
        save_system_setting("notification", "smtp_server", data["smtp_server"])
        save_system_setting("notification", "smtp_port", data["smtp_port"])
        save_system_setting("notification", "smtp_username", data["smtp_username"])
        save_system_setting("notification", "smtp_password", data["smtp_password"])
        save_system_setting("notification", "email_footer", data["email_footer"])

        return redirect(
            "/admin/settings?success=Notification+settings+updated+successfully"
        )

    except Exception as e:
        return redirect(f"/admin/settings?error={str(e)}")


# ============ VOUCHER SYSTEM FUNCTIONS ============
import random
import string

def generate_voucher_number(prefix="PAY"):
    """Generate unique voucher number"""
    timestamp = datetime.now().strftime("%Y%m%d")
    random_str = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{prefix}-{timestamp}-{random_str}"

def create_payment_voucher(payment_id, agent_id, amount, payment_date, payment_method):
    """Create payment voucher record - SIMPLIFIED VERSION"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        voucher_number = generate_voucher_number(
            get_system_setting("payment", "voucher_prefix", "PAY")
        )

        cursor.execute(
            """
            INSERT INTO payment_vouchers 
            (voucher_number, payment_id, agent_id, amount, payment_date, payment_method, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """,
            (voucher_number, payment_id, agent_id, amount, payment_date, payment_method),
        )

        voucher_id = cursor.lastrowid
        conn.commit()
        
        return voucher_id, voucher_number

    except Exception as e:
        print(f"❌ Error creating payment voucher: {e}")
        raise e
    finally:
        if conn:
            conn.close()

@app.route("/admin/approve/<int:listing_id>")
def approve_listing(listing_id):
    """UPDATED: Approve listing using FUND-BASED commission system"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = None
    try:
        conn = sqlite3.connect("real_estate.db")
        cursor = conn.cursor()

        # 1. Get listing details WITH FUND-BASED FIELDS
        cursor.execute(
            """
            SELECT pl.*, 
                   u.name as agent_name, 
                   u.upline_id, 
                   u.upline2_id,
                   -- FUND-BASED FIELDS:
                   u.commission_structure,
                   u.total_commission_fund_pct,
                   u.agent_fund_pct,
                   u.upline_fund_pct,
                   u.upline2_fund_pct,
                   u.company_fund_pct
            FROM property_listings pl
            JOIN users u ON pl.agent_id = u.id
            WHERE pl.id = ?
        """,
            (listing_id,),
        )

        listing = cursor.fetchone()

        if not listing:
            flash("❌ Listing not found", "error")
            return redirect("/admin/documents")

        if listing[8] == "approved":  # status column
            flash("⚠️ Listing already approved", "warning")
            return redirect(f"/admin/documents/{listing_id}")

        agent_id = listing[1]
        agent_name = listing[20] if len(listing) > 20 else "Unknown"
        sale_price = listing[7]  # sale_price column
        direct_upline_id = listing[22] if len(listing) > 22 else None
        upline2_id = listing[23] if len(listing) > 23 else None
        
        # FUND-BASED FIELDS (indices based on SELECT query above)
        commission_structure = listing[24] if len(listing) > 24 else 'fund_based'
        total_fund_pct = float(listing[25]) if len(listing) > 25 and listing[25] is not None else 2.0
        agent_fund_pct = float(listing[26]) if len(listing) > 26 and listing[26] is not None else 80.0
        upline_fund_pct = float(listing[27]) if len(listing) > 27 and listing[27] is not None else 10.0
        upline2_fund_pct = float(listing[28]) if len(listing) > 28 and listing[28] is not None else 5.0
        company_fund_pct = float(listing[29]) if len(listing) > 29 and listing[29] is not None else 5.0

        # 2. Update listing status
        cursor.execute(
            """
            UPDATE property_listings 
            SET status = 'approved', 
                approved_at = ?,
                approved_by = ?,
                commission_status = 'pending'
            WHERE id = ?
        """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                session["user_id"],
                listing_id,
            ),
        )

        # 3. FUND-BASED COMMISSION CALCULATION
        # Calculate total commission fund
        total_fund = sale_price * (total_fund_pct / 100)
        
        # Agent's share
        agent_payment_amount = total_fund * (agent_fund_pct / 100)
        
        # Create AGENT commission payment
        cursor.execute(
            """
            INSERT INTO commission_payments
            (listing_id, agent_id, commission_amount, payment_status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
        """,
            (
                listing_id,
                agent_id,
                agent_payment_amount,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

        # 4. Create DIRECT upline commission using FUND-BASED rate
        if direct_upline_id and upline_fund_pct > 0:
            direct_commission = total_fund * (upline_fund_pct / 100)

            # 4a. Upline commission record (direct) - USE FUND-BASED RATE
            cursor.execute(
                """
                INSERT INTO upline_commissions
                (listing_id, agent_id, upline_id, amount, status, 
                 commission_type, commission_rate, created_at)
                VALUES (?, ?, ?, ?, 'pending', 'direct', ?, ?)
            """,
                (
                    listing_id,
                    agent_id,
                    direct_upline_id,
                    direct_commission,
                    upline_fund_pct,  # USE FUND-BASED RATE (10%), not legacy 5%
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

        # 5. Create INDIRECT upline commission using FUND-BASED rate
        if upline2_id and upline2_fund_pct > 0:
            indirect_commission = total_fund * (upline2_fund_pct / 100)

            # NO commission_payments for indirect upline either!
            # Only upline_commissions record
            cursor.execute("""
                INSERT INTO upline_commissions
                (listing_id, agent_id, upline_id, amount, status, 
                 commission_type, commission_rate, created_at)
                VALUES (?, ?, ?, ?, 'pending', 'indirect', ?, ?)
            """, (
                listing_id,
                agent_id,  # Selling agent (Erwin)
                upline2_id,  # Indirect upline (Edmond)
                indirect_commission,
                upline2_fund_pct,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ))

        # 6. COMPANY balance (optional - can be saved to separate table)
        if company_fund_pct > 0:
            company_balance = total_fund * (company_fund_pct / 100)
            # You might want to save this to a company_earnings table
            # cursor.execute("INSERT INTO company_earnings ...", (listing_id, company_balance, ...))

        # 7. Save calculation details to commission_calculations table
        cursor.execute(
            """
            INSERT INTO commission_calculations
            (listing_id, agent_id, sale_price, base_rate, commission, calculation_details)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                listing_id,
                agent_id,
                sale_price,
                total_fund_pct,
                agent_payment_amount,
                json.dumps({
                    "commission_source": "fund_based",
                    "total_fund_percentage": total_fund_pct,
                    "total_commission_fund": float(total_fund),
                    "agent_fund_pct": agent_fund_pct,
                    "upline_fund_pct": upline_fund_pct,
                    "upline2_fund_pct": upline2_fund_pct,
                    "company_fund_pct": company_fund_pct,
                    "commission_structure": commission_structure,
                    "calculated_at": datetime.now().isoformat()
                })
            ),
        )

        # 8. Update agent's total commission
        cursor.execute(
            """
            UPDATE users 
            SET total_commission = COALESCE(total_commission, 0) + ? 
            WHERE id = ?
        """,
            (agent_payment_amount, agent_id),
        )

        # Update direct upline's total commission
        if direct_upline_id and upline_fund_pct > 0:
            cursor.execute(
                """
                UPDATE users 
                SET total_commission = COALESCE(total_commission, 0) + ? 
                WHERE id = ?
            """,
                (direct_commission, direct_upline_id),
            )

        # Update indirect upline's total commission
        if upline2_id and upline2_fund_pct > 0:
            cursor.execute(
                """
                UPDATE users 
                SET total_commission = COALESCE(total_commission, 0) + ? 
                WHERE id = ?
            """,
                (indirect_commission, upline2_id),
            )

        # 9. Create notification for agent
        cursor.execute(
            """
            INSERT INTO agent_notifications
            (agent_id, title, message, notification_type, 
             related_id, related_type, priority, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                agent_id,
                "✅ Listing Approved",
                f"Your submission #{listing_id} has been approved.",
                "listing_approved",
                listing_id,
                "listing",
                "high",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

        # 10. COMMIT EVERYTHING
        conn.commit()
        conn.close()

        flash(f"✅ Listing #{listing_id} approved! Fund-based commissions calculated.", "success")
        return redirect(f"/admin/documents/{listing_id}")

    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()

        flash(f"❌ Approval failed: {str(e)}", "error")
        return redirect(f"/admin/documents/{listing_id}")


@app.route("/admin/reject/<int:listing_id>", methods=["GET", "POST"])
def reject_listing(listing_id):
    """Reject listing with reason"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    if request.method == "POST":
        rejection_reason = request.form.get("rejection_reason", "")

        conn = sqlite3.connect("real_estate.db")
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE property_listings 
            SET status = 'rejected', 
                commission_status = 'rejected',
                rejection_reason = ?
            WHERE id = ?
        """,
            (rejection_reason, listing_id),
        )

        conn.commit()
        conn.close()

        return redirect("/admin/dashboard")

    # GET request - show rejection form - FIXED VERSION
    rejection_template = (
        """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Reject Submission</title>
        <style>
            body { 
                font-family: Arial, sans-serif; 
                max-width: 600px; 
                margin: 50px auto; 
                padding: 20px;
                background: #f5f5f5;
            }
            .form-box { 
                background: white; 
                padding: 30px; 
                border-radius: 10px; 
                box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
            }
            h2 { 
                margin-top: 0; 
                color: #dc3545; 
            }
            textarea { 
                width: 100%; 
                padding: 15px; 
                margin: 15px 0; 
                border: 1px solid #ddd; 
                border-radius: 5px; 
                min-height: 150px;
                box-sizing: border-box;
            }
            .reason-options { 
                margin: 15px 0; 
            }
            .reason-btn { 
                display: block; 
                width: 100%; 
                padding: 10px; 
                margin: 5px 0; 
                background: #f8f9fa; 
                border: 1px solid #ddd; 
                border-radius: 5px; 
                text-align: left; 
                cursor: pointer;
                color: #333;  /* FIXED: Added text color */
                font-size: 14px;
            }
            .reason-btn:hover { 
                background: #e9ecef;
                border-color: #007bff;
            }
            button[type="submit"] { 
                padding: 12px 25px; 
                background: #dc3545; 
                color: white; 
                border: none; 
                border-radius: 5px; 
                cursor: pointer; 
                margin-right: 10px;
                font-size: 16px;
            }
            button[type="submit"]:hover { 
                background: #c82333; 
            }
            .btn-cancel { 
                padding: 12px 25px; 
                background: #6c757d; 
                color: white; 
                text-decoration: none; 
                border-radius: 5px;
                display: inline-block;
                font-size: 16px;
            }
            .btn-cancel:hover { 
                background: #545b62; 
            }
            .form-actions {
                margin-top: 20px;
                display: flex;
                gap: 10px;
                align-items: center;
            }
        </style>
    </head>
    <body>
        <div class="form-box">
            <h2>❌ Reject Submission #"""
        + str(listing_id)
        + """</h2>
            <p>Please provide a reason for rejection. This will be visible to the agent.</p>
            
            <form method="POST">
                <div class="reason-options">
                    <strong>Common Reasons (click to select):</strong>
                    <button type="button" class="reason-btn" onclick="document.getElementById('reason').value='Missing or incomplete documents'">
                        📄 Missing or incomplete documents
                    </button>
                    <button type="button" class="reason-btn" onclick="document.getElementById('reason').value='Incorrect or insufficient information'">
                        📝 Incorrect or insufficient information
                    </button>
                    <button type="button" class="reason-btn" onclick="document.getElementById('reason').value='Commission calculation error'">
                        💰 Commission calculation error
                    </button>
                    <button type="button" class="reason-btn" onclick="document.getElementById('reason').value='Customer verification required'">
                        👤 Customer verification required
                    </button>
                    <button type="button" class="reason-btn" onclick="document.getElementById('reason').value='Property documentation incomplete'">
                        🏠 Property documentation incomplete
                    </button>
                    <button type="button" class="reason-btn" onclick="document.getElementById('reason').value='Failed agent verification check'">
                        🛡️ Failed agent verification check
                    </button>
                    <button type="button" class="reason-btn" onclick="document.getElementById('reason').value='Customer information mismatch'">
                        🔍 Customer information mismatch
                    </button>
                </div>
                
                <textarea id="reason" name="rejection_reason" placeholder="Enter rejection reason here..." required></textarea>
                
                <div class="form-actions">
                    <button type="submit">Confirm Rejection</button>
                    <a href="/admin/dashboard" class="btn-cancel">Cancel</a>
                </div>
            </form>
        </div>
        
        <script>
        document.addEventListener('DOMContentLoaded', function() {
            // Add click handlers to reason buttons
            document.querySelectorAll('.reason-btn').forEach(btn => {
                btn.addEventListener('click', function() {
                    document.getElementById('reason').value = this.textContent.trim();
                    document.getElementById('reason').focus();
                    this.style.background = '#d4edda';
                    this.style.borderColor = '#28a745';
                    
                    // Reset other buttons
                    document.querySelectorAll('.reason-btn').forEach(otherBtn => {
                        if (otherBtn !== this) {
                            otherBtn.style.background = '#f8f9fa';
                            otherBtn.style.borderColor = '#ddd';
                        }
                    });
                });
            });
        });
        </script>
    </body>
    </html>
    """
    )

    return render_template_string(rejection_template)

@app.route("/admin/payments")
def admin_payments():
    """Payment management page with BOTH agent and upline payments"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get payment status filter
    status_filter = request.args.get("status", "all")
    agent_filter = request.args.get("agent", "all")
    info_message = request.args.get("info", "")
    success_message = request.args.get("success", "")
    error_message = request.args.get("error", "")

    # ============ 1. AGENT PAYMENTS (Agent's own commissions) ============
    # First check what columns exist in projects table
    cursor.execute("PRAGMA table_info(projects)")
    project_columns = [col[1] for col in cursor.fetchall()]
    print(f"Projects table columns: {project_columns}")

    # Use appropriate column name for project name
    project_name_column = (
        "name"
        if "name" in project_columns
        else "project_name" if "project_name" in project_columns else "title"
    )

    query_agent = f"""
        SELECT 
            cp.id,
            cp.listing_id,
            cp.agent_id,
            u.name as agent_name,
            u.email as agent_email,
            cp.commission_amount,
            cp.payment_status,
            cp.payment_date,
            cp.created_at,
            cp.updated_at,
            pl.property_address,
            pl.customer_name,
            p.{project_name_column} as project_name
        FROM commission_payments cp
        LEFT JOIN users u ON cp.agent_id = u.id
        LEFT JOIN property_listings pl ON cp.listing_id = pl.id
        LEFT JOIN projects p ON pl.project_id = p.id
        WHERE 1=1
    """

    params_agent = []

    # Apply filters
    if status_filter != "all":
        query_agent += " AND cp.payment_status = ?"
        params_agent.append(status_filter)

    if agent_filter != "all":
        query_agent += " AND cp.agent_id = ?"
        params_agent.append(agent_filter)

    query_agent += " ORDER BY cp.created_at DESC"

    print(f"Agent query: {query_agent}")

    cursor.execute(query_agent, params_agent)
    all_payments = cursor.fetchall()

    # Filter agent payments (those where agent is the listing agent)
    agent_payments = []
    for payment in all_payments:
        listing_id = payment[1]
        agent_id = payment[2]

        # Check if this agent is the listing agent
        cursor.execute(
            "SELECT agent_id FROM property_listings WHERE id = ?", (listing_id,)
        )
        listing_result = cursor.fetchone()

        if listing_result and listing_result[0] == agent_id:
            # This is an agent's own commission
            agent_payments.append(payment)

    # ============ 2. UPLINE PAYMENTS ============
    # Get upline commissions with correct column structure
    query_upline = f"""
        SELECT 
            uc.id,
            uc.listing_id,
            uc.upline_id,
            uu.name as upline_name,
            uu.email as upline_email,
            uc.amount,
            uc.status,
            uc.created_at,
            uc.paid_at,
            pl.property_address,
            pl.customer_name,
            ua.name as from_agent_name,
            ua.email as from_agent_email,
            pl.agent_id as from_agent_id,
            p.{project_name_column} as project_name,
            COALESCE(uc.commission_type, 'direct') as commission_type,
            COALESCE(uc.commission_rate, 5.0) as commission_rate
        FROM upline_commissions uc
        LEFT JOIN users uu ON uc.upline_id = uu.id
        LEFT JOIN property_listings pl ON uc.listing_id = pl.id
        LEFT JOIN users ua ON pl.agent_id = ua.id
       LEFT JOIN projects p ON pl.project_id = p.id
        WHERE 1=1
    """

    params_upline = []

    # Apply filters
    if status_filter != "all":
        query_upline += " AND uc.status = ?"
        params_upline.append(status_filter)

    if agent_filter != "all":
        query_upline += " AND uc.upline_id = ?"
        params_upline.append(agent_filter)

    query_upline += " ORDER BY uc.created_at DESC"

    print(f"Upline query: {query_upline}")

    try:
        cursor.execute(query_upline, params_upline)
        upline_payments = cursor.fetchall()
        print(f"Found {len(upline_payments)} upline payments")
    except Exception as e:
        print(f"Error fetching upline payments: {e}")
        # Try without project name
        query_upline_simple = """
            SELECT 
                uc.id,
                uc.listing_id,
                uc.upline_id,
                uu.name as upline_name,
                uu.email as upline_email,
                uc.amount,
                uc.status,
                uc.created_at,
                uc.paid_at,
                pl.property_address,
                pl.customer_name,
                ua.name as from_agent_name,
                ua.email as from_agent_email,
                pl.agent_id as from_agent_id,
                COALESCE(uc.commission_type, 'direct') as commission_type,
                uc.commission_rate
            FROM upline_commissions uc
            LEFT JOIN users uu ON uc.upline_id = uu.id
            LEFT JOIN property_listings pl ON uc.listing_id = pl.id
            LEFT JOIN users ua ON pl.agent_id = ua.id
            WHERE 1=1
        """

        if status_filter != "all":
            query_upline_simple += " AND uc.status = ?"

        if agent_filter != "all":
            query_upline_simple += " AND uc.upline_id = ?"

        query_upline_simple += " ORDER BY uc.created_at DESC"

        cursor.execute(query_upline_simple, params_upline)
        upline_payments = cursor.fetchall()

    # ============ 3. CALCULATE SEPARATE STATS ============
    # Agent payments: only from commission_payments where agent is the listing agent
    agent_pending_ids = set()  # Track agent payment IDs
    total_agent_amount = 0
    total_agent_paid = 0
    total_agent_pending = 0
    
    for payment in agent_payments:
        amount = payment[5] or 0  # commission_amount
        status = payment[6]  # payment_status
        
        total_agent_amount += amount
        
        if status == "paid":
            total_agent_paid += amount
        elif status == "pending":
            total_agent_pending += amount
            agent_pending_ids.add(payment[0])  # Store payment ID

    # Upline payments: only from upline_commissions
    upline_pending_ids = set()  # Track upline payment IDs  
    total_upline_amount = 0
    total_upline_paid = 0
    total_upline_pending = 0
    
    for payment in upline_payments:
        amount = payment[5] or 0  # amount
        status = payment[6]  # status
        
        total_upline_amount += amount
        
        if status == "paid":
            total_upline_paid += amount
        elif status == "pending":
            total_upline_pending += amount
            upline_pending_ids.add(payment[0])  # Store payment ID

    print(f"DEBUG: Agent pending amount: {total_agent_pending}")
    print(f"DEBUG: Upline pending amount: {total_upline_pending}")
    print(f"DEBUG: Total pending should be: {total_agent_pending + total_upline_pending}")

    # ============ 4. GET COMBINED STATS FROM DATABASE ============
    # Get stats from commission_payments table
    query_cp_stats = """
        SELECT 
            COUNT(*) as total_agent_payments,
            SUM(CASE WHEN payment_status = 'paid' THEN commission_amount ELSE 0 END) as total_agent_paid,
            SUM(CASE WHEN payment_status = 'pending' THEN commission_amount ELSE 0 END) as total_agent_pending_db,
            SUM(CASE WHEN payment_status = 'processing' THEN commission_amount ELSE 0 END) as total_agent_processing
        FROM commission_payments
    """

    cursor.execute(query_cp_stats)
    cp_stats = cursor.fetchone()

    # Get stats from upline_commissions table
    query_uc_stats = """
        SELECT 
            COUNT(*) as total_upline_payments,
            SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) as total_upline_paid_db,
            SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END) as total_upline_pending_db
        FROM upline_commissions
    """

    cursor.execute(query_uc_stats)
    uc_stats = cursor.fetchone()

    # ============ 5. CALCULATE COMBINED STATS ============
    # Use the calculated values instead of database values to ensure consistency
    total_payments = len(agent_payments) + len(upline_payments)
    total_paid = total_agent_paid + total_upline_paid
    total_pending = total_agent_pending + total_upline_pending
    total_processing = cp_stats[3] or 0 if cp_stats else 0

    stats = (total_payments, total_paid, total_pending, total_processing)

    # Get all agents for filter dropdown
    cursor.execute('SELECT id, name FROM users WHERE role = "agent" ORDER BY name')
    agents = cursor.fetchall()

    conn.close()

    # ============ 6. RENDER TEMPLATE ============
    return render_template(
        "admin/payments.html",
        agent_payments=agent_payments,
        upline_payments=upline_payments,
        stats=stats,
        agents=agents,
        status_filter=status_filter,
        agent_filter=agent_filter,
        total_agent_amount=total_agent_amount,
        total_upline_amount=total_upline_amount,
        total_agent_pending=total_agent_pending,
        total_upline_pending=total_upline_pending,
        total_pending_correct=total_pending,  # Already calculated above
        info_message=info_message,
        success_message=success_message,
        error_message=error_message,
    )

@app.route("/admin/set-upline")
def set_upline():
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get all agents
    cursor.execute("SELECT id, name FROM users WHERE role = 'agent' ORDER BY name")
    agents = cursor.fetchall()

    # Get potential uplines
    cursor.execute(
        "SELECT id, name FROM users WHERE role IN ('admin', 'agent') ORDER BY name"
    )
    uplines = cursor.fetchall()

    html = """
    <h1>Set Upline Relationships</h1>
    <p><a href="/admin/dashboard">← Back</a></p>
    
    <form action="/admin/update-upline" method="post">
    <table border="1" style="width: 100%;">
        <tr>
            <th>Agent</th>
            <th>Current Upline</th>
            <th>Set New Upline</th>
        </tr>"""

    for agent in agents:
        agent_id, agent_name = agent

        # Get current upline
        cursor.execute(
            """
            SELECT upline_id, (SELECT name FROM users WHERE id = users.upline_id) 
            FROM users WHERE id = ?
        """,
            (agent_id,),
        )
        current = cursor.fetchone()
        current_upline = current[1] if current and current[0] else "None"

        html += f"""
        <tr>
            <td>{agent_name} (ID: {agent_id})</td>
            <td>{current_upline}</td>
            <td>
                <select name="upline_{agent_id}">
                    <option value="">-- No Upline --</option>"""

        for upline in uplines:
            upline_id, upline_name = upline
            if upline_id != agent_id:  # Can't be own upline
                selected = "selected" if current and current[0] == upline_id else ""
                html += f'<option value="{upline_id}" {selected}>{upline_name} (ID: {upline_id})</option>'

        html += """
                </select>
            </td>
        </tr>"""

    html += """
    </table>
    <br>
    <button type="submit">Update All Upline Relationships</button>
    </form>"""

    conn.close()
    return html


@app.route("/admin/update-upline", methods=["POST"])
def update_upline():
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get all agents
    cursor.execute("SELECT id FROM users WHERE role = 'agent'")
    agents = cursor.fetchall()

    for agent in agents:
        agent_id = agent[0]
        upline_id = request.form.get(f"upline_{agent_id}")

        if upline_id == "":
            upline_id = None

        cursor.execute(
            "UPDATE users SET upline_id = ? WHERE id = ?", (upline_id, agent_id)
        )

    conn.commit()
    conn.close()

    return redirect("/admin/set-upline")


@app.route("/admin/upline-payments")
def upline_payments():
    """Admin page to view and pay upline commissions"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get all pending upline commissions - FIXED QUERY
    cursor.execute(
        """
        SELECT 
            uc.id,
            uc.listing_id,
            uc.agent_id,
            u.name as agent_name,
            uc.upline_id,
            uu.name as upline_name,
            uc.amount,
            uc.status,
            uc.notes,
            uc.created_at,
            pl.commission_amount as total_commission
        FROM upline_commissions uc
        JOIN users u ON uc.agent_id = u.id
        JOIN users uu ON uc.upline_id = uu.id
        JOIN property_listings pl ON uc.listing_id = pl.id
        WHERE uc.status = 'pending'
        ORDER BY uc.created_at DESC
    """
    )

    pending_commissions = cursor.fetchall()

    # Get statistics
    cursor.execute(
        'SELECT COUNT(*), SUM(amount) FROM upline_commissions WHERE status = "pending"'
    )
    stats = cursor.fetchone()

    conn.close()

    # Debug: Print commission structure
    print(f"Number of pending commissions: {len(pending_commissions)}")
    if pending_commissions:
        print(f"First commission structure: {pending_commissions[0]}")
        print(f"Number of columns: {len(pending_commissions[0])}")

    return render_template_string(
        """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Upline Commission Payments</title>
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background: #f5f7fa;
                margin: 0;
                padding: 20px;
                color: #333;
            }
            .header {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 25px;
                border-radius: 10px;
                margin-bottom: 25px;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            }
            .header h1 {
                margin: 0;
                font-size: 28px;
            }
            .header p {
                margin: 10px 0 0;
                opacity: 0.9;
            }
            .stats-card {
                background: white;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 25px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08);
                border-left: 5px solid #667eea;
            }
            .stats-card h3 {
                margin: 0 0 15px 0;
                color: #333;
            }
            .commission-grid {
                display: grid;
                gap: 15px;
            }
            .commission-card {
                background: white;
                padding: 20px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08);
                border: 1px solid #e1e5e9;
                transition: transform 0.2s, box-shadow 0.2s;
            }
            .commission-card:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 15px rgba(0,0,0,0.1);
            }
            .commission-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 15px;
                padding-bottom: 15px;
                border-bottom: 1px solid #eef2f7;
            }
            .commission-id {
                font-weight: bold;
                color: #667eea;
                font-size: 14px;
            }
            .commission-date {
                color: #666;
                font-size: 13px;
            }
            .agent-info {
                margin-bottom: 15px;
            }
            .agent-row {
                display: flex;
                align-items: center;
                margin-bottom: 10px;
            }
            .agent-label {
                width: 80px;
                color: #666;
                font-size: 14px;
            }
            .agent-value {
                font-weight: 500;
            }
            .commission-details {
                background: #f8f9fa;
                padding: 15px;
                border-radius: 8px;
                margin: 15px 0;
            }
            .amount-row {
                display: flex;
                justify-content: space-between;
                margin-bottom: 8px;
            }
            .amount-label {
                color: #666;
            }
            .amount-value {
                font-weight: bold;
            }
            .total-commission {
                color: #28a745;
                font-size: 18px;
            }
            .upline-share {
                color: #dc3545;
                font-size: 18px;
            }
            .btn {
                display: inline-block;
                padding: 10px 20px;
                background: #28a745;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                border: none;
                cursor: pointer;
                font-weight: 500;
                transition: background 0.2s;
            }
            .btn:hover {
                background: #218838;
            }
            .btn-pay {
                width: 100%;
                text-align: center;
                margin-top: 15px;
            }
            .empty-state {
                text-align: center;
                padding: 40px;
                background: white;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08);
            }
            .empty-state h3 {
                color: #666;
                margin-bottom: 10px;
            }
            .empty-state p {
                color: #999;
            }
            .back-link {
                display: inline-block;
                margin-top: 20px;
                color: #667eea;
                text-decoration: none;
            }
            .back-link:hover {
                text-decoration: underline;
            }
            .status-badge {
                display: inline-block;
                padding: 4px 10px;
                border-radius: 20px;
                font-size: 12px;
                font-weight: 500;
            }
            .status-pending {
                background: #fff3cd;
                color: #856404;
            }
            .status-paid {
                background: #d4edda;
                color: #155724;
            }
            table {
                width: 100%;
                background: white;
                border-radius: 10px;
                overflow: hidden;
                box-shadow: 0 2px 10px rgba(0,0,0,0.08);
                border-collapse: collapse;
            }
            th {
                background: #f8f9fa;
                padding: 15px;
                text-align: left;
                font-weight: 600;
                color: #333;
                border-bottom: 2px solid #eef2f7;
            }
            td {
                padding: 15px;
                border-bottom: 1px solid #eef2f7;
            }
            tr:hover {
                background: #f8f9fa;
            }
            .actions {
                display: flex;
                gap: 10px;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>💰 Upline Commission Payments</h1>
            <p>Pay 5% commissions to team leaders/supervisors</p>
        </div>
        
        <div class="stats-card">
            <h3>Pending Upline Commissions</h3>
            <div style="display: flex; gap: 20px; align-items: center;">
                <div>
                    <div style="font-size: 24px; font-weight: bold; color: #dc3545;">"""
        + str(stats[0] or 0)
        + """</div>
                    <div style="color: #666; font-size: 14px;">Pending Payments</div>
                </div>
                <div>
                    <div style="font-size: 24px; font-weight: bold; color: #28a745;">RM"""
        + ("{:,.2f}".format(stats[1] or 0))
        + """</div>
                    <div style="color: #666; font-size: 14px;">Total Amount</div>
                </div>
            </div>
        </div>
        
        <h2 style="color: #333; margin-bottom: 20px;">📋 Pending Upline Commissions</h2>
        
        """
        + (
            """
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Listing</th>
                    <th>Agent</th>
                    <th>Upline</th>
                    <th>Total Commission</th>
                    <th>Upline Share (5%)</th>
                    <th>Date</th>
                    <th>Status</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
        """
            if pending_commissions
            else ""
        )
        + """
        
        """
        + (
            "".join(
                f"""
                <tr>
                    <td><span class="commission-id">#{c[0]}</span></td>
                    <td>#{c[1]}</td>
                    <td>
                        <div class="agent-value">{c[3]}</div>
                        <div style="font-size: 12px; color: #666;">Agent ID: {c[2]}</div>
                    </td>
                    <td>
                        <div class="agent-value">{c[5]}</div>
                        <div style="font-size: 12px; color: #666;">Upline ID: {c[4]}</div>
                    </td>
                    <td><span style="font-weight: bold; color: #333;">RM{"{:,.2f}".format(c[10] if c[10] else 0)}</span></td>
                    <td><span style="font-weight: bold; color: #dc3545;">RM{"{:,.2f}".format(c[6] if c[6] else 0)}</span></td>
                    <td>
                        <div class="commission-date">{c[9].split()[0] if c[9] else "N/A"}</div>
                    </td>
                    <td>
                        <span class="status-badge status-pending">Pending</span>
                    </td>
                    <td class="actions">
                        <a href="/admin/pay-upline/{c[0]}" class="btn" 
                           onclick="return confirm('Pay RM{"{:,.2f}".format(c[6] if c[6] else 0)} to {c[5]}?')">
                            Pay Now
                        </a>
                    </td>
                </tr>
        """
                for c in pending_commissions
            )
            if pending_commissions
            else """
                <tr>
                    <td colspan="9">
                        <div class="empty-state">
                            <h3>🎉 No pending upline commissions!</h3>
                            <p>All upline commissions have been paid.</p>
                        </div>
                    </td>
                </tr>
        """
        )
        + """
        
        """
        + (
            """
            </tbody>
        </table>
        """
            if pending_commissions
            else ""
        )
        + """
        
        <a href="/admin/dashboard" class="back-link" style="font-weight: bold; color: #000; font-size: 16px; text-decoration: none; padding: 10px 0; display: inline-block; margin-top: 20px;">← Back to Dashboard</a>
        
        <script>
            // Confirmation for payment
            function confirmPayment(commissionId, amount, uplineName) {
                return confirm(`Pay RMRM{amount.toFixed(2)} to RM{uplineName}?`);
            }
        </script>
    </body>
    </html>
    """
    )


@app.route("/admin/payment/<int:payment_id>")
def payment_details(payment_id):
    """View payment details"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        # First check what columns exist in projects table
        cursor.execute("PRAGMA table_info(projects)")
        project_columns = [col[1] for col in cursor.fetchall()]
        print(f"Projects table columns: {project_columns}")

        # Use appropriate column name for project name
        project_name_column = (
            "name"
            if "name" in project_columns
            else "project_name" if "project_name" in project_columns else "title"
        )

        # Get payment details with proper joins
        query = f"""
            SELECT 
                cp.id,
                cp.listing_id,
                cp.agent_id,
                cp.commission_amount,
                cp.payment_status,
                cp.payment_date,
                cp.payment_method,
                cp.transaction_id,
                cp.paid_by,
                cp.updated_at,
                cp.notes,
                cp.created_at,
                u.name as agent_name,
                u.email as agent_email,
                pl.customer_name,
                pl.property_address,
                pl.sale_price,
                cc.base_rate as commission_rate,
                p.{project_name_column} as project_name
            FROM commission_payments cp
            LEFT JOIN users u ON cp.agent_id = u.id
            LEFT JOIN property_listings pl ON cp.listing_id = pl.id
            LEFT JOIN commission_calculations cc ON cp.listing_id = cc.listing_id
            LEFT JOIN projects p ON pl.project_id = p.id
            WHERE cp.id = ?
        """

        print(f"Payment details query: {query}")

        cursor.execute(query, (payment_id,))

        payment = cursor.fetchone()

        if not payment:
            conn.close()
            return "Payment not found", 404

        print(f"Payment data fetched: {len(payment) if payment else 0} columns")
        print(f"Payment columns: {payment}")

        # Get additional listing details
        cursor.execute(
            """
            SELECT pl.customer_email, pl.customer_phone, pl.closing_date, 
                   pl.status, pl.submitted_at, pl.approved_at, pl.commission_status
            FROM property_listings pl
            WHERE pl.id = ?
        """,
            (payment[1],),
        )  # listing_id

        listing_details = cursor.fetchone()

        conn.close()

        # Prepare payment data dictionary
        payment_data = {
            "id": payment[0],
            "listing_id": payment[1],
            "agent_id": payment[2],
            "commission_amount": payment[3],
            "payment_status": payment[4],
            "payment_date": payment[5],
            "payment_method": payment[6],
            "transaction_id": payment[7],
            "paid_by": payment[8],
            "updated_at": payment[9],
            "notes": payment[10],
            "created_at": payment[11],
            "agent_name": payment[12],
            "agent_email": payment[13],
            "customer_name": payment[14],
            "property_address": payment[15],
            "sale_price": payment[16],
            "commission_rate": payment[17],
            "project_name": payment[18],
        }

        # Add listing details if available
        if listing_details:
            payment_data.update(
                {
                    "customer_email": listing_details[0],
                    "customer_phone": listing_details[1],
                    "closing_date": listing_details[2],
                    "listing_status": listing_details[3],
                    "submitted_at": listing_details[4],
                    "approved_at": listing_details[5],
                    "commission_status": listing_details[6],
                }
            )

        # Format the commission rate
        if payment_data["commission_rate"]:
            payment_data["commission_rate"] = f"{payment_data['commission_rate']}%"

        # Debug: Print what we have
        print(f"Payment data prepared:")
        for key, value in payment_data.items():
            print(f"  {key}: {value}")

        return render_template_string(
            """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Payment Details #{{ payment_data.id }}</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
                .container { max-width: 1200px; margin: 0 auto; }
                .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
                .btn { background: #007bff; color: white; padding: 8px 15px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; display: inline-block; }
                .btn:hover { background: #0056b3; }
                .payment-card { background: white; padding: 30px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
                .payment-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
                .payment-amount { font-size: 36px; font-weight: bold; color: #28a745; }
                .payment-status { padding: 8px 16px; border-radius: 20px; font-weight: bold; }
                .status-pending { background: #fff3cd; color: #856404; }
                .status-paid { background: #d4edda; color: #155724; }
                .status-processing { background: #cce5ff; color: #004085; }
                .details-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; }
                .detail-group { margin-bottom: 15px; }
                .detail-label { font-weight: bold; color: #666; font-size: 14px; margin-bottom: 5px; }
                .detail-value { font-size: 16px; }
                .section-title { font-size: 18px; font-weight: bold; margin: 25px 0 15px 0; padding-bottom: 10px; border-bottom: 2px solid #007bff; color: #007bff; }
                .info-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-bottom: 20px; }
                .info-card { background: #f8f9fa; padding: 15px; border-radius: 5px; }
                .notes-box { background: #f8f9fa; padding: 15px; border-radius: 5px; margin-top: 20px; font-style: italic; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>💰 Payment Details #{{ payment_data.id }}</h1>
                    <div style="display: flex; gap: 10px; margin-top: 10px;">
                        <a href="/admin/payments" class="btn" style="background: #6c757d;">← Back to Payments</a>
                        {% if payment_data.payment_status != 'paid' %}
                        <a href="/admin/mark-paid/{{ payment_data.id }}" class="btn" style="background: #28a745;">✅ Mark as Paid</a>
                        {% endif %}
                    </div>
                </div>
                
                <div class="payment-card">
                    <div class="payment-header">
                        <div>
                            <div class="payment-amount">RM{{ "{:,.2f}".format(payment_data.commission_amount or 0) }}</div>
                            <div style="font-size: 14px; color: #666; margin-top: 5px;">Commission Payment</div>
                        </div>
                        <div class="payment-status status-{{ payment_data.payment_status }}">
                            {{ payment_data.payment_status|upper }}
                        </div>
                    </div>
                    
                    <div class="details-grid">
                        <div class="detail-group">
                            <div class="detail-label">Payment ID</div>
                            <div class="detail-value">#{{ payment_data.id }}</div>
                        </div>
                        <div class="detail-group">
                            <div class="detail-label">Listing ID</div>
                            <div class="detail-value">#{{ payment_data.listing_id }}</div>
                        </div>
                        <div class="detail-group">
                            <div class="detail-label">Agent ID</div>
                            <div class="detail-value">#{{ payment_data.agent_id }}</div>
                        </div>
                        <div class="detail-group">
                            <div class="detail-label">Payment Date</div>
                            <div class="detail-value">{{ payment_data.payment_date if payment_data.payment_date else 'Not set' }}</div>
                        </div>
                        <div class="detail-group">
                            <div class="detail-label">Payment Method</div>
                            <div class="detail-value">{{ payment_data.payment_method if payment_data.payment_method else 'Not set' }}</div>
                        </div>
                        <div class="detail-group">
                            <div class="detail-label">Transaction ID</div>
                            <div class="detail-value">{{ payment_data.transaction_id if payment_data.transaction_id else 'Not set' }}</div>
                        </div>
                        <div class="detail-group">
                            <div class="detail-label">Paid By</div>
                            <div class="detail-value">{{ payment_data.paid_by if payment_data.paid_by else 'Not set' }}</div>
                        </div>
                        <div class="detail-group">
                            <div class="detail-label">Updated At</div>
                            <div class="detail-value">{{ payment_data.updated_at }}</div>
                        </div>
                        <div class="detail-group">
                            <div class="detail-label">Created At</div>
                            <div class="detail-value">{{ payment_data.created_at }}</div>
                        </div>
                    </div>
                </div>
                
                <div class="section-title">📋 Transaction Details</div>
                
                <div class="info-grid">
                    <div class="info-card">
                        <div class="detail-label">Agent Name</div>
                        <div class="detail-value">{{ payment_data.agent_name or 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Agent Email</div>
                        <div class="detail-value">{{ payment_data.agent_email or 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Project</div>
                        <div class="detail-value">{{ payment_data.project_name or 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Customer Name</div>
                        <div class="detail-value">{{ payment_data.customer_name or 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Customer Email</div>
                        <div class="detail-value">{{ payment_data.customer_email if payment_data.customer_email else 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Customer Phone</div>
                        <div class="detail-value">{{ payment_data.customer_phone if payment_data.customer_phone else 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Property Address</div>
                        <div class="detail-value">{{ payment_data.property_address or 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Sale Price</div>
                        <div class="detail-value">RM{{ "{:,.2f}".format(payment_data.sale_price or 0) }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Commission Rate</div>
                        <div class="detail-value">{{ payment_data.commission_rate or 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Closing Date</div>
                        <div class="detail-value">{{ payment_data.closing_date if payment_data.closing_date else 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Listing Status</div>
                        <div class="detail-value">{{ payment_data.listing_status if payment_data.listing_status else 'N/A' }}</div>
                    </div>
                    <div class="info-card">
                        <div class="detail-label">Commission Status</div>
                        <div class="detail-value">{{ payment_data.commission_status if payment_data.commission_status else 'N/A' }}</div>
                    </div>
                </div>
                
                {% if payment_data.notes %}
                <div class="section-title">📝 Payment Notes</div>
                <div class="notes-box">
                    {{ payment_data.notes }}
                </div>
                {% endif %}
                
                <div class="section-title">📅 Timeline</div>
                <div class="info-grid">
                    {% if payment_data.submitted_at %}
                    <div class="info-card">
                        <div class="detail-label">Submitted At</div>
                        <div class="detail-value">{{ payment_data.submitted_at }}</div>
                    </div>
                    {% endif %}
                    
                    {% if payment_data.approved_at %}
                    <div class="info-card">
                        <div class="detail-label">Approved At</div>
                        <div class="detail-value">{{ payment_data.approved_at }}</div>
                    </div>
                    {% endif %}
                    
                    {% if payment_data.created_at %}
                    <div class="info-card">
                        <div class="detail-label">Payment Created</div>
                        <div class="detail-value">{{ payment_data.created_at }}</div>
                    </div>
                    {% endif %}
                    
                    {% if payment_data.updated_at and payment_data.updated_at != payment_data.created_at %}
                    <div class="info-card">
                        <div class="detail-label">Last Updated</div>
                        <div class="detail-value">{{ payment_data.updated_at }}</div>
                    </div>
                    {% endif %}
                </div>
            </div>
        </body>
        </html>
        """,
            payment_data=payment_data,
        )

    except Exception as e:
        conn.close()
        print(f"Error fetching payment details: {e}")
        return f"Error loading payment details: {str(e)}", 500


@app.route("/admin/mark-commission-paid/<string:record_id>", methods=["GET", "POST"])
def mark_commission_paid(record_id):
    """UNIFIED: Mark ANY commission as paid - supports UC- and CP- prefixes"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    # ===== HANDLE PREFIXES =====
    is_commission_payment = None
    actual_id = None

    if record_id.startswith("CP-"):
        try:
            actual_id = int(record_id[3:])
            is_commission_payment = True
        except ValueError:
            return "Invalid payment ID format", 400

    elif record_id.startswith("UC-"):
        try:
            actual_id = int(record_id[3:])
            is_commission_payment = False
        except ValueError:
            return "Invalid commission ID format", 400

    else:
        try:
            actual_id = int(record_id)
        except ValueError:
            return "Invalid ID format", 400

    if request.method == "POST":
        payment_method = request.form.get("payment_method", "")
        transaction_id = request.form.get("transaction_id", "")
        notes = request.form.get("notes", "")

        conn = sqlite3.connect("real_estate.db")
        cursor = conn.cursor()

        try:
            if is_commission_payment is None:
                cursor.execute("SELECT id FROM commission_payments WHERE id = ?", (actual_id,))
                if cursor.fetchone():
                    is_commission_payment = True
                else:
                    cursor.execute("SELECT id FROM upline_commissions WHERE id = ?", (actual_id,))
                    if cursor.fetchone():
                        is_commission_payment = False
                    else:
                        conn.close()
                        return "Commission record not found", 404

            if is_commission_payment:
                # ===== PROCESS AGENT COMMISSION PAYMENT (CP-) =====
                cursor.execute(
                    """
                    SELECT cp.*, pl.agent_id as listing_agent_id
                    FROM commission_payments cp
                    LEFT JOIN property_listings pl ON cp.listing_id = pl.id
                    WHERE cp.id = ?
                """,
                    (actual_id,),
                )

                payment = cursor.fetchone()
                if not payment:
                    conn.close()
                    return f"Commission payment {record_id} not found", 404

                agent_id = payment[2]
                listing_id = payment[1]
                amount = payment[3]
                listing_agent_id = payment[12] if len(payment) > 12 else None

                # Update commission_payments
                cursor.execute(
                    """
                    UPDATE commission_payments 
                    SET payment_status = 'paid',
                        payment_date = ?,
                        payment_method = ?,
                        transaction_id = ?,
                        notes = ?,
                        updated_at = ?,
                        paid_by = ?
                    WHERE id = ?
                """,
                    (
                        datetime.now().strftime("%Y-%m-%d"),
                        payment_method,
                        transaction_id,
                        notes,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        session["user_id"],
                        actual_id,
                    ),
                )

                # If agent's own commission, update property_listings
                if listing_agent_id and agent_id == listing_agent_id:
                    cursor.execute(
                        """
                        UPDATE property_listings 
                        SET commission_status = 'paid'
                        WHERE id = ?
                    """,
                        (listing_id,),
                    )
                    print(f"✅ Updated property_listings commission status for listing {listing_id}")
                    
                    # Create notification for agent's own commission
                    notification_title = "💸 Agent Commission Paid"
                    notification_message = f"Your own commission of RM{amount:,.2f} has been paid. Method: {payment_method}, Ref: {transaction_id or 'N/A'}"
                    
                    cursor.execute(
                        """
                        INSERT INTO agent_notifications 
                        (agent_id, notification_type, title, message, priority, 
                         created_at, expires_at, is_read, related_id, related_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            agent_id,
                            "commission_paid",
                            notification_title,
                            notification_message,
                            "normal",
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
                            0,
                            actual_id,
                            "commission_payment",
                        ),
                    )
                
                # ⚠️ IMPORTANT: DO NOT create/update commission_payments for upline here!
                # Upline payments should be processed separately with UC- prefix

                success_msg = f"Payment {record_id} marked as paid successfully!"

            else:
                # ===== PROCESS UPLINE COMMISSION (UC-) =====
                cursor.execute(
                    """
                    SELECT uc.*, pl.agent_id as selling_agent_id
                    FROM upline_commissions uc
                    LEFT JOIN property_listings pl ON uc.listing_id = pl.id
                    WHERE uc.id = ?
                    """,
                    (actual_id,),
                )

                commission = cursor.fetchone()
                if not commission:
                    conn.close()
                    return f"Upline commission {record_id} not found", 404

                upline_id = commission[3]
                amount = commission[4]
                status = commission[5]
                listing_id = commission[1]
                selling_agent_id = commission[12] if len(commission) > 12 else None

                # === NEW: Check if direct or indirect upline ===
                cursor.execute(
                    """
                    SELECT name, upline_id 
                    FROM users 
                    WHERE id = ?
                    """,
                    (selling_agent_id,)
                )
                selling_agent_data = cursor.fetchone()
                selling_agent_name = selling_agent_data[0] if selling_agent_data else f"Agent {selling_agent_id}"
                selling_agent_upline_id = selling_agent_data[1] if selling_agent_data else None

                # Determine if direct or indirect
                is_direct_upline = (selling_agent_upline_id == upline_id) if selling_agent_upline_id else False

                # Get direct upline name for indirect notifications
                direct_upline_name = None
                if not is_direct_upline and selling_agent_upline_id:
                    cursor.execute("SELECT name FROM users WHERE id = ?", (selling_agent_upline_id,))
                    direct_upline_result = cursor.fetchone()
                    direct_upline_name = direct_upline_result[0] if direct_upline_result else None

                # Update upline_commissions
                cursor.execute(
                    """
                    UPDATE upline_commissions 
                    SET status = 'paid', 
                        paid_at = ?,
                        notes = ?,
                        transaction_id = ?
                    WHERE id = ?
                    """,
                    (
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        f"Payment method: {payment_method}",
                        transaction_id,
                        actual_id,
                    ),
                )

                # === UPDATED: Create appropriate notification ===
                if is_direct_upline:
                    notification_title = "💰 Upline Commission Paid"
                    notification_message = f"Your upline commission of RM{amount:,.2f} from {selling_agent_name} has been paid. Method: {payment_method}, Ref: {transaction_id or 'N/A'}"
                else:
                    notification_title = "💰 Indirect Upline Commission Paid"
                    if direct_upline_name:
                        notification_message = f"Your indirect upline commission of RM{amount:,.2f} from {selling_agent_name} (via {direct_upline_name}) has been paid. Method: {payment_method}, Ref: {transaction_id or 'N/A'}"
                    else:
                        notification_message = f"Your indirect upline commission of RM{amount:,.2f} from {selling_agent_name} has been paid. Method: {payment_method}, Ref: {transaction_id or 'N/A'}"

                cursor.execute(
                    """
                    INSERT INTO agent_notifications 
                    (agent_id, notification_type, title, message, priority, 
                     created_at, expires_at, is_read, related_id, related_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        upline_id,
                        'commission_paid',
                        notification_title,
                        notification_message,
                        'normal',
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),
                        0,
                        actual_id,
                        'upline_commission'
                    ),
                )

                success_msg = f"Upline commission {record_id} marked as paid successfully!"

            conn.commit()
            conn.close()
            return redirect(f"/admin/payments?success={success_msg}")

        except Exception as e:
            conn.rollback()
            conn.close()
            print(f"❌ Error marking commission as paid: {e}")
            return redirect(f"/admin/payments?error=Payment+failed:+{str(e)}")

    # ===== GET REQUEST - SHOW PAYMENT FORM =====
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Determine record type if not already determined by prefix
    if is_commission_payment is None:
        # Auto-detect
        cursor.execute("SELECT id FROM commission_payments WHERE id = ?", (actual_id,))
        if cursor.fetchone():
            is_commission_payment = True
            print(f"🔍 GET: Auto-detected as COMMISSION PAYMENT: ID {actual_id}")
        else:
            cursor.execute(
                "SELECT id FROM upline_commissions WHERE id = ?", (actual_id,)
            )
            if cursor.fetchone():
                is_commission_payment = False
                print(f"🔍 GET: Auto-detected as UPLINE COMMISSION: ID {actual_id}")
            else:
                conn.close()
                return f"Commission record {record_id} not found", 404

    if is_commission_payment:
        # Agent commission payment form
        cursor.execute(
            """
            SELECT cp.*, u.name, u.email, pl.property_address,
                   CASE 
                       WHEN cp.agent_id = pl.agent_id THEN 'Agent Own Commission'
                       ELSE 'Upline Commission'
                   END as payment_type_name
            FROM commission_payments cp
            JOIN users u ON cp.agent_id = u.id
            LEFT JOIN property_listings pl ON cp.listing_id = pl.id
            WHERE cp.id = ?
        """,
            (actual_id,),
        )

        payment = cursor.fetchone()
        conn.close()

        if not payment:
            return f"Commission payment {record_id} not found", 404

        return render_template("admin/mark_commission_paid.html", payment_id=record_id)

    else:
        # Upline commission form
        cursor.execute(
            """
            SELECT uc.amount, uc.upline_id, uu.name as upline_name, uu.email,
                   pl.property_address, pl.customer_name,
                   CASE 
                       WHEN uc.commission_type = 'direct' THEN 'Direct Upline Commission'
                       ELSE 'Indirect Upline Commission'
                   END as payment_type
            FROM upline_commissions uc
            JOIN users uu ON uc.upline_id = uu.id
            LEFT JOIN property_listings pl ON uc.listing_id = pl.id
            WHERE uc.id = ?
        """,
            (actual_id,),
        )

        commission = cursor.fetchone()
        conn.close()

        if not commission:
            return f"Upline commission {record_id} not found", 404

        (
            amount,
            upline_id,
            upline_name,
            upline_email,
            property_address,
            customer_name,
            payment_type,
        ) = commission

        return render_template(
            "admin/mark_upline_commission_paid.html",
            record_id=record_id,
            amount=amount,
            upline_name=upline_name,
            upline_email=upline_email,
            property_address=property_address,
            customer_name=customer_name,
            payment_type=payment_type,
        )

# ============ BATCH PAYMENT PROCESSING ============


@app.route("/admin/batch-payments", methods=["GET", "POST"])
def batch_payments():
    """Batch process multiple payments - SIMPLER WORKING VERSION"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    if request.method == "POST":
        # Get selected payment IDs
        selected_payments = request.form.getlist("payment_ids")
        payment_method = request.form.get("payment_method", "bank_transfer")
        transaction_id = request.form.get("transaction_id", "")
        notes = request.form.get("notes", "")

        if not selected_payments:
            conn.close()
            return redirect("/admin/batch-payments?error=No payments selected")

        # Process each selected payment
        processed_count = 0
        today = datetime.now().strftime("%Y-%m-%d")

        for payment_id in selected_payments:
            try:
                # Update payment record
                cursor.execute(
                    """
                    UPDATE commission_payments 
                    SET payment_status = 'paid',
                        payment_date = ?,
                        payment_method = ?,
                        transaction_id = ?,
                        notes = COALESCE(notes || ' | ', '') || ?,
                        updated_at = ?,
                        paid_by = ?
                    WHERE id = ? AND payment_status = 'pending'
                """,
                    (
                        today,
                        payment_method,
                        transaction_id,
                        f"Batch processed on {today}",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        session["user_id"],
                        payment_id,
                    ),
                )

                # Update the property listing commission status
                cursor.execute(
                    """
                    UPDATE property_listings 
                    SET commission_status = 'paid'
                    WHERE id = (
                        SELECT listing_id FROM commission_payments WHERE id = ?
                    )
                """,
                    (payment_id,),
                )

                processed_count += 1

            except Exception as e:
                print(f"Error processing payment {payment_id}: {e}")
                continue

        conn.commit()
        conn.close()

        if processed_count > 0:
            return redirect(
                f"/admin/payments?success={processed_count} payments processed successfully"
            )
        else:
            return redirect("/admin/payments?error=No payments were processed")

    # GET request - show batch payment page
    # Get all pending payments
    cursor.execute(
        """
        SELECT 
            cp.id,
            cp.commission_amount,
            cp.created_at,
            u.name as agent_name,
            u.email as agent_email,
            pl.customer_name,
            pl.property_address,
            pl.id as listing_id
        FROM commission_payments cp
        JOIN users u ON cp.agent_id = u.id
        JOIN property_listings pl ON cp.listing_id = pl.id
        WHERE cp.payment_status = 'pending'
        ORDER BY cp.created_at ASC, u.name
    """
    )

    pending_payments = cursor.fetchall()

    # Calculate totals
    total_amount = sum([p[1] for p in pending_payments]) if pending_payments else 0
    total_count = len(pending_payments)

    # Get today's date for default transaction ID
    today_str = datetime.now().strftime("%Y%m%d")

    conn.close()

    # Create a simple HTML string without complex template syntax
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Batch Payment Processing</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
            .header {{ background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .stats {{ display: flex; gap: 15px; margin: 20px 0; }}
            .stat-card {{ background: white; padding: 15px; border-radius: 8px; flex: 1; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
            .stat-value {{ font-size: 1.8em; font-weight: bold; }}
            .payment-list {{ background: white; padding: 20px; border-radius: 10px; margin: 20px 0; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
            th, td {{ padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }}
            th {{ background: #2c3e50; color: white; }}
            .btn {{ padding: 10px 20px; background: #28a745; color: white; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; display: inline-block; }}
            .btn-secondary {{ background: #6c757d; }}
            .empty-state {{ text-align: center; padding: 40px 20px; color: #666; }}
            .checkbox-cell {{ width: 50px; text-align: center; }}
            input[type="checkbox"] {{ width: 18px; height: 18px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>💰 Batch Payment Processing</h1>
            <div>
                <a href="/admin/payments" class="btn btn-secondary">← Back to Payments</a>
            </div>
        </div>
        
        <div class="stats">
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Pending Payments</div>
                <div class="stat-value" style="color: #007bff;">{total_count}</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Total Amount</div>
                <div class="stat-value" style="color: #28a745;">RM{total_amount:,.2f}</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Average Payment</div>
                <div class="stat-value" style="color: #6f42c1;">RM{total_amount/total_count if total_count > 0 else 0:,.2f}</div>
            </div>
        </div>
    """

    if pending_payments:
        html_content += f"""
        <form method="POST">
            <div class="payment-list">
                <h2>Select Payments to Process ({total_count} available)</h2>
                <button type="button" id="selectAllBtn" style="margin: 10px 0; padding: 8px 15px; background: #6c757d; color: white; border: none; border-radius: 5px;">Select All</button>
                
                <table>
                    <thead>
                        <tr>
                            <th class="checkbox-cell"><input type="checkbox" id="selectAllCheckbox"></th>
                            <th>Payment ID</th>
                            <th>Agent</th>
                            <th>Customer</th>
                            <th>Amount</th>
                            <th>Created Date</th>
                        </tr>
                    </thead>
                    <tbody>
        """

        for payment in pending_payments:
            html_content += f"""
                        <tr>
                            <td class="checkbox-cell">
                                <input type="checkbox" name="payment_ids" value="{payment[0]}" class="payment-checkbox" data-amount="{payment[1]}">
                            </td>
                            <td><strong>#{payment[0]}</strong></td>
                            <td>
                                <div>{payment[3]}</div>
                                <small style="color: #666;">{payment[4]}</small>
                            </td>
                            <td>
                                <div>{payment[5]}</div>
                                <small style="color: #666;">{payment[6][:30]}{'...' if len(payment[6]) > 30 else ''}</small>
                            </td>
                            <td style="font-weight: bold; color: #28a745;">RM{payment[1]:,.2f}</td>
                            <td>{payment[2][:10] if payment[2] else 'N/A'}</td>
                        </tr>
            """

        html_content += f"""
                    </tbody>
                </table>
            </div>
            
            <div class="payment-list">
                <h2>Payment Details</h2>
                
                <div style="margin: 20px 0;">
                    <label style="display: block; margin-bottom: 5px; font-weight: bold;">Payment Method *</label>
                    <select name="payment_method" style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px;" required>
                        <option value="">Select method</option>
                        <option value="bank_transfer" selected>Bank Transfer</option>
                        <option value="check">Check</option>
                        <option value="cash">Cash</option>
                        <option value="paypal">PayPal</option>
                    </select>
                </div>
                
                <div style="margin: 20px 0;">
                    <label style="display: block; margin-bottom: 5px; font-weight: bold;">Transaction/Reference ID</label>
                    <input type="text" name="transaction_id" value="BATCH-{today_str}-001" 
                           style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px;"
                           placeholder="e.g., BATCH-20240115-001">
                </div>
                
                <div style="margin: 20px 0;">
                    <label style="display: block; margin-bottom: 5px; font-weight: bold;">Notes (Optional)</label>
                    <textarea name="notes" rows="3" style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px;" 
                              placeholder="Add any notes about this batch payment...">Batch processed on {datetime.now().strftime('%Y-%m-%d')}</textarea>
                </div>
                
                <div style="background: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <h3 style="margin-top: 0;">Batch Summary</h3>
                    <div style="display: flex; gap: 20px;">
                        <div style="text-align: center; flex: 1;">
                            <div id="selectedCount" style="font-size: 24px; font-weight: bold; color: #007bff;">0</div>
                            <div style="font-size: 14px; color: #666;">Selected Payments</div>
                        </div>
                        <div style="text-align: center; flex: 1;">
                            <div id="selectedAmount" style="font-size: 24px; font-weight: bold; color: #28a745;">RM0.00</div>
                            <div style="font-size: 14px; color: #666;">Total Amount</div>
                        </div>
                    </div>
                </div>
                
                <div style="margin-top: 20px;">
                    <button type="submit" class="btn" id="processBtn" disabled>✅ Process Selected Payments</button>
                    <button type="button" class="btn btn-secondary" onclick="clearSelection()">Clear Selection</button>
                    <a href="/admin/payments" class="btn btn-secondary">Cancel</a>
                </div>
            </div>
        </form>
        
        <script>
            const selectAllCheckbox = document.getElementById('selectAllCheckbox');
            const selectAllBtn = document.getElementById('selectAllBtn');
            const paymentCheckboxes = document.querySelectorAll('.payment-checkbox');
            const processBtn = document.getElementById('processBtn');
            
            function updateSummary() {{
                const selectedCheckboxes = document.querySelectorAll('.payment-checkbox:checked');
                const selectedCount = selectedCheckboxes.length;
                
                let totalAmount = 0;
                selectedCheckboxes.forEach(cb => {{
                    totalAmount += parseFloat(cb.getAttribute('data-amount')) || 0;
                }});
                
                document.getElementById('selectedCount').textContent = selectedCount;
                document.getElementById('selectedAmount').textContent = 'RM' + totalAmount.toLocaleString('en-US', {{minimumFractionDigits: 2}});
                
                processBtn.disabled = selectedCount === 0;
                processBtn.textContent = selectedCount > 0 
                    ? '✅ Process ' + selectedCount + ' Payment' + (selectedCount !== 1 ? 's' : '')
                    : '✅ Process Selected Payments';
                
                if (selectedCount === paymentCheckboxes.length) {{
                    selectAllCheckbox.checked = true;
                    selectAllCheckbox.indeterminate = false;
                }} else if (selectedCount > 0) {{
                    selectAllCheckbox.checked = false;
                    selectAllCheckbox.indeterminate = true;
                }} else {{
                    selectAllCheckbox.checked = false;
                    selectAllCheckbox.indeterminate = false;
                }}
            }}
            
            selectAllCheckbox.addEventListener('change', function() {{
                paymentCheckboxes.forEach(cb => {{
                    cb.checked = this.checked;
                }});
                updateSummary();
            }});
            
            selectAllBtn.addEventListener('click', function() {{
                const allChecked = Array.from(paymentCheckboxes).every(cb => cb.checked);
                paymentCheckboxes.forEach(cb => {{
                    cb.checked = !allChecked;
                }});
                selectAllCheckbox.checked = !allChecked;
                updateSummary();
            }});
            
            paymentCheckboxes.forEach(cb => {{
                cb.addEventListener('change', updateSummary);
            }});
            
            function clearSelection() {{
                paymentCheckboxes.forEach(cb => {{
                    cb.checked = false;
                }});
                selectAllCheckbox.checked = false;
                updateSummary();
            }}
            
            document.querySelector('form').addEventListener('submit', function(e) {{
                const selectedCount = document.querySelectorAll('.payment-checkbox:checked').length;
                if (selectedCount === 0) {{
                    e.preventDefault();
                    alert('Please select at least one payment to process.');
                    return false;
                }}
                
                if (!confirm('Are you sure you want to process ' + selectedCount + ' payment' + (selectedCount !== 1 ? 's' : '') + '?')) {{
                    e.preventDefault();
                }}
            }});
            
            updateSummary();
        </script>
        """
    else:
        html_content += f"""
        <div class="empty-state">
            <h3>✅ No Pending Payments!</h3>
            <p>All commission payments have been processed. Great job!</p>
            <div style="margin-top: 20px;">
                <a href="/admin/payments" class="btn">Back to Payments</a>
                <a href="/admin/dashboard" class="btn btn-secondary">Go to Dashboard</a>
            </div>
        </div>
        """

    html_content += """
    </body>
    </html>
    """

    return html_content


@app.route("/download/<int:doc_id>")
def download_document(doc_id):
    """Download document"""
    if "user_id" not in session:
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
    doc = cursor.fetchone()
    conn.close()

    if doc and os.path.exists(doc[3]):
        return send_file(doc[3], as_attachment=True, download_name=doc[2])
    else:
        return "File not found", 404


@app.route("/admin/sync-payments")
def sync_payments():
    """Create payment records for approved but unpaid commissions (BOTH agent and upline)"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        messages = []
        total_created = 0

        # ============ 1. AGENT COMMISSION PAYMENTS ============
        cursor.execute(
            """
            SELECT pl.id, pl.agent_id, pl.commission_amount, pl.approved_at
            FROM property_listings pl
            LEFT JOIN commission_payments cp ON pl.id = cp.listing_id AND cp.agent_id = pl.agent_id
            WHERE pl.status = 'approved'
              AND (pl.commission_status IS NULL OR pl.commission_status != 'paid')
              AND cp.id IS NULL
        """
        )

        pending_agent_commissions = cursor.fetchall()
        agent_created = 0

        for (
            listing_id,
            agent_id,
            commission_amount,
            approved_at,
        ) in pending_agent_commissions:
            # Check if payment already exists
            cursor.execute(
                "SELECT id FROM commission_payments WHERE listing_id = ? AND agent_id = ?",
                (listing_id, agent_id),
            )
            if cursor.fetchone():
                continue

            # Get project name for notes
            cursor.execute(
                """
                SELECT p.project_name, pl.customer_name
                FROM property_listings pl
                LEFT JOIN projects p ON pl.project_id = p.id
                WHERE pl.id = ?
            """,
                (listing_id,),
            )
            project_info = cursor.fetchone()

            project_name = project_info[0] if project_info else None
            customer_name = (
                project_info[1]
                if project_info and project_info[1]
                else f"listing #{listing_id}"
            )

            # Create agent notes
            if project_name:
                agent_notes = f"Agent commission for {project_name} - 95% of RM{commission_amount:,.2f}"
            else:
                agent_notes = f"Agent commission for {customer_name} - 95% of RM{commission_amount:,.2f}"

            # Create agent payment record
            cursor.execute(
                """
                INSERT INTO commission_payments 
                (listing_id, agent_id, commission_amount, payment_status, created_at, updated_at, notes)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
            """,
                (
                    listing_id,
                    agent_id,
                    commission_amount * 0.95,
                    approved_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    agent_notes,
                ),
            )

            agent_created += 1

        if agent_created > 0:
            messages.append(f"Created {agent_created} agent payment(s)")
            total_created += agent_created

        # ============ 2. UPLINE COMMISSION PAYMENTS ============
        # Find approved listings where agent has an upline, but no upline commission exists
        # FIXED: Using NOT EXISTS to properly check for duplicates
        cursor.execute(
            """
            SELECT 
                pl.id as listing_id,
                pl.agent_id,
                u.upline_id,
                pl.commission_amount,
                pl.approved_at,
                u.name as agent_name,
                upline.name as upline_name,
                p.project_name
            FROM property_listings pl
            JOIN users u ON pl.agent_id = u.id
            LEFT JOIN users upline ON u.upline_id = upline.id
            LEFT JOIN projects p ON pl.project_id = p.id
            WHERE pl.status = 'approved'
              AND u.upline_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM upline_commissions uc 
                  WHERE uc.listing_id = pl.id 
                    AND uc.upline_id = u.upline_id
              )
        """
        )

        pending_upline_commissions = cursor.fetchall()
        upline_created = 0

        for (
            listing_id,
            agent_id,
            upline_id,
            commission_amount,
            approved_at,
            agent_name,
            upline_name,
            project_name,
        ) in pending_upline_commissions:

            # Calculate upline commission (5% of agent's commission)
            upline_commission = commission_amount * 0.05  # 5% upline share

            # Create upline notes
            if project_name:
                upline_notes = f"Upline commission from agent {agent_name} for {project_name} - 5% of RM{commission_amount:,.2f}"
            else:
                upline_notes = f"Upline commission from agent {agent_name} - 5% of RM{commission_amount:,.2f}"

            # Create upline commission record
            cursor.execute(
                """
                INSERT INTO upline_commissions 
                (listing_id, agent_id, upline_id, amount, status, notes, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
                (
                    listing_id,
                    agent_id,
                    upline_id,
                    upline_commission,
                    upline_notes,
                    approved_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

            # Also create commission_payments record for upline
            cursor.execute(
                """
                SELECT id FROM commission_payments 
                WHERE listing_id = ? AND agent_id = ? AND commission_amount = ?
            """,
                (listing_id, upline_id, upline_commission),
            )

            if not cursor.fetchone():
                # Create commission payment for upline
                cursor.execute(
                    """
                    INSERT INTO commission_payments
                    (listing_id, agent_id, commission_amount, payment_status, created_at, updated_at, notes)
                    VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                    (
                        listing_id,
                        upline_id,
                        upline_commission,
                        approved_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        upline_notes,
                    ),
                )

            upline_created += 1

        if upline_created > 0:
            messages.append(f"Created {upline_created} upline payment(s)")
            total_created += upline_created

        # ============ 3. UPDATE COMMISSION STATUSES ============
        # Update commission status for approved listings
        cursor.execute(
            """
            UPDATE property_listings 
            SET commission_status = 'pending'
            WHERE status = 'approved' 
              AND (commission_status IS NULL OR commission_status = 'approved')
        """
        )

        conn.commit()
        conn.close()

        if total_created > 0:
            message = " | ".join(messages)
            return redirect(f"/admin/payments?success={message}")
        else:
            return redirect(
                "/admin/payments?info=No pending payments need to be created. All approved listings already have payment records."
            )

    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"Error in sync_payments: {e}")
        return redirect(f"/admin/payments?error=Sync failed: {str(e)}")


@app.route("/admin/fix-payments")
def fix_payments():
    """Quick fix for payment synchronization"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    try:
        # SQL to create missing payment records
        cursor.execute(
            """
            INSERT INTO commission_payments (listing_id, agent_id, commission_amount, payment_status, created_at, updated_at)
            SELECT 
                pl.id,
                pl.agent_id,
                pl.commission_amount,
                'pending',
                pl.approved_at,
                pl.approved_at
            FROM property_listings pl
            LEFT JOIN commission_payments cp ON pl.id = cp.listing_id
            WHERE pl.status = 'approved' AND cp.id IS NULL
        """
        )

        created = cursor.rowcount

        # Update commission status
        cursor.execute(
            """
            UPDATE property_listings 
            SET commission_status = 'pending' 
            WHERE status = 'approved' AND (commission_status IS NULL OR commission_status = 'approved')
        """
        )

        conn.commit()
        conn.close()

        return redirect(
            f"/admin/payments?success={created} payment records created. Refresh the batch payments page."
        )

    except Exception as e:
        conn.rollback()
        conn.close()
        return redirect(f"/admin/payments?error=Fix failed: {str(e)}")


@app.route("/admin/create-project", methods=["GET", "POST"])
def create_project():
    """Create a new project"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    if request.method == "POST":
        # Get form data
        data = request.form
        sale_type = data.get("sale_type", "sales")  # Default to sales
        print(f"DEBUG: Form data received: {dict(request.form)}")

        try:
            conn = sqlite3.connect("real_estate.db")
            cursor = conn.cursor()

            # Debug check for table structure
            cursor.execute("PRAGMA table_info(projects)")
            columns = [col[1] for col in cursor.fetchall()]
            print(f"DEBUG: Current columns in 'projects' table: {columns}")

            # Check if project_sale_type column exists
            if "project_sale_type" not in columns:
                print("DEBUG: Adding 'project_sale_type' column to projects table...")
                try:
                    cursor.execute(
                        'ALTER TABLE projects ADD COLUMN project_sale_type TEXT DEFAULT "sales"'
                    )
                    conn.commit()
                    print("DEBUG: Column added successfully!")
                except Exception as alter_error:
                    print(f"DEBUG: Error adding column: {alter_error}")

            # Get project_sale_type (default to 'sales' if not provided)
            project_sale_type = data.get("project_sale_type", "sales")
            print(f"DEBUG: Project sale type: {project_sale_type}")

            # Get all required fields with defaults
            project_name = data.get("name", "").strip()
            description = data.get("description", "").strip()
            location = data.get("location", "").strip()
            project_type = data.get("project_type", "residential")
            category = data.get("category", "condo")
            commission_rate = float(data.get("project_commission", 3.0))

            # Validate required fields
            if not project_name or not location:
                flash("❌ Project Name and Location are required", "error")
                return redirect("/admin/create-project")

            print(f"DEBUG: Inserting project: {project_name}")
            print(f"DEBUG: Commission rate: {commission_rate}")
            print(f"DEBUG: Sale type: {project_sale_type}")

            # Insert project - FIXED: using project_name instead of name
            cursor.execute(
                """
                INSERT INTO projects 
                (project_name, description, location, project_type, category, 
                 commission_rate, project_sale_type, created_by, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    project_name,
                    description,
                    location,
                    project_type,
                    category,
                    commission_rate,
                    project_sale_type,
                    session["user_id"],
                    "active",
                ),
            )

            project_id = cursor.lastrowid
            print(f"DEBUG: Project created with ID: {project_id}")

            # Handle units
            unit_counter = 1
            units_added = 0
            while f"unit_code_{unit_counter}" in data:
                unit_code = data.get(f"unit_code_{unit_counter}", "").strip()
                unit_type = data.get(f"unit_type_{unit_counter}", "").strip()
                unit_price = data.get(f"unit_price_{unit_counter}", "").strip()
                unit_size = data.get(f"unit_size_{unit_counter}", "").strip()
                unit_commission = data.get(
                    f"unit_commission_{unit_counter}", ""
                ).strip()

                if unit_code:
                    # Convert empty strings to None
                    price = float(unit_price) if unit_price else None
                    size = float(unit_size) if unit_size else None
                    commission = float(unit_commission) if unit_commission else None

                    # ✅ UPDATED: Use correct column names that match database
                    cursor.execute(
                        """
                        INSERT INTO project_units 
                        (project_id, unit_code, unit_type, base_price, square_feet, 
                        commission_rate, quantity, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            project_id,
                            unit_code,
                            unit_type,
                            price,  # Maps to base_price column
                            size,  # Maps to square_feet column
                            commission,  # commission_rate column already exists
                            1,
                            "available",
                        ),
                    )
                    units_added += 1
                    print(f"DEBUG: Added unit: {unit_code}")

                unit_counter += 1

            print(f"DEBUG: Total units added: {units_added}")

            conn.commit()
            conn.close()
            print("DEBUG: Database changes committed successfully")

            flash(f'✅ Project "{project_name}" created successfully!', "success")
            return redirect("/admin/projects")

        except Exception as e:
            print(f"DEBUG: ERROR occurred: {str(e)}")
            import traceback

            print(f"DEBUG: Full traceback:\n{traceback.format_exc()}")
            flash(f"❌ Error creating project: {str(e)}", "error")
            return redirect("/admin/create-project")

    # GET request - show form
    # ✅ FIX 3: Updated HTML with Sales/Rental dropdown
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Create New Project</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; }
            .form-container { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            h1 { color: #333; margin-bottom: 30px; }
            .form-group { margin-bottom: 20px; }
            label { display: block; margin-bottom: 5px; font-weight: bold; color: #555; }
            input, select, textarea { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; font-size: 14px; }
            .form-row { display: flex; gap: 20px; }
            .form-row > div { flex: 1; }
            .btn { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; }
            .btn-secondary { background: #6c757d; }
            .units-section { margin-top: 30px; padding: 20px; background: #f8f9fa; border-radius: 5px; }
            .unit-row { display: flex; gap: 10px; margin-bottom: 10px; align-items: center; }
            .unit-row input { flex: 1; }
            .commission-info { background: #e8f4ff; padding: 15px; border-radius: 5px; margin: 20px 0; }
        </style>
    </head>
    <body>
        <div class="form-container">
            <h1>🏗️ Create New Project</h1>
            
            <form method="POST" onsubmit="return validateForm()">
                <!-- Basic Project Info -->
                <div class="form-group">
                    <label>Project Name *</label>
                    <input type="text" name="name" required id="projectName">
                </div>
                
                <div class="form-group">
                    <label>Description</label>
                    <textarea name="description" rows="3"></textarea>
                </div>
                
                <div class="form-row">
                    <div class="form-group">
                        <label>Location *</label>
                        <input type="text" name="location" required id="location">
                    </div>
                    <div class="form-group">
                        <label>Project Type</label>
                        <select name="project_type">
                            <option value="residential">Residential</option>
                            <option value="commercial">Commercial</option>
                            <option value="mixed">Mixed Development</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Category</label>
                        <select name="category">
                            <option value="condo">Condo/Apartment</option>
                            <option value="landed">Landed House</option>
                            <option value="commercial">Commercial</option>
                            <option value="industrial">Industrial</option>
                        </select>
                    </div>
                </div>
                
                <!-- ✅ FIX 4: ADDED Sales/Rental Section -->
                <div class="form-row">
                    <div class="form-group">
                        <label>Sales or Rental *</label>
                        <select name="project_sale_type" required id="projectSaleType">
                            <option value="">-- Select --</option>
                            <option value="sales">Sales</option>
                            <option value="rental">Rental</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <!-- Empty for spacing -->
                    </div>
                    <div class="form-group">
                        <!-- Empty for spacing -->
                    </div>
                </div>
                
                <!-- Commission Settings -->
                <div class="commission-info">
                    <h2>💰 Commission Settings</h2>
                    
                    <div class="form-group">
                        <label>Default Project Commission Rate (%)</label>
                        <input type="number" name="project_commission" min="0" max="100" step="0.1" 
                               value="3.0" style="max-width: 150px;">
                        <small style="color: #666;">Base commission rate for this project</small>
                    </div>
                    
                    <div style="background: #d4edda; padding: 10px; border-radius: 5px; margin-top: 15px;">
                        <strong>💡 Commission Calculation:</strong>
                        <ul style="margin: 5px 0 0 20px; color: #555;">
                            <li>Unit-specific commission overrides project commission</li>
                            <li>If no unit rate, uses project commission rate</li>
                            <li>Commission = Sale Price × Commission Rate</li>
                            <li>Commission is capped: Min RM1,000 - Max RM50,000</li>
                        </ul>
                    </div>
                </div>
                
                <!-- Units Section -->
                <div class="units-section">
                    <h3>🏠 Project Units</h3>
                    <p style="color: #666; margin-bottom: 15px;">Add units that agents can sell. All units will be marked as "available".</p>
                    
                    <div id="unitsContainer">
                        <!-- Unit rows will be added here by JavaScript -->
                        <div class="unit-row">
                            <input type="text" name="unit_code_1" placeholder="Unit Code (e.g., A-101)" required>
                            <input type="text" name="unit_type_1" placeholder="Type (e.g., 3BR, Studio)">
                            <input type="number" name="unit_price_1" placeholder="Price (Optional)" step="1000">
                            <input type="number" name="unit_size_1" placeholder="Size sq ft (Optional)" step="10">
                            <input type="number" name="unit_commission_1" placeholder="Comm % (Optional)" min="0" max="100" step="0.1">
                        </div>
                    </div>
                    
                    <button type="button" onclick="addUnit()" style="background: #28a745; color: white; padding: 8px 15px; border: none; border-radius: 5px; margin-top: 10px;">
                        ➕ Add Another Unit
                    </button>
                </div>
                
                <div style="margin-top: 30px;">
                    <button type="submit" class="btn" id="submitBtn">✅ Create Project</button>
                    <a href="/admin/projects" class="btn btn-secondary" style="margin-left: 10px;">Cancel</a>
                </div>
            </form>
        </div>
        
        <script>
            let unitCounter = 1;
            
            function addUnit() {
                unitCounter++;
                const unitsContainer = document.getElementById('unitsContainer');
                
                const unitRow = document.createElement('div');
                unitRow.className = 'unit-row';
                // ✅ FIX 5: Fixed template literal - changed RM{} to $ {}
                unitRow.innerHTML = `
                    <input type="text" name="unit_code_${unitCounter}" placeholder="Unit Code (e.g., A-101)" required>
                    <input type="text" name="unit_type_${unitCounter}" placeholder="Type (e.g., 3BR, Studio)">
                    <input type="number" name="unit_price_${unitCounter}" placeholder="Price (Optional)" step="1000">
                    <input type="number" name="unit_size_${unitCounter}" placeholder="Size sq ft (Optional)" step="10">
                    <input type="number" name="unit_commission_${unitCounter}" placeholder="Comm % (Optional)" min="0" max="100" step="0.1">
                `;
                
                unitsContainer.appendChild(unitRow);
            }
            
            function validateForm() {
                const projectName = document.getElementById('projectName').value.trim();
                const location = document.getElementById('location').value.trim();
                const projectSaleType = document.getElementById('projectSaleType').value;
                const submitBtn = document.getElementById('submitBtn');
                
                // Validate required fields
                if (!projectName || !location || !projectSaleType) {
                    alert('Please fill all required fields: Project Name, Location, and Sales/Rental');
                    return false;
                }
                
                // Disable button to prevent double submission
                submitBtn.disabled = true;
                submitBtn.innerHTML = 'Creating...';
                
                return true; // Allow form submission
            }
        </script>
    </body>
    </html>
    """


@app.route("/admin/edit-project/<int:project_id>", methods=["GET", "POST"])
def edit_project(project_id):
    """Edit existing project"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get project details
    cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = cursor.fetchone()

    if not project:
        conn.close()
        return "Project not found", 404

    # Get existing units
    cursor.execute(
        "SELECT * FROM project_units WHERE project_id = ? ORDER BY unit_type",
        (project_id,),
    )
    existing_units = cursor.fetchall()

    if request.method == "POST":
        try:
            data = request.form
            sale_type = data.get("sale_type", "sales")  # Default to sales

            # Update main project
            cursor.execute(
                """
                UPDATE projects 
                SET project_name = ?,
                    category = ?,
                    project_type = ?,
                    location = ?,
                    description = ?,
                    commission_rate = ?,
                    updated_at = ?
                WHERE id = ?
            """,
                (
                    data["project_name"],
                    data["category"],
                    data["project_type"],
                    data.get("location", ""),
                    data.get("description", ""),
                    float(data.get("project_commission", 0)),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    project_id,
                ),
            )

            # Delete existing units (we'll recreate them)
            cursor.execute(
                "DELETE FROM project_units WHERE project_id = ?", (project_id,)
            )

            # Handle unit types - dynamic form fields
            unit_counter = 0
            while f"unit_type_{unit_counter}" in data:
                unit_type = data.get(f"unit_type_{unit_counter}")
                square_feet = data.get(f"square_feet_{unit_counter}")
                base_price = data.get(f"base_price_{unit_counter}")
                rental_price = data.get(f"rental_price_{unit_counter}")
                unit_commission = data.get(f"unit_commission_{unit_counter}")
                quantity = data.get(f"quantity_{unit_counter}", 1)

                if unit_type:  # Only insert if unit type is provided
                    cursor.execute(
                        """
                        INSERT INTO project_units 
                        (project_id, unit_type, square_feet, base_price, rental_price, 
                         commission_rate, quantity, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'available')
                    """,
                        (
                            project_id,
                            unit_type,
                            int(square_feet) if square_feet else None,
                            float(base_price) if base_price else None,
                            float(rental_price) if rental_price else None,
                            float(unit_commission) if unit_commission else None,
                            int(quantity) if quantity else 1,
                        ),
                    )

                unit_counter += 1

            conn.commit()
            conn.close()

            return redirect(
                f"/admin/project/{project_id}?success=Project updated successfully!"
            )

        except Exception as e:
            conn.rollback()
            conn.close()
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Error</title>
                <style>
                    body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                    .error-box {{ border: 2px solid #dc3545; padding: 30px; border-radius: 10px; text-align: center; }}
                    h2 {{ color: #dc3545; }}
                </style>
            </head>
            <body>
                <div class="error-box">
                    <h2>❌ Error Updating Project</h2>
                    <p><strong>Error:</strong> {str(e)}</p>
                    <div style="margin-top: 30px;">
                        <a href="/admin/edit-project/{project_id}" style="background: #007bff; color: white; padding: 10px 20px; 
                           text-decoration: none; border-radius: 5px; margin-right: 10px;">Try Again</a>
                        <a href="/admin/project/{project_id}" style="background: #6c757d; color: white; padding: 10px 20px; 
                           text-decoration: none; border-radius: 5px;">Back to Project</a>
                    </div>
                </div>
            </body>
            </html>
            """

    # GET request - show edit form
    # Build existing units JavaScript data
    units_js_data = []
    for unit in existing_units:
        units_js_data.append(
            {
                "unit_type": unit[2],
                "square_feet": unit[3] or "",
                "base_price": unit[4] or "",
                "rental_price": unit[5] or "",
                "commission_rate": unit[6] or "",
                "quantity": unit[7] or 1,
            }
        )

    conn.close()

    edit_project_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Edit Project - {project[1]}</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
            .container {{ max-width: 1000px; margin: 0 auto; }}
            .header {{ background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .form-container {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .form-section {{ border: 1px solid #e0e0e0; padding: 20px; margin-bottom: 20px; border-radius: 8px; }}
            .form-group {{ margin-bottom: 15px; }}
            label {{ display: block; margin-bottom: 5px; font-weight: bold; color: #555; }}
            .required:after {{ content: " *"; color: red; }}
            input, select, textarea {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }}
            .btn {{ padding: 12px 25px; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; text-decoration: none; display: inline-block; }}
            .btn-primary {{ background: #007bff; color: white; }}
            .btn-secondary {{ background: #6c757d; color: white; }}
            .unit-row {{ display: grid; grid-template-columns: 2fr 1fr 1fr 1fr 1fr 1fr 50px; gap: 10px; margin-bottom: 10px; align-items: end; }}
            .delete-unit {{ background: #dc3545; color: white; border: none; border-radius: 5px; padding: 8px 12px; cursor: pointer; }}
            .add-unit {{ background: #28a745; color: white; border: none; border-radius: 5px; padding: 8px 15px; cursor: pointer; margin: 10px 0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>✏️ Edit Project: {project[1]}</h1>
                <div>
                    <a href="/admin/project/{project_id}" class="btn btn-secondary">← Back to Project</a>
                    <a href="/admin/projects" class="btn btn-secondary">📋 All Projects</a>
                </div>
            </div>
            
            <form method="POST" class="form-container">
                <!-- Basic Project Information -->
                <div class="form-section">
                    <h2>📋 Basic Information</h2>
                    <div class="form-group">
                        <label class="required">Project Name</label>
                        <input type="text" name="project_name" value="{project[1]}" required>
                    </div>
                    
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                        <div class="form-group">
                            <label class="required">Category</label>
                            <select name="category" id="categorySelect" required onchange="togglePriceFields()">
                                <option value="sales" {'selected' if project[2] == 'sales' else ''}>Sales</option>
                                <option value="rental" {'selected' if project[2] == 'rental' else ''}>Rental</option>
                            </select>
                        </div>
                        
                        <div class="form-group">
                            <label class="required">Project Type</label>
                            <select name="project_type" required>
                                <option value="residential" {'selected' if project[3] == 'residential' else ''}>Residential</option>
                                <option value="commercial" {'selected' if project[3] == 'commercial' else ''}>Commercial</option>
                            </select>
                        </div>
                    </div>
                    
                    <div class="form-group">
                        <label>Location</label>
                        <input type="text" name="location" value="{project[4] or ''}" placeholder="e.g., Downtown, Singapore">
                    </div>
                    
                    <div class="form-group">
                        <label>Description</label>
                        <textarea name="description" rows="3">{project[5] or ''}</textarea>
                    </div>
                </div>
                
                <!-- Unit Types -->
                <div class="form-section">
                    <h2>🏠 Unit Types & Pricing</h2>
                    <p style="color: #666; margin-bottom: 15px;">Modify unit types for this project</p>
                    
                    <div id="unitContainer">
                        <!-- Unit rows will be added here by JavaScript -->
                    </div>
                    
                    <button type="button" class="add-unit" onclick="addUnitRow()">➕ Add Another Unit Type</button>
                </div>
                
                <!-- Commission Settings -->
                <div class="form-section">
                    <h2>💰 Commission Settings</h2>
                    
                    <div class="form-group">
                        <label>Default Project Commission Rate (%)</label>
                        <input type="number" name="project_commission" min="0" max="100" step="0.1" 
                               value="{project[7] or 3.0}" placeholder="e.g., 3.0">
                        <small>This rate will be used if no unit-specific rate is set</small>
                    </div>
                    
                    <div style="background: #e8f4ff; padding: 15px; border-radius: 5px; margin-top: 15px;">
                        <strong>💡 Commission Calculation:</strong>
                        <ul style="margin: 10px 0 0 0; padding-left: 20px;">
                            <li>Unit-specific commission overrides project commission</li>
                            <li>Commission = Sale Price × Commission Rate</li>
                            <li>Commission is capped: Min RM1,000 - Max RM50,000</li>
                        </ul>
                    </div>
                </div>
                
                <div style="margin-top: 30px; text-align: center;">
                    <button type="submit" class="btn btn-primary">✅ Save Changes</button>
                    <button type="reset" class="btn btn-secondary" onclick="loadExistingUnits()">🔄 Reset Form</button>
                    <a href="/admin/project/{project_id}" class="btn btn-secondary">Cancel</a>
                </div>
            </form>
        </div>
        
        <script>
        let unitCounter = 0;
        const existingUnits = {json.dumps(units_js_data)};
        
        function togglePriceFields() {{
            const category = document.getElementById('categorySelect').value;
            const priceLabels = document.querySelectorAll('.price-label');
            const rentalInputs = document.querySelectorAll('.rental-input');
            
            if (category === 'sales') {{
                priceLabels.forEach(label => {{
                    label.textContent = 'Sale Price (RM)';
                }});
                rentalInputs.forEach(input => {{
                    input.style.display = 'none';
                    input.previousElementSibling.style.display = 'none';
                }});
            }} else if (category === 'rental') {{
                priceLabels.forEach(label => {{
                    label.textContent = 'Monthly Rent (RM)';
                }});
                rentalInputs.forEach(input => {{
                    input.style.display = 'block';
                    input.previousElementSibling.style.display = 'block';
                }});
            }}
        }}
        
        function addUnitRow(unitData = null) {{
            const container = document.getElementById('unitContainer');
            const category = document.getElementById('categorySelect').value;
            
            const unitRow = document.createElement('div');
            unitRow.className = 'unit-row';
            unitRow.id = `unitRow_RM{{unitCounter}}`;
            
            const unitType = unitData?.unit_type || '';
            const squareFeet = unitData?.square_feet || '';
            const basePrice = unitData?.base_price || '';
            const rentalPrice = unitData?.rental_price || '';
            const commission = unitData?.commission_rate || '';
            const quantity = unitData?.quantity || 1;
            
            unitRow.innerHTML = `
                <div>
                    <label>Unit Type</label>
                    <input type="text" name="unit_type_RM{{unitCounter}}" value="RM{{unitType}}" placeholder="e.g., Studio, 2-Bedroom" required>
                </div>
                <div>
                    <label>Square Feet</label>
                    <input type="number" name="square_feet_RM{{unitCounter}}" value="RM{{squareFeet}}" min="100" step="10" placeholder="e.g., 800">
                </div>
                <div>
                    <label class="price-label">RM{{category === 'rental' ? 'Monthly Rent (RM)' : 'Sale Price (RM)'}}</label>
                    <input type="number" name="base_price_RM{{unitCounter}}" value="RM{{basePrice}}" min="0" step="1000" required 
                           placeholder="RM{{category === 'rental' ? 'e.g., 2500' : 'e.g., 500000'}}">
                </div>
                <div>
                    <label class="rental-label" style="display: RM{{category === 'rental' ? 'block' : 'none'}}">Security Deposit (RM)</label>
                    <input type="number" name="rental_price_RM{{unitCounter}}" value="RM{{rentalPrice}}" min="0" step="100" 
                           class="rental-input" style="display: RM{{category === 'rental' ? 'block' : 'none'}}"
                           placeholder="e.g., 5000">
                </div>
                <div>
                    <label>Commission Rate (%)</label>
                    <input type="number" name="unit_commission_RM{{unitCounter}}" value="RM{{commission}}" min="0" max="100" step="0.1" 
                           placeholder="e.g., 3.0">
                </div>
                <div>
                    <label>Quantity</label>
                    <input type="number" name="quantity_RM{{unitCounter}}" value="RM{{quantity}}" min="1">
                </div>
                <div>
                    <button type="button" class="delete-unit" onclick="removeUnitRow(RM{{unitCounter}})">🗑️</button>
                </div>
            `;
            
            container.appendChild(unitRow);
            unitCounter++;
        }}
        
        function removeUnitRow(rowId) {{
            const row = document.getElementById(`unitRow_RM{{rowId}}`);
            if (row) {{
                row.remove();
            }}
        }}
        
        function loadExistingUnits() {{
            document.getElementById('unitContainer').innerHTML = '';
            unitCounter = 0;
            existingUnits.forEach(unit => addUnitRow(unit));
            if (existingUnits.length === 0) {{
                addUnitRow();
            }}
        }}
        
        // Initialize with existing units on page load
        document.addEventListener('DOMContentLoaded', function() {{
            loadExistingUnits();
        }});
        </script>
    </body>
    </html>
    """

    return edit_project_template


@app.route("/admin/projects")
def list_projects():
    """List all projects"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get all projects
    cursor.execute(
        """
        SELECT p.*, u.name as created_by_name, 
               COUNT(pu.id) as unit_count,
               SUM(pu.quantity) as total_units,
               p.is_active
        FROM projects p
        LEFT JOIN users u ON p.created_by = u.id
        LEFT JOIN project_units pu ON p.id = pu.project_id
        GROUP BY p.id
        ORDER BY p.is_active DESC, p.created_at DESC
    """
    )

    projects = cursor.fetchall()
    conn.close()

    if projects:
        print(f"\n📋 PROJECTS STATUS CHECK:")
        for p in projects:
            if len(p) > 16:
                print(f"  ID {p[0]}: {p[1]} - is_active = {p[16]}")
            else:
                print(f"  ID {p[0]}: {p[1]} - Not enough columns ({len(p)})")

    # Calculate stats
    active_count = 0
    total_units_sum = 0
    sales_count = 0

    for p in projects:
        # is_active at index 16
        if len(p) > 16 and p[16] == 1:
            active_count += 1

        # total_units at index 14
        if len(p) > 14 and p[14] is not None:
            try:
                total_units_sum += int(p[14])
            except:
                pass

        # category at index 2
        if len(p) > 2 and p[2] == "sales":
            sales_count += 1

    # Generate HTML
    html = (
        """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Manage Projects</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .nav a { margin-right: 15px; color: #007bff; text-decoration: none; font-weight: bold; }
            .stats { display: flex; gap: 15px; margin: 20px 0; flex-wrap: wrap; }
            .stat-card { background: white; padding: 15px; border-radius: 8px; flex: 1; min-width: 120px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); text-align: center; }
            .stat-value { font-size: 1.8em; font-weight: bold; }
            .project-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; margin: 20px 0; }
            .project-card { background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .project-header { padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
            .project-body { padding: 20px; }
            .project-meta { display: flex; justify-content: space-between; margin: 10px 0; font-size: 14px; }
            .badge { padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }
            .badge-sales { background: #d4edda; color: #155724; }
            .badge-rental { background: #cce5ff; color: #004085; }
            .badge-residential { background: #fff3cd; color: #856404; }
            .badge-commercial { background: #e2e3e5; color: #383d41; }
            .btn { padding: 8px 16px; border-radius: 5px; text-decoration: none; display: inline-block; margin-right: 5px; font-size: 14px; }
            .btn-view { background: #17a2b8; color: white; }
            .btn-edit { background: #ffc107; color: #000; }
            .btn-warning { background: #ffc107; color: #000; }
            .btn-success { background: #28a745; color: white; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🏢 Manage Projects</h1>
            <div class="nav">
                <a href="/admin/dashboard">← Dashboard</a>
                <a href="/admin/create-project" style="background: #28a745; color: white; padding: 8px 16px; border-radius: 5px;">➕ Create New Project</a>
            </div>
        </div>
        
        <div class="stats">
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Total Projects</div>
                <div class="stat-value" style="color: #007bff;">"""
        + str(len(projects))
        + """</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Active Projects</div>
                <div class="stat-value" style="color: #28a745;">"""
        + str(active_count)
        + """</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Total Units</div>
                <div class="stat-value" style="color: #6f42c1;">"""
        + str(total_units_sum)
        + """</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Sales Projects</div>
                <div class="stat-value" style="color: #fd7e14;">"""
        + str(sales_count)
        + """</div>
            </div>
        </div>
        
        <div class="project-grid">
    """
    )

    for project in projects:
        if len(project) < 17:
            continue

        # CORRECT COLUMN INDICES based on debug:
        project_id = project[0]
        project_name = project[1]
        category = project[2] or "N/A"
        project_type = project[3] or "N/A"
        location = project[4] or "Not specified"
        commission = project[7] or "N/A"
        created_at = project[9]  # Index 9, not 8!
        created_by_name = project[11] or "Unknown"  # Index 11
        unit_count = project[12] or 0  # Index 12
        total_units = project[14] or 0  # Index 14 (not 13!)
        is_active = project[16] if len(project) > 16 else 1  # Index 16

        # Format date safely
        if isinstance(created_at, str):
            created_date = created_at[:10] if len(created_at) >= 10 else created_at
        else:
            created_date = str(created_at)[:10] if created_at else "N/A"

        html += f"""
            <div class="project-card">
                <div class="project-header">
                    <h3 style="margin: 0;">{project_name}
                        <span style="background: {'#28a745' if is_active == 1 else '#6c757d'}; color: white; padding: 2px 8px; border-radius: 10px; font-size: 12px; margin-left: 10px;">
                            {'Active' if is_active == 1 else 'Inactive'}
                        </span>
                    </h3>
         
                    <div style="margin-top: 5px; font-size: 14px;">
                        <span class="badge badge-{category}">{category.title()}</span>
                        <span class="badge badge-{project_type}">{project_type.title()}</span>
                    </div>
                </div>
                <div class="project-body">
                    <div class="project-meta">
                        <div>
                            <strong>Location:</strong><br>
                            {location}
                        </div>
                        <div>
                            <strong>Commission:</strong><br>
                            {commission}%
                        </div>
                    </div>
                    
                    <div class="project-meta">
                        <div>
                            <strong>Units:</strong><br>
                            {unit_count} types<br>
                            {total_units} total
                        </div>
                        <div>
                            <strong>Created:</strong><br>
                            {created_date}<br>
                            by {created_by_name}
                        </div>
                    </div>
                    
                    <div style="margin-top: 15px;">
                        <a href="/admin/project/{project_id}" class="btn btn-view">👁️ View Details</a>
                        <a href="/admin/edit-project/{project_id}" class="btn btn-edit">✏️ Edit</a>
        """

        if is_active == 1:
            html += f"""
                        <a href="/admin/toggle-project/{project_id}" 
                           class="btn btn-warning"
                           onclick="return confirm('Deactivate {project_name}? Agents will not see it.')">
                           ⏸️ Deactivate
                        </a>
            """
        else:
            html += f"""
                        <a href="/admin/toggle-project/{project_id}" 
                           class="btn btn-success"
                           onclick="return confirm('Activate {project_name}? Agents will see it again.')">
                           ▶️ Activate
                        </a>
            """

        html += """
                    </div>
                </div>
            </div>
        """

    if not projects:
        html += """
        <div style="padding: 40px; text-align: center; background: white; border-radius: 10px; grid-column: 1 / -1;">
            <h3>No projects found</h3>
            <p>You haven't created any projects yet.</p>
            <a href="/admin/create-project" style="background: #28a745; color: white; padding: 10px 20px; border-radius: 5px; text-decoration: none; display: inline-block; margin-top: 15px;">Create Your First Project</a>
        </div>
        """

    html += """
        </div>
    </body>
    </html>
    """

    return html


@app.route("/admin/project/<int:project_id>")
def view_project(project_id):
    """View project details"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get project details
    cursor.execute(
        """
        SELECT p.*, u.name as created_by_name
        FROM projects p
        LEFT JOIN users u ON p.created_by = u.id
        WHERE p.id = ?
    """,
        (project_id,),
    )

    project = cursor.fetchone()

    if not project:
        conn.close()
        return "Project not found", 404

    # Get project units
    cursor.execute(
        "SELECT * FROM project_units WHERE project_id = ? ORDER BY unit_type",
        (project_id,),
    )
    units = cursor.fetchall()

    conn.close()

    # Determine price label based on project_sale_type
    price_label = "Sale Price" if project[11] == "sales" else "Monthly Rent"

    # Prepare units HTML
    units_html = ""
    if units:
        for unit in units:
            # Unit columns from earlier output:
            # 0: id, 1: project_id, 2: unit_type, 3: square_feet,
            # 4: base_price, 5: rental_price, 6: commission_rate,
            # 7: quantity, 8: status, 9: created_at, 10: updated_at,
            # 11: unit_code, 12: price, 13: size

            # Choose correct price column
            if project[11] == "rental":
                # For rental projects, use rental_price OR base_price if rental_price is empty
                price_value = unit[5] if unit[5] is not None else unit[4]
            else:
                # For sales projects, use base_price
                price_value = unit[4]

            # Format price
            formatted_price = f"RM{price_value:,.2f}" if price_value else "N/A"

            # Get commission rate (unit or project default)
            commission_rate = unit[6] if unit[6] else project[7]
            formatted_commission = f"{commission_rate}%" if commission_rate else "N/A"

            status_color = "#28a745" if unit[8] == "available" else "#dc3545"

            units_html += f"""
            <tr>
                <td>{unit[11] or 'N/A'}</td>
                <td>{unit[2] or 'N/A'}</td>
                <td>{unit[3] or 'N/A'}</td>
                <td>{formatted_price}</td>
                <td>{formatted_commission}</td>
                <td>{unit[7] or 1}</td>
                <td><span style="color: {status_color}">{unit[8].title()}</span></td>
            </tr>
            """
    else:
        units_html = """
        <tr>
            <td colspan="7" style="text-align: center; padding: 40px; color: #666;">
                <h3>No units defined yet</h3>
                <p>Add unit types to this project by editing it.</p>
            </td>
        </tr>
        """

    # Create the template with corrected price_label
    detail_template = f"""<!DOCTYPE html>
<html>
<head>
    <title>{project[1]} - Project Details</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .info-card {{ background: white; padding: 25px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .unit-table {{ width: 100%; background: white; border-radius: 10px; overflow: hidden; margin: 20px 0; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        th, td {{ padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #2c3e50; color: white; }}
        .btn {{ padding: 8px 16px; border-radius: 5px; text-decoration: none; display: inline-block; margin-right: 10px; }}
        .btn-back {{ background: #6c757d; color: white; }}
        .btn-edit {{ background: #007bff; color: white; }}
        .badge {{ background: #d4edda; color: #155724; padding: 5px 10px; border-radius: 3px; margin-right: 10px; font-size: 12px; }}
        .badge-type {{ background: #cce5ff; color: #004085; }}
        .badge-sale {{ background: #f8d7da; color: #721c24; }}
        .badge-rental {{ background: #d1ecf1; color: #0c5460; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏢 {project[1]}</h1>
            <div style="margin-top: 10px;">
                <a href="/admin/projects" class="btn btn-back">← Back to Projects</a>
                <a href="/admin/edit-project/{project_id}" class="btn btn-edit">✏️ Edit Project</a>
            </div>
            <div style="margin-top: 10px;">
                <span class="badge">{project[2].upper() if project[2] else 'N/A'}</span>
                <span class="badge badge-type">{project[3].upper() if project[3] else 'N/A'}</span>
                <span class="badge {'badge-sale' if project[11] == 'sales' else 'badge-rental'}">
                    {project[11].upper() if project[11] else 'SALES'}
                </span>
            </div>
        </div>
        
        <div class="info-card">
            <h2>📋 Project Information</h2>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 20px;">
                <div>
                    <p><strong>Location:</strong> {project[4] or 'Not specified'}</p>
                    <p><strong>Default Commission:</strong> {project[7] or 'N/A'}%</p>
                    <p><strong>Status:</strong> {project[6].title() if project[6] else 'N/A'}</p>
                </div>
                <div>
                    <p><strong>Created:</strong> {project[9][:19] if project[9] else 'N/A'}</p>
                    <p><strong>By:</strong> {project[12] or 'Unknown'}</p>
                    <p><strong>Last Updated:</strong> {project[10][:19] if project[10] else 'N/A'}</p>
                </div>
            </div>
            {f'<div style="margin-top: 15px;"><strong>Description:</strong><p>{project[5]}</p></div>' if project[5] else ''}
        </div>
        
        <div class="info-card">
            <h2>💰 Commission Structure</h2>
            <div style="padding: 20px; background: #f8f9fa; border-radius: 8px; text-align: center;">
                <div style="font-size: 14px; color: #666; margin-bottom: 10px;">Project Commission Rate</div>
                <div style="font-size: 48px; font-weight: bold; color: #28a745;">
                    {project[7] or 'N/A'}%
                </div>
                <div style="margin-top: 20px; padding: 15px; background: #e8f4ff; border-radius: 5px; text-align: left;">
                    <strong>📌 Commission Rules:</strong>
                    <ul style="margin: 10px 0 0 20px;">
                        <li>This is the default commission rate for all sales in this project</li>
                        <li>Unit-specific commission rates can override this default rate</li>
                        <li>Commission is calculated as: Sale Price × Commission Rate</li>
                        <li>Commission is subject to caps: Min RM1,000 - Max RM50,000</li>
                    </ul>
                </div>
            </div>
        </div>
        
        <div class="info-card">
            <h2>🏠 Project Units ({len(units)} units)</h2>
            <table class="unit-table">
                <thead>
                    <tr>
                        <th>Unit Code</th>
                        <th>Unit Type</th>
                        <th>Size (sqft)</th>
                        <th>{price_label}</th>
                        <th>Commission</th>
                        <th>Quantity</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
                    {units_html}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""

    return detail_template


@app.route("/admin/toggle-project/<int:project_id>")
def toggle_project(project_id):
    """Toggle project active/inactive status - FIXED VERSION"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = None
    try:
        conn = sqlite3.connect("real_estate.db")
        cursor = conn.cursor()

        print(f"\n🔄 TOGGLE REQUESTED for project {project_id}")

        # 1. First ensure is_active column exists
        try:
            cursor.execute(
                "ALTER TABLE projects ADD COLUMN is_active INTEGER DEFAULT 1"
            )
            conn.commit()
            print("✅ Added is_active column (if missing)")
        except Exception as e:
            print(f"✓ Column check: {e}")

        # 2. Get current is_active value
        cursor.execute(
            "SELECT project_name, is_active FROM projects WHERE id = ?", (project_id,)
        )
        project = cursor.fetchone()

        if not project:
            conn.close()
            flash("Project not found", "error")
            return redirect("/admin/projects")

        project_name, current = project
        print(f"✓ Found project: {project_name}, current is_active = {current}")

        # Handle None/Null values
        if current is None:
            current = 1  # Default to active

        # 3. Toggle the value
        new_value = 0 if current == 1 else 1
        print(f"✓ Changing is_active from {current} to {new_value}")

        # 4. Update database
        cursor.execute(
            "UPDATE projects SET is_active = ? WHERE id = ?", (new_value, project_id)
        )
        rows_updated = cursor.rowcount

        conn.commit()
        conn.close()

        print(f"✅ Updated {rows_updated} row(s)")

        status_text = "activated" if new_value == 1 else "deactivated"
        flash(f'Project "{project_name}" {status_text} successfully', "success")

    except Exception as e:
        print(f"❌ TOGGLE ERROR: {e}")
        if conn:
            conn.rollback()
            conn.close()
        flash(f"Error: {str(e)}", "error")

    return redirect("/admin/projects")


@app.route("/admin/export-data")
def export_data():
    """Export data to CSV/Excel"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    export_type = request.args.get("type", "csv")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    if export_type == "commissions":
        # Export commission data
        cursor.execute(
            """
            SELECT 
                pl.id as listing_id,
                pl.customer_name,
                pl.customer_email,
                pl.property_address,
                pl.property_type,
                pl.sale_price,
                pl.commission_amount,
                pl.status,
                pl.submitted_at,
                pl.approved_at,
                u.name as agent_name,
            FROM property_listings pl
            JOIN users u ON pl.agent_id = u.id
            ORDER BY pl.created_at DESC
        """
        )
        data = cursor.fetchall()
        filename = f"commissions_export_{datetime.now().strftime('%Y%m%d')}.csv"
        csv_content = "Listing ID,Customer Name,Customer Email,Property Address,Property Type,Sale Price,Commission,Status,Submitted Date,Approved Date,Agent Name,Agent Tier\n"

        for row in data:
            # Escape commas in CSV
            row_escaped = []
            for item in row:
                if item and "," in str(item):
                    row_escaped.append(f'"{item}"')
                else:
                    row_escaped.append(str(item) if item else "")
            csv_content += ",".join(row_escaped) + "\n"

        response = app.response_class(
            response=csv_content,
            status=200,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
        return response

    elif export_type == "agents":
        # Export agent data
        cursor.execute(
            """
            SELECT 
                u.id,
                u.name,
                u.email,
                u.created_at,
                COUNT(pl.id) as total_listings,
                SUM(CASE WHEN pl.status = 'approved' THEN pl.commission_amount ELSE 0 END) as total_commission
            FROM users u
            LEFT JOIN property_listings pl ON u.id = pl.agent_id
            WHERE u.role = 'agent'
            GROUP BY u.id
            ORDER BY u.name
        """
        )
        data = cursor.fetchall()
        filename = f"agents_export_{datetime.now().strftime('%Y%m%d')}.csv"
        csv_content = (
            "Agent ID,Name,Email,Tier,Created Date,Total Listings,Total Commission\n"
        )

        for row in data:
            row_escaped = []
            for item in row:
                if item and "," in str(item):
                    row_escaped.append(f'"{item}"')
                else:
                    row_escaped.append(str(item) if item else "")
            csv_content += ",".join(row_escaped) + "\n"

        response = app.response_class(
            response=csv_content,
            status=200,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
        return response

    elif export_type == "payments":
        # Export payment data
        cursor.execute(
            """
            SELECT 
                cp.id,
                cp.listing_id,
                cp.agent_id,
                cp.commission_amount,
                cp.payment_status,
                cp.payment_date,
                cp.payment_method,
                cp.transaction_id,
                cp.created_at,
                u.name as agent_name,
                pl.customer_name
            FROM commission_payments cp
            JOIN users u ON cp.agent_id = u.id
            JOIN property_listings pl ON cp.listing_id = pl.id
            ORDER BY cp.created_at DESC
        """
        )
        data = cursor.fetchall()
        filename = f"payments_export_{datetime.now().strftime('%Y%m%d')}.csv"
        csv_content = "Payment ID,Listing ID,Agent ID,Amount,Status,Payment Date,Payment Method,Transaction ID,Created Date,Agent Name,Customer Name\n"

        for row in data:
            row_escaped = []
            for item in row:
                if item and "," in str(item):
                    row_escaped.append(f'"{item}"')
                else:
                    row_escaped.append(str(item) if item else "")
            csv_content += ",".join(row_escaped) + "\n"

        response = app.response_class(
            response=csv_content,
            status=200,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
        return response

    conn.close()

    # If no export type specified, show export options page
    export_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Export Data</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
            .export-options { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin: 20px 0; }
            .export-card { background: white; padding: 25px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); text-align: center; }
            .export-icon { font-size: 48px; margin-bottom: 15px; }
            .btn { padding: 10px 20px; background: #28a745; color: white; text-decoration: none; border-radius: 5px; display: inline-block; margin: 5px; }
            .btn:hover { background: #218838; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>📤 Export Data</h1>
            <div>
                <a href="/admin/dashboard">← Dashboard</a>
            </div>
        </div>
        
        <div class="export-options">
            <div class="export-card">
                <div class="export-icon">💰</div>
                <h3>Commissions Export</h3>
                <p>Export all commission records to CSV</p>
                <a href="/admin/export-data?type=commissions" class="btn">Download CSV</a>
            </div>
            
            <div class="export-card">
                <div class="export-icon">👥</div>
                <h3>Agents Export</h3>
                <p>Export agent information and performance</p>
                <a href="/admin/export-data?type=agents" class="btn">Download CSV</a>
            </div>
            
            <div class="export-card">
                <div class="export-icon">💳</div>
                <h3>Payments Export</h3>
                <p>Export payment transaction history</p>
                <a href="/admin/export-data?type=payments" class="btn">Download CSV</a>
            </div>
            
            <div class="export-card">
                <div class="export-icon">💾</div>
                <h3>Full Database Export</h3>
                <p>Export complete database with all tables (SQL + CSV)</p>
                <a href="/admin/export-full-db" class="btn" style="background: #6f42c1;">Download Full Backup</a>
            </div>
        </div>
        
        <div style="margin-top: 30px; padding: 20px; background: #f8f9fa; border-radius: 10px;">
            <h3>Export Notes:</h3>
            <ul>
                <li>CSV files can be opened in Excel, Google Sheets, or any spreadsheet software</li>
                <li>Data is exported in UTF-8 format</li>
                <li>All exports include headers for easy identification</li>
                <li>Exports are generated with current date in filename</li>
            </ul>
        </div>
    </body>
    </html>
    """

    return render_template_string(export_template)


@app.route("/admin/agent-performance")
def agent_performance_admin():
    """Admin view of agent performance analytics"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Get agent performance data - REMOVED agent_tier
    cursor.execute(
        """
        SELECT 
            u.id,
            u.name,
            u.email,
            u.created_at as join_date,
            COUNT(pl.id) as total_listings,
            SUM(CASE WHEN pl.status = 'approved' THEN 1 ELSE 0 END) as approved_listings,
            SUM(CASE WHEN pl.status = 'rejected' THEN 1 ELSE 0 END) as rejected_listings,
            SUM(pl.sale_price) as total_sales,
            SUM(pl.commission_amount) as total_commission,
            AVG(pl.sale_price) as avg_sale_price,
            AVG(pl.commission_amount) as avg_commission,
            MAX(pl.approved_at) as last_approval
        FROM users u
        LEFT JOIN property_listings pl ON u.id = pl.agent_id
        WHERE u.role = 'agent'
        GROUP BY u.id
        ORDER BY total_commission DESC
    """
    )

    agents_data = cursor.fetchall()

    # Get monthly performance data
    cursor.execute(
        """
        SELECT 
            strftime('%Y-%m', pl.approved_at) as month,
            u.name as agent_name,
            COUNT(pl.id) as listings,
            SUM(pl.commission_amount) as commission
        FROM property_listings pl
        JOIN users u ON pl.agent_id = u.id
        WHERE pl.status = 'approved' AND pl.approved_at IS NOT NULL
        GROUP BY month, u.id
        ORDER BY month DESC, commission DESC
        LIMIT 50
    """
    )

    monthly_data = cursor.fetchall()

    conn.close()

    # Prepare data for template - REMOVED tier references
    performance_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Agent Performance</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .nav a { margin-right: 15px; color: #007bff; text-decoration: none; font-weight: bold; }
            .stats-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin: 20px 0; }
            .stat-box { background: white; padding: 15px; border-radius: 8px; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
            .stat-value { font-size: 1.5em; font-weight: bold; }
            table { width: 100%; background: white; border-radius: 10px; overflow: hidden; margin: 20px 0; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }
            th { background: #2c3e50; color: white; }
            .success-rate { font-weight: bold; }
            .rate-high { color: #28a745; }
            .rate-medium { color: #ffc107; }
            .rate-low { color: #dc3545; }
            .performance-section { margin: 30px 0; }
            .section-header { background: #f8f9fa; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>📊 Agent Performance Analytics</h1>
            <div class="nav">
                <a href="/admin/dashboard">← Dashboard</a>
                <a href="/admin/agents">👥 Manage Agents</a>
                <a href="/admin/export-data?type=agents">📤 Export Report</a>
            </div>
        </div>
        
        <div class="stats-summary">
            <div class="stat-box">
                <div style="color: #666; font-size: 14px;">Total Agents</div>
                <div class="stat-value" style="color: #007bff;">{{ agent_count }}</div>
            </div>
            <div class="stat-box">
                <div style="color: #666; font-size: 14px;">Total Commissions</div>
                <div class="stat-value" style="color: #28a745;">RM{{ "{:,.2f}".format(total_commissions) }}</div>
            </div>
            <div class="stat-box">
                <div style="color: #666; font-size: 14px;">Total Sales</div>
                <div class="stat-value" style="color: #6f42c1;">RM{{ "{:,.2f}".format(total_sales) }}</div>
            </div>
            <div class="stat-box">
                <div style="color: #666; font-size: 14px;">Avg. Success Rate</div>
                <div class="stat-value" style="color: #17a2b8;">{{ avg_success_rate }}%</div>
            </div>
        </div>
        
        <div class="performance-section">
            <div class="section-header">
                <h2 style="margin: 0;">👥 Agent Performance Ranking</h2>
                <p>Sorted by total commission earned</p>
            </div>
            
            {% if agents_data %}
            <table>
                <thead>
                    <tr>
                        <th>Rank</th>
                        <th>Agent</th>
                        <th>Listings</th>
                        <th>Approved</th>
                        <th>Rejected</th>
                        <th>Success Rate</th>
                        <th>Total Sales</th>
                        <th>Total Commission</th>
                        <th>Avg. Sale</th>
                        <th>Last Approval</th>
                    </tr>
                </thead>
                <tbody>
                    {% for agent in agents_data %}
                    <tr>
                        <td>{{ loop.index }}</td>
                        <td>
                            <strong>{{ agent[1] }}</strong><br>
                            <small>{{ agent[2] }}</small>
                        </td>
                        <td>{{ agent[4] or 0 }}</td>
                        <td>{{ agent[5] or 0 }}</td>
                        <td>{{ agent[6] or 0 }}</td>
                        <td>
                            {% set total = agent[4] or 0 %}
                            {% set approved = agent[5] or 0 %}
                            {% if total > 0 %}
                                {% set rate = (approved / total * 100)|round|int %}
                                <span class="success-rate 
                                    {% if rate >= 80 %}rate-high
                                    {% elif rate >= 50 %}rate-medium
                                    {% else %}rate-low
                                    {% endif %}">
                                    {{ rate }}%
                                </span>
                            {% else %}
                                <span style="color: #999;">N/A</span>
                            {% endif %}
                        </td>
                        <td>RM{{ "{:,.2f}".format(agent[7] or 0) }}</td>
                        <td>
                            <strong style="color: #28a745;">RM{{ "{:,.2f}".format(agent[8] or 0) }}</strong>
                        </td>
                        <td>RM{{ "{:,.0f}".format(agent[9] or 0) }}</td>
                        <td>{{ agent[11][:10] if agent[11] else 'Never' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div style="padding: 40px; text-align: center; background: white; border-radius: 10px;">
                <h3>No agent data available</h3>
                <p>No agents have submitted any listings yet.</p>
            </div>
            {% endif %}
        </div>
        
        <div class="performance-section">
            <div class="section-header">
                <h2 style="margin: 0;">📅 Monthly Performance</h2>
                <p>Recent months commission activity</p>
            </div>
            
            {% if monthly_data %}
            <table>
                <thead>
                    <tr>
                        <th>Month</th>
                        <th>Agent</th>
                        <th>Approved Listings</th>
                        <th>Commission Earned</th>
                    </tr>
                </thead>
                <tbody>
                    {% for month in monthly_data %}
                    <tr>
                        <td>{{ month[0] }}</td>
                        <td>{{ month[1] }}</td>
                        <td>{{ month[2] or 0 }}</td>
                        <td><strong>RM{{ "{:,.2f}".format(month[3] or 0) }}</strong></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div style="padding: 20px; background: white; border-radius: 10px; text-align: center; color: #666;">
                No monthly performance data available
            </div>
            {% endif %}
        </div>
    </body>
    </html>
    """

    # Calculate summary statistics
    agent_count = len(agents_data)
    total_commissions = sum(agent[8] or 0 for agent in agents_data)
    total_sales = sum(agent[7] or 0 for agent in agents_data)

    # Calculate average success rate
    success_rates = []
    for agent in agents_data:
        total_listings = agent[4] or 0
        approved = agent[5] or 0
        if total_listings > 0:
            success_rates.append((approved / total_listings) * 100)

    avg_success_rate = (
        round(sum(success_rates) / max(len(success_rates), 1)) if success_rates else 0
    )

    return render_template_string(
        performance_template,
        agents_data=agents_data,
        monthly_data=monthly_data,
        agent_count=agent_count,
        total_commissions=total_commissions,
        total_sales=total_sales,
        avg_success_rate=avg_success_rate,
    )


@app.route("/admin/export-full-db")
def export_full_database():
    """Export complete database as SQL and CSV files"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    try:
        # Create export directory
        export_dir = "database_exports"
        if not os.path.exists(export_dir):
            os.makedirs(export_dir)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_filename = f"real_estate_db_export_{timestamp}"

        conn = sqlite3.connect("real_estate.db")
        cursor = conn.cursor()

        # Get all table names
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]

        # Create SQL dump
        sql_dump = f'-- Real Estate Database Export\n-- Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n-- Tables: {len(tables)}\n\n'

        # Create ZIP file in memory
        from io import BytesIO
        import zipfile

        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:

            # 1. Export as SQL
            for table in tables:
                # Get table schema
                cursor.execute(
                    f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'"
                )
                schema = cursor.fetchone()[0]

                sql_dump += f"--\n-- Table: {table}\n--\n\n"
                sql_dump += f"{schema};\n\n"

                # Get table data
                cursor.execute(f"SELECT * FROM {table}")
                rows = cursor.fetchall()

                if rows:
                    # Get column names
                    cursor.execute(f"PRAGMA table_info({table})")
                    columns = [col[1] for col in cursor.fetchall()]

                    sql_dump += f"-- Data for table {table} ({len(rows)} rows)\n"

                    for row in rows:
                        values = []
                        for value in row:
                            if value is None:
                                values.append("NULL")
                            elif isinstance(value, (int, float)):
                                values.append(str(value))
                            else:
                                # Escape single quotes in strings
                                escaped = str(value).replace("'", "''")
                                values.append(f"'{escaped}'")

                        sql_dump += f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)});\n"

                sql_dump += "\n"

            # Add SQL file to zip
            zip_file.writestr(f"{export_filename}.sql", sql_dump)

            # 2. Export each table as CSV
            for table in tables:
                cursor.execute(f"SELECT * FROM {table}")
                rows = cursor.fetchall()

                if rows:
                    # Get column names
                    cursor.execute(f"PRAGMA table_info({table})")
                    columns = [col[1] for col in cursor.fetchall()]

                    # Create CSV content
                    csv_content = ",".join(columns) + "\n"

                    for row in rows:
                        row_data = []
                        for value in row:
                            if value is None:
                                row_data.append("")
                            elif isinstance(value, (int, float)):
                                row_data.append(str(value))
                            else:
                                # Escape commas and quotes in CSV
                                escaped = str(value).replace('"', '""')
                                if "," in escaped or '"' in escaped or "\n" in escaped:
                                    escaped = f'"{escaped}"'
                                row_data.append(escaped)

                        csv_content += ",".join(row_data) + "\n"

                    # Add CSV file to zip
                    zip_file.writestr(f"{export_filename}/{table}.csv", csv_content)

            # 3. Create README file
            readme_content = f"""Real Estate Database Export
===============================

Export Details:
- Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- Database: real_estate.db
- Tables exported: {len(tables)}
- Export ID: {export_filename}

Table Information:
{'-' * 40}

"""

            for table in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                readme_content += f"{table}: {count} rows\n"

            readme_content += f"""

Export Contents:
{'-' * 40}
1. {export_filename}.sql - Complete SQL dump of database
2. {export_filename}/ - Folder containing CSV files for each table

Usage:
- SQL file: Can be imported into any SQLite database
- CSV files: Can be opened in Excel, Google Sheets, or any spreadsheet software

Tables:
{'-' * 40}
"""

            for table in tables:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = cursor.fetchall()
                readme_content += f"\n{table}:\n"
                for col in columns:
                    col_name = col[1]
                    col_type = col[2]
                    col_notnull = "NOT NULL" if col[3] else "NULL"
                    col_pk = "PRIMARY KEY" if col[5] else ""
                    readme_content += (
                        f"  - {col_name} ({col_type}) {col_notnull} {col_pk}\n"
                    )

            readme_content += f"""

Generated by Real Estate Sales System
Admin: {session.get('user_name', 'Unknown')}
"""

            zip_file.writestr(f"{export_filename}/README.txt", readme_content)

        conn.close()

        # Prepare response
        zip_buffer.seek(0)
        response = app.response_class(
            response=zip_buffer.getvalue(),
            status=200,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename={export_filename}.zip",
                "Content-Type": "application/zip",
            },
        )

        return response

    except Exception as e:
        error_template = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Export Error</title>
            <style>
                body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }
                .error-box { border: 2px solid #dc3545; padding: 30px; border-radius: 10px; text-align: center; }
                h2 { color: #dc3545; }
            </style>
        </head>
        <body>
            <div class="error-box">
                <h2>❌ Database Export Failed</h2>
                <p><strong>Error:</strong> {{ error }}</p>
                <div style="margin-top: 30px;">
                    <a href="/admin/export-data" style="background: #007bff; color: white; padding: 10px 20px; 
                       text-decoration: none; border-radius: 5px; margin-right: 10px;">← Back to Export</a>
                    <a href="/admin/dashboard" style="background: #6c757d; color: white; padding: 10px 20px; 
                       text-decoration: none; border-radius: 5px;">Dashboard</a>
                </div>
            </div>
        </body>
        </html>
        """
        return render_template_string(error_template, error=str(e))


@app.route("/admin/check-db-structure")
def check_db_structure():
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    result = "<h1>Database Structure Check</h1>"

    # List all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()

    result += "<h2>Existing Tables:</h2><ul>"
    for table in tables:
        result += f"<li>{table[0]}</li>"
    result += "</ul>"

    # Check users table columns
    cursor.execute("PRAGMA table_info(users)")
    users_columns = cursor.fetchall()

    result += "<h2>Users Table Columns:</h2><ul>"
    for col in users_columns:
        result += f"<li>{col[1]} ({col[2]})</li>"
    result += "</ul>"

    conn.close()
    return result


# ============ RUN APPLICATION ============
if __name__ == "__main__":
    print("🚀 Starting Real Estate Sales System...")
    print("Initializing database...")
    init_database()
    print("Updating database schema...")
    update_database()  # This now includes tier removal
    cleanup_tier_data()  # Add this line
    print("✅ System ready!")
    print("🌐 Open your browser and go to: http://localhost:5000")
    print("👤 Test accounts:")
    print("   Admin: admin@example.com / admin123")
    print("   Agent: agent@example.com / agent123")
    print("📁 Upload folder: ./uploads/")
    print("👁️ Document preview feature enabled!")
    app.run(host="0.0.0.0", port=5000)