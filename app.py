import sqlite3
import json
import smtplib
import csv
from flask import Flask, render_template, render_template_string, request, redirect, session, jsonify, flash, send_file, url_for
from io import StringIO
from datetime import datetime
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
            conn = sqlite3.connect('real_estate.db', timeout=timeout)
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
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'

# ============ ADD SECURITY CONFIGURATION HERE ============
from datetime import timedelta

# Session security settings
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)
app.config['SESSION_COOKIE_SECURE'] = False  # Set to True when using HTTPS
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# File upload security
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'jpg', 'jpeg', 'png'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def validate_file_size(file_storage):
    """Validate file size before saving"""
    # Get file size without reading entire file
    if hasattr(file_storage, 'content_length'):
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
    if file_type in ['pdf']:
        return '📄'
    elif file_type in ['jpg', 'jpeg', 'png', 'gif', 'bmp']:
        return '🖼️'
    elif file_type in ['doc', 'docx']:
        return '📝'
    elif file_type in ['xls', 'xlsx']:
        return '📊'
    elif file_type in ['txt']:
        return '📋'
    else:
        return '📎'

# ===================== MULTI-LEVEL COMMISSION HELPERS =====================
# ADD THE HELPER FUNCTIONS RIGHT HERE:

def get_agent_with_upline_info(agent_id):
    """Get agent information with upline details"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get agent with upline names - UPDATED for users table
    cursor.execute('''
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
    ''', (agent_id,))
    
    agent = cursor.fetchone()
    conn.close()
    
    if agent:
        # Convert to dictionary with proper column mapping
        columns = ['id', 'email', 'password', 'name', 'role', 
                  'upline_id', 'upline_commission_rate', 'created_at',
                  'upline2_id', 'upline2_commission_rate', 'commission_rate',
                  'total_listings', 'total_commission', 'joined_date',
                  'upline_name', 'upline_email', 'upline2_name', 'upline2_email']
        
        # Fill missing columns with None (adjust based on your actual table structure)
        agent_data = dict(zip(columns[:len(agent)], agent))
        return agent_data
    return None

# ===================== COMMISSION CALCULATION =====================
# ADD THIS FUNCTION RIGHT HERE:

def calculate_multi_level_commission(sale_amount, agent_id):
    """Calculate commissions for multiple upline levels"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get agent's commission rates
    cursor.execute('''
    SELECT upline_id, upline2_id, upline_commission_rate, upline2_commission_rate, commission_rate 
    FROM users WHERE id = ? AND role = "agent"
    ''', (agent_id,))
    
    agent = cursor.fetchone()
    
    commissions = []
    
    if agent:
        upline_id, upline2_id, upline_rate, upline2_rate, agent_rate = agent
        
        # Calculate for agent themselves
        if agent_rate and agent_rate > 0:
            amount = sale_amount * (agent_rate / 100)
            commissions.append({
                'agent_id': agent_id,
                'amount': amount,
                'rate': agent_rate,
                'level': 0,
                'type': 'self'
            })
            
            # Update agent's total commission
            cursor.execute("""
            UPDATE users 
            SET total_commission = COALESCE(total_commission, 0) + ? 
            WHERE id = ?
            """, (amount, agent_id))
        
        # Calculate for direct upline
        if upline_id and upline_rate and upline_rate > 0:
            amount = sale_amount * (upline_rate / 100)
            commissions.append({
                'agent_id': upline_id,
                'amount': amount,
                'rate': upline_rate,
                'level': 1,
                'type': 'direct_upline'
            })
            
            # Update upline's total commission
            cursor.execute("""
            UPDATE users 
            SET total_commission = COALESCE(total_commission, 0) + ? 
            WHERE id = ?
            """, (amount, upline_id))
        
        # Calculate for indirect upline
        if upline2_id and upline2_rate and upline2_rate > 0:
            amount = sale_amount * (upline2_rate / 100)
            commissions.append({
                'agent_id': upline2_id,
                'amount': amount,
                'rate': upline2_rate,
                'level': 2,
                'type': 'indirect_upline'
            })
            
            # Update upline2's total commission
            cursor.execute("""
            UPDATE users 
            SET total_commission = COALESCE(total_commission, 0) + ? 
            WHERE id = ?
            """, (amount, upline2_id))
        
        conn.commit()
    
    conn.close()
    return commissions

def update_upline_chain(agent_id, upline_id):
    """Update an agent's upline and automatically set upline2"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get the new upline's upline (for upline2)
    cursor.execute("SELECT upline_id FROM users WHERE id = ? AND role = 'agent'", (upline_id,))
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
    previewable_types = ['pdf', 'jpg', 'jpeg', 'png', 'gif', 'txt']
    return file_type.lower() in previewable_types

def check_and_notify_incomplete_docs(listing_id, agent_id, customer_name):
    """Check if submission has insufficient documents and notify agent immediately"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Count documents for this listing
    cursor.execute('SELECT COUNT(*) FROM documents WHERE listing_id = ?', (listing_id,))
    doc_count = cursor.fetchone()[0]
    
    conn.close()
    
    # Create notification based on document count
    if doc_count == 0:
        create_agent_notification(
            agent_id=agent_id,
            notification_type='incomplete_docs',
            title="🚨 CRITICAL: No Documents Uploaded",
            message=f"Submission #{listing_id} ({customer_name}) has NO documents uploaded. This cannot be submitted.",
            related_id=listing_id,
            related_type='listing',
            priority='urgent'
        )
    elif doc_count == 1:
        create_agent_notification(
            agent_id=agent_id,
            notification_type='incomplete_docs',
            title=" Very Incomplete Documents",
            message=f"Submission #{listing_id} ({customer_name}) has only 1/3 documents. Minimum 3 documents required.",
            related_id=listing_id,
            related_type='listing',
            priority='high'
        )
    elif doc_count == 2:
        create_agent_notification(
            agent_id=agent_id,
            notification_type='incomplete_docs',
            title="📎 Missing Documents",
            message=f"Submission #{listing_id} ({customer_name}) has {doc_count}/3 documents. One more document needed.",
            related_id=listing_id,
            related_type='listing',
            priority='normal'
        )

# ============ DATABASE SETUP ============
def init_database():
    """Create all necessary tables"""
    import time
    import traceback
    
    print("🔧 Starting database initialization...")
    
    conn = None
    try:
        # Try to connect with timeout
        conn = sqlite3.connect('real_estate.db', timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")  # Enable Write-Ahead Logging
        cursor = conn.cursor()
        
        # ============ CREATE TABLES ============
        
        # Users Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT DEFAULT 'agent',
                upline_id INTEGER NULL,
                agent_commission_rate DECIMAL(5,4) DEFAULT 0.025,  -- 2.5%
                upline_commission_rate DECIMAL(5,4) DEFAULT 0.20,  -- 20% of agent's commission
                is_deleted BOOLEAN DEFAULT 0,
                deleted_at TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (upline_id) REFERENCES users(id)
            )
        ''')
        
        # Property Listings Table
        cursor.execute('''
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
        ''')
        
        # Commission Distributions Table
        cursor.execute('''
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
        ''')
        
        # Commission Calculations Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS commission_calculations (
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
        ''')
        
        # Documents Table
        cursor.execute('''
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
        ''')
        
        # Commission Payments Table
        cursor.execute('''
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
        ''')
        
        # Projects Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT NOT NULL,
                category TEXT NOT NULL,
                project_type TEXT NOT NULL,
                location TEXT,
                description TEXT,
                status TEXT DEFAULT 'active',
                commission_rate DECIMAL(5,2),
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Project Units Table
        cursor.execute('''
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
        ''')
        
        # System Settings Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_type TEXT NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(setting_type, setting_key)
            )
        ''')
        
        # Payment Vouchers Table
        cursor.execute('''
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
        ''')
        
        # Email Logs Table
        cursor.execute('''
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
        ''')
        
        # Deletion Logs Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS deletion_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                deleted_by INTEGER NOT NULL,
                reason TEXT,
                deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (deleted_by) REFERENCES users(id)
            )
        ''')
        
        conn.commit()
        
        # ============ INITIALIZE DEFAULT SETTINGS ============
        
        default_settings = [
            ('payment', 'processing_days', '14'),
            ('payment', 'min_payout', '100'),
            ('payment', 'payout_schedule', 'monthly'),
            ('payment', 'auto_generate_voucher', 'yes'),
            ('payment', 'voucher_template', 'detailed'),
            ('payment', 'voucher_prefix', 'PAY'),
            ('payment', 'payment_methods', 'bank_transfer,check'),
            ('notification', 'notifications', 'submission_received,submission_approved,payment_processed,reminders'),
            ('notification', 'auto_approve_threshold', '0'),
            ('notification', 'reminder_days', '3'),
            ('notification', 'admin_email', 'admin@example.com'),
            ('notification', 'system_from_email', 'noreply@realestate.com'),
            ('notification', 'smtp_server', ''),
            ('notification', 'smtp_port', ''),
            ('notification', 'smtp_username', ''),
            ('notification', 'smtp_password', ''),
            ('notification', 'email_footer', '© 2024 Real Estate System. All rights reserved.'),
            ('commission', 'default_agent_rate', '0.025'),  # 2.5%
            ('commission', 'default_upline_rate', '0.20'),   # 20%
            ('commission', 'max_upline_levels', '3'),
            ('commission', 'calculation_method', 'percentage_of_commission')
        ]
        
        for setting_type, setting_key, default_value in default_settings:
            cursor.execute('''
                INSERT OR IGNORE INTO system_settings (setting_type, setting_key, setting_value)
                VALUES (?, ?, ?)
            ''', (setting_type, setting_key, default_value))
        
        conn.commit()
        
        # ============ CREATE SAMPLE USERS ============
        print("\n👤 Checking for sample users...")
        
        # Check for specific users BEFORE creating them
        cursor.execute("SELECT email FROM users WHERE email IN ('admin@example.com', 'agent@example.com', 'john_agent@yahoo.com', 'erwin@yahoo.com')")
        existing_emails = [row[0] for row in cursor.fetchall()]
        print(f"📊 Existing sample users: {len(existing_emails)}")
        
        # Only create users that don't exist
        try:
            if 'admin@example.com' not in existing_emails:
                print("➕ Creating admin user...")
                from werkzeug.security import generate_password_hash
                admin_password = generate_password_hash('admin456***')
                cursor.execute(
                    "INSERT INTO users (email, password, name, role) VALUES (?, ?, ?, ?)",
                    ('admin@example.com', admin_password, 'Admin User', 'admin')
                )
                print("   ✅ Admin user created")
            
            if 'agent@example.com' not in existing_emails:
                print("➕ Creating agent user...")
                from werkzeug.security import generate_password_hash
                agent_password = generate_password_hash('agent123')
                cursor.execute(
                    "INSERT INTO users (email, password, name, role, agent_commission_rate) VALUES (?, ?, ?, ?, ?)",
                    ('agent@example.com', agent_password, 'John Agent', 'agent', 0.025)
                )
                print("   ✅ Agent user created")
            
            # Get John's ID for upline reference
            cursor.execute("SELECT id FROM users WHERE email = 'agent@example.com'")
            john_result = cursor.fetchone()
            john_id = john_result[0] if john_result else None
            
            if 'erwin@yahoo.com' not in existing_emails and john_id:
                print("➕ Creating Erwin user (John's downline)...")
                from werkzeug.security import generate_password_hash
                erwin_password = generate_password_hash('erwin123')
                cursor.execute(
                    '''INSERT INTO users (email, password, name, role, upline_id, 
                                         agent_commission_rate, upline_commission_rate) 
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    ('erwin@yahoo.com', erwin_password, 'Erwin', 'agent', 
                     john_id, 0.025, 0.20)
                )
                print("   ✅ Erwin user created as John's downline")
            
            conn.commit()
            print("✅ Sample users created successfully")
            
        except Exception as e:
            print(f" User creation warning: {e}")
            conn.rollback()
        
        # Create uploads folder if it doesn't exist
        if not os.path.exists('uploads'):
            os.makedirs('uploads')
            print("✅ Uploads folder created")
        
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
        traceback.print_exc()
        raise e
        
    finally:
        if conn:
            conn.close()

def calculate_commission_for_listing(listing_id):
    """Calculate commission for a specific listing"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    try:
        # Get listing with agent and upline info
        cursor.execute('''
            SELECT pl.sale_price, u.name as agent_name, u.agent_commission_rate,
                   u.upline_id, upline.name as upline_name, u.upline_commission_rate
            FROM property_listings pl
            JOIN users u ON pl.agent_id = u.id
            LEFT JOIN users upline ON u.upline_id = upline.id
            WHERE pl.id = ?
        ''', (listing_id,))
        
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
                "net_commission": float(agent_net)
            },
            "upline": {
                "name": upline_name if upline_id else None,
                "rate": f"{upline_rate * 100}%" if upline_id else "0%",
                "commission": float(upline_commission)
            } if upline_id else None
        }
        
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()

def get_agent_commission_summary(agent_id):
    """Get commission summary for an agent"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    try:
        # Get agent's own commissions
        cursor.execute('''
            SELECT COUNT(*) as total_sales,
                   SUM(sale_price) as total_sales_value,
                   SUM(agent_net_commission) as total_net_commission,
                   SUM(upline_commission) as total_upline_commission
            FROM commission_distributions
            WHERE agent_id = ? AND payment_status = 'paid'
        ''', (agent_id,))
        
        agent_stats = cursor.fetchone()
        
        # Get upline commissions (commissions from downlines)
        cursor.execute('''
            SELECT COUNT(*) as downline_sales,
                   SUM(upline_commission) as total_upline_earnings
            FROM commission_distributions
            WHERE upline_id = ? AND payment_status = 'paid'
        ''', (agent_id,))
        
        upline_stats = cursor.fetchone()
        
        return {
            "agent_id": agent_id,
            "own_sales": {
                "count": agent_stats[0] or 0,
                "total_value": float(agent_stats[1] or 0),
                "net_commission": float(agent_stats[2] or 0),
                "upline_paid": float(agent_stats[3] or 0)
            },
            "upline_earnings": {
                "downline_sales_count": upline_stats[0] or 0,
                "total_earnings": float(upline_stats[1] or 0)
            }
        }
        
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()

def cleanup_tier_data():
    """Clean up tier-related data from the database"""
    conn = sqlite3.connect('real_estate.db')
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
                    if 'tier_multiplier' in details:
                        del details['tier_multiplier']
                    if 'agent_tier' in details:
                        del details['agent_tier']
                    
                    # Update the record
                    cursor.execute('''
                        UPDATE commission_calculations 
                        SET calculation_details = ?
                        WHERE id = ?
                    ''', (json.dumps(details), calc_id))
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
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    try:
        # Check if project_id column exists in property_listings
        cursor.execute("PRAGMA table_info(property_listings)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'project_id' not in columns:
            print("🔄 Adding project_id column to property_listings table...")
            cursor.execute('ALTER TABLE property_listings ADD COLUMN project_id INTEGER NULL')
            conn.commit()
            print("✅ project_id column added!")
        
        if 'unit_id' not in columns:
            print("🔄 Adding unit_id column to property_listings table...")
            cursor.execute('ALTER TABLE property_listings ADD COLUMN unit_id INTEGER NULL')
            conn.commit()
            print("✅ unit_id column added!")
        
        # Check if upline columns exist in users table
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'upline_id' not in columns:
            print("🔄 Adding upline_id column to users table...")
            cursor.execute('ALTER TABLE users ADD COLUMN upline_id INTEGER NULL')
            conn.commit()
            print("✅ upline_id column added!")
        
        if 'upline_commission_rate' not in columns:
            print("🔄 Adding upline_commission_rate column to users table...")
            cursor.execute('ALTER TABLE users ADD COLUMN upline_commission_rate DECIMAL(5,2) DEFAULT 0.00')
            conn.commit()
            print("✅ upline_commission_rate column added!")
        
        # ============ REMOVE TIER SYSTEM ============
        # Remove agent_tier from users table
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'agent_tier' in columns:
            print("🔄 Removing agent_tier column from users table...")
            
            # Create temporary table without agent_tier
            cursor.execute('''
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
            ''')
            
            # Copy data (excluding agent_tier)
            cursor.execute('''
                INSERT INTO users_new (id, email, password, name, role, upline_id, upline_commission_rate, created_at)
                SELECT id, email, password, name, role, upline_id, upline_commission_rate, created_at
                FROM users
            ''')
            
            # Drop old table and rename new one
            cursor.execute('DROP TABLE users')
            cursor.execute('ALTER TABLE users_new RENAME TO users')
            
            print("✅ agent_tier column removed from users table!")
        
        # Update commission_calculations table to remove tier columns
        cursor.execute("PRAGMA table_info(commission_calculations)")
        columns = [col[1] for col in cursor.fetchall()]
        
        # Check if agent_tier column exists
        if 'agent_tier' in columns:
            print("🔄 Removing tier columns from commission_calculations table...")
            
            # Create temporary table without tier columns
            cursor.execute('''
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
            ''')
            
            # Copy data (excluding tier columns)
            cursor.execute('''
                INSERT INTO commission_calculations_new 
                (id, listing_id, agent_id, property_type, sale_price, base_rate, commission, calculation_details, calculated_at)
                SELECT id, listing_id, agent_id, property_type, sale_price, base_rate, commission, calculation_details, calculated_at
                FROM commission_calculations
            ''')
            
            # Drop old table and rename new one
            cursor.execute('DROP TABLE commission_calculations')
            cursor.execute('ALTER TABLE commission_calculations_new RENAME TO commission_calculations')
            
            print("✅ Tier columns removed from commission_calculations!")
        
        # Remove tier_multiplier column if it exists
        cursor.execute("PRAGMA table_info(commission_calculations)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'tier_multiplier' in columns:
            print("🔄 Removing tier_multiplier column from commission_calculations...")
            
            # Create another temporary table without tier_multiplier
            cursor.execute('''
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
            ''')
            
            # Copy data
            cursor.execute('''
                INSERT INTO commission_calculations_final 
                (id, listing_id, agent_id, property_type, sale_price, base_rate, commission, calculation_details, calculated_at)
                SELECT id, listing_id, agent_id, property_type, sale_price, base_rate, commission, calculation_details, calculated_at
                FROM commission_calculations
            ''')
            
            # Drop and rename
            cursor.execute('DROP TABLE commission_calculations')
            cursor.execute('ALTER TABLE commission_calculations_final RENAME TO commission_calculations')
            
            print("✅ tier_multiplier column removed!")
        
        # Drop project_commissions table (tier-specific commissions)
        cursor.execute("DROP TABLE IF EXISTS project_commissions")
        print("✅ project_commissions table removed!")
        
        # ============ REMOVE PROPERTY TYPE SYSTEM ============
        # Remove property_type from property_listings table
        cursor.execute("PRAGMA table_info(property_listings)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'property_type' in columns:
            print("🔄 Removing property_type column from property_listings table...")
            
            # Create temporary table without property_type
            cursor.execute('''
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
            ''')
            
            # Copy data (excluding property_type)
            cursor.execute('''
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
            ''')
            
            # Drop old table and rename new one
            cursor.execute('DROP TABLE property_listings')
            cursor.execute('ALTER TABLE property_listings_temp RENAME TO property_listings')
            
            print("✅ property_type column removed from property_listings table!")
        
        # Remove property_type from commission_calculations table
        cursor.execute("PRAGMA table_info(commission_calculations)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'property_type' in columns:
            print("🔄 Removing property_type column from commission_calculations table...")
            
            # Create temporary table without property_type
            cursor.execute('''
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
            ''')
            
            # Copy data (excluding property_type)
            cursor.execute('''
                INSERT INTO commission_calculations_temp 
                (id, listing_id, agent_id, sale_price, base_rate, commission, calculation_details, calculated_at)
                SELECT id, listing_id, agent_id, sale_price, base_rate, commission, calculation_details, calculated_at
                FROM commission_calculations
            ''')
            
            # Drop old table and rename new one
            cursor.execute('DROP TABLE commission_calculations')
            cursor.execute('ALTER TABLE commission_calculations_temp RENAME TO commission_calculations')
            
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
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_notifications'")
        if not cursor.fetchone():
            print("🔄 Creating agent_notifications table...")
            cursor.execute('''
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
            ''')
            
            # Add index for faster queries
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_notifications_agent ON agent_notifications(agent_id, is_read)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_notifications_expires ON agent_notifications(expires_at)')
            
            print("✅ agent_notifications table created!")
        else:
            print("✅ agent_notifications table already exists")
            
        conn.commit()
        
    except Exception as e:
        print(f"❌ Error creating notifications table: {e}")
        conn.rollback()
    
    # ============ CREATE EMAIL LOGS TABLE ============
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='email_logs'")
        if not cursor.fetchone():
            print("🔄 Creating email_logs table...")
            cursor.execute('''
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
            ''')
            print("✅ email_logs table created!")
            
        conn.commit()
        
    except Exception as e:
        print(f"❌ Error creating email_logs table: {e}")
        conn.rollback()
    
    # ============ CREATE PAYMENT VOUCHERS TABLE ============
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payment_vouchers'")
        if not cursor.fetchone():
            print("🔄 Creating payment_vouchers table...")
            cursor.execute('''
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
            ''')
            print("✅ payment_vouchers table created!")
            
        conn.commit()
        
    except Exception as e:
        print(f"❌ Error creating payment_vouchers table: {e}")
        conn.rollback()
    
    # ============ CREATE SYSTEM SETTINGS TABLE ============
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='system_settings'")
        if not cursor.fetchone():
            print("🔄 Creating system_settings table...")
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS system_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    setting_type TEXT NOT NULL,
                    setting_key TEXT NOT NULL,
                    setting_value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(setting_type, setting_key)
                )
            ''')
            print("✅ system_settings table created!")
            
            # Insert default settings
            default_settings = [
                ('payment', 'processing_days', '14'),
                ('payment', 'min_payout', '100'),
                ('payment', 'payout_schedule', 'monthly'),
                ('payment', 'auto_generate_voucher', 'yes'),
                ('payment', 'voucher_template', 'detailed'),
                ('payment', 'voucher_prefix', 'PAY'),
                ('payment', 'payment_methods', 'bank_transfer,check'),
                ('notification', 'notifications', 'submission_received,submission_approved,payment_processed,reminders'),
                ('notification', 'auto_approve_threshold', '0'),
                ('notification', 'reminder_days', '3'),
                ('notification', 'admin_email', 'admin@example.com'),
                ('notification', 'system_from_email', 'noreply@realestate.com'),
                ('notification', 'smtp_server', ''),
                ('notification', 'smtp_port', ''),
                ('notification', 'smtp_username', ''),
                ('notification', 'smtp_password', ''),
                ('notification', 'email_footer', '© 2024 Real Estate System. All rights reserved.')
            ]
            
            for setting_type, setting_key, default_value in default_settings:
                cursor.execute('''
                    INSERT OR IGNORE INTO system_settings (setting_type, setting_key, setting_value)
                    VALUES (?, ?, ?)
                ''', (setting_type, setting_key, default_value))
            
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
LOGIN_TEMPLATE = '''
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
'''

AGENT_FORM_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>New {{ "Rental" if transaction_type == "rental" else "Sales" }} Entry</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }
        .container { max-width: 800px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 20px rgba(0,0,0,0.1); }
        h1 { color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }
        .agent-info { background: #e8f4ff; padding: 10px; border-radius: 5px; margin-bottom: 20px; }
        .commission-preview { background: #f0f8ff; padding: 15px; border-left: 4px solid #007bff; margin: 20px 0; border-radius: 5px; }
        .form-section { border: 1px solid #e0e0e0; padding: 20px; margin-bottom: 20px; border-radius: 8px; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; color: #555; }
        .required:after { content: " *"; color: red; }
        input, select, textarea { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box; }
        input:focus, select:focus, textarea:focus { border-color: #007bff; outline: none; box-shadow: 0 0 5px rgba(0,123,255,0.3); }
        .btn { padding: 12px 25px; border: none; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }
        .btn-primary { background: #007bff; color: white; }
        .btn-primary:hover { background: #0056b3; }
        .btn-secondary { background: #6c757d; color: white; }
        .btn-secondary:hover { background: #545b62; }
        .success { background: #d4edda; color: #155724; padding: 15px; border-radius: 5px; margin: 20px 0; }
        .file-upload { border: 2px dashed #ddd; padding: 20px; text-align: center; border-radius: 5px; }
        .file-upload:hover { border-color: #007bff; background: #f8f9fa; }
        .checklist { background: #e7f3ff; padding: 15px; border-radius: 5px; margin-top: 20px; }
        .project-info { background: #f0fdf4; padding: 15px; border-radius: 5px; margin: 15px 0; border-left: 4px solid #28a745; display: none; }
        .unit-info { background: #f8f9fa; padding: 10px; border-radius: 5px; margin-top: 10px; }
        .transaction-badge { 
            background-color: {% if transaction_type == "rental" %}#17a2b8{% else %}#28a745{% endif %}; 
            color: white; 
            padding: 5px 15px; 
            border-radius: 20px; 
            font-size: 0.9em; 
            display: inline-block;
            margin-left: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>
            {% if transaction_type == "rental" %}
                📝 New Rental Entry
            {% else %}
                📝 New Sales Entry
            {% endif %}
            <span class="transaction-badge">
                {% if transaction_type == "rental" %}
                    RENTAL
                {% else %}
                    SALES
                {% endif %}
            </span>
        </h1>
        
        <div class="agent-info">
            <strong>Agent:</strong> {{ agent_name }} | 
            <strong>ID:</strong> {{ agent_id }}
        </div>
        
        {% if success %}
        <div class="success">
            ✅ {% if transaction_type == "rental" %}Rental{% else %}Sale{% endif %} submitted successfully! Commission: RM{{ commission|default('0.00') }}
        </div>
        {% endif %}

        <div class="transaction-buttons" style="background: #f8f9fa; padding: 20px; border-radius: 10px; margin: 20px 0; text-align: center;">
            <h3 style="margin-top: 0; color: #495057;">📊 Select Transaction Type</h3>
            
            <div style="display: flex; gap: 20px; justify-content: center; margin: 20px 0;">
                <a href="/new-listing?type=sales" 
                   style="text-decoration: none; flex: 0 1 200px;">
                    <div style="padding: 20px; border-radius: 10px; 
                               background: {% if transaction_type == 'sales' %}#28a745{% else %}#ffffff{% endif %}; 
                               border: 3px solid {% if transaction_type == 'sales' %}#28a745{% else %}#dee2e6{% endif %}; 
                               color: {% if transaction_type == 'sales' %}#ffffff{% else %}#495057{% endif %}; 
                               transition: all 0.3s;">
                        <div style="font-size: 32px;">💰</div>
                        <div style="font-weight: bold; font-size: 18px; margin: 10px 0;">Sales</div>
                        <div style="font-size: 14px; opacity: 0.8;">Property Purchase</div>
                    </div>
                </a>
                
                <a href="/new-listing?type=rental" 
                   style="text-decoration: none; flex: 0 1 200px;">
                    <div style="padding: 20px; border-radius: 10px; 
                               background: {% if transaction_type == 'rental' %}#17a2b8{% else %}#ffffff{% endif %}; 
                               border: 3px solid {% if transaction_type == 'rental' %}#17a2b8{% else %}#dee2e6{% endif %}; 
                               color: {% if transaction_type == 'rental' %}#ffffff{% else %}#495057{% endif %}; 
                               transition: all 0.3s;">
                        <div style="font-size: 32px;">🏠</div>
                        <div style="font-weight: bold; font-size: 18px; margin: 10px 0;">Rental</div>
                        <div style="font-size: 14px; opacity: 0.8;">Property Rental</div>
                    </div>
                </a>
            </div>
            
            <div style="padding: 10px; background: #e8f4ff; border-radius: 5px; display: inline-block;">
                <strong>Currently viewing:</strong> 
                <span style="color: {% if transaction_type == 'sales' %}#28a745{% else %}#17a2b8{% endif %}; font-weight: bold;">
                    {{ 'Sales' if transaction_type == 'sales' else 'Rental' }} Projects
                </span>
                • {{ projects|length }} projects available
            </div>
        </div>
        
        <div class="commission-preview">
            <h3>💵 Estimated Commission: <span id="estCommission">RM0.00</span></h3>
            <p id="commissionBreakdown">
                {% if transaction_type == "rental" %}
                    Enter rental price to see calculation
                {% else %}
                    Enter sale price to see calculation
                {% endif %}
            </p>
            <div id="projectCommissionInfo" style="display: none; margin-top: 10px; padding: 10px; background: #e8f4ff; border-radius: 5px;">
                <strong>Project Commission:</strong> <span id="projectCommissionRate">0%</span>
            </div>
        </div>
        
        <form method="POST" action="/submit-listing" enctype="multipart/form-data">
            <input type="hidden" name="sale_type" value="{{ transaction_type }}">
            
            <!-- Project Selection -->
            <div class="form-section">
                <h2>🏢 Select Project</h2>
                <div class="form-group">
                    <label>Select Project (Optional)</label>
                    <select name="project_id" id="projectSelect" onchange="loadProjectDetails()">
                        <option value="">-- Select a Project --</option>
                        {% for project in projects %}
                        <option value="{{ project.id }}" 
                                data-category="{{ project.category }}" 
                                data-type="{{ project.project_type }}" 
                                data-commission="{{ project.commission_rate or '' }}"
                                data-sale-type="{{ project.project_sale_type if project.project_sale_type is defined else 'sales' }}">
                            {{ project.project_name }} ({{ project.category|title }} - {{ project.project_type|title }} - {{ (project.project_sale_type if project.project_sale_type is defined else 'sales')|title }})
                        </option>
                        {% endfor %}
                    </select>
                    <small>Selecting a project will auto-fill property type and commission rate</small>
                </div>
                
                <div id="projectDetails" class="project-info">
                    <h4 style="margin-top: 0;">📋 Selected Project Details</h4>
                    <div id="projectInfoContent"></div>
                </div>
                
                <div id="unitSelection" style="display: none;">
                    <div class="form-group">
                        <label>Select Unit Type</label>
                        <select name="unit_id" id="unitSelect" onchange="updateUnitDetails()">
                            <option value="">-- Select Unit Type --</option>
                        </select>
                    </div>
                    
                    <div id="unitDetails" class="unit-info">
                        <!-- Unit details will be populated here -->
                    </div>
                </div>
            </div>
            
            <!-- Customer Information -->
            <div class="form-section">
                <h2>👤 Customer Information</h2>
                <div class="form-group">
                    <label class="required">Customer Name</label>
                    <input type="text" name="customer_name" required>
                </div>
                <div class="form-group">
                    <label class="required">Customer Email</label>
                    <input type="email" name="customer_email" required>
                </div>
                <div class="form-group">
                    <label>Customer Phone</label>
                    <input type="tel" name="customer_phone">
                </div>
            </div>
            
            <!-- Property Details -->
            <div class="form-section">
                <h2>🏠 Property Details</h2>
                <div class="form-group">
                    <label class="required">Property Address</label>
                    <textarea name="property_address" id="propertyAddress" rows="3" required></textarea>
                </div>
                
                <div class="form-group">
                    <label class="required">
                        {% if transaction_type == "rental" %}
                            Monthly Rental Price (RM)
                        {% else %}
                            Sale Price (RM)
                        {% endif %}
                    </label>
                    <input type="number" name="sale_price" id="salePrice" 
                           min="0" step="1000" required 
                           oninput="updateCommission()" 
                           placeholder="{% if transaction_type == 'rental' %}e.g., 2500{% else %}e.g., 500000{% endif %}">
                </div>
                
                <div class="form-group">
                    <label>
                        {% if transaction_type == "rental" %}
                            Available From
                        {% else %}
                            Closing Date
                        {% endif %}
                    </label>
                    <input type="date" name="closing_date">
                </div>
                
                <!-- Rental Specific Fields -->
                {% if transaction_type == "rental" %}
                <div style="border-top: 1px solid #e0e0e0; padding-top: 15px; margin-top: 15px;">
                    <h4 style="color: #17a2b8;">🏠 Rental Specific Details</h4>
                    
                    <div class="form-group">
                        <label>Security Deposit (RM)</label>
                        <input type="number" name="deposit" min="0" step="100" placeholder="e.g., 2500 (usually 1-2 months rent)">
                    </div>
                    
                    <div class="form-group">
                        <label>Minimum Tenancy Period (months)</label>
                        <input type="number" name="minimum_tenancy" min="1" placeholder="e.g., 12 months">
                    </div>
                    
                    <div class="form-group">
                        <label>Furnishing Type</label>
                        <select name="furnishing_type">
                            <option value="">-- Select Furnishing --</option>
                            <option value="fully_furnished">Fully Furnished</option>
                            <option value="partially_furnished">Partially Furnished</option>
                            <option value="unfurnished">Unfurnished</option>
                        </select>
                    </div>
                </div>
                {% endif %}
            </div>
            
            <!-- Document Upload -->
            <div class="form-section">
                <h2>📎 Upload Documents</h2>
                <p style="color: #666; margin-bottom: 15px;">
                    Upload signed documents for verification. Maximum 10MB per file.
                </p>
                
                <div class="form-group">
                    <label>
                        {% if transaction_type == "rental" %}
                            Tenancy Agreement (Required)
                        {% else %}
                            Sales & Purchase Agreement (Required)
                        {% endif %}
                    </label>
                    <div class="file-upload">
                        <input type="file" name="agreement" accept=".pdf,.doc,.docx,.jpg,.jpeg,.png">
                        <small>
                            {% if transaction_type == "rental" %}
                                Signed tenancy agreement between landlord and tenant
                            {% else %}
                                Signed agreement between buyer and seller
                            {% endif %}
                        </small>
                    </div>
                </div>
                
                <div class="form-group">
                    <label>Customer ID Proof</label>
                    <div class="file-upload">
                        <input type="file" name="id_proof" accept=".pdf,.jpg,.jpeg,.png">
                        <small>NRIC/Passport copy of customer</small>
                    </div>
                </div>
                
                <div class="form-group">
                    <label>Property Documents</label>
                    <div class="file-upload">
                        <input type="file" name="property_docs" accept=".pdf,.doc,.docx">
                        <small>Title deed, floor plan, etc.</small>
                    </div>
                </div>
                
                <div class="form-group">
                    <label>Additional Documents</label>
                    <div class="file-upload">
                        <input type="file" name="additional_docs" multiple accept=".pdf,.doc,.docx,.jpg,.jpeg,.png">
                        <small>Hold CTRL to select multiple files</small>
                    </div>
                </div>
                
                <div class="checklist">
                    <h4 style="margin-top: 0;">📋 Document Checklist:</h4>
                    <ul style="margin-bottom: 0;">
                        {% if transaction_type == "rental" %}
                            <li>✅ Signed Tenancy Agreement</li>
                            <li>✅ Customer ID Proof</li>
                            <li>✅ Property Title/Deed</li>
                            <li>✅ Deposit Receipt</li>
                        {% else %}
                            <li>✅ Signed Sales & Purchase Agreement</li>
                            <li>✅ Customer ID Proof</li>
                            <li>✅ Property Title/Deed</li>
                            <li>✅ Commission Agreement (if separate)</li>
                        {% endif %}
                    </ul>
                </div>
            </div>
            
            <!-- Additional Information -->
            <div class="form-section">
                <h2>📋 Additional Information</h2>
                <div class="form-group">
                    <label>Special Notes</label>
                    <textarea name="notes" rows="4" 
                              placeholder="Any special conditions, requirements, or notes..."></textarea>
                </div>
            </div>
            
            <button type="submit" class="btn btn-primary">
                {% if transaction_type == "rental" %}
                    ✅ Submit Rental for Approval
                {% else %}
                    ✅ Submit Sale for Approval
                {% endif %}
            </button>
            <button type="button" class="btn btn-secondary" onclick="saveDraft()">💾 Save as Draft</button>
            <a href="/agent/dashboard" style="color: #6c757d; margin-left: 15px;">← Back to Dashboard</a>
        </form>
    </div>
    
    <script>
    // ============ SALES/RENTAL FILTERING ============
    function filterProjectsByType(selectedType) {
        console.log(`🎯 filterProjectsByType called with: ${selectedType}`);
        
        const projectSelect = document.getElementById('projectSelect');
        if (!projectSelect) {
            console.error('❌ projectSelect element not found!');
            return;
        }
        
        const options = projectSelect.options;
        console.log(`📋 Total options: ${options.length}`);
        
        // Show/hide options based on saleType
        let shownCount = 0;
        let hiddenCount = 0;
        
        for (let i = 0; i < options.length; i++) {
            const option = options[i];
            const saleType = option.dataset.saleType || 'sales'; // Default to sales
            
            console.log(`  Option ${i}: "${option.text.substring(0, 30)}..." - saleType: "${saleType}"`);
            
            if (i === 0) {
                // First option is "Select a Project" - always show
                option.style.display = '';
                option.disabled = false;
                shownCount++;
            } else if (selectedType === 'all' || saleType === selectedType) {
                option.style.display = '';
                option.disabled = false;
                shownCount++;
            } else {
                option.style.display = 'none';
                option.disabled = true;
                hiddenCount++;
            }
        }
        
        console.log(`✅ Result: ${shownCount} shown, ${hiddenCount} hidden`);
        
        // Reset selection if current selection is hidden
        const selectedOption = projectSelect.options[projectSelect.selectedIndex];
        if (selectedOption && selectedOption.disabled) {
            console.log('🔄 Resetting selection');
            projectSelect.selectedIndex = 0;
            
            // Update commission calculation
            if (typeof updateCommission === 'function') {
                updateCommission();
            }
        }
    }

    // Initialize when page loads
    document.addEventListener('DOMContentLoaded', function() {
        console.log('🚀 Page loaded, initializing filter...');
        
        // Set filter based on current transaction type
        const currentType = "{{ transaction_type }}";
        filterProjectsByType(currentType);
        
        // Update price label based on transaction type
        updatePriceLabel(currentType);
    });

    function updatePriceLabel(transactionType) {
        const priceInput = document.getElementById('salePrice');
        if (!priceInput) return;
        
        // Update placeholder based on transaction type
        if (transactionType === 'rental') {
            priceInput.placeholder = 'e.g., 2500 (monthly rent)';
        } else {
            priceInput.placeholder = 'e.g., 500000 (sale price)';
        }
    }

    // Store project data
    let projectsData = {{ projects_json|safe }};
    let unitsData = {};
    
    function loadProjectDetails() {
        const projectSelect = document.getElementById('projectSelect');
        const projectId = projectSelect.value;
        const projectDetails = document.getElementById('projectDetails');
        const unitSelection = document.getElementById('unitSelection');
        const projectInfoContent = document.getElementById('projectInfoContent');
        
        // Reset
        projectDetails.style.display = 'none';
        unitSelection.style.display = 'none';
        document.getElementById('unitSelect').innerHTML = '<option value="">-- Select Unit Type --</option>';
        document.getElementById('unitDetails').innerHTML = '';
        
        if (!projectId) return;
        
        // Find selected project
        const project = projectsData.find(p => p.id == projectId);
        if (!project) return;
        
        // Show project details
        projectDetails.style.display = 'block';
        projectInfoContent.innerHTML = `
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                <div>
                    <strong>Category:</strong> ${project.category.toUpperCase()}<br>
                    <strong>Type:</strong> ${project.project_type.toUpperCase()}<br>
                    <strong>Location:</strong> ${project.location || 'Not specified'}
                </div>
                <div>
                    <strong>Commission Rate:</strong> ${project.commission_rate || 'N/A'}%<br>
                    <strong>Status:</strong> ${project.status.toUpperCase()}<br>
                    ${project.description ? `<strong>Description:</strong> ${project.description.substring(0, 100)}...` : ''}
                </div>
            </div>
        `;
        
        // Auto-fill property address with project location if empty
        const propertyAddress = document.getElementById('propertyAddress');
        if (!propertyAddress.value.trim() && project.location) {
            propertyAddress.value = project.location;
        }
        
        // Show commission info
        if (project.commission_rate) {
            document.getElementById('projectCommissionInfo').style.display = 'block';
            document.getElementById('projectCommissionRate').textContent = project.commission_rate + '%';
        }
        
        // Load units if available
        if (project.units && project.units.length > 0) {
            unitSelection.style.display = 'block';
            const unitSelect = document.getElementById('unitSelect');
            unitSelect.innerHTML = '<option value="">-- Select Unit Type --</option>';
            
            project.units.forEach(unit => {
                const option = document.createElement('option');
                option.value = unit.id;
                // Use appropriate price based on transaction type
                const transactionType = "{{ transaction_type }}";
                const price = (transactionType === 'rental') ? 
                              (unit.rental_price || unit.base_price || 0) : 
                              (unit.base_price || unit.rental_price || 0);
                option.textContent = `${unit.unit_type} (${unit.square_feet || 'N/A'} sqft) - RM${price.toLocaleString()}`;
                option.setAttribute('data-price', price);
                option.setAttribute('data-sale-type', project.project_sale_type || 'sales');
                option.setAttribute('data-commission', unit.commission_rate || project.commission_rate || 0);
                option.setAttribute('data-sqft', unit.square_feet || '');
                unitSelect.appendChild(option);
            });
            
            // Store units data for this project
            unitsData[projectId] = project.units;
        }
        
        updateCommission();
    }
    
    function updateUnitDetails() {
        const unitSelect = document.getElementById('unitSelect');
        const unitId = unitSelect.value;
        const unitDetails = document.getElementById('unitDetails');
        const salePriceInput = document.getElementById('salePrice');
        
        if (!unitId) {
            unitDetails.innerHTML = '';
            return;
        }
        
        const projectId = document.getElementById('projectSelect').value;
        const units = unitsData[projectId];
        const unit = units.find(u => u.id == unitId);
        
        if (!unit) return;
        
        // Get appropriate price based on transaction type
        const transactionType = "{{ transaction_type }}";
        const price = (transactionType === 'rental') ? 
                      (unit.rental_price || unit.base_price || 0) : 
                      (unit.base_price || unit.rental_price || 0);
        const priceLabel = (transactionType === 'rental') ? 'Monthly Rent' : 'Price';
        
        unitDetails.innerHTML = `
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                <div>
                    <strong>Unit Type:</strong> ${unit.unit_type}<br>
                    <strong>Size:</strong> ${unit.square_feet || 'N/A'} sqft<br>
                    <strong>Status:</strong> ${unit.status.toUpperCase()}
                </div>
                <div>
                    <strong>${priceLabel}:</strong> RM${price.toLocaleString()}<br>
                    <strong>Commission:</strong> ${unit.commission_rate || projectsData.find(p => p.id == projectId).commission_rate || 'N/A'}%<br>
                    <strong>Quantity Available:</strong> ${unit.quantity || 0}
                </div>
            </div>
        `;
        
        // Auto-fill sale/rental price
        if (price > 0) {
            salePriceInput.value = price;
            updateCommission();
        }
    }
    
    function updateCommission() {
        const salePrice = parseFloat(document.getElementById('salePrice').value) || 0;
        const projectSelect = document.getElementById('projectSelect');
        const projectId = projectSelect.value;
        const selectedOption = projectSelect.options[projectSelect.selectedIndex];
        const unitSelect = document.getElementById('unitSelect');
        const unitId = unitSelect.value;
        
        // Get transaction type from template
        const transactionType = "{{ transaction_type }}";
        const isRental = transactionType === 'rental';

        let commissionRate = 0;

        // Check for unit-specific commission
        if (unitId && unitsData[projectId]) {
            const unit = unitsData[projectId].find(u => u.id == unitId);
            if (unit && unit.commission_rate) {
                commissionRate = unit.commission_rate / 100;
            }
        }

        // Check for project commission
        if (!commissionRate && selectedOption && selectedOption.dataset.commission) {
            commissionRate = parseFloat(selectedOption.dataset.commission) / 100;
        }

        // Use default rate if no project/unit commission
        if (!commissionRate) {
            commissionRate = 0.03; // Default 3% commission for sales
            // For rentals, typically 1 month rent commission
            if (isRental) {
                commissionRate = 1; // 1 month rent
            }
        }

        let commission = 0;
        let breakdown = '';
        
        if (isRental) {
            // For rentals: commission is typically 1 month's rent
            commission = salePrice; // 1 month rent as commission
            breakdown = `Rental: RM${salePrice.toLocaleString()} × 1 month = RM${commission.toLocaleString()}`;
        } else {
            // For sales: percentage of sale price
            commission = salePrice * commissionRate;
            
            // Apply caps (RM1,000 - RM50,000)
            commission = Math.max(1000, Math.min(commission, 50000));
            
            breakdown = `Sale Price: RM${salePrice.toLocaleString()} × ${(commissionRate*100).toFixed(1)}%`;
        }

        // Update display
        document.getElementById('estCommission').textContent = 
            'RM' + commission.toLocaleString('en-US', {minimumFractionDigits: 2});

        if (projectId) {
            breakdown += ` (Project Rate)`;
        } else {
            breakdown += ` (Default Rate)`;
        }

        document.getElementById('commissionBreakdown').innerHTML = breakdown;
    }
    
    function saveDraft() {
        alert('Draft saved! You can continue later.');
        // In real implementation, save via AJAX
    }
    </script>
</body>
</html>
'''

DASHBOARD_TEMPLATE = '''<!DOCTYPE html>
<html>
<head>
    <title>Agent Dashboard</title>
    <style>
        /* ============ EXISTING STYLES ============ */
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .stats { display: flex; gap: 12px; margin: 15px 0; flex-wrap: wrap; }
        .stat-card { background: white; padding: 12px; border-radius: 8px; flex: 1; min-width: 140px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .stat-card h3 { margin-top: 0; color: #555; font-size: 13px; margin-bottom: 8px; }
        .stat-value { font-size: 1.5em; font-weight: bold; margin-bottom: 5px; }
        .stat-card small { font-size: 11px; color: #666; line-height: 1.3; }
        .actions { margin: 30px 0; }
        .btn { display: inline-block; padding: 12px 25px; background: #007bff; color: white; text-decoration: none; border-radius: 5px; margin-right: 10px; }
        .btn:hover { background: #0056b3; }
        table { width: 100%; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f8f9fa; font-weight: bold; color: #495057; }
        .status-draft { background: #fff3cd; color: #856404; padding: 3px 8px; border-radius: 3px; }
        .status-submitted { background: #cce5ff; color: #004085; padding: 3px 8px; border-radius: 3px; }
        .status-approved { background: #d4edda; color: #155724; padding: 3px 8px; border-radius: 3px; }
        .project-badge { background: #e8f4ff; color: #0066cc; padding: 3px 8px; border-radius: 3px; font-size: 12px; margin-top: 3px; display: inline-block; }
        .unit-badge { background: #f0f8ff; color: #004d99; padding: 2px 6px; border-radius: 3px; font-size: 11px; margin-left: 5px; }
        
        /* ============ UPLINE EARNINGS STYLE ============ */
        .upline-earnings-card {
            background: linear-gradient(135deg, #d4edda, #c3e6cb);
            border-left: 4px solid #28a745;
            padding: 12px !important; /* Force same padding */
        }
        
        .upline-earnings-card .stat-value {
            color: #155724;
            font-size: 1.5em !important; /* Force same font size */
        }
        
        .upline-earnings-card h3 {
            font-size: 13px !important; /* Force same header size */
        }
        
        .upline-earnings-card small {
            font-size: 11px !important; /* Force same small text size */
        }
        
        /* ============ NETWORK STYLES ============ */
        .network-section {
            background: white;
            padding: 20px;
            border-radius: 10px;
            margin: 20px 0;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        
        .network-section h2 {
            margin-top: 0;
            color: #333;
            border-bottom: 2px solid #007bff;
            padding-bottom: 10px;
            margin-bottom: 20px;
        }
        
        .network-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-top: 15px;
        }
        
        @media (max-width: 768px) {
            .network-grid {
                grid-template-columns: 1fr;
            }
        }
        
        .network-card {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            border: 1px solid #e0e0e0;
            height: 100%;
        }
        
        .network-card h3 {
            margin-top: 0;
            color: #495057;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .network-member {
            display: flex;
            align-items: center;
            padding: 12px;
            background: white;
            border-radius: 8px;
            margin-bottom: 10px;
            border: 1px solid #e0e0e0;
            transition: transform 0.2s ease;
        }
        
        .network-member:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
            border-color: #007bff;
        }
        
        .member-icon {
            font-size: 24px;
            margin-right: 15px;
            width: 40px;
            height: 40px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            background: #e9ecef;
        }
        
        .member-details {
            flex: 1;
        }
        
        .member-details strong {
            display: block;
            color: #333;
            margin-bottom: 4px;
        }
        
        .member-meta {
            font-size: 13px;
            color: #666;
        }
        
        .commission-badge {
            background: #fff3cd;
            color: #856404;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: bold;
            display: inline-block;
            margin-top: 5px;
        }
        
        .network-stats {
            margin-top: 15px;
            padding: 8px;
            background: #e8f4ff;
            border-radius: 5px;
            font-size: 11px;
            color: #004085;
            line-height: 1.4;
        }
        
        .network-stats small {
            font-size: 10px;
        }
        
        .network-stats strong {
            display: block;
            margin-bottom: 5px;
        }
        
        .empty-network {
            padding: 30px;
            text-align: center;
            color: #666;
        }
        
        .empty-network .icon {
            font-size: 48px;
            margin-bottom: 10px;
            display: block;
        }
        
        .empty-network h4 {
            margin: 10px 0;
            color: #495057;
        }
        
        .commission-flow {
            background: #f0f9ff;
            padding: 15px;
            border-radius: 8px;
            margin: 15px 0;
            border-left: 4px solid #007bff;
        }
        
        .commission-flow h4 {
            margin-top: 0;
            color: #004085;
        }
        
        .commission-flow p {
            margin-bottom: 10px;
            font-size: 14px;
        }
        
        /* Add to existing .btn styles */
        .btn-network {
            background: #fd7e14;
            color: white;
        }
        
        .btn-network:hover {
            background: #e96c00;
        }
        
        /* ============ PAYMENT TYPE BADGES ============ */
        .payment-type-badge {
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: bold;
            display: inline-block;
        }
        
        .payment-type-own {
            background: #d4edda;
            color: #155724;
        }
        
        .payment-type-upline {
            background: #cce5ff;
            color: #004085;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>📊 Agent Dashboard</h1>
        <p>Welcome back, {{ user_name }}!
            {% if unread_count > 0 %}
            <span style="background: #dc3545; color: white; padding: 2px 8px; border-radius: 12px; font-size: 12px; margin-left: 10px;">
                {{ unread_count }} new notification{% if unread_count > 1 %}s{% endif %}
            </span>
            {% endif %}
        </p>
        <div>
            <a href="/agent/notifications" class="btn" style="background: #ffc107; color: #000;">
                🔔 Notifications {% if unread_count > 0 %}({{ unread_count }}){% endif %}
            </a>
            <a href="/logout" style="color: #dc3545; margin-left: 10px;">Logout</a>
        </div>
    </div>
    
    <!-- ============ NOTIFICATION SECTION ============ -->
    {% if unread_count > 0 or incomplete_submissions %}
    <div class="notification-section">
        <div class="notification-header">
            <h2 style="margin: 0;">🔔 Notifications & Pending Tasks ({{ unread_count + incomplete_submissions|length }})</h2>
            {% if unread_count > 0 %}
            <a href="/agent/mark-all-read" class="btn" style="background: #6c757d; color: white; padding: 5px 10px; font-size: 12px;">
                Mark All as Read
            </a>
            {% endif %}
        </div>
    
        <!-- Notifications -->
        {% if notifications and notifications|length > 0 %}
        <div class="notifications-list">
            {% for notif in notifications %}
            <div class="notification-item priority-{{ notif.priority }}">
                <div class="notification-icon">
                    {% if notif.type == 'incomplete_docs' %}📎
                    {% elif notif.type == 'rejected_submission' %}❌
                    {% elif notif.priority == 'urgent' %}🚨
                    {% elif notif.priority == 'high' %}
                    {% else %}📌
                    {% endif %}
                </div>
                <div class="notification-content">
                    <div class="notification-title">
                        <strong>{{ notif.title }}</strong>
                        <span class="notification-time">{{ notif.created_at[:10] }}</span>
                    </div>
                    <p class="notification-message">{{ notif.message }}</p>
                    {% if notif.related_id and notif.related_type == 'listing' %}
                    <div class="notification-actions">
                        <a href="/agent/submission/{{ notif.related_id }}" class="btn-small">View Submission</a>
                        <a href="/agent/reupload-documents/{{ notif.related_id }}" class="btn-small" style="background: #28a745;">Upload Documents</a>
                        <a href="/agent/mark-notification-read/{{ notif.id }}" class="btn-small" style="background: #6c757d;">Mark as Read</a>
                    </div>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}
    
        <!-- Incomplete Submissions -->
        {% if incomplete_submissions %}
        <div class="pending-tasks">
            <h3 style="margin-top: 20px;">📋 Incomplete Submissions ({{ incomplete_submissions|length }})</h3>
            <div class="incomplete-list">
                {% for sub in incomplete_submissions %}
                <div class="incomplete-item doc-{{ sub.doc_status }}">
                    <div class="incomplete-icon">
                        {% if sub.doc_status == 'critical' %}❌
                        {% elif sub.doc_status == 'warning' %}
                        {% else %}📝
                        {% endif %}
                    </div>
                    <div class="incomplete-details">
                        <strong>Submission #{{ sub.id }}</strong>
                        <p>{{ sub.customer_name }} • Created: {{ sub.created_date }}</p>
                        <div class="doc-status">
                            <span class="doc-count">{{ sub.doc_count }}/3 documents</span>
                            {% if sub.doc_count == 0 %}
                            <span class="status-badge critical">CRITICAL: No documents</span>
                            {% elif sub.doc_count == 1 %}
                            <span class="status-badge warning">Very Incomplete</span>
                            {% else %}
                            <span class="status-badge info">Missing documents</span>
                            {% endif %}
                        </div>
                    </div>
                    <div class="incomplete-actions">
                        <a href="/agent/reupload-documents/{{ sub.id }}" class="btn-small" style="background: #28a745;">Upload Now</a>
                        <a href="/agent/submission/{{ sub.id }}" class="btn-small">View Details</a>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>
        {% endif %}
    
        {% if unread_count == 0 and not incomplete_submissions %}
        <div class="no-notifications">
            <div class="no-notifications-icon">🎉</div>
            <h3>All caught up!</h3>
            <p>You have no pending tasks or notifications.</p>
        </div>
        {% endif %}
    </div>

    <style>
    .notification-section {
        background: white;
        padding: 20px;
        border-radius: 10px;
        margin: 20px 0;
        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        border-left: 4px solid #ffc107;
    }

   .notification-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 15px;
        padding-bottom: 10px;
        border-bottom: 1px solid #eee;
    }

    .notifications-list {
        max-height: 300px;
        overflow-y: auto;
    }

    .notification-item {
        display: flex;
        align-items: flex-start;
        padding: 15px;
        margin-bottom: 10px;
        border-radius: 8px;
        border: 1px solid #e0e0e0;
    }

    .priority-urgent {
        background: #fff5f5;
        border-color: #f5c6cb;
        border-left: 4px solid #dc3545;
    }

    .priority-high {
        background: #fff3cd;
        border-color: #ffeaa7;
        border-left: 4px solid #ffc107;
    }

    .priority-normal {
        background: #f8f9fa;
        border-left: 4px solid #17a2b8;
    }

    .notification-icon {
        font-size: 24px;
        margin-right: 15px;
        min-width: 30px;
    }

    .notification-content {
        flex: 1;
    }

    .notification-title {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 8px;
    }

    .notification-time {
        font-size: 12px;
        color: #666;
    }

    .notification-message {
       margin: 5px 0 10px 0;
        color: #333;
    }

    .notification-actions {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
   }

    .btn-small {
        padding: 4px 8px;
        font-size: 12px;
        border-radius: 4px;
        text-decoration: none;
        background: #007bff;
        color: white;
    }

    .pending-tasks {
      margin-top: 20px;
      padding-top: 20px;
      border-top: 1px solid #eee;
   }

   .incomplete-list {
        margin-top: 10px;
    }

    .incomplete-item {
        display: flex;
        align-items: center;
        padding: 12px;
        margin-bottom: 8px;
        border-radius: 6px;
        background: #f8f9fa;
        border: 1px solid #e0e0e0;
    }

    .doc-critical {
        background: #fff5f5;
        border-color: #f5c6cb;
    }

    .doc-warning {
        background: #fff3cd;
        border-color: #ffeaa7;
    }

    .incomplete-icon {
        font-size: 20px;
        margin-right: 15px;
        min-width: 30px;
    }

    .incomplete-details {
        flex: 1;
    }

    .incomplete-details strong {
        display: block;
        margin-bottom: 5px;
    }

    .incomplete-details p {
        margin: 0;
        font-size: 13px;
        color: #666;
    }

    .doc-status {
        margin-top: 5px;
        display: flex;
        align-items: center;
        gap: 10px;
    }

    .doc-count {
        font-size: 12px;
        color: #666;
    }

    .status-badge {
        padding: 2px 8px;
        border-radius: 12px;
        font-size: 11px;
        font-weight: bold;
    }

    .status-badge.critical {
        background: #dc3545;
        color: white;
   }

    .status-badge.warning {
        background: #ffc107;
        color: #000;
    }

    .status-badge.info {
        background: #17a2b8;
        color: white;
    }

    .incomplete-actions {
        display: flex;
        gap: 8px;
    }

    .no-notifications {
        text-align: center;
        padding: 30px;
        color: #666;
    }

    .no-notifications-icon {
        font-size: 48px;
        margin-bottom: 10px;
    }
    </style>
    {% endif %}

    <!-- ============ STATS SECTION WITH UPLINE EARNINGS ============ -->
    <div class="stats">
        <div class="stat-card">
            <h3>Total Commission</h3>
            <div class="stat-value" style="color: #007bff;">RM{{ total_commission }}</div>
            <small>From your own sales</small>
        </div>
        
        <!-- ============ NEW: UPLINE EARNINGS CARD ============ -->
        <div class="stat-card upline-earnings-card">
            <h3>📈 Upline Earnings</h3>
            <div class="stat-value" style="color: #155724;">RM{{ "{:,.2f}".format(upline_earnings or 0) }}</div>
            <small>
                From {{ downline_stats.upline_payments_count or 0 }} downline sale{% if downline_stats.upline_payments_count != 1 %}s{% endif %}
                {% if downline_stats.count > 0 %}
                <br>Across {{ downline_stats.count }} downline agent{% if downline_stats.count != 1 %}s{% endif %}
                {% endif %}
            </small>
        </div>
        
        <div class="stat-card">
            <h3>Pending Approval</h3>
            <div class="stat-value" style="color: #ffc107;">{{ pending_count }}</div>
        </div>
        
        <div class="stat-card">
            <h3>Total Sales</h3>
            <div class="stat-value" style="color: #6f42c1;">{{ total_sales }}</div>
        </div>
        
        <div class="stat-card">
            <h3>Project Sales</h3>
            <div class="stat-value" style="color: #fd7e14;">{{ project_sales_count }}</div>
            <small>From {{ unique_projects_count }} projects</small>
        </div>
        
        <div class="stat-card">
            <h3>Paid Out</h3>
            <div class="stat-value" style="color: #28a745;">RM{{ "{:,.2f}".format(total_paid or 0) }}</div>
            <small>Total commissions paid</small>
        </div>
    </div>
    
    <div class="actions">
        <a href="/new-listing" class="btn">➕ New Sales Entry</a>
        <a href="/agent/submissions" class="btn" style="background: #28a745;">📋 My Submissions</a>
        <a href="/agent/commissions" class="btn" style="background: #17a2b8;">💰 Commissions</a>
        <a href="/agent/projects" class="btn" style="background: #6f42c1;">🏢 My Projects</a>
        <a href="/agent/notifications" class="btn" style="background: #ffc107; color: #000;">
        🔔 Notifications {% if unread_count > 0 %}<span style="background: #dc3545; color: white; padding: 2px 6px; border-radius: 10px; font-size: 11px;">{{ unread_count }} new</span>{% endif %}
        </a>
        <a href="/agent/my-downline" class="btn btn-network">👥 My Downline</a>
    </div>
    
    <!-- ============ NETWORK SECTION ============ -->
    <div class="network-section">
        <h2>👥 My Network</h2>
        
        <div class="commission-flow">
            <h4>💰 How Commissions Work in Your Network</h4>
            <p>• <strong>Your Sales:</strong> You earn commission from your own property sales</p>
            <p>• <strong>Upline:</strong> Agent who supervises you (earns {{ upline_info.commission_rate if upline_info else 0 }}% of your commission)</p>
            <p>• <strong>Downline:</strong> Agents you supervise (you earn commission from their sales)</p>
            <p>• <strong>Your Upline Earnings:</strong> You have earned <strong>RM{{ "{:,.2f}".format(upline_earnings or 0) }}</strong> from your downline network</p>
        </div>
        
        <div class="network-grid">
            <!-- UPLINE CARD -->
            <div class="network-card">
                <h3><span style="font-size: 20px;">⬆️</span> My Upline</h3>
                
                {% if upline_info %}
                <div class="network-member">
                    <div class="member-icon">👑</div>
                    <div class="member-details">
                        <strong>{{ upline_info.name }}</strong>
                        <div class="member-meta">
                            <small>{{ upline_info.email }}</small>
                            <div style="margin-top: 8px;">
                                <span class="commission-badge">
                                    Earns {{ upline_info.commission_rate }}% of your commissions
                                </span>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="network-stats">
                    <strong>📊 Commission Flow:</strong>
                    <small>{{ upline_info.commission_rate }}% of your approved commissions goes to your upline</small>
                </div>
                {% else %}
                <div class="empty-network">
                    <span class="icon">👑</span>
                    <h4>Top Level Agent</h4>
                    <p>You don't have an upline assigned</p>
                    <small>You keep 100% of your commissions</small>
                </div>
                {% endif %}
            </div>
            
            <!-- DOWNLINE CARD -->
            <div class="network-card">
                <h3><span style="font-size: 20px;">⬇️</span> My Downline ({{ downline_stats.count }})</h3>
                
                {% if downline_agents %}
                <div style="max-height: 200px; overflow-y: auto; margin-bottom: 15px;">
                    {% for agent in downline_agents %}
                    <div class="network-member">
                        <div class="member-icon">👤</div>
                        <div class="member-details">
                            <strong>{{ agent.name }}</strong>
                            <div class="member-meta">
                                <small>{{ agent.email }}</small>
                                <div style="margin-top: 5px;">
                                    <span class="commission-badge">
                                        Earn {{ agent.commission_rate }}% of their commissions
                                    </span>
                                </div>
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
                
                <div class="network-stats">
                    <strong>📊 Downline Performance:</strong>
                    <small>{{ downline_stats.count }} agent(s) under your supervision</small><br>
                    <small>You have earned <strong>RM{{ "{:,.2f}".format(upline_earnings or 0) }}</strong> from downline</small><br>
                    <small>Average rate: {{ "{:.1f}".format(downline_stats.total_commission_rate / downline_stats.count if downline_stats.count > 0 else 0) }}% per agent</small>
                </div>
                {% else %}
                <div class="empty-network">
                    <span class="icon">👥</span>
                    <h4>No Downline Yet</h4>
                    <p>You don't have any agents under your supervision</p>
                    <small>Build your team to earn passive income!</small>
                </div>
                {% endif %}
            </div>
        </div>
    </div>

        <!-- ============ INCOMPLETE SUBMISSIONS SECTION ============ -->
    {% if incomplete_submissions %}
    <div class="incomplete-section">
        <h2>📋 Incomplete Submissions ({{ incomplete_submissions|length }})</h2>
        <p style="color: #666; margin-bottom: 15px;">
            These submissions are missing documents. Upload documents to submit for approval.
        </p>
        
        <div class="incomplete-list">
            {% for sub in incomplete_submissions %}
            <div class="incomplete-item">
                <div class="incomplete-header">
                    <span class="incomplete-icon">
                        {% if sub.doc_count == 0 %}🚨{% elif sub.doc_count == 1 %}{% else %}📝{% endif %}
                    </span>
                    <div>
                        <strong>Submission #{{ sub.id }}</strong>
                        <div class="incomplete-meta">
                            Customer: {{ sub.customer_name }} | 
                            Status: {{ sub.status|title }} | 
                            Created: {{ sub.created_at[:10] }}
                        </div>
                    </div>
                </div>
                
                <div class="incomplete-details">
                    <div class="doc-status">
                        <span class="doc-count">{{ sub.doc_count }}/3 documents</span>
                        {% if sub.doc_count == 0 %}
                        <span class="status-badge critical">CRITICAL: No documents</span>
                        {% elif sub.doc_count == 1 %}
                        <span class="status-badge warning">Very Incomplete</span>
                        {% else %}
                        <span class="status-badge info">Missing documents</span>
                        {% endif %}
                    </div>
                    
                    <div class="incomplete-actions">
                        <a href="/agent/reupload-documents/{{ sub.id }}" class="btn-small" style="background: #28a745;">Upload Documents</a>
                        <a href="/agent/submission/{{ sub.id }}" class="btn-small">View Details</a>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        
        <div style="margin-top: 20px; text-align: center;">
            <a href="/agent/submissions?status=draft" class="btn" style="background: #17a2b8;">
                View All Incomplete Submissions →
            </a>
        </div>
    </div>
    
    <style>
    .incomplete-section {
        background: white;
        padding: 20px;
        border-radius: 10px;
        margin: 20px 0;
        box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        border-left: 4px solid #ffc107;
    }
    
    .incomplete-list {
        margin-top: 15px;
    }
    
    .incomplete-item {
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 15px;
        margin-bottom: 10px;
        background: #f8f9fa;
    }
    
    .incomplete-header {
        display: flex;
        align-items: center;
        margin-bottom: 10px;
    }
    
    .incomplete-icon {
        font-size: 24px;
        margin-right: 15px;
        width: 40px;
        text-align: center;
    }
    
    .incomplete-meta {
        font-size: 13px;
        color: #666;
        margin-top: 5px;
    }
    
    .incomplete-details {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding-top: 10px;
        border-top: 1px solid #e0e0e0;
    }
    
    .doc-status {
        display: flex;
        align-items: center;
        gap: 10px;
    }
    
    .doc-count {
        font-weight: bold;
        font-size: 14px;
    }
    
    .status-badge {
        padding: 4px 8px;
        border-radius: 12px;
        font-size: 11px;
        font-weight: bold;
    }
    
    .status-badge.critical {
        background: #dc3545;
        color: white;
    }
    
    .status-badge.warning {
        background: #ffc107;
        color: #000;
    }
    
    .status-badge.info {
        background: #17a2b8;
        color: white;
    }
    
    .incomplete-actions {
        display: flex;
        gap: 8px;
    }
    
    .btn-small {
        padding: 5px 10px;
        font-size: 12px;
        border-radius: 4px;
        text-decoration: none;
        background: #007bff;
        color: white;
    }
    </style>
    {% endif %}
    
    <h2>Recent Sales</h2>
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Customer</th>
                <th>Sale Price</th>
                <th>Commission</th>
                <th>Project</th>
                <th>Status</th>
                <th>Date</th>
            </tr>
        </thead>
        <tbody>
            {% for sale in recent_sales %}
            <tr>
                <td>#{{ sale.id }}</td>
                <td>
                    {{ sale.customer_name }}
                    {% if sale.unit_type %}
                    <br><small class="unit-badge">Unit: {{ sale.unit_type }}</small>
                    {% endif %}
                </td>
                <td>RM{{ "%.2f"|format(sale.sale_price|float) }}</td>
                <td>RM{{ "%.2f"|format(sale.commission_amount|float if sale.commission_amount else 0) }}</td>
                <td>
                    {% if sale.project_name %}
                    <span class="project-badge">{{ sale.project_name }}</span>
                    {% if sale.project_category %}
                    <br><small style="color: #666;">{{ sale.project_category|title }}</small>
                    {% endif %}
                    {% else %}
                    <span style="color: #999;">—</span>
                    {% endif %}
                </td>
                <td><span class="status-{{ sale.status }}">{{ sale.status|title }}</span></td>
                <td>{{ sale.created_at[:10] }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    
    <h2>Recent Payments</h2>
    <table>
        <thead>
            <tr>
                <th>Date</th>
                <th>Amount</th>
                <th>Type</th>
                <th>Status</th>
                <th>Reference</th>
                <th>Project</th>
            </tr>
        </thead>
        <tbody>
            {% for payment in recent_payments %}
            <tr>
                <td>{{ payment.payment_date or payment.created_at[:10] }}</td>
                <td>RM{{ "%.2f"|format(payment.commission_amount|float) }}</td>
                <td>
                    {% if payment.is_upline_payment %}
                    <span class="payment-type-badge payment-type-upline">Upline</span>
                    {% if payment.selling_agent_name %}
                    <br><small style="font-size: 11px; color: #666;">From: {{ payment.selling_agent_name }}</small>
                    {% endif %}
                    {% else %}
                    <span class="payment-type-badge payment-type-own">Own</span>
                    {% endif %}
                </td>
                <td><span class="status-{{ payment.payment_status }}">{{ payment.payment_status|title }}</span></td>
                <td>{{ payment.transaction_id or 'N/A' }}</td>
                <td>
                    {% if payment.project_name %}
                    <span class="project-badge">{{ payment.project_name }}</span>
                    {% else %}
                    <span style="color: #999;">—</span>
                    {% endif %}
                </td>
            </tr>
            {% else %}
            <tr>
                <td colspan="6" style="text-align: center; color: #666;">No payment history yet</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</body>
</html>'''

# ============ NOTIFICATION FUNCTIONS ============
def create_agent_notification(agent_id, notification_type, title, message, related_id=None, related_type=None, priority='normal', expires_in_days=7):
    """Create a notification for an agent"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    expires_at = None
    if expires_in_days:
        from datetime import timedelta
        expires_at = (datetime.now() + timedelta(days=expires_in_days)).strftime('%Y-%m-%d %H:%M:%S')
    
    cursor.execute('''
        INSERT INTO agent_notifications 
        (agent_id, notification_type, title, message, related_id, related_type, priority, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (agent_id, notification_type, title, message, related_id, related_type, priority, expires_at))
    
    conn.commit()
    conn.close()
    
    return cursor.lastrowid

def get_agent_notifications(agent_id, unread_only=True, limit=10):
    """Get notifications for an agent - WITH DEBUG"""
    print(f"DEBUG get_agent_notifications: agent_id={agent_id}, unread_only={unread_only}")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = '''
        SELECT * FROM agent_notifications 
        WHERE agent_id = ? AND (expires_at IS NULL OR expires_at > datetime('now'))
    '''
    
    if unread_only:
        query += ' AND is_read = 0'
    
    query += ' ORDER BY CASE priority WHEN "urgent" THEN 1 WHEN "high" THEN 2 WHEN "normal" THEN 3 ELSE 4 END, created_at DESC'
    
    if limit:
        query += f' LIMIT {limit}'
    
    print(f"DEBUG SQL: {query}")
    cursor.execute(query, (agent_id,))
    notifications = cursor.fetchall()
    
    print(f"DEBUG: Found {len(notifications)} notifications")
    
    conn.close()
    
    # Format notifications
    formatted_notifications = []
    for notif in notifications:
        formatted_notifications.append({
            'id': notif[0],
            'agent_id': notif[1],
            'type': notif[2],
            'title': notif[3],
            'message': notif[4],
            'related_id': notif[5],
            'related_type': notif[6],
            'is_read': notif[7],
            'priority': notif[8],
            'created_at': notif[9],
            'read_at': notif[10],
            'expires_at': notif[11]
        })
    
    return formatted_notifications

def get_unread_notification_count(agent_id):
    """Count unread notifications for an agent - WITH DEBUG"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT COUNT(*) FROM agent_notifications 
        WHERE agent_id = ? AND is_read = 0 
        AND (expires_at IS NULL OR expires_at > datetime('now'))
    ''', (agent_id,))
    
    count = cursor.fetchone()[0]
    conn.close()
    
    print(f"DEBUG get_unread_notification_count: agent_id={agent_id}, count={count}")
    
    return count

def mark_notification_read(notification_id):
    """Mark a notification as read"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE agent_notifications 
        SET is_read = 1, read_at = ?
        WHERE id = ?
    ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), notification_id))
    
    conn.commit()
    conn.close()

def mark_all_notifications_read(agent_id):
    """Mark all notifications as read for an agent"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE agent_notifications 
        SET is_read = 1, read_at = ?
        WHERE agent_id = ? AND is_read = 0
    ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), agent_id))
    
    conn.commit()
    conn.close()

def get_unread_notification_count(agent_id):
    """Count unread notifications for an agent"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT COUNT(*) FROM agent_notifications 
        WHERE agent_id = ? AND is_read = 0 
        AND (expires_at IS NULL OR expires_at > datetime('now'))
    ''', (agent_id,))
    
    count = cursor.fetchone()[0]
    conn.close()
    
    return count

def check_agent_pending_tasks(agent_id):
    """Check for pending tasks and create notifications - ENHANCED VERSION"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Check for incomplete documents in pending submissions
    cursor.execute('''
        SELECT pl.id, pl.customer_name, pl.status,
               (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) as doc_count
        FROM property_listings pl
        WHERE pl.agent_id = ? 
          AND pl.status IN ('draft', 'rejected')
          AND (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) < 3
        ORDER BY pl.created_at DESC
    ''', (agent_id,))
    
    incomplete_listings = cursor.fetchall()
    
    # Create notifications for incomplete submissions
    for listing in incomplete_listings:
        listing_id = listing[0]
        customer_name = listing[1]
        status = listing[2]
        doc_count = listing[3]
        
        # Check if notification already exists
        cursor.execute('''
            SELECT id FROM agent_notifications 
            WHERE agent_id = ? AND related_id = ? AND related_type = 'listing' 
            AND is_read = 0 AND notification_type = 'incomplete_docs'
        ''', (agent_id, listing_id))
        
        existing = cursor.fetchone()
        
        if not existing:
            # Determine priority based on document count
            if doc_count == 0:
                priority = 'urgent'
                title = "🚨 CRITICAL: No Documents Uploaded"
                message = f"Submission #{listing_id} ({customer_name}) has NO documents uploaded. This cannot be submitted."
            elif doc_count == 1:
                priority = 'high'
                title = " Very Incomplete Documents"
                message = f"Submission #{listing_id} ({customer_name}) has only 1/3 documents. Minimum 3 documents required."
            else:
                priority = 'normal'
                title = "📎 Missing Documents"
                message = f"Submission #{listing_id} ({customer_name}) has {doc_count}/3 documents. One more document needed."
            
            create_agent_notification(
                agent_id=agent_id,
                notification_type='incomplete_docs',
                title=title,
                message=message,
                related_id=listing_id,
                related_type='listing',
                priority=priority,
                expires_in_days=7
            )
    
    # Check for rejected submissions that need resubmission
    cursor.execute('''
        SELECT id, customer_name FROM property_listings 
        WHERE agent_id = ? AND status = 'rejected'
    ''', (agent_id,))
    
    rejected_listings = cursor.fetchall()
    
    for listing in rejected_listings:
        listing_id = listing[0]
        customer_name = listing[1]
        
        # Check if notification already exists
        cursor.execute('''
            SELECT id FROM agent_notifications 
            WHERE agent_id = ? AND related_id = ? AND related_type = 'listing' 
            AND is_read = 0 AND notification_type = 'rejected_submission'
        ''', (agent_id, listing_id))
        
        existing = cursor.fetchone()
        
        if not existing:
            # Create notification
            create_agent_notification(
                agent_id=agent_id,
                notification_type='rejected_submission',
                title="❌ Submission Rejected",
                message=f"Submission #{listing_id} ({customer_name}) was rejected. Please review and resubmit.",
                related_id=listing_id,
                related_type='listing',
                priority='high'
            )
    
    # Get count of incomplete submissions for dashboard display
    incomplete_count = len(incomplete_listings)
    
    conn.close()
    
    return incomplete_count

def cleanup_expired_notifications():
    """Remove expired notifications"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        DELETE FROM agent_notifications 
        WHERE expires_at IS NOT NULL AND expires_at < datetime('now')
    ''')
    
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    
    if deleted > 0:
        print(f"🧹 Cleaned up {deleted} expired notifications")
    
    return deleted

# ============ ROUTES ============
@app.route('/')
def home():
    return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        conn = sqlite3.connect('real_estate.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE email = ?', (email,))
        user = cursor.fetchone()
        conn.close()
        
        if user and check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['user_email'] = user[1]
            session['user_name'] = user[3]
            session['user_role'] = user[4]
            
            if user[4] == 'admin':
                return redirect('/admin/dashboard')
            else:
                return redirect('/agent/dashboard')
        else:
            return render_template_string(LOGIN_TEMPLATE, error="Invalid email or password")
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/new-listing')
def new_listing():
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get transaction type from URL
    transaction_type = request.args.get('type', 'sales')
    
    # ===== DEBUG =====
    print("\n" + "="*60)
    print(f"DEBUG: URL parameter 'type' = '{transaction_type}'")
    # ===== END DEBUG =====
    
    # Build the SQL query
    if transaction_type == 'all':
        sql_query = """
            SELECT p.id, p.project_name, p.category, p.project_type, 
                   p.location, p.description, p.status, p.commission_rate,
                   p.project_sale_type
            FROM projects p
            WHERE p.status = 'active'
            ORDER BY p.project_name
        """
        params = ()
    else:
        sql_query = """
            SELECT p.id, p.project_name, p.category, p.project_type, 
                   p.location, p.description, p.status, p.commission_rate,
                   p.project_sale_type
            FROM projects p
            WHERE p.status = 'active' AND p.project_sale_type = ?
            ORDER BY p.project_name
        """
        params = (transaction_type,)
    
    cursor.execute(sql_query, params)
    projects_raw = cursor.fetchall()
    
    # ===== DEBUG =====
    print(f"DEBUG: Fetched {len(projects_raw)} projects for type '{transaction_type}'")
    for i, project in enumerate(projects_raw):
        print(f"DEBUG: Project {i+1}: {project[1]} (ID: {project[0]}, Type: '{project[8]}')")
    # ===== END DEBUG =====
    
    projects = []
    for project in projects_raw:
        cursor.execute("""
            SELECT id, unit_type, square_feet, base_price, rental_price, 
                   commission_rate, quantity, status
            FROM project_units 
            WHERE project_id = ? AND status = 'available'
            ORDER BY unit_type
        """, (project[0],))
        
        units = cursor.fetchall()
        
        # Format units data
        unit_list = []
        for unit in units:
            unit_list.append({
                'id': unit[0],
                'unit_type': unit[1],
                'square_feet': unit[2],
                'base_price': unit[3],
                'rental_price': unit[4],
                'commission_rate': unit[5],
                'quantity': unit[6],
                'status': unit[7]
            })
        
        projects.append({
            'id': project[0],
            'project_name': project[1],
            'category': project[2],
            'project_type': project[3],
            'location': project[4],
            'description': project[5],
            'status': project[6],
            'commission_rate': float(project[7]) if project[7] else 0.0,
            'project_sale_type': project[8],
            'units': unit_list
        })
    
    conn.close()
    
    return render_template_string(
        AGENT_FORM_TEMPLATE,
        agent_name=session.get('user_name', 'Agent'),
        agent_id=session.get('user_id'),
        agent_tier='standard',
        projects=projects,
        transaction_type=transaction_type,
        projects_json=json.dumps(projects)
    )

@app.route('/submit-listing', methods=['POST'])
def submit_listing():
    """Submit a new property listing"""
    if 'user_id' not in session:
        return redirect('/login')
    
    # Initialize variables
    conn = None
    cursor = None
    listing_id = None
    
    try:
        data = request.form
        sale_type = data.get('sale_type', 'sales')  # Default to sales
        
        # Get project and unit info
        project_id = data.get('project_id')
        unit_id = data.get('unit_id')
        
        # Calculate commission
        sale_price = float(data['sale_price'])
        
        # Initialize commission calculation variables
        commission_rate = None
        project_commission_rate = None
        unit_commission_rate = None
        commission_source = 'default'
        
        # OPEN SINGLE DATABASE CONNECTION WITH TIMEOUT
        conn = sqlite3.connect('real_estate.db', timeout=30.0)
        cursor = conn.cursor()
        
        # Check for project-specific commission
        if project_id:
            # Get project commission rate
            cursor.execute('SELECT commission_rate FROM projects WHERE id = ?', (project_id,))
            project = cursor.fetchone()
            if project and project[0]:
                project_commission_rate = float(project[0])
                commission_rate = project_commission_rate / 100
                commission_source = 'project'
            
            # Check for unit-specific commission
            if unit_id:
                cursor.execute('SELECT commission_rate FROM project_units WHERE id = ?', (unit_id,))
                unit = cursor.fetchone()
                if unit and unit[0]:
                    unit_commission_rate = float(unit[0])
                    commission_rate = unit_commission_rate / 100
                    commission_source = 'unit'
        
        # If no project commission, use default rate
        if commission_rate is None:
            commission_rate = 0.03  # Default 3% commission
            commission = sale_price * commission_rate
            commission_source = 'default'
        else:
            # Use project/unit commission rate
            commission = sale_price * commission_rate
        
        # Apply caps (RM1,000 - RM50,000)
        commission = max(1000, min(commission, 50000))
        
        # Save to database
        cursor.execute('''
            INSERT INTO property_listings
            (agent_id, customer_name, customer_email, customer_phone,
            property_address, sale_type, sale_price, closing_date,
            commission_amount, status, submitted_at, notes,
            project_id, unit_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            session['user_id'],
            data['customer_name'],
            data['customer_email'],
            data.get('customer_phone'),
            data['property_address'],
            sale_type,  # ← Changed from sale_price to sale_type
            sale_price,  # ← sale_price moved to correct position
            data.get('closing_date'),
            round(commission, 2),
            'draft',  # ← Added missing status parameter
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            data.get('notes', ''),
            project_id if project_id else None,
            unit_id if unit_id else None
        ))
        
        listing_id = cursor.lastrowid

        # ============ CREATE NOTIFICATIONS FOR AGENT ============
        # Success notification
        cursor.execute('''
            INSERT INTO agent_notifications 
            (agent_id, notification_type, title, message, related_id, related_type, priority, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            session['user_id'],
            'submission_success',
            "✅ Submission Created",
            f"Submission #{listing_id} has been created successfully. Commission: RM{commission:,.2f}",
            listing_id,
            'listing',
            'normal',
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))

        # ============ ENHANCED FILE UPLOAD HANDLING ============
        uploaded_files = []
        
        # Allowed file extensions
        ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'jpg', 'jpeg', 'png'}
        
        def allowed_file(filename):
            return '.' in filename and \
                   filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
        
        # Create structured folder: uploads/agent_id/date/listing_id/
        agent_id = session['user_id']
        current_date = datetime.now().strftime('%Y-%m-%d')
        
        # Create folder structure
        base_folder = f"uploads/agent_{agent_id}"
        date_folder = f"{base_folder}/{current_date}"
        listing_folder = f"{date_folder}/listing_{listing_id}"
        
        # Create folders if they don't exist
        for folder in [base_folder, date_folder, listing_folder]:
            if not os.path.exists(folder):
                os.makedirs(folder)
        
        # Handle single files
        file_fields = ['agreement', 'id_proof', 'property_docs']
        for field_name in file_fields:
            if field_name in request.files:
                file = request.files[field_name]
                if file and file.filename and allowed_file(file.filename):
                    # ============ ADD VALIDATION HERE ============
                    # Check file type
                    if not allowed_file(file.filename):
                        # Handle invalid file type
                        flash(f"❌ File type not allowed: {file.filename}", "error")
                        continue
                
                    # Check file size
                    if not validate_file_size(file):
                        flash(f"❌ File too large: {file.filename}", "error")
                        continue
                    # ============ END VALIDATION ============
                    filename = secure_filename(file.filename)
                    filepath = os.path.join(listing_folder, filename)
                    file.save(filepath)
                    
                    # Save to database
                    cursor.execute('''
                        INSERT INTO documents 
                        (listing_id, filename, filepath, file_type, file_size, uploaded_by, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        listing_id,
                        filename,
                        filepath,
                        filename.rsplit('.', 1)[1].lower(),
                        os.path.getsize(filepath),
                        session['user_id'],
                        f"Uploaded by {session.get('user_name', 'Agent')} on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    ))
                    uploaded_files.append(filename)
        
        # Handle multiple additional files
        if 'additional_docs' in request.files:
            files = request.files.getlist('additional_docs')
            for index, file in enumerate(files):
                if file and file.filename and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    filepath = os.path.join(listing_folder, filename)
                    file.save(filepath)
                    
                    cursor.execute('''
                        INSERT INTO documents 
                        (listing_id, filename, filepath, file_type, file_size, uploaded_by, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        listing_id,
                        filename,
                        filepath,
                        filename.rsplit('.', 1)[1].lower(),
                        os.path.getsize(filepath),
                        session['user_id'],
                        f"Additional document #{index+1}"
                    ))
                    uploaded_files.append(filename)
        
        # Update commission calculation details
        calculation_details = {
            'commission_source': commission_source,
            'base_rate': commission_rate * 100,
            'project_commission_rate': project_commission_rate,
            'unit_commission_rate': unit_commission_rate
        }
        
        # Save commission calculation
        cursor.execute('''
            INSERT INTO commission_calculations 
            (listing_id, agent_id, sale_price,
             base_rate, commission, calculation_details)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            listing_id,
            session['user_id'],
            sale_price,
            commission_rate * 100,  # Store as percentage
            round(commission, 2),
            json.dumps(calculation_details)
        ))
        
        # Commit all changes at once
        conn.commit()
        
        # Show success message with upload info
        upload_message = ""
        if uploaded_files:
            upload_message = f"<br>📎 Uploaded {len(uploaded_files)} document(s): {', '.join(uploaded_files[:3])}"
            if len(uploaded_files) > 3:
                upload_message += f" and {len(uploaded_files)-3} more"
        
        success_html = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Submission Successful</title>
            <meta http-equiv="refresh" content="5;url=/agent/dashboard">
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                .success-box {{ border: 2px solid #28a745; padding: 30px; border-radius: 10px; text-align: center; }}
                h2 {{ color: #28a745; }}
                .details {{ text-align: left; background: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0; }}
                .redirect {{ margin-top: 20px; color: #666; font-size: 14px; }}
            </style>
        </head>
        <body>
            <div class="success-box">
                <h2>✅ Sale Submitted Successfully!</h2>
                <p>Reference ID: <strong>#{listing_id}</strong></p>
                
                <div class="details">
                    <p><strong>Agent ID:</strong> {agent_id}</p>
                    <p><strong>Submission Date:</strong> {current_date}</p>
                    <p><strong>Customer:</strong> {data['customer_name']}</p>
                    <p><strong>Property:</strong> {data['property_address'][:50]}...</p>
                    <p><strong>Sale Price:</strong> RM{"{:,.2f}".format(sale_price)}</p>
                    <p><strong>Commission:</strong> <span style="color: #28a745; font-weight: bold;">
                    RM{"{:,.2f}".format(commission)}</span></p>
                    <p><strong>Folder Structure:</strong> agent_{agent_id}/{current_date}/listing_{listing_id}/</p>
                    {upload_message}
                </div>
                
                <p>Your submission is now pending admin approval.</p>
                <div class="redirect">
                    Redirecting to dashboard in 5 seconds...
                </div>
                <div style="margin-top: 30px;">
                    <a href="/new-listing" style="background: #007bff; color: white; padding: 10px 20px; 
                       text-decoration: none; border-radius: 5px; margin-right: 10px;">➕ New Sale</a>
                    <a href="/agent/dashboard" style="background: #6c757d; color: white; padding: 10px 20px; 
                       text-decoration: none; border-radius: 5px;">📊 Go to Dashboard</a>
                </div>
            </div>
        </body>
        </html>
        '''
        
        return success_html
        
    except sqlite3.OperationalError as e:
        if conn:
            conn.rollback()
        
        # Specific handling for database locked error
        if 'locked' in str(e).lower():
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
        print(f"Error in submit-listing: {error_details}")
        return render_error_page(f"Unexpected error: {str(e)}")
        
    finally:
        # Always close the database connection
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def render_error_page(error_message):
    """Helper function to render error page"""
    error_html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Submission Error</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
            .error-box {{ border: 2px solid #dc3545; padding: 30px; border-radius: 10px; text-align: center; }}
            h2 {{ color: #dc3545; }}
        </style>
    </head>
    <body>
        <div class="error-box">
            <h2>❌ Submission Failed</h2>
            <p><strong>Error:</strong> {error_message}</p>
            <div style="margin-top: 30px;">
                <a href="/new-listing" style="background: #007bff; color: white; padding: 10px 20px; 
                   text-decoration: none; border-radius: 5px; margin-right: 10px;">← Try Again</a>
                <a href="/agent/dashboard" style="background: #6c757d; color: white; padding: 10px 20px; 
                   text-decoration: none; border-radius: 5px;">Dashboard →</a>
            </div>
        </div>
    </body>
    </html>
    '''
    return error_html

@app.route('/agent/dashboard')
def agent_dashboard():
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')

    user_id = session['user_id']
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # ============ SIMPLIFIED QUERIES - NO COMMENTS IN SQL ============
    
    # 1. Get basic agent stats
    cursor.execute('''
        SELECT 
            COUNT(*) as total_sales,
            SUM(commission_amount) as total_commission,
            SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) as drafts,
            SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected
        FROM property_listings 
        WHERE agent_id = ?
    ''', (user_id,))
    
    stats = cursor.fetchone()
    
    # 2. Get upline earnings
    cursor.execute('''
        SELECT 
            SUM(cp.commission_amount) as upline_earnings,
            COUNT(cp.id) as upline_payments_count
        FROM commission_payments cp
        JOIN property_listings pl ON cp.listing_id = pl.id
        WHERE cp.agent_id = ?
        AND pl.agent_id != ?
        AND cp.payment_status != 'rejected'
    ''', (user_id, user_id))
    
    upline_earnings_result = cursor.fetchone()
    upline_earnings = upline_earnings_result[0] if upline_earnings_result and upline_earnings_result[0] else 0
    upline_payments_count = upline_earnings_result[1] if upline_earnings_result and upline_earnings_result[1] else 0
    
    # 3. Get paid commissions
    cursor.execute('''
        SELECT 
            SUM(commission_amount) as total_paid,
            COUNT(*) as total_payments
        FROM commission_payments 
        WHERE agent_id = ? AND payment_status = 'paid'
    ''', (user_id,))
    
    paid_commissions = cursor.fetchone()
    total_paid = paid_commissions[0] if paid_commissions and paid_commissions[0] else 0
    total_payments = paid_commissions[1] if paid_commissions and paid_commissions[1] else 0
    
    # 4. Get upline info (SIMPLIFIED)
    cursor.execute('''
        SELECT 
            upline.name,
            upline.email,
            users.upline_commission_rate
        FROM users
        LEFT JOIN users upline ON users.upline_id = upline.id
        WHERE users.id = ?
    ''', (user_id,))
    
    upline_info = cursor.fetchone()
    
    # 5. Get downline agents (SIMPLIFIED)
    cursor.execute('''
        SELECT 
            id,
            name,
            email,
            upline_commission_rate,
            created_at
        FROM users 
        WHERE upline_id = ? AND role = 'agent'
        ORDER BY created_at DESC
    ''', (user_id,))
    
    downline_agents = cursor.fetchall()
    
    # 6. Get recent sales (SIMPLIFIED)
    cursor.execute('''
        SELECT 
            pl.id,
            pl.customer_name,
            pl.sale_price,
            pl.commission_amount,
            pl.status,
            pl.created_at,
            p.project_name
        FROM property_listings pl
        LEFT JOIN projects p ON pl.project_id = p.id
        WHERE pl.agent_id = ?
        ORDER BY pl.created_at DESC 
        LIMIT 10
    ''', (user_id,))
    
    recent_sales = cursor.fetchall()
    
    # 7. Get recent payments (SIMPLIFIED - REMOVED PROBLEMATIC COLUMNS)
    cursor.execute('''
        SELECT 
            cp.id,
            cp.listing_id,
            cp.agent_id,
            cp.commission_amount,
            cp.payment_status,
            cp.payment_date,
            cp.transaction_id,
            cp.created_at,
            pl.customer_name,
            p.project_name
        FROM commission_payments cp
        JOIN property_listings pl ON cp.listing_id = pl.id
        LEFT JOIN projects p ON pl.project_id = p.id
        WHERE cp.agent_id = ?
        ORDER BY cp.payment_date DESC, cp.created_at DESC 
        LIMIT 10
    ''', (user_id,))
    
    recent_payments = cursor.fetchall()
    
    conn.close()
    
    # ============ PREPARE DATA FOR TEMPLATE ============
    
    # Convert recent sales
    recent_sales_list = []
    for row in recent_sales:
        recent_sales_list.append({
            'id': row[0],
            'customer_name': row[1],
            'sale_price': row[2],
            'commission_amount': row[3],
            'status': row[4],
            'created_at': row[5],
            'project_name': row[6]
        })
    
    # Convert recent payments
    recent_payments_list = []
    for row in recent_payments:
        payment_type = "own" if row[2] == user_id else "upline"
        recent_payments_list.append({
            'id': row[0],
            'listing_id': row[1],
            'commission_amount': row[3],
            'payment_status': row[4],
            'payment_date': row[5],
            'transaction_id': row[6],
            'created_at': row[7],
            'project_name': row[9],
            'payment_type': payment_type,
            'is_upline_payment': payment_type == "upline"
        })
    
    # Calculate project stats
    project_sales_count = sum(1 for sale in recent_sales_list if sale['project_name'])
    unique_projects = set(sale['project_name'] for sale in recent_sales_list if sale['project_name'])
    unique_projects_count = len(unique_projects)
    
    # Prepare upline data
    upline_data = None
    if upline_info and upline_info[0]:
        upline_data = {
            'name': upline_info[0],
            'email': upline_info[1],
            'commission_rate': upline_info[2] if upline_info[2] else 0
        }
    
    # Prepare downline data
    downline_list = []
    for agent in downline_agents:
        downline_list.append({
            'id': agent[0],
            'name': agent[1],
            'email': agent[2],
            'commission_rate': agent[3] if agent[3] else 0,
            'join_date': agent[4][:10] if agent[4] else ''
        })
    
    # Downline stats
    downline_stats = {
        'count': len(downline_list),
        'total_commission_rate': sum(d['commission_rate'] for d in downline_list),
        'upline_earnings': upline_earnings,
        'upline_payments_count': upline_payments_count
    }
    
    # Skip notifications and incomplete submissions for now to simplify
    notifications = []
    incomplete_list = []
    unread_count = 0
    
    # ============ RENDER TEMPLATE ============
    return render_template_string(DASHBOARD_TEMPLATE,
        user_name=session.get('user_name'),
        total_sales=stats[0] if stats else 0,
        total_commission=format(stats[1] if stats and stats[1] else 0, ',.2f'),
        pending_count=stats[2] if stats else 0,
        draft_count=stats[3] if stats else 0,
        rejected_count=stats[4] if stats else 0,
        recent_sales=recent_sales_list,
        recent_payments=recent_payments_list,
        project_sales_count=project_sales_count,
        unique_projects_count=unique_projects_count,
        upline_info=upline_data,
        downline_agents=downline_list,
        downline_stats=downline_stats,
        notifications=notifications,
        unread_count=unread_count,
        incomplete_submissions=incomplete_list,
        upline_earnings=upline_earnings,
        upline_payments_count=upline_payments_count,
        total_paid=total_paid,
        total_payments=total_payments)

@app.route('/agent/my-downline')
def agent_downline():
    """Agent view of their downline network including indirect downlines"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    agent_id = session['user_id']
    
    # ========== DIRECT DOWNLINES (Level 1) ==========
    cursor.execute('''
        SELECT 
            id,
            name,
            email,
            upline_commission_rate,
            created_at,
            'direct' as relationship_type
        FROM users 
        WHERE upline_id = ? AND role = 'agent'
        ORDER BY created_at DESC
    ''', (agent_id,))
    
    direct_downlines = cursor.fetchall()
    
    # ========== INDIRECT DOWNLINES (Level 2) ==========
    cursor.execute('''
        SELECT 
            u2.id,
            u2.name,
            u2.email,
            2.5 as commission_rate,  -- Fixed 2.5% for indirect
            u2.created_at,
            'indirect' as relationship_type,
            u1.name as direct_upline_name  -- Who connects them to you
        FROM users u1
        JOIN users u2 ON u1.id = u2.upline_id
        WHERE u1.upline_id = ? 
        AND u2.role = 'agent'
        AND u1.role = 'agent'
        ORDER BY u2.created_at DESC
    ''', (agent_id,))
    
    indirect_downlines = cursor.fetchall()
    
    # ========== GET COMMISSIONS FROM INDIRECT DOWNLINES ==========
    indirect_commissions = {}
    if indirect_downlines:
        # Get all indirect downline IDs
        indirect_ids = [str(d[0]) for d in indirect_downlines]
        indirect_commissions = {}
        if indirect_downlines:
            # Get all indirect downline IDs
            indirect_ids = [str(d[0]) for d in indirect_downlines]
     
            if indirect_ids:
                # Create parameter placeholders
                placeholders = ','.join(['?' for _ in indirect_ids])
                query = f'''
                    SELECT 
                        agent_id,
                        SUM(amount) as total_commission,
                        COUNT(*) as commission_count
                    FROM upline_commissions 
                    WHERE upline_id = ?
                    AND agent_id IN ({placeholders})
                    AND commission_type = 'indirect'
                    GROUP BY agent_id
                '''
                # Pass all parameters safely
                cursor.execute(query, (agent_id, *indirect_ids))
        
                for row in cursor.fetchall():
                    indirect_commissions[row[0]] = {
                        'total': row[1] or 0,
                        'count': row[2] or 0
                    }
        
    # ========== GET DIRECT COMMISSIONS ==========
    direct_commissions = {}
    if direct_downlines:
        # Get all direct downline IDs
        direct_ids = [str(d[0]) for d in direct_downlines]
        direct_commissions = {}
        if direct_downlines:
            # Get all direct downline IDs
            direct_ids = [str(d[0]) for d in direct_downlines]
    
            if direct_ids:
                placeholders = ','.join(['?' for _ in direct_ids])
                query = f'''
                    SELECT 
                        agent_id,
                        SUM(amount) as total_commission,
                        COUNT(*) as commission_count
                    FROM upline_commissions 
                    WHERE upline_id = ?
                    AND agent_id IN ({placeholders})
                    AND commission_type = 'direct'
                    GROUP BY agent_id
                '''
                cursor.execute(query, (agent_id, *direct_ids))
        
                for row in cursor.fetchall():
                    direct_commissions[row[0]] = {
                        'total': row[1] or 0,
                        'count': row[2] or 0
                    }

    # ========== TOTAL STATISTICS ==========
    # Combine direct and indirect downlines for stats
    all_downline_ids = [str(d[0]) for d in direct_downlines] + [str(d[0]) for d in indirect_downlines]
    all_ids_str = ','.join(all_downline_ids) if all_downline_ids else '0'
    
    cursor.execute(f'''
        SELECT 
            COUNT(pl.id) as total_sales,
            SUM(pl.sale_price) as total_sales_value,
            SUM(pl.commission_amount) as total_commission,
            SUM(CASE WHEN pl.status = 'approved' THEN pl.commission_amount ELSE 0 END) as approved_commission
        FROM users u
        LEFT JOIN property_listings pl ON u.id = pl.agent_id
        WHERE u.id IN ({all_ids_str})
    ''')
    
    stats = cursor.fetchone()
    
    conn.close()
    
    # ========== PREPARE DATA FOR TEMPLATE ==========
    direct_downline_list = []
    indirect_downline_list = []
    
    total_direct_earnings = 0
    total_indirect_earnings = 0
    
    # Process direct downlines
    for agent in direct_downlines:
        commission_rate = agent[3] if agent[3] else 5.0  # Default 5%
        agent_id_val = agent[0]
        
        # Get commissions for this direct downline
        agent_commission = direct_commissions.get(agent_id_val, {'total': 0, 'count': 0})
        total_direct_earnings += agent_commission['total']
        
        direct_downline_list.append({
            'id': agent_id_val,
            'name': agent[1],
            'email': agent[2],
            'commission_rate': commission_rate,
            'join_date': agent[4][:10] if agent[4] else '',
            'commission_percentage': f"{commission_rate}%",
            'relationship': 'direct',
            'earnings_from_agent': agent_commission['total'],
            'commission_count': agent_commission['count']
        })
    
    # Process indirect downlines
    for agent in indirect_downlines:
        agent_id_val = agent[0]
        direct_upline_name = agent[6] if len(agent) > 6 else "Direct Upline"
        
        # Get commissions for this indirect downline
        agent_commission = indirect_commissions.get(agent_id_val, {'total': 0, 'count': 0})
        total_indirect_earnings += agent_commission['total']
        
        indirect_downline_list.append({
            'id': agent_id_val,
            'name': agent[1],
            'email': agent[2],
            'commission_rate': 2.5,  # Fixed for indirect
            'join_date': agent[4][:10] if agent[4] else '',
            'commission_percentage': "2.5%",
            'relationship': 'indirect',
            'direct_upline_name': direct_upline_name,
            'earnings_from_agent': agent_commission['total'],
            'commission_count': agent_commission['count']
        })
    
    # ========== CALCULATE TOTAL STATS ==========
    total_commission = stats[2] if stats and stats[2] else 0
    
    stats_dict = {
        'total_downline': len(direct_downline_list) + len(indirect_downline_list),
        'direct_downline_count': len(direct_downline_list),
        'indirect_downline_count': len(indirect_downline_list),
        'total_sales': stats[0] if stats and stats[0] else 0,
        'total_sales_value': stats[1] if stats and stats[1] else 0,
        'total_commission': total_commission,
        'approved_commission': stats[3] if stats and stats[3] else 0,
        'total_direct_earnings': total_direct_earnings,
        'total_indirect_earnings': total_indirect_earnings,
        'total_your_earnings': total_direct_earnings + total_indirect_earnings
    }
    
    # ========== UPDATED TEMPLATE WITH BOTH SECTIONS ==========
    downline_template = '''<!DOCTYPE html>
<html>
<head>
    <title>My Downline Network</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .stats { display: flex; gap: 15px; margin: 20px 0; flex-wrap: wrap; }
        .stat-card { background: white; padding: 15px; border-radius: 8px; flex: 1; min-width: 150px; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .stat-value { font-size: 1.8em; font-weight: bold; }
        .downline-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; margin: 20px 0; }
        .downline-card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .agent-info { display: flex; align-items: center; margin-bottom: 15px; }
        .agent-avatar { font-size: 40px; margin-right: 15px; width: 60px; height: 60px; display: flex; align-items: center; justify-content: center; border-radius: 50%; background: #e9ecef; }
        .agent-details { flex: 1; }
        .commission-rate { padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; display: inline-block; margin-top: 5px; }
        .direct-rate { background: #e3f2fd; color: #1565c0; }
        .indirect-rate { background: #fff3cd; color: #856404; }
        .btn { padding: 8px 16px; border-radius: 5px; text-decoration: none; display: inline-block; }
        .btn-back { background: #6c757d; color: white; }
        .empty-state { text-align: center; padding: 50px 20px; background: white; border-radius: 10px; color: #666; }
        .earnings-badge { background: #d4edda; color: #155724; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-left: 5px; }
        .section-header { display: flex; justify-content: space-between; align-items: center; margin: 30px 0 15px 0; padding-bottom: 10px; border-bottom: 2px solid #eee; }
        .relationship-badge { padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: bold; text-transform: uppercase; }
        .direct-badge { background: #e3f2fd; color: #1565c0; }
        .indirect-badge { background: #fff3cd; color: #856404; }
    </style>
</head>
<body>
    <div class="header">
        <h1>👥 My Downline Network</h1>
        <div>
            <a href="/agent/dashboard" class="btn btn-back">← Back to Dashboard</a>
        </div>
    </div>
    
    <!-- Enhanced Stats -->
    <div class="stats">
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Total Downline</div>
            <div class="stat-value" style="color: #007bff;">{{ stats.total_downline }}</div>
            <small>{{ stats.direct_downline_count }} direct + {{ stats.indirect_downline_count }} indirect</small>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Direct Earnings</div>
            <div class="stat-value" style="color: #28a745;">RM{{ "%.2f"|format(stats.total_direct_earnings|float) }}</div>
            <small>5% from direct downlines</small>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Indirect Earnings</div>
            <div class="stat-value" style="color: #6f42c1;">RM{{ "%.2f"|format(stats.total_indirect_earnings|float) }}</div>
            <small>2.5% from indirect downlines</small>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Your Total Earnings</div>
            <div class="stat-value" style="color: #fd7e14;">RM{{ "%.2f"|format(stats.total_your_earnings|float) }}</div>
            <small>From entire network</small>
        </div>
    </div>
    
    <!-- DIRECT DOWNLINES SECTION -->
    {% if direct_downline_agents %}
    <div class="section-header">
        <div style="display: flex; align-items: center; gap: 20px; flex-wrap: wrap;">
            <h2 style="margin: 0; font-size: 1.5em;">
                📋 Direct Downlines ({{ direct_downline_agents|length }})
            </h2>
        
            <div style="display: flex; align-items: center; gap: 15px; background: #f8f9fa; padding: 8px 15px; border-radius: 10px; border-left: 4px solid #007bff;">
                <!-- Commission -->
                <div style="text-align: center;">
                    <div style="font-size: 11px; color: #6c757d; font-weight: 500;">COMMISSION</div>
                    <div style="color: #1565c0; font-weight: bold; font-size: 16px;">5%</div>
                </div>
            
                <div style="width: 1px; height: 25px; background: #dee2e6;"></div>
            
                <!-- Total Earnings -->
                <div style="text-align: center;">
                    <div style="font-size: 11px; color: #6c757d; font-weight: 500;">TOTAL</div>
                    <div style="color: #28a745; font-weight: bold; font-size: 18px;">
                    RM{{ "%.2f"|format(stats.total_direct_earnings|float) }}
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="downline-grid">
        {% for agent in direct_downline_agents %}
        <div class="downline-card">
            <div class="agent-info">
                <div class="agent-avatar">👤</div>
                <div class="agent-details">
                    <strong style="font-size: 18px;">{{ agent.name }}</strong>
                    <div style="color: #666; font-size: 14px;">{{ agent.email }}</div>
                    <div style="margin-top: 8px;">
                        <span class="commission-rate direct-rate">
                            {{ agent.commission_percentage }} commission to you
                        </span>
                        {% if agent.earnings_from_agent > 0 %}
                        <span class="earnings-badge">
                            RM{{ "%.2f"|format(agent.earnings_from_agent|float) }} earned
                        </span>
                        {% endif %}
                    </div>
                </div>
            </div>
            
            <div style="border-top: 1px solid #eee; padding-top: 15px; margin-top: 15px;">
                <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                    <span style="color: #666;">Agent ID:</span>
                    <span>#{{ agent.id }}</span>
                </div>
                <div style="display: flex; justify-content: space-between;">
                    <span style="color: #666;">Joined:</span>
                    <span>{{ agent.join_date }}</span>
                </div>
                {% if agent.commission_count > 0 %}
                <div style="display: flex; justify-content: space-between; margin-top: 5px;">
                    <span style="color: #666;">Commissions:</span>
                    <span style="color: #28a745; font-weight: bold;">
                        {{ agent.commission_count }} sales
                    </span>
                </div>
                {% endif %}
            </div>
            
            <div style="margin-top: 15px; display: flex; gap: 10px;">
                <a href="/agent/downline-performance/{{ agent.id }}" class="btn" style="background: #17a2b8; color: white; padding: 6px 12px; font-size: 12px;">View Performance</a>
                <a href="mailto:{{ agent.email }}" class="btn" style="background: #28a745; color: white; padding: 6px 12px; font-size: 12px;">Send Email</a>
            </div>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div style="padding: 20px; background: white; border-radius: 10px; margin: 20px 0; text-align: center;">
        <h3 style="color: #666;">No Direct Downlines Yet</h3>
        <p style="color: #888;">You don't have any direct agents under your supervision yet.</p>
    </div>
    {% endif %}
    
    <!-- INDIRECT DOWNLINES SECTION -->
    {% if indirect_downline_agents %}
    <div class="section-header">
        <div style="display: flex; align-items: center; gap: 20px; flex-wrap: wrap;">
            <h2 style="margin: 0; font-size: 1.5em;">
                📋 Indirect Downlines ({{ indirect_downline_agents|length }})
            </h2>
        
            <div style="display: flex; align-items: center; gap: 15px; background: #f8f9fa; padding: 8px 15px; border-radius: 10px; border-left: 4px solid #6f42c1;">
                <!-- Commission -->
                <div style="text-align: center;">
                    <div style="font-size: 11px; color: #6c757d; font-weight: 500;">COMMISSION</div>
                    <div style="color: #6f42c1; font-weight: bold; font-size: 16px;">2.5%</div>
                </div>
            
                <div style="width: 1px; height: 25px; background: #dee2e6;"></div>
            
                <!-- Total Earnings -->
                <div style="text-align: center;">
                    <div style="font-size: 11px; color: #6c757d; font-weight: 500;">TOTAL</div>
                    <div style="color: #6f42c1; font-weight: bold; font-size: 18px;">
                        RM{{ "%.2f"|format(stats.total_indirect_earnings|float) }}
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="downline-grid">
        {% for agent in indirect_downline_agents %}
        <div class="downline-card">
            <div class="agent-info">
                <div class="agent-avatar">👥</div>
                <div class="agent-details">
                    <strong style="font-size: 18px;">{{ agent.name }}</strong>
                    <div style="color: #666; font-size: 14px;">{{ agent.email }}</div>
                    <div style="margin-top: 8px;">
                        <span class="commission-rate indirect-rate">
                            {{ agent.commission_percentage }} commission to you
                        </span>
                        {% if agent.earnings_from_agent > 0 %}
                        <span class="earnings-badge">
                            RM{{ "%.2f"|format(agent.earnings_from_agent|float) }} earned
                        </span>
                        {% endif %}
                    </div>
                    <div style="margin-top: 5px; font-size: 12px; color: #666;">
                        Via: {{ agent.direct_upline_name }}
                    </div>
                </div>
            </div>
            
            <div style="border-top: 1px solid #eee; padding-top: 15px; margin-top: 15px;">
                <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                    <span style="color: #666;">Agent ID:</span>
                    <span>#{{ agent.id }}</span>
                </div>
                <div style="display: flex; justify-content: space-between;">
                    <span style="color: #666;">Joined:</span>
                    <span>{{ agent.join_date }}</span>
                </div>
                {% if agent.commission_count > 0 %}
                <div style="display: flex; justify-content: space-between; margin-top: 5px;">
                    <span style="color: #666;">Indirect Commissions:</span>
                    <span style="color: #6f42c1; font-weight: bold;">
                        {{ agent.commission_count }} sales
                    </span>
                </div>
                {% endif %}
            </div>
            
            <div style="margin-top: 15px; display: flex; gap: 10px;">
                <a href="/agent/downline-performance/{{ agent.id }}" class="btn" style="background: #6c757d; color: white; padding: 6px 12px; font-size: 12px;">View Profile</a>
                <a href="mailto:{{ agent.email }}" class="btn" style="background: #28a745; color: white; padding: 6px 12px; font-size: 12px;">Send Email</a>
            </div>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div style="padding: 20px; background: white; border-radius: 10px; margin: 20px 0; text-align: center;">
        <h3 style="color: #666;">No Indirect Downlines Yet</h3>
        <p style="color: #888;">Indirect downlines appear when your direct downlines recruit their own agents.</p>
    </div>
    {% endif %}
    
    <!-- Network Info Section -->
    <div style="margin-top: 30px; padding: 20px; background: #e8f4ff; border-radius: 10px;">
        <h3>💰 Multi-Level Commission System</h3>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 15px;">
            <div>
                <strong>📈 Your Earnings Structure:</strong>
                <ul style="margin: 10px 0 0 20px;">
                    <li><strong>Direct Downlines:</strong> 5% of their commission</li>
                    <li><strong>Indirect Downlines:</strong> 2.5% of their commission</li>
                    <li><strong>Network Depth:</strong> Current system supports 2 levels</li>
                    <li><strong>Payout:</strong> Weekly or Monthly commissions</li>
                </ul>
            </div>
            <div>
                <strong>👥 Building Your Network:</strong>
                <ul style="margin: 10px 0 0 20px;">
                    <li>Recruit agents to become your <strong>direct downlines</strong></li>
                    <li>When they recruit agents, they become your <strong>indirect downlines</strong></li>
                    <li>Each level adds to your passive income stream</li>
                    <li>Network grows exponentially as agents recruit others</li>
                </ul>
            </div>
        </div>
        <div style="margin-top: 20px; padding: 15px; background: white; border-radius: 8px; border-left: 4px solid #007bff;">
            <strong>💡 Current Network Status:</strong>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px;">
                <div>
                    ✅ <strong>Direct Downlines:</strong> {{ stats.direct_downline_count }} agents
                    <div style="margin-left: 20px;">• 5% commission rate</div>
                    <div style="margin-left: 20px;">• RM{{ "%.2f"|format(stats.total_direct_earnings|float) }} earned</div>
                </div>
                <div>
                    ✅ <strong>Indirect Downlines:</strong> {{ stats.indirect_downline_count }} agents
                    <div style="margin-left: 20px;">• 2.5% commission rate</div>
                    <div style="margin-left: 20px;">• RM{{ "%.2f"|format(stats.total_indirect_earnings|float) }} earned</div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>'''
    
    return render_template_string(downline_template, 
                                 direct_downline_agents=direct_downline_list,
                                 indirect_downline_agents=indirect_downline_list,
                                 stats=stats_dict)

@app.route('/agent/downline-performance/<int:agent_id>')
def agent_downline_performance(agent_id):
    """Agent view of a specific downline agent's performance"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    # Verify this agent is actually in the current user's downline
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT upline_id FROM users WHERE id = ?', (agent_id,))
    result = cursor.fetchone()
    
    if not result or result[0] != session['user_id']:
        conn.close()
        return "Access denied - This agent is not in your downline", 403
    
    # Get downline agent details
    cursor.execute('SELECT name, email, upline_commission_rate, created_at FROM users WHERE id = ?', (agent_id,))
    agent_info = cursor.fetchone()
    
    # Get performance data
    sql = """SELECT 
COUNT(*) as total_listings,
SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved_listings,
SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected_listings,
SUM(sale_price) as total_sales,
SUM(commission_amount) as total_commission,
SUM(CASE WHEN status = 'approved' THEN commission_amount ELSE 0 END) as approved_commission,
AVG(sale_price) as avg_sale_price,
AVG(commission_amount) as avg_commission
FROM property_listings 
WHERE agent_id = ?"""
    cursor.execute(sql, (agent_id,))
    
    performance = cursor.fetchone()
    conn.close()
    
    # Process the data
    if agent_info:
        agent_data = {
            'id': agent_id,
            'name': agent_info[0],
            'email': agent_info[1],
            'upline_commission_rate': agent_info[2] if agent_info[2] else 0,
            'created_at': agent_info[3][:10] if agent_info[3] else '',
            'commission_percentage': f"{agent_info[2]}%" if agent_info[2] else "0%"
        }
    else:
        agent_data = None
    
    if performance:
        perf_data = {
            'total_listings': performance[0] or 0,
            'approved_listings': performance[1] or 0,
            'rejected_listings': performance[2] or 0,
            'total_sales': performance[3] or 0,
            'total_commission': performance[4] or 0,
            'approved_commission': performance[5] or 0,
            'avg_sale_price': performance[6] or 0,
            'avg_commission': performance[7] or 0
        }
    else:
        perf_data = None
    
    # Calculate conversion rates
    if perf_data and perf_data['total_listings'] > 0:
        approval_rate = (perf_data['approved_listings'] / perf_data['total_listings']) * 100
        rejection_rate = (perf_data['rejected_listings'] / perf_data['total_listings']) * 100
    else:
        approval_rate = 0
        rejection_rate = 0
    
    # Calculate your earnings from this downline
    your_earnings = 0
    if perf_data and perf_data['total_commission'] > 0 and agent_data:
        commission_rate = agent_data['upline_commission_rate'] if agent_data['upline_commission_rate'] else 5
        your_earnings = perf_data['total_commission'] * (commission_rate / 100)
    
    # Create template - MINIMAL VERSION
    template = """<!DOCTYPE html>
<html>
<head>
    <title>Downline Performance</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin: 20px 0; }
        .stat-card { background: white; padding: 15px; border-radius: 8px; text-align: center; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .stat-value { font-size: 1.8em; font-weight: bold; margin: 10px 0; }
        .btn { padding: 8px 16px; border-radius: 5px; text-decoration: none; display: inline-block; }
        .btn-back { background: #6c757d; color: white; }
        .agent-header { display: flex; align-items: center; gap: 20px; margin-bottom: 20px; }
        .agent-avatar { font-size: 50px; width: 80px; height: 80px; display: flex; align-items: center; justify-content: center; border-radius: 50%; background: #e9ecef; }
        .commission-rate { background: #e3f2fd; color: #1565c0; padding: 5px 10px; border-radius: 15px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="header">
        <h1>📊 Downline Performance</h1>
        <div style="margin-top: 10px;">
            <a href="/agent/my-downline" class="btn btn-back">← Back to Downline Network</a>
        </div>
    </div>
    
    {% if agent %}
    <div class="agent-header">
        <div class="agent-avatar">👤</div>
        <div style="flex: 1;">
            <h2 style="margin: 0;">{{ agent.name }}</h2>
            <div style="color: #666; margin: 5px 0;">{{ agent.email }}</div>
            <div style="margin-top: 10px;">
                <span class="commission-rate">{{ agent.commission_percentage }} commission to you</span>
                <div style="font-size: 14px; color: #666; margin-top: 5px;">
                    Agent ID: #{{ agent.id }} | Joined: {{ agent.created_at }}
                </div>
            </div>
        </div>
        <div style="text-align: right;">
            <div style="font-size: 14px; color: #666;">Your Earnings</div>
            <div style="font-size: 28px; font-weight: bold; color: #28a745;">RM{{ "%.2f"|format(your_earnings|float) }}</div>
        </div>
    </div>
    
    {% if performance %}
    <div class="stats">
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Total Listings</div>
            <div class="stat-value" style="color: #007bff;">{{ performance.total_listings }}</div>
            <div style="font-size: 12px; color: #666;">
                {{ performance.approved_listings }} approved<br>
                {{ performance.rejected_listings }} rejected
            </div>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Total Sales Value</div>
            <div class="stat-value" style="color: #28a745;">RM{{ "%.0f"|format(performance.total_sales|float) }}</div>
            <div style="font-size: 12px; color: #666;">
                Avg: RM{{ "%.0f"|format(performance.avg_sale_price|float) }}
            </div>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Total Commission</div>
            <div class="stat-value" style="color: #6f42c1;">RM{{ "%.2f"|format(performance.total_commission|float) }}</div>
            <div style="font-size: 12px; color: #666;">
                Avg: RM{{ "%.2f"|format(performance.avg_commission|float) }}
            </div>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Approval Rate</div>
            <div class="stat-value" style="color: #fd7e14;">{{ "%.1f"|format(approval_rate|float) }}%</div>
            <div style="font-size: 12px; color: #666;">
                Rejection: {{ "%.1f"|format(rejection_rate|float) }}%
            </div>
        </div>
    </div>
    
    <div style="background: white; padding: 20px; border-radius: 10px; margin: 20px 0; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
        <h3>💰 Earnings Summary</h3>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 15px;">
            <div>
                <strong>Downline Agent Earnings:</strong>
                <div style="margin: 10px 0; padding: 10px; background: #f8f9fa; border-radius: 5px;">
                    <div>Total Commission: <strong>RM{{ "%.2f"|format(performance.total_commission|float) }}</strong></div>
                    <div>Approved Commission: <strong>RM{{ "%.2f"|format(performance.approved_commission|float) }}</strong></div>
                </div>
            </div>
            <div>
                <strong>Your Earnings from this Agent:</strong>
                <div style="margin: 10px 0; padding: 10px; background: #d4edda; border-radius: 5px; color: #155724;">
                    <div style="font-size: 18px; font-weight: bold;">RM{{ "%.2f"|format(your_earnings|float) }}</div>
                    <div style="font-size: 12px;">({{ agent.commission_percentage }} of their commission)</div>
                </div>
            </div>
        </div>
    </div>
    {% else %}
    <div style="padding: 40px; text-align: center; background: white; border-radius: 10px;">
        <h3 style="color: #666;">No Performance Data Yet</h3>
        <p style="color: #888;">This agent hasn't submitted any listings yet.</p>
    </div>
    {% endif %}
    
    {% else %}
    <div style="padding: 40px; text-align: center; background: white; border-radius: 10px;">
        <h3 style="color: #666;">Agent Not Found</h3>
        <p style="color: #888;">The agent you're looking for doesn't exist or you don't have access.</p>
        <a href="/agent/my-downline" class="btn btn-back" style="margin-top: 20px;">Back to Downline Network</a>
    </div>
    {% endif %}
    
    <div style="margin-top: 30px;">
        <a href="/agent/my-downline" class="btn btn-back">← Back to Downline Network</a>
    </div>
</body>
</html>"""
    
    return render_template_string(template, 
                                 agent=agent_data, 
                                 performance=perf_data, 
                                 your_earnings=your_earnings,
                                 approval_rate=approval_rate,
                                 rejection_rate=rejection_rate)

# ============ NOTIFICATION MANAGEMENT ROUTES ============
@app.route('/agent/mark-notification-read/<int:notification_id>')
def mark_notification_read_route(notification_id):
    """Mark a notification as read"""
    if 'user_id' not in session:
        return redirect('/login')
    
    mark_notification_read(notification_id)
    return redirect('/agent/dashboard')

@app.route('/agent/mark-all-read')
def mark_all_notifications_read_route():
    """Mark all notifications as read"""
    if 'user_id' not in session:
        return redirect('/login')
    
    mark_all_notifications_read(session['user_id'])
    return redirect('/agent/dashboard')

@app.route('/agent/notifications')
def agent_notifications_page():
    """Agent notifications page"""
    if 'user_id' not in session:
        return redirect('/login')
    
    # Get all notifications (read and unread)
    notifications = get_agent_notifications(session['user_id'], unread_only=False, limit=50)
    
    notification_template = '''<!DOCTYPE html>
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
        .type-listing { background: #fff3cd; color: #856404; }
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
                        <span class="notification-type type-{{ notification.type }}">{{ notification.type|title }}</span>
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
</html>'''
    
    return render_template_string(notification_template, notifications=notifications)

@app.route('/agent/submissions')
def agent_submissions():
    """Agent view all their submissions - FIXED VERSION WITH DOCUMENT STATUS"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get filter parameters
    status_filter = request.args.get('status', 'all')
    search_query = request.args.get('search', '')
    
    # Build query based on filters - UPDATED with document count
    query = '''
        SELECT p.id, p.status, p.customer_name, p.property_address, 
               p.sale_price, p.commission_amount, p.created_at, 
               p.submitted_at, p.approved_at,
               (SELECT COUNT(*) FROM documents WHERE listing_id = p.id) as doc_count
        FROM property_listings p
        WHERE p.agent_id = ?
    '''
    params = [session['user_id']]
    
    if status_filter == 'incomplete':
        query += ' AND (SELECT COUNT(*) FROM documents d WHERE d.listing_id = p.id) < 3'
    elif status_filter != 'all':
        query += ' AND p.status = ?'
        params.append(status_filter)
    
    if search_query:
        query += ' AND (p.customer_name LIKE ? OR p.property_address LIKE ?)'
        params.extend([f'%{search_query}%', f'%{search_query}%'])
    
    query += ' ORDER BY p.created_at DESC'
    
    cursor.execute(query, params)
    submissions = cursor.fetchall()
    
    # Get counts for each status
    cursor.execute('''
        SELECT status, COUNT(*) as count 
        FROM property_listings 
        WHERE agent_id = ? 
        GROUP BY status
    ''', (session['user_id'],))
    status_counts_raw = cursor.fetchall()
    
    # Get incomplete count
    cursor.execute('''
        SELECT COUNT(*) as incomplete_count
        FROM property_listings p
        WHERE p.agent_id = ? 
        AND (SELECT COUNT(*) FROM documents WHERE listing_id = p.id) < 3
    ''', (session['user_id'],))
    incomplete_count = cursor.fetchone()[0] or 0
    
    # Get total count
    cursor.execute('SELECT COUNT(*) FROM property_listings WHERE agent_id = ?', (session['user_id'],))
    total_count = cursor.fetchone()[0] or 0
    
    # CLOSE THE DATABASE CONNECTION
    conn.close()
    
    # Convert status_counts to dictionary for easier access
    status_counts = {}
    for status, count in status_counts_raw:
        status_key = status if status else 'draft'
        status_counts[status_key] = count
    
    # ========== BUILD TABLE ROWS ==========
    table_rows = ''
    if submissions:
        for sub in submissions:
            customer_name = sub[2] if sub[2] else ''
            property_address = sub[3] if sub[3] else ''
            prop_address_display = property_address[:30] + ('...' if len(property_address) > 30 else '')
            sale_price = float(sub[4]) if sub[4] else 0
            commission = float(sub[5]) if sub[5] else 0
            status = sub[1] if sub[1] else 'draft'
            submitted_date = sub[7][:10] if sub[7] else 'Not submitted'
            approved_date = sub[8][:10] if sub[8] and status == 'approved' else ''
            doc_count = sub[9] if len(sub) > 9 else 0
            
            # Document status badge
            if doc_count == 0:
                doc_status = '<span style="color: #dc3545; font-size: 12px; font-weight: bold;">❌ No Docs</span>'
            elif doc_count < 3:
                doc_status = f'<span style="color: #ffc107; font-size: 12px; font-weight: bold;">⚠️ {doc_count}/3</span>'
            else:
                doc_status = f'<span style="color: #28a745; font-size: 12px;">✅ {doc_count}</span>'
            
            table_rows += f'''
                <tr>
                    <td>#{sub[0]}</td>
                    <td>{customer_name}</td>
                    <td>{prop_address_display}</td>
                    <td>RM{sale_price:,.2f}</td>
                    <td>RM{commission:,.2f}</td>
                    <td>{doc_status}</td>
                    <td>
                        <span class="status-badge status-{status}">
                            {status.title()}
                        </span>
                        {'<br><small>Approved: ' + approved_date + '</small>' if approved_date else ''}
                    </td>
                    <td>{submitted_date}</td>
                    <td>
                        <a href="/agent/submission/{sub[0]}" class="action-btn btn-view">👁️ View</a>
                        <a href="/agent/documents/{sub[0]}" class="action-btn btn-docs">📎 Docs</a>
                    </td>
                </tr>'''
    
    # Build empty state message
    if status_filter != 'all':
        if status_filter == 'incomplete':
            empty_message = 'No incomplete submissions found. All submissions have sufficient documents!'
        else:
            empty_message = f'No {status_filter} submissions found.'
    else:
        empty_message = 'You haven\'t created any submissions yet.'
    
    # Build the select options
    status_options = f'''
    <select name="status">
        <option value="all" {'selected' if status_filter == 'all' else ''}>All Status ({total_count})</option>
        <option value="incomplete" {'selected' if status_filter == 'incomplete' else ''}>Incomplete Documents ({incomplete_count})</option>
        <option value="draft" {'selected' if status_filter == 'draft' else ''}>Drafts ({status_counts.get('draft', 0)})</option>
        <option value="submitted" {'selected' if status_filter == 'submitted' else ''}>Submitted ({status_counts.get('submitted', 0)})</option>
        <option value="approved" {'selected' if status_filter == 'approved' else ''}>Approved ({status_counts.get('approved', 0)})</option>
        <option value="rejected" {'selected' if status_filter == 'rejected' else ''}>Rejected ({status_counts.get('rejected', 0)})</option>
    </select>
    '''
    
    # Build the main content
    if submissions:
        main_content = f'''
        <table>
            <thead>
                <tr>
                    <th>ID</th>
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
                {table_rows}
            </tbody>
        </table>
        '''
    else:
        main_content = f'''
        <div class="empty-state">
            <h3>No submissions found</h3>
            <p>{empty_message}</p>
            <a href="/new-listing" class="btn" style="background: #28a745; margin-top: 15px;">Create Your First Submission</a>
        </div>
        '''
    
    # Create the HTML template
    html_template = f'''<!DOCTYPE html>
<html>
<head>
    <title>My Submissions</title>
    <style>
        body {{ 
            font-family: Arial, sans-serif;
            margin: 20px; 
            background: #f5f5f5; 
        }}
        .header {{ 
            background: white; 
            padding: 20px; 
            border-radius: 10px; 
            margin-bottom: 20px; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
        }}
        .stats {{ 
            display: flex; 
            gap: 15px; 
            margin: 20px 0; 
            flex-wrap: wrap; 
        }}
        .stat-card {{ 
            background: white; 
            padding: 15px; 
            border-radius: 8px; 
            flex: 1; 
            min-width: 120px; 
            box-shadow: 0 2px 5px rgba(0,0,0,0.1); 
            text-align: center; 
        }}
        .stat-value {{ 
            font-size: 1.8em; 
            font-weight: bold; 
        }}
        .filters {{ 
            background: white; 
            padding: 20px; 
            border-radius: 10px; 
            margin-bottom: 20px; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
        }}
        .filter-group {{ 
            display: flex; 
            gap: 15px; 
            align-items: center; 
        }}
        select, input {{ 
            padding: 8px 12px; 
            border: 1px solid #ddd; 
            border-radius: 5px; 
        }}
        .btn {{ 
            padding: 8px 16px; 
            background: #007bff; 
            color: white; 
            border: none; 
            border-radius: 5px; 
            cursor: pointer; 
            text-decoration: none; 
            display: inline-block;
        }}
        .btn:hover {{
            background: #0056b3;
        }}
        table {{ 
            width: 100%; 
            background: white; 
            border-radius: 10px; 
            overflow: hidden; 
            box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
            margin: 20px 0; 
        }}
        th, td {{ 
            padding: 12px 15px; 
            text-align: left; 
            border-bottom: 1px solid #eee; 
        }}
        th {{ 
            background: #2c3e50; 
            color: white; 
        }}
        .status-badge {{ 
            padding: 4px 10px; 
            border-radius: 12px; 
            font-size: 12px; 
            font-weight: bold; 
        }}
        .status-draft {{ 
            background: #fff3cd; 
            color: #856404; 
        }}
        .status-submitted {{ 
            background: #cce5ff; 
            color: #004085; 
        }}
        .status-approved {{ 
            background: #d4edda; 
            color: #155724; 
        }}
        .status-rejected {{ 
            background: #f8d7da; 
            color: #721c24; 
        }}
        .action-btn {{ 
            padding: 4px 8px; 
            border-radius: 4px; 
            font-size: 12px; 
            text-decoration: none; 
            margin-right: 5px; 
        }}
        .btn-view {{ 
            background: #17a2b8; 
            color: white; 
        }}
        .btn-docs {{ 
            background: #6f42c1; 
            color: white; 
        }}
        .empty-state {{ 
            text-align: center; 
            padding: 50px 20px; 
            background: white; 
            border-radius: 10px; 
            color: #666; 
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>📋 My Submissions</h1>
        <div>
            <a href="/agent/dashboard" class="btn">← Dashboard</a>
            <a href="/new-listing" class="btn" style="background: #28a745;">➕ New Submission</a>
            <a href="/logout" style="color: #dc3545; margin-left: 20px;">Logout</a>
        </div>
    </div>
    
    <div class="stats">
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Total</div>
            <div class="stat-value" style="color: #007bff;">{total_count}</div>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Draft</div>
            <div class="stat-value" style="color: #6c757d;">{status_counts.get('draft', 0)}</div>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Submitted</div>
            <div class="stat-value" style="color: #007bff;">{status_counts.get('submitted', 0)}</div>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Approved</div>
            <div class="stat-value" style="color: #28a745;">{status_counts.get('approved', 0)}</div>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Rejected</div>
            <div class="stat-value" style="color: #dc3545;">{status_counts.get('rejected', 0)}</div>
        </div>
    </div>
    
    <div class="filters">
        <h3>Filter Submissions</h3>
        <form method="GET" class="filter-group">
            {status_options}
            
            <input type="text" name="search" placeholder="Search by customer or property..." value="{search_query}">
            
            <button type="submit" class="btn">🔍 Filter</button>
            <a href="/agent/submissions" class="btn" style="background: #6c757d;">Clear</a>
        </form>
    </div>
    
    {main_content}
</body>
</html>'''
    
    return html_template

@app.route('/agent/submission/<int:listing_id>')
def agent_view_submission(listing_id):
    """Agent view a single submission"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    # Verify the listing belongs to this agent
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT agent_id FROM property_listings WHERE id = ?', (listing_id,))
    listing = cursor.fetchone()
    
    if not listing or listing[0] != session['user_id']:
        conn.close()
        return "Access denied or listing not found", 403
    
    # Get submission details
    cursor.execute('''
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
    ''', (listing_id,))
    
    submission = cursor.fetchone()
    
    if not submission:
        conn.close()
        return "Submission not found", 404
    
    # Get uploaded documents
    cursor.execute('SELECT * FROM documents WHERE listing_id = ? ORDER BY uploaded_at', (listing_id,))
    documents = cursor.fetchall()
    
    conn.close()
    
    # Format the data for the template
    sub_data = {
        'id': submission[0],
        'agent_id': submission[1],
        'status': submission[2],
        'customer_name': submission[3],
        'customer_email': submission[4],
        'customer_phone': submission[5],
        'property_address': submission[6],
        'sale_price': submission[7],
        'closing_date': submission[8],
        'commission_amount': submission[9],
        'commission_status': submission[10],
        'created_at': submission[11],
        'submitted_at': submission[12],
        'approved_at': submission[13],
        'approved_by': submission[14],
        'notes': submission[15],
        'rejection_reason': submission[17],
        'project_name': submission[18],
        'unit_type': submission[19],
        'agent_name': submission[20],
        'doc_count': submission[21]
    }
    
    # Create the template HTML using proper Jinja2 syntax
    template = f'''
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
    '''
    
    # Add dynamic buttons based on status
    if sub_data['status'] in ['draft', 'rejected']:
        template += f'<a href="/agent/reupload-documents/{listing_id}" class="btn btn-primary">📤 Add/Replace Documents</a>'
    
    if sub_data['status'] == 'rejected':
        template += f'<a href="/agent/resubmit/{listing_id}" class="btn btn-success">✅ Resubmit for Approval</a>'
    
    template += f'''
                    <a href="/agent/documents/{listing_id}" class="btn btn-primary">📎 View Documents ({sub_data['doc_count']})</a>
                    <a href="/new-listing" class="btn btn-success">➕ Create New Sale</a>
                </div>
            </div>
    '''
    
    # Add rejection reason if rejected
    if sub_data['status'] == 'rejected' and sub_data['rejection_reason']:
        template += f'''
            <div class="rejection-box">
                <strong>❌ Rejection Reason:</strong>
                <p>{sub_data['rejection_reason']}</p>
            </div>
        '''
    
    # Add commission info if approved
    if sub_data['status'] == 'approved' and sub_data['commission_amount']:
        template += f'''
            <div class="commission-box">
                <strong>💰 Commission Amount:</strong> RM{"{:,.2f}".format(sub_data["commission_amount"])}
            </div>
        '''
    
    # Continue with the rest of the template
    template += f'''
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
    '''
    
    # Add project info if any
    if sub_data['project_name']:
        template += f'''
                <div style="margin-top: 15px;">
                    <div class="info-label">Project</div>
                    <div class="info-value">{sub_data['project_name']}</div>
                </div>
        '''
    
    if sub_data['unit_type']:
        template += f'''
                <div style="margin-top: 10px;">
                    <div class="info-label">Unit Type</div>
                    <div class="info-value">{sub_data['unit_type']}</div>
                </div>
        '''
    
    template += f'''
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
    '''
    
    # Add notes if any
    if sub_data['notes']:
        template += f'''
            <div class="info-card">
                <h3>📝 Notes</h3>
                <div style="padding: 15px; background: #f8f9fa; border-radius: 5px;">
                    {sub_data['notes']}
                </div>
            </div>
        '''
    
    # Add documents preview
    if documents:
        template += f'''
            <div class="info-card">
                <h3>📎 Documents ({len(documents)})</h3>
                <p><a href="/agent/documents/{listing_id}" class="btn btn-primary">View All Documents →</a></p>
            </div>
        '''
    else:
        template += f'''
            <div class="info-card">
                <h3>📎 Documents</h3>
                <p>No documents uploaded yet. <a href="/agent/reupload-documents/{listing_id}" class="btn btn-primary">Upload Documents</a></p>
            </div>
        '''
    
    # Add navigation footer
    template += f'''
            <!-- Navigation -->
            <div style="text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd;">
                <a href="/agent/submissions" class="btn btn-secondary">← Back to My Submissions</a>
                <a href="/new-listing" class="btn btn-success">➕ Create New Sale</a>
                <a href="/agent/dashboard" class="btn btn-primary">📊 Dashboard</a>
            </div>
        </div>
    </body>
    </html>
    '''
    
    return template

@app.route('/view-document/<int:doc_id>')
def view_document(doc_id):
    """View/download a specific document"""
    if 'user_id' not in session:
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get document info
    if session['user_role'] == 'admin':
        # Admin can view any document
        cursor.execute('''
            SELECT d.*, pl.agent_id, u.name as agent_name, pl.customer_name
            FROM documents d
            JOIN property_listings pl ON d.listing_id = pl.id
            JOIN users u ON pl.agent_id = u.id
            WHERE d.id = ?
        ''', (doc_id,))
    else:
        # Agent can only view their own documents
        cursor.execute('''
            SELECT d.*, pl.agent_id, u.name as agent_name, pl.customer_name
            FROM documents d
            JOIN property_listings pl ON d.listing_id = pl.id
            JOIN users u ON pl.agent_id = u.id
            WHERE d.id = ? AND pl.agent_id = ?
        ''', (doc_id, session['user_id']))
    
    document = cursor.fetchone()
    conn.close()
    
    if not document:
        return "Document not found or access denied", 404
    
    filepath = document[3]
    filename = document[2]
    
    # Check if file exists
    if not os.path.exists(filepath):
        return f"File not found: {filename}", 404
    
    # Determine content type
    content_type = 'application/octet-stream'
    file_extension = filename.lower().split('.')[-1] if '.' in filename else ''
    
    content_types = {
        'pdf': 'application/pdf',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'gif': 'image/gif',
        'doc': 'application/msword',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'txt': 'text/plain'
    }
    
    if file_extension in content_types:
        content_type = content_types[file_extension]
    
    # Check if download parameter is specified
    download = request.args.get('download', '0')
    as_attachment = download == '1'
    
    return send_file(
        filepath,
        mimetype=content_type,
        as_attachment=as_attachment,
        download_name=filename
    )

@app.route('/agent/documents/<int:listing_id>')
def agent_view_documents(listing_id):
    """Agent view all documents for a listing"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    # Verify the listing belongs to this agent
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT agent_id, customer_name FROM property_listings WHERE id = ?', (listing_id,))
    listing = cursor.fetchone()
    
    if not listing or listing[0] != session['user_id']:
        conn.close()
        return "Access denied", 403
    
    # Get all documents for this listing
    cursor.execute('''
        SELECT d.*, u.name as uploader_name
        FROM documents d
        LEFT JOIN users u ON d.uploaded_by = u.id
        WHERE d.listing_id = ?
        ORDER BY d.uploaded_at DESC
    ''', (listing_id,))
    
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
            
            docs_html += f'''
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
            '''
    else:
        docs_html = f'''
        <div style="padding: 40px; text-align: center; color: #666; background: #f8f9fa; border-radius: 5px;">
            <h3>No documents uploaded yet</h3>
            <p>Upload documents using the button below</p>
            <a href="/agent/reupload-documents/{listing_id}" class="btn" style="background: #28a745; color: white; padding: 10px 20px; border-radius: 5px; text-decoration: none;">
                📤 Upload Documents
            </a>
        </div>
        '''
    
    # Create the full page
    template = f'''
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
    '''
    
    return template

@app.route('/agent/reupload-documents/<int:listing_id>', methods=['GET', 'POST'])
def agent_reupload_documents(listing_id):
    """Agent reupload documents to existing listing - FIXED VERSION"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    # Verify the listing belongs to this agent
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT agent_id, status FROM property_listings WHERE id = ?', (listing_id,))
    listing = cursor.fetchone()
    
    if not listing or listing[0] != session['user_id']:
        conn.close()
        return "Access denied or listing not found", 403
    
    status = listing[1]
    
    # Check if listing status allows reupload
    allowed_statuses = ['draft', 'rejected']
    if status not in allowed_statuses:
        conn.close()
        # Show a user-friendly message with options
        error_template = '''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Cannot Add Documents</title>
            <style>
                body { 
                    font-family: Arial, sans-serif; 
                    max-width: 600px; 
                    margin: 50px auto; 
                    padding: 20px; 
                    background: #f5f5f5; 
                }
                .error-box { 
                    background: white; 
                    padding: 30px; 
                    border-radius: 10px; 
                    text-align: center; 
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1); 
                    border: 2px solid #ffc107;
                }
                h2 { 
                    margin-top: 0; 
                    color: #856404; 
                }
                .status-badge { 
                    padding: 8px 16px; 
                    border-radius: 20px; 
                    font-weight: bold; 
                    font-size: 14px; 
                    margin: 10px 0; 
                    display: inline-block;
                }
                .status-submitted { background: #cce5ff; color: #004085; }
                .status-approved { background: #d4edda; color: #155724; }
                .btn { 
                    padding: 10px 20px; 
                    border-radius: 5px; 
                    text-decoration: none; 
                    display: inline-block; 
                    margin: 10px 5px;
                }
                .btn-primary { background: #007bff; color: white; }
                .btn-secondary { background: #6c757d; color: white; }
                .info-box { 
                    background: #e8f4ff; 
                    padding: 15px; 
                    border-radius: 5px; 
                    margin: 20px 0; 
                    text-align: left;
                }
            </style>
        </head>
        <body>
            <div class="error-box">
                <h2> Cannot Add Documents to This Submission</h2>
                
                <div style="margin: 20px 0;">
                    <div class="status-badge status-{{ status }}">
                        {{ status|upper }}
                    </div>
                    <p>Submission #{{ listing_id }} is currently <strong>{{ status }}</strong>.</p>
                </div>
                
                <div class="info-box">
                    <h4 style="margin-top: 0;">📋 Document Upload Rules:</h4>
                    <ul>
                        <li><strong>Draft/Rejected:</strong> Can add/replace documents freely</li>
                        <li><strong>Submitted:</strong> Under admin review - cannot modify documents</li>
                        <li><strong>Approved:</strong> Completed - cannot modify documents</li>
                    </ul>
                </div>
                
                {% if status == 'submitted' %}
                <div style="margin: 20px 0; padding: 15px; background: #fff3cd; border-radius: 5px;">
                    <h4 style="margin-top: 0;">⏳ Submission Under Review</h4>
                    <p>Your submission is currently being reviewed by the admin team. Once the review is complete, you will be notified of the outcome.</p>
                    <p><strong>If you need to add urgent documents:</strong> Contact the admin team directly.</p>
                </div>
                {% endif %}
                
                {% if status == 'approved' %}
                <div style="margin: 20px 0; padding: 15px; background: #d4edda; border-radius: 5px;">
                    <h4 style="margin-top: 0;">✅ Submission Approved</h4>
                    <p>Your submission has been approved! The commission process has begun.</p>
                    <p><strong>Need to make changes?</strong> Contact the admin team if there are any issues.</p>
                </div>
                {% endif %}
                
                <div style="margin-top: 30px;">
                    <a href="/agent/submission/{{ listing_id }}" class="btn btn-primary">📄 View Submission Details</a>
                    <a href="/agent/documents/{{ listing_id }}" class="btn" style="background: #17a2b8; color: white;">📎 View Existing Documents</a>
                    <a href="/agent/submissions" class="btn btn-secondary">📋 Back to My Submissions</a>
                </div>
            </div>
        </body>
        </html>
        '''
        return render_template_string(error_template, 
                                    listing_id=listing_id, 
                                    status=status)
    
    # Get existing documents
    cursor.execute('SELECT filename, uploaded_at FROM documents WHERE listing_id = ? ORDER BY uploaded_at DESC', (listing_id,))
    existing_docs = cursor.fetchall()
    conn.close()
    
    if request.method == 'POST':
        try:
            conn = sqlite3.connect('real_estate.db')
            cursor = conn.cursor()
            
            # Get listing details for folder structure
            cursor.execute('SELECT agent_id FROM property_listings WHERE id = ?', (listing_id,))
            listing_info = cursor.fetchone()
            agent_id = listing_info[0]
            
            # Find existing upload folder
            cursor.execute('SELECT filepath FROM documents WHERE listing_id = ? LIMIT 1', (listing_id,))
            doc = cursor.fetchone()
            
            if doc:
                # Use existing folder
                filepath = doc[0]
                upload_folder = os.path.dirname(filepath)
            else:
                # Create new folder structure
                current_date = datetime.now().strftime('%Y-%m-%d')
                upload_folder = f"uploads/agent_{agent_id}/{current_date}/listing_{listing_id}"
            
            # Create folder if it doesn't exist
            if not os.path.exists(upload_folder):
                os.makedirs(upload_folder)
            
            uploaded_files = []
            ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'jpg', 'jpeg', 'png'}
            
            def allowed_file(filename):
                return '.' in filename and \
                       filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
            
            # Handle file uploads
            for field_name in request.files:
                files = request.files.getlist(field_name)
                for file in files:
                    if file and file.filename and allowed_file(file.filename):
                        filename = secure_filename(file.filename)
                        filepath = os.path.join(upload_folder, filename)
                        file.save(filepath)
                        
                        # Check if document already exists
                        cursor.execute('SELECT id FROM documents WHERE listing_id = ? AND filename = ?', 
                                      (listing_id, filename))
                        existing = cursor.fetchone()
                        
                        if existing:
                            # Update existing document
                            cursor.execute('''
                                UPDATE documents 
                                SET filepath = ?, uploaded_at = ?, notes = ?
                                WHERE id = ?
                            ''', (filepath, 
                                  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                  f"Reuploaded by {session['user_name']} on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                                  existing[0]))
                            uploaded_files.append(f"📄 Updated: {filename}")
                        else:
                            # Add new document
                            cursor.execute('''
                                INSERT INTO documents 
                                (listing_id, filename, filepath, file_type, file_size, uploaded_by, notes)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                listing_id,
                                filename,
                                filepath,
                                filename.rsplit('.', 1)[1].lower(),
                                os.path.getsize(filepath),
                                session['user_id'],
                                f"Uploaded by {session['user_name']} on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                            ))
                            uploaded_files.append(f"📄 Added: {filename}")
            
            # If status was 'rejected', change it back to 'draft' after adding documents
            if status == 'rejected':
                cursor.execute('''
                    UPDATE property_listings 
                    SET status = 'draft'
                    WHERE id = ?
                ''', (listing_id,))
            
            # Get customer name for notifications BEFORE closing connection
            cursor.execute('SELECT customer_name FROM property_listings WHERE id = ?', (listing_id,))
            customer_result = cursor.fetchone()
            customer_name = customer_result[0] if customer_result else 'Unknown'
            
            conn.commit()
            conn.close()

            # ===== FIX: RESUBMIT LISTING FOR ADMIN REVIEW =====
            conn = sqlite3.connect('real_estate.db')
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE property_listings
                SET status = 'submitted',
                    submitted_at = CURRENT_TIMESTAMP,
                    rejection_reason = NULL
                WHERE id = ?
            """, (listing_id,))

            conn.commit()
            conn.close()
            # ================================================


            
           
            # ============ CREATE NOTIFICATION ============
            create_agent_notification(
                agent_id=session['user_id'],
                notification_type='documents_uploaded',
                title="📎 Documents Uploaded",
                message=f"Documents uploaded for submission #{listing_id}",
                related_id=listing_id,
                related_type='listing',
                priority='normal'
            )

            # Re-check document completeness after upload
            check_and_notify_incomplete_docs(
                listing_id=listing_id,
                agent_id=session['user_id'],
                customer_name=customer_name
            )
            
            # Success message
            success_html = f'''
            <!DOCTYPE html>
            <html>
            <head>
                <title>Documents Uploaded Successfully</title>
                <style>
                    body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                    .success-box {{ border: 2px solid #28a745; padding: 30px; border-radius: 10px; text-align: center; }}
                    h2 {{ color: #28a745; }}
                    .uploaded-files {{ text-align: left; background: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0; }}
                </style>
            </head>
            <body>
                <div class="success-box">
                    <h2>✅ Documents Uploaded Successfully!</h2>
                    <p>Listing: <strong>#{listing_id}</strong></p>
                    
                    {f'<div style="background: #d4edda; padding: 10px; border-radius: 5px; margin: 15px 0;"><strong>Status Updated:</strong> Changed from rejected to draft. You can now resubmit.</div>' if status == 'rejected' else ''}
                    
                    <div class="uploaded-files">
                        <h3>Uploaded Files:</h3>
                        {"<br>".join(uploaded_files) if uploaded_files else "<p>No new files were uploaded</p>"}
                    </div>
                    
                    <p>Documents have been added/updated in the system.</p>
                    <div style="margin-top: 30px;">
                        <a href="/agent/submission/{listing_id}" style="background: #007bff; color: white; padding: 10px 20px; 
                           text-decoration: none; border-radius: 5px; margin-right: 10px;">📄 View Submission</a>
                        <a href="/agent/documents/{listing_id}" style="background: #17a2b8; color: white; padding: 10px 20px; 
                           text-decoration: none; border-radius: 5px;">📎 View All Documents</a>
                        {f'<a href="/agent/resubmit/{listing_id}" style="background: #28a745; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">✅ Resubmit Now</a>' if status == 'rejected' else ''}
                    </div>
                </div>
            </body>
            </html>
            '''
            
            return success_html
            
        except Exception as e:
            # Safely handle errors without accessing closed connections
            error_msg = str(e)
            safe_error_msg = error_msg.replace('\\', '/')
            
            # Try to rollback if connection is still open
            try:
                if 'conn' in locals() and conn:
                    conn.rollback()
                    conn.close()
            except:
                pass  # Ignore rollback errors
            
            error_html = f'''
            <!DOCTYPE html>
            <html>
            <head>
                <title>Upload Error</title>
                <style>
                    body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                    .error-box {{ border: 2px solid #dc3545; padding: 30px; border-radius: 10px; text-align: center; }}
                    h2 {{ color: #dc3545; }}
                </style>
            </head>
            <body>
                <div class="error-box">
                    <h2>❌ Document Upload Failed</h2>
                    <p><strong>Error:</strong> {safe_error_msg}</p>
                    <div style="margin-top: 30px;">
                        <a href="/agent/reupload-documents/{listing_id}" style="background: #007bff; color: white; padding: 10px 20px; 
                           text-decoration: none; border-radius: 5px; margin-right: 10px;">Try Again</a>
                        <a href="/agent/submission/{listing_id}" style="background: #6c757d; color: white; padding: 10px 20px; 
                           text-decoration: none; border-radius: 5px;">Back to Submission</a>
                    </div>
                </div>
            </body>
            </html>
            '''
            return error_html
    
    # GET request - show reupload form
    reupload_template = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Upload Documents - Listing #{listing_id}</title>
        <style>
            body {{ 
                font-family: Arial, sans-serif; 
                max-width: 800px; 
                margin: 50px auto; 
                padding: 20px; 
                background: #f5f5f5; 
            }}
            .container {{ 
                background: white; 
                padding: 30px; 
                border-radius: 10px; 
                box-shadow: 0 2px 20px rgba(0,0,0,0.1); 
            }}
            .header {{ 
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 20px;
                padding-bottom: 20px;
                border-bottom: 2px solid #007bff;
            }}
            .existing-docs {{
                background: #f8f9fa;
                padding: 15px;
                border-radius: 5px;
                margin: 15px 0;
                max-height: 200px;
                overflow-y: auto;
            }}
            .file-upload {{
                border: 2px dashed #ddd;
                padding: 30px;
                text-align: center;
                border-radius: 5px;
                margin: 20px 0;
                background: #fafbfc;
            }}
            .file-upload:hover {{
                border-color: #28a745;
                background: #f8fff9;
            }}
            .form-group {{
                margin-bottom: 20px;
            }}
            label {{
                display: block;
                margin-bottom: 5px;
                font-weight: bold;
                color: #555;
            }}
            .btn {{
                padding: 12px 25px;
                border: none;
                border-radius: 5px;
                cursor: pointer;
                font-size: 16px;
                text-decoration: none;
                display: inline-block;
            }}
            .btn-primary {{
                background: #28a745;
                color: white;
            }}
            .btn-secondary {{
                background: #6c757d;
                color: white;
            }}
            .note-box {{
                background: #fff3cd;
                border: 1px solid #ffeaa7;
                color: #856404;
                padding: 15px;
                border-radius: 5px;
                margin: 15px 0;
            }}
            .status-banner {{
                background: #cce5ff;
                border: 1px solid #b8daff;
                color: #004085;
                padding: 10px 15px;
                border-radius: 5px;
                margin: 15px 0;
                display: flex;
                align-items: center;
                gap: 10px;
            }}
            .status-rejected {{
                background: #f8d7da;
                border-color: #f5c6cb;
                color: #721c24;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📤 Upload Documents</h1>
                <div>
                    <a href="/agent/submission/{listing_id}" class="btn btn-secondary">← Back to Submission</a>
                </div>
            </div>
            
            {'<div class="status-banner status-rejected"><span style="font-size: 24px;"></span><div><strong>This submission was rejected</strong><br>Add missing documents and resubmit for review</div></div>' if status == 'rejected' else ''}
            
            <div class="note-box">
                <h3 style="margin-top: 0;">📋 Important Notes:</h3>
                <ul>
                    <li>You can upload new documents or replace existing ones</li>
                    <li>Files with the same name will be replaced</li>
                    <li>Maximum file size: 10MB per file</li>
                    <li>Allowed formats: PDF, DOC, DOCX, JPG, JPEG, PNG</li>
                    <li>After uploading, the submission status will be changed to <strong>draft</strong></li>
                </ul>
            </div>
            
            {f'<div class="existing-docs"><h4>Existing Documents ({len(existing_docs)}):</h4>' + '<br>'.join([f'📄 {doc[0]} (Uploaded: {doc[1][:19]})' for doc in existing_docs]) + '</div>' if existing_docs else '<p style="color: #666;">No existing documents found.</p>'}
            
            <form method="POST" action="/agent/reupload-documents/{listing_id}" enctype="multipart/form-data">
                <div class="form-group">
                    <label>Sales & Purchase Agreement</label>
                    <div class="file-upload">
                        <input type="file" name="agreement" accept=".pdf,.doc,.docx,.jpg,.jpeg,.png">
                        <p>Signed agreement between buyer and seller</p>
                    </div>
                </div>
                
                <div class="form-group">
                    <label>Customer ID Proof</label>
                    <div class="file-upload">
                        <input type="file" name="id_proof" accept=".pdf,.jpg,.jpeg,.png">
                        <p>NRIC/Passport copy of customer</p>
                    </div>
                </div>
                
                <div class="form-group">
                    <label>Property Documents</label>
                    <div class="file-upload">
                        <input type="file" name="property_docs" accept=".pdf,.doc,.docx">
                        <p>Title deed, floor plan, etc.</p>
                    </div>
                </div>
                
                <div class="form-group">
                    <label>Additional Documents</label>
                    <div class="file-upload">
                        <input type="file" name="additional_docs" multiple accept=".pdf,.doc,.docx,.jpg,.jpeg,.png">
                        <p>Hold CTRL to select multiple files</p>
                    </div>
                </div>
                
                <div style="margin-top: 30px;">
                    <button type="submit" class="btn btn-primary">✅ Upload Documents</button>
                    <a href="/agent/submission/{listing_id}" class="btn btn-secondary">Cancel</a>
                </div>
            </form>
        </div>
    </body>
    </html>
    '''
    
    return reupload_template



@app.route('/agent/resubmit/<int:listing_id>', methods=['GET', 'POST'])
def resubmit_listing(listing_id):
    """Agent resubmit a rejected listing"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Check if listing exists and belongs to agent
    cursor.execute('SELECT * FROM property_listings WHERE id = ? AND agent_id = ?', 
                   (listing_id, session['user_id']))
    listing = cursor.fetchone()
    
    if not listing:
        conn.close()
        return "Listing not found or access denied", 404
    
    # Check if listing can be resubmitted (must be rejected)
    if listing[2] != 'rejected':
        conn.close()
        return redirect(f'/agent/submission/{listing_id}')
    
    if request.method == 'POST':
        # Handle resubmission
        try:
            data = request.form
            sale_type = data.get('sale_type', 'sales')  # Default to sales
            
            # Get existing documents
            cursor.execute('SELECT filename, filepath FROM documents WHERE listing_id = ?', (listing_id,))
            existing_docs = cursor.fetchall()
            
            # Update the listing with new data - REMOVED property_type
            cursor.execute('''
                UPDATE property_listings 
                SET customer_name = ?,
                    customer_email = ?,
                    customer_phone = ?,
                    property_address = ?,
                    sale_price = ?,
                    closing_date = ?,
                    notes = ?,
                    status = 'submitted',
                    submitted_at = ?,
                    rejection_reason = NULL
                WHERE id = ?
            ''', (
                data['customer_name'],
                data['customer_email'],
                data.get('customer_phone'),
                data['property_address'],
                float(data['sale_price']),
                data.get('closing_date'),
                data.get('notes', ''),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                listing_id
            ))
            
            # Handle file uploads for resubmission
            ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'jpg', 'jpeg', 'png'}
            
            def allowed_file(filename):
                return '.' in filename and \
                       filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
            
            # Create folder structure for new uploads
            agent_id = session['user_id']
            current_date = datetime.now().strftime('%Y-%m-%d')
            listing_folder = f"uploads/agent_{agent_id}/{current_date}/listing_{listing_id}"
            
            if not os.path.exists(listing_folder):
                os.makedirs(listing_folder)
            
            uploaded_files = []
            
            # Handle new file uploads
            for field_name in request.files:
                files = request.files.getlist(field_name)
                for file in files:
                    if file and file.filename and allowed_file(file.filename):
                        filename = secure_filename(file.filename)
                        filepath = os.path.join(listing_folder, filename)
                        file.save(filepath)
                        
                        # Check if document already exists
                        cursor.execute('SELECT id FROM documents WHERE listing_id = ? AND filename = ?', 
                                      (listing_id, filename))
                        existing = cursor.fetchone()
                        
                        if existing:
                            # Update existing document
                            cursor.execute('''
                                UPDATE documents 
                                SET filepath = ?, uploaded_at = ?
                                WHERE id = ?
                            ''', (filepath, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), existing[0]))
                        else:
                            # Add new document
                            cursor.execute('''
                                INSERT INTO documents 
                                (listing_id, filename, filepath, file_type, file_size, uploaded_by, notes)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                listing_id,
                                filename,
                                filepath,
                                filename.rsplit('.', 1)[1].lower(),
                                os.path.getsize(filepath),
                                session['user_id'],
                                f"Resubmitted by {session['user_name']} on {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                            ))
                        
                        uploaded_files.append(filename)
            
            conn.commit()
            conn.close()

            # ============ CREATE NOTIFICATION ============
            create_agent_notification(
                agent_id=session['user_id'],
                notification_type='resubmission_success',
                title="🔄 Submission Resubmitted",
                message=f"Submission #{listing_id} has been resubmitted for approval",
                related_id=listing_id,
                related_type='listing',
                priority='normal'
            )
            
            # Success message
            success_html = f'''
            <!DOCTYPE html>
            <html>
            <head>
                <title>Resubmission Successful</title>
                <style>
                    body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                    .success-box {{ border: 2px solid #28a745; padding: 30px; border-radius: 10px; text-align: center; }}
                    h2 {{ color: #28a745; }}
                    .details {{ text-align: left; background: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0; }}
                </style>
            </head>
            <body>
                <div class="success-box">
                    <h2>✅ Submission Resubmitted Successfully!</h2>
                    <p>Reference ID: <strong>#{listing_id}</strong></p>
                    
                    <div class="details">
                        <p><strong>Customer:</strong> {data['customer_name']}</p>
                        <p><strong>Property:</strong> {data['property_address'][:50]}...</p>
                        <p><strong>Sale Price:</strong> RM{"{:,.2f}".format(float(data['sale_price']))}</p>
                        <p><strong>Status:</strong> Submitted for Review</p>
                        {f'<p><strong>Uploaded Files:</strong> {len(uploaded_files)} new file(s)</p>' if uploaded_files else ''}
                    </div>
                    
                    <p>Your resubmission is now pending admin approval.</p>
                    <div style="margin-top: 30px;">
                        <a href="/agent/submission/{listing_id}" style="background: #007bff; color: white; padding: 10px 20px; 
                           text-decoration: none; border-radius: 5px; margin-right: 10px;">📄 View Submission</a>
                        <a href="/agent/submissions" style="background: #6c757d; color: white; padding: 10px 20px; 
                           text-decoration: none; border-radius: 5px;">📋 My Submissions</a>
                    </div>
                </div>
            </body>
            </html>
            '''
            
            return success_html
            
        except Exception as e:
            conn.rollback()
            conn.close()
            return f"Error: {str(e)}"
    
    # GET request - show resubmission form
    # Create submission data dictionary - REMOVED property_type
    sub_data = {
        'id': listing[0],
        'customer_name': listing[3],
        'customer_email': listing[4],
        'customer_phone': listing[5],
        'property_address': listing[6],
        'sale_price': listing[7],  # Changed from index 8 to 7
        'closing_date': listing[8],  # Changed from index 9 to 8
        'notes': listing[15],  # Changed from index 16 to 15
        'rejection_reason': listing[17]  # Changed from index 18 to 17
    }
    
    # Get existing documents
    cursor.execute('SELECT filename FROM documents WHERE listing_id = ?', (listing_id,))
    existing_docs = [doc[0] for doc in cursor.fetchall()]
    
    conn.close()
    
    # Create resubmission form - REMOVED property_type references
    resubmit_template = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Resubmit Submission #{listing_id}</title>
        <style>
            body {{ 
                font-family: Arial, sans-serif; 
                margin: 40px; 
                background: #f5f5f5; 
            }}
            .container {{ 
                max-width: 800px; 
                margin: 0 auto; 
                background: white; 
                padding: 30px; 
                border-radius: 10px; 
                box-shadow: 0 2px 20px rgba(0,0,0,0.1); 
            }}
            .header {{ 
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 20px;
                padding-bottom: 20px;
                border-bottom: 2px solid #007bff;
            }}
            .rejection-alert {{
                background: #fff3cd;
                border: 1px solid #ffeaa7;
                color: #856404;
                padding: 15px;
                border-radius: 5px;
                margin: 20px 0;
            }}
            .existing-data {{
                background: #e8f4ff;
                padding: 15px;
                border-radius: 5px;
                margin: 15px 0;
            }}
            .form-section {{ 
                border: 1px solid #e0e0e0; 
                padding: 20px; 
                margin-bottom: 20px; 
                border-radius: 8px; 
            }}
            .form-group {{ 
                margin-bottom: 15px; 
            }}
            label {{ 
                display: block; 
                margin-bottom: 5px; 
                font-weight: bold; 
                color: #555; 
            }}
            .required:after {{ 
                content: " *"; 
                color: red; 
            }}
            input, select, textarea {{ 
                width: 100%; 
                padding: 10px; 
                border: 1px solid #ddd; 
                border-radius: 5px; 
                box-sizing: border-box; 
            }}
            .btn {{ 
                padding: 12px 25px; 
                border: none; 
                border-radius: 5px; 
                cursor: pointer; 
                font-size: 16px; 
                margin-right: 10px; 
                text-decoration: none;
                display: inline-block;
            }}
            .btn-primary {{ 
                background: #28a745; 
                color: white; 
            }}
            .btn-secondary {{ 
                background: #6c757d; 
                color: white; 
            }}
            .document-list {{
                background: #f8f9fa;
                padding: 15px;
                border-radius: 5px;
                margin: 10px 0;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>✏️ Resubmit Submission #{listing_id}</h1>
                <div>
                    <a href="/agent/submission/{listing_id}" class="btn btn-secondary">← Back to Submission</a>
                </div>
            </div>
            
            <div class="rejection-alert">
                <h3 style="margin-top: 0;"> Previous Rejection Reason:</h3>
                <p><strong>{sub_data['rejection_reason'] or 'No reason provided'}</strong></p>
                <p>Please fix the issues mentioned above before resubmitting.</p>
            </div>
            
            <form method="POST" action="/agent/resubmit/{listing_id}" enctype="multipart/form-data">
                
                <!-- Customer Information -->
                <div class="form-section">
                    <h2>👤 Customer Information</h2>
                    <div class="existing-data">
                        <strong>Current Data:</strong> {sub_data['customer_name']} | {sub_data['customer_email']} | {sub_data['customer_phone'] or 'No phone'}
                    </div>
                    <div class="form-group">
                        <label class="required">Customer Name</label>
                        <input type="text" name="customer_name" value="{sub_data['customer_name']}" required>
                    </div>
                    <div class="form-group">
                        <label class="required">Customer Email</label>
                        <input type="email" name="customer_email" value="{sub_data['customer_email']}" required>
                    </div>
                    <div class="form-group">
                        <label>Customer Phone</label>
                        <input type="tel" name="customer_phone" value="{sub_data['customer_phone'] or ''}">
                    </div>
                </div>
                
                <!-- Property Details -->
                <div class="form-section">
                    <h2>🏠 Property Details</h2>
                    <div class="existing-data">
                        <strong>Current Data:</strong> RM{"{:,.2f}".format(sub_data['sale_price'])} | {sub_data['closing_date'] or 'No closing date'}
                    </div>
                    <div class="form-group">
                        <label class="required">Property Address</label>
                        <textarea name="property_address" rows="3" required>{sub_data['property_address']}</textarea>
                    </div>
                    
                    <div class="form-group">
                        <label class="required">Sale Price (RM)</label>
                        <input type="number" name="sale_price" value="{sub_data['sale_price']}" min="0" step="1000" required>
                    </div>
                    <div class="form-group">
                        <label>Closing Date</label>
                        <input type="date" name="closing_date" value="{sub_data['closing_date'] or ''}">
                    </div>
                </div>
                
                <!-- Document Update -->
                <div class="form-section">
                    <h2>📎 Update Documents</h2>
                    <div class="existing-data">
                        <strong>Existing Documents:</strong> {len(existing_docs)} file(s) uploaded
                        <div class="document-list">
                            {'<br>'.join([f'📄 {doc}' for doc in existing_docs[:5]])}
                            {f'<br>... and {len(existing_docs) - 5} more' if len(existing_docs) > 5 else ''}
                        </div>
                    </div>
                    <p style="color: #666; margin-bottom: 15px;">
                        You can upload new documents or replacements. Existing documents will be kept unless you upload files with the same names.
                    </p>
                    
                    <div class="form-group">
                        <label>Update Agreement</label>
                        <input type="file" name="agreement" accept=".pdf,.doc,.docx,.jpg,.jpeg,.png">
                        <small>Upload new version if needed</small>
                    </div>
                    
                    <div class="form-group">
                        <label>Update ID Proof</label>
                        <input type="file" name="id_proof" accept=".pdf,.jpg,.jpeg,.png">
                        <small>Upload new version if needed</small>
                    </div>
                    
                    <div class="form-group">
                        <label>Additional Documents</label>
                        <input type="file" name="additional_docs" multiple accept=".pdf,.doc,.docx,.jpg,.jpeg,.png">
                        <small>Hold CTRL to select multiple files</small>
                    </div>
                </div>
                
                <!-- Additional Information -->
                <div class="form-section">
                    <h2>📋 Additional Information</h2>
                    <div class="form-group">
                        <label>Special Notes</label>
                        <textarea name="notes" rows="4" placeholder="Any special conditions, requirements, or notes...">{sub_data['notes']}</textarea>
                    </div>
                </div>
                
                <div style="margin-top: 30px; text-align: center;">
                    <button type="submit" class="btn btn-primary">✅ Submit for Review</button>
                    <a href="/agent/submission/{listing_id}" class="btn btn-secondary">Cancel</a>
                    <a href="/new-listing" class="btn" style="background: #007bff; color: white;">➕ Create New Instead</a>
                </div>
            </form>
        </div>
    </body>
    </html>
    '''
    
    return resubmit_template

@app.route('/agent/commissions')
def agent_commissions():
    """Agent commission tracking - IMPROVED VERSION"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get commission summary - REMOVED property_type
    cursor.execute('''
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
        WHERE pl.agent_id = ? AND pl.status = 'approved'
        ORDER BY pl.approved_at DESC
    ''', (session['user_id'],))
    
    commissions = cursor.fetchall()
    
    # Calculate totals
    cursor.execute('''
        SELECT 
            SUM(commission_amount) as total_approved,
            COUNT(*) as total_count
        FROM property_listings 
        WHERE agent_id = ? AND status = 'approved'
    ''', (session['user_id'],))
    
    totals = cursor.fetchone()
    
    conn.close()
    
    # Create a properly formatted commissions list - REMOVED property_type
    commissions_list = []
    for comm in commissions:
        commissions_list.append({
            'id': comm[0],
            'customer_name': comm[1],
            'agent_name': comm[2],
            'sale_price': float(comm[3]) if comm[3] else 0,
            'commission_amount': float(comm[4]) if comm[4] else 0,
            'status': comm[5],
            'approved_at': comm[6]
        })
    
    # Calculate totals safely
    total_approved = float(totals[0]) if totals and totals[0] else 0
    total_count = totals[1] if totals and totals[1] else 0
    
    commission_template = '''<!DOCTYPE html>
<html>
<head>
    <title>My Commissions</title>
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
        .empty-state {
            padding: 40px;
            text-align: center;
            background: white;
            border-radius: 10px;
            color: #666;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>💰 My Commissions</h1>
        <div>
            <a href="/agent/dashboard" class="btn">← Dashboard</a>
            <a href="/agent/submissions" class="btn" style="background: #28a745;">📋 My Submissions</a>
        </div>
    </div>
    
    <div class="stats">
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Total Commission</div>
            <div class="stat-value">RM{% if total_approved %}{{ "{:,.2f}".format(total_approved) }}{% else %}0.00{% endif %}</div>
        </div>
        <div class="stat-card">
            <div style="font-size: 14px; color: #666;">Approved Sales</div>
            <div class="stat-value">{{ total_count }}</div>
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
    <div class="empty-state">
        <h3>No approved commissions yet</h3>
        <p>Once your submissions are approved by admin, they will appear here.</p>
        <a href="/agent/submissions" class="btn" style="background: #28a745; margin-top: 15px;">View My Submissions</a>
    </div>
    {% endif %}
</body>
</html>'''
    
    return render_template_string(commission_template, 
                                 commissions_list=commissions_list,
                                 total_approved=total_approved,
                                 total_count=total_count)

@app.route('/agent/projects')
def agent_projects():
    """Agent view of projects they've worked on"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get projects the agent has worked on
    cursor.execute('''
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
    ''', (session['user_id'],))
    
    projects = cursor.fetchall()
    
    conn.close()
    
    projects_template = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>My Projects</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .project-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; margin: 20px 0; }
            .project-card { background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .project-header { padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
            .project-body { padding: 20px; }
            .project-meta { display: flex; justify-content: space-between; margin: 10px 0; font-size: 14px; }
            .badge { padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }
            .badge-sales { background: #d4edda; color: #155724; }
            .badge-rental { background: #cce5ff; color: #004085; }
            .btn { padding: 8px 16px; border-radius: 5px; text-decoration: none; display: inline-block; margin-right: 5px; font-size: 14px; }
            .btn-view { background: #17a2b8; color: white; }
            .empty-state { text-align: center; padding: 50px 20px; background: white; border-radius: 10px; color: #666; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🏢 My Projects</h1>
            <div>
                <a href="/agent/dashboard" class="btn" style="background: #6c757d; color: white;">← Dashboard</a>
                <a href="/new-listing" class="btn" style="background: #28a745; color: white;">➕ New Sale</a>
            </div>
        </div>
        
        {% if projects %}
        <div class="project-grid">
            {% for project in projects %}
            <div class="project-card">
                <div class="project-header">
                    <h3 style="margin: 0;">{{ project[1] }}</h3>
                    <div style="margin-top: 5px;">
                        <span class="badge badge-{{ project[2] }}">{{ project[2]|title }}</span>
                        <span class="badge" style="background: #fff3cd; color: #856404;">{{ project[3]|title }}</span>
                    </div>
                </div>
                <div class="project-body">
                    <div class="project-meta">
                        <div>
                            <strong>Location:</strong><br>
                            {{ project[4] or 'Not specified' }}
                        </div>
                        <div>
                            <strong>Commission Rate:</strong><br>
                            {{ project[5] or 'N/A' }}%
                        </div>
                    </div>
                    
                    <div class="project-meta">
                        <div>
                            <strong>Total Sales:</strong><br>
                            {{ project[6] or 0 }}
                        </div>
                        <div>
                            <strong>Total Value:</strong><br>
                            RM{{ "{:,.2f}".format(project[7] or 0) }}
                        </div>
                    </div>
                    
                    <div class="project-meta">
                        <div>
                            <strong>Total Commission:</strong><br>
                            <span style="color: #28a745; font-weight: bold;">RM{{ "{:,.2f}".format(project[8] or 0) }}</span>
                        </div>
                        <div>
                            <strong>Last Sale:</strong><br>
                            {{ project[9][:10] if project[9] else 'Never' }}
                        </div>
                    </div>
                    
                    <div style="margin-top: 15px;">
                        <a href="/agent/project-sales/{{ project[0] }}" class="btn btn-view">📊 View Sales</a>
                        <a href="/new-listing?project_id={{ project[0] }}" class="btn" style="background: #007bff; color: white;">➕ New Sale</a>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div class="empty-state">
            <h3>No project sales yet</h3>
            <p>You haven't made any sales from projects yet.</p>
            <div style="margin-top: 20px;">
                <a href="/new-listing" class="btn" style="background: #28a745; color: white; padding: 10px 20px;">Make Your First Sale</a>
            </div>
        </div>
        {% endif %}
    </body>
    </html>
    '''
    
    return render_template_string(projects_template, projects=projects)

@app.route('/agent/project-sales/<int:project_id>')
def agent_project_sales(project_id):
    """View sales for a specific project"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Verify agent has access to this project
    cursor.execute('''
        SELECT p.project_name, p.category, p.project_type, p.location
        FROM projects p
        JOIN property_listings pl ON p.id = pl.project_id
        WHERE p.id = ? AND pl.agent_id = ?
        LIMIT 1
    ''', (project_id, session['user_id']))
    
    project = cursor.fetchone()
    
    if not project:
        conn.close()
        return "Project not found or access denied", 404
    
    # Get all sales for this project by this agent
    cursor.execute('''
        SELECT pl.*, pu.unit_type
        FROM property_listings pl
        LEFT JOIN project_units pu ON pl.unit_id = pu.id
        WHERE pl.project_id = ? AND pl.agent_id = ?
        ORDER BY pl.created_at DESC
    ''', (project_id, session['user_id']))
    
    sales = cursor.fetchall()
    
    conn.close()
    
    # Build the sales rows HTML - Adjusted indices
    sales_rows = ""
    if sales:
        for sale in sales:
            unit_type = sale[19] if len(sale) > 19 and sale[19] else 'N/A'  # Changed from 20 to 19
            status = sale[2] if sale[2] else 'draft'
            created_at = sale[12][:10] if sale[12] else ''  # Changed from 13 to 12
            
            sales_rows += f'''
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
            '''
    
    # Create the template
    project_sales_template = f'''<!DOCTYPE html>
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
'''
    
    # Add table or empty state
    if sales:
        project_sales_template += f'''
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
'''
    else:
        project_sales_template += f'''
    <div style="padding: 40px; text-align: center; background: white; border-radius: 10px;">
        <h3>No sales yet for this project</h3>
        <p>You haven't made any sales for this project yet.</p>
        <a href="/new-listing?project_id={project_id}" class="btn" style="background: #28a745; color: white; margin-top: 15px;">Make Your First Sale</a>
    </div>
'''
    
    # Close the HTML
    project_sales_template += '''
</body>
</html>'''
    
    return project_sales_template

@app.route('/agent/performance')
def agent_performance():
    """Agent performance analytics"""
    if 'user_id' not in session or session['user_role'] != 'agent':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Monthly performance
    cursor.execute('''
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
    ''', (session['user_id'],))
    
    monthly_stats = cursor.fetchall()
    
    # Property type breakdown
    cursor.execute('''
        SELECT 
            property_type,
            COUNT(*) as count,
            AVG(sale_price) as avg_price,
            SUM(commission_amount) as total_commission
        FROM property_listings 
        WHERE agent_id = ? AND status = 'approved'
        GROUP BY property_type
    ''', (session['user_id'],))
    
    property_breakdown = cursor.fetchall()
    
    conn.close()
    
    # Return performance dashboard
    performance_template = '''
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
    '''
    
    # Calculate stats
    total_submissions = len(monthly_stats)
    total_commission = sum([m[3] or 0 for m in monthly_stats])
    avg_monthly = total_commission / max(total_submissions, 1)
    
    # Prepare chart data
    monthly_labels = [m[0] for m in monthly_stats][::-1]
    monthly_commissions = [m[3] or 0 for m in monthly_stats][::-1]
    
    return render_template_string(performance_template,
        monthly_stats=monthly_stats,
        property_breakdown=property_breakdown,
        avg_monthly=avg_monthly,
        success_rate=75,  # Calculate this from your data
        avg_sale_price=500000,  # Calculate this
        top_property_type='Residential',
        monthly_labels=json.dumps(monthly_labels),
        monthly_commissions=json.dumps(monthly_commissions))

# ============ COMPLETE ADMIN SYSTEM ============
@app.route('/admin/dashboard')
def admin_dashboard():
    """Admin dashboard - shows all submissions with filtering"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get filter parameters
    status_filter = request.args.get('status', 'all')
    type_filter = request.args.get('type', 'all')
    search_query = request.args.get('search', '')
    
    # Build query based on filters
    query = '''
        SELECT pl.id, pl.agent_id, pl.status, pl.customer_name, pl.customer_email, 
               pl.customer_phone, pl.property_address, pl.sale_price, pl.closing_date,
               pl.commission_amount, pl.commission_status, pl.created_at, pl.submitted_at,
               pl.approved_at, pl.approved_by, pl.notes, pl.metadata, pl.rejection_reason,
               pl.project_id, pl.unit_id, pl.sale_type, u.name as agent_name,
               (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id) as document_count
        FROM property_listings pl
        JOIN users u ON pl.agent_id = u.id
        WHERE 1=1
    '''
    
    params = []
    
    # Apply status filter
    if status_filter == 'submitted':
        query += ' AND pl.status = ?'
        params.append('submitted')
    elif status_filter == 'approved':
        query += ' AND pl.status = ?'
        params.append('approved')
    elif status_filter == 'rejected':
        query += ' AND pl.status = ?'
        params.append('rejected')
    elif status_filter == 'draft':
        query += ' AND (pl.status = ? OR pl.status IS NULL)'
        params.append('draft')
    # 'all' shows everything
    
    # Apply type filter
    if type_filter == 'sales':
        query += ' AND pl.sale_type = ?'
        params.append('sales')
    elif type_filter == 'rental':
        query += ' AND pl.sale_type = ?'
        params.append('rental')
    # 'all' shows both types
    
    # Apply search filter
    if search_query:
        query += ' AND (pl.customer_name LIKE ? OR pl.property_address LIKE ? OR u.name LIKE ?)'
        search_term = f'%{search_query}%'
        params.extend([search_term, search_term, search_term])
    
    query += ' ORDER BY pl.created_at DESC'
    
    cursor.execute(query, params)
    all_submissions = cursor.fetchall()
    
    # Get pending submissions count (for separate display)
    cursor.execute('''
        SELECT COUNT(*) FROM property_listings WHERE status = 'submitted'
    ''')
    pending_count = cursor.fetchone()[0] or 0
    
    # Get all listings for stats
    cursor.execute('''
        SELECT 
            COUNT(*) as total_listings,
            SUM(CASE WHEN sale_type = 'sales' THEN sale_price ELSE 0 END) as total_sales,
            SUM(CASE WHEN sale_type = 'rental' THEN sale_price ELSE 0 END) as total_rentals,
            SUM(commission_amount) as total_commissions,
            SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) as approved,
            SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejected,
            SUM(CASE WHEN status = 'draft' OR status IS NULL THEN 1 ELSE 0 END) as draft,
            SUM(CASE WHEN sale_type = 'sales' THEN 1 ELSE 0 END) as sales_count,
            SUM(CASE WHEN sale_type = 'rental' THEN 1 ELSE 0 END) as rentals_count
        FROM property_listings
    ''')
    stats = cursor.fetchone()
    
    # Get total agents
    cursor.execute('SELECT COUNT(*) FROM users WHERE role = "agent"')
    total_agents = cursor.fetchone()[0]
    
    # Get today's submissions
    cursor.execute('''
        SELECT COUNT(*) FROM property_listings 
        WHERE DATE(created_at) = DATE('now')
    ''')
    todays_submissions = cursor.fetchone()[0]
    
    # Commission calculations
    total_commissions = stats[3] if stats and stats[3] else 0

    # Calculate commissions using ACTUAL upline amounts from database
    cursor.execute('SELECT SUM(amount) FROM upline_commissions')
    actual_upline_result = cursor.fetchone()
    upline_commissions = actual_upline_result[0] if actual_upline_result and actual_upline_result[0] else 0

    # Agent commissions = total - actual upline
    agent_commissions = total_commissions - upline_commissions

    # Get total paid from commission_payments table
    cursor.execute('SELECT SUM(commission_amount) FROM commission_payments WHERE payment_status = "paid"')
    paid_result = cursor.fetchone()
    total_paid = paid_result[0] if paid_result and paid_result[0] else 0

    # Calculate balance (what's still unpaid)
    balance = total_commissions - total_paid

    conn.close()
    
    # Prepare data for template
    submissions_list = []
    for sub in all_submissions:
        submissions_list.append({
            'id': sub[0],
            'agent_id': sub[1],
            'status': sub[2] or 'draft',
            'customer_name': sub[3],
            'customer_email': sub[4],
            'customer_phone': sub[5],
            'property_address': sub[6],
            'sale_price': sub[7],
            'closing_date': sub[8],
            'commission_amount': sub[9],
            'commission_status': sub[10],
            'created_at': sub[11],
            'submitted_at': sub[12],
            'approved_at': sub[13],
            'approved_by': sub[14],
            'notes': sub[15],
            'metadata': sub[16],
            'rejection_reason': sub[17],
            'project_id': sub[18],
            'unit_id': sub[19],
            'sale_type': sub[20] or 'sales',
            'agent_name': sub[21],
            'document_count': sub[22] or 0
        })
    
    stats_dict = {
        'total_listings': stats[0] if stats else 0,
        'total_sales': stats[1] if stats and stats[1] else 0,
        'total_rentals': stats[2] if stats and stats[2] else 0,
        'total_commissions': total_commissions,
        'agent_commissions': agent_commissions,
        'upline_commissions': upline_commissions,
        'total_paid': total_paid,
        'balance': balance,
        'approved': stats[4] if stats else 0,
        'pending': stats[5] if stats else 0,
        'rejected': stats[6] if stats else 0,
        'draft': stats[7] if stats else 0,
        'sales_count': stats[8] if stats else 0,
        'rentals_count': stats[9] if stats else 0
    }
    
    # Build the HTML template
    admin_template = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Dashboard</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .stats { display: flex; gap: 12px; margin: 15px 0; flex-wrap: wrap; }
            .stat-card { background: white; padding: 12px; border-radius: 8px; flex: 1; min-width: 140px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
            .stat-card h3 { margin-top: 0; color: #555; font-size: 13px; margin-bottom: 8px; }
            .stat-value { font-size: 1.5em; font-weight: bold; margin-bottom: 5px; }
            .stat-card small { font-size: 11px; color: #666; line-height: 1.3; }
            table { width: 100%; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); margin: 20px 0; }
            th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }
            th { background: #2c3e50; color: white; }
            .btn { padding: 8px 15px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
            .nav { margin: 20px 0; padding: 15px; background: white; border-radius: 10px; }
            .nav a { margin-right: 15px; color: #007bff; text-decoration: none; font-weight: bold; }
            .positive { color: #28a745; }
            .negative { color: #dc3545; }
            .neutral { color: #007bff; }
            .quick-actions { display: flex; gap: 10px; margin: 20px 0; flex-wrap: wrap; }
            .action-btn { padding: 12px 20px; background: white; border-radius: 8px; text-decoration: none; color: #333; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
            .action-btn:hover { background: #f8f9fa; }
            .filters { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
            .filter-group { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
            select, input[type="text"] { padding: 8px 12px; border: 1px solid #ddd; border-radius: 5px; }
            .badge-sales { background: #28a745; color: white; padding: 3px 8px; border-radius: 12px; font-size: 11px; }
            .badge-rental { background: #17a2b8; color: white; padding: 3px 8px; border-radius: 12px; font-size: 11px; }
            .status-badge { padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
            .status-draft { background: #6c757d; color: white; }
            .status-submitted { background: #007bff; color: white; }
            .status-approved { background: #28a745; color: white; }
            .status-rejected { background: #dc3545; color: white; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>👑 Admin Dashboard</h1>
            <p>Welcome, {{ admin_name }}! | <a href="/logout" style="color: #dc3545;">Logout</a></p>
        </div>
        
        <div class="nav">
            <a href="/admin/dashboard">📊 Dashboard</a>
            <a href="/admin/projects">🏢 Projects</a>
            <a href="/admin/create-project" style="background: #007bff; color: white; padding: 5px 10px; border-radius: 3px;">➕ Create Project</a>
            <a href="/admin/agents">👥 Manage Agents</a>
            <a href="/admin/commissions">💰 Commissions</a>
            <a href="/admin/payments">💰 Payments</a>
            <a href="/admin/sync-payments" style="background: #28a745; color: white; padding: 5px 10px; border-radius: 3px;">🔄 Sync Payments</a>
            <a href="/admin/agent-performance">📈 Agent Performance</a>
            <a href="/admin/export-data">📤 Export Data</a>
            <a href="/admin/reports">📈 Reports</a>
            <a href="/admin/settings">⚙️ Settings</a>
        </div>
        
        <!-- ============ STATS SECTION ============ -->
        <div class="stats">
            <div class="stat-card">
                <h3>Total Listings</h3>
                <div class="stat-value">{{ stats.total_listings }}</div>
                <small>Sales: {{ stats.sales_count }} | Rentals: {{ stats.rentals_count }}</small>
            </div>
            <div class="stat-card">
                <h3>Total Sales Value</h3>
                <div class="stat-value">RM{{ "{:,.2f}".format(stats.total_sales or 0) }}</div>
                <small>Total property sales</small>
            </div>
            <div class="stat-card">
                <h3>Total Rentals Value</h3>
                <div class="stat-value">RM{{ "{:,.2f}".format(stats.total_rentals or 0) }}</div>
                <small>Monthly rental value</small>
            </div>
            <div class="stat-card">
                <h3>Total Commissions</h3>
                <div class="stat-value" style="color: #007bff;">RM{{ "{:,.2f}".format(stats.total_commissions or 0) }}</div>
                <small>Generated from all listings</small>
            </div>
            <div class="stat-card">
                <h3>Pending Approval</h3>
                <div class="stat-value" style="color: #ffc107;">{{ stats.pending }}</div>
                <small>{{ pending_count }} need review</small>
            </div>
            <div class="stat-card">
                <h3>Approved</h3>
                <div class="stat-value" style="color: #28a745;">{{ stats.approved }}</div>
                <small>Completed listings</small>
            </div>
            <div class="stat-card">
                <h3>Rejected</h3>
                <div class="stat-value" style="color: #dc3545;">{{ stats.rejected }}</div>
                <small>Declined listings</small>
            </div>
            <div class="stat-card">
                <h3>Drafts</h3>
                <div class="stat-value">{{ stats.draft }}</div>
                <small>Unsubmitted listings</small>
            </div>
        </div>
        
        <!-- ============ FILTERS SECTION ============ -->
        <div class="filters">
            <h3>Filter Submissions</h3>
            <form method="GET" class="filter-group">
                <select name="status">
                    <option value="all" {% if status_filter == "all" %}selected{% endif %}>All Status</option>
                    <option value="submitted" {% if status_filter == "submitted" %}selected{% endif %}>Pending ({{ stats.pending }})</option>
                    <option value="approved" {% if status_filter == "approved" %}selected{% endif %}>Approved ({{ stats.approved }})</option>
                    <option value="rejected" {% if status_filter == "rejected" %}selected{% endif %}>Rejected ({{ stats.rejected }})</option>
                    <option value="draft" {% if status_filter == "draft" %}selected{% endif %}>Drafts ({{ stats.draft }})</option>
                </select>
                
                <select name="type">
                    <option value="all" {% if type_filter == "all" %}selected{% endif %}>All Types</option>
                    <option value="sales" {% if type_filter == "sales" %}selected{% endif %}>Sales ({{ stats.sales_count }})</option>
                    <option value="rental" {% if type_filter == "rental" %}selected{% endif %}>Rentals ({{ stats.rentals_count }})</option>
                </select>
                
                <input type="text" name="search" placeholder="Search by customer, property, or agent..." value="{{ search_query }}">
                
                <button type="submit" class="btn" style="background: #007bff; color: white;">🔍 Filter</button>
                <a href="/admin/dashboard" class="btn" style="background: #6c757d; color: white;">Clear</a>
            </form>
        </div>
        
        <!-- ============ SUBMISSIONS TABLE ============ -->
        <h2>📋 All Submissions ({{ submissions_list|length }})</h2>
        
        {% if submissions_list %}
        <div style="margin-bottom: 20px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
            <div style="display: flex; align-items: center; gap: 5px;">
                <div style="width: 12px; height: 12px; background: #dc3545; border-radius: 50%;"></div>
                <small>Incomplete Docs (0-2)</small>
            </div>
            <div style="display: flex; align-items: center; gap: 5px;">
                <div style="width: 12px; height: 12px; background: #ffc107; border-radius: 50%;"></div>
                <small>Minimum Docs (3)</small>
            </div>
            <div style="display: flex; align-items: center; gap: 5px;">
                <div style="width: 12px; height: 12px; background: #28a745; border-radius: 50%;"></div>
                <small>Complete Docs (4+)</small>
            </div>
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Type</th>
                    <th>Agent</th>
                    <th>Customer</th>
                    <th>Property</th>
                    <th>Amount</th>
                    <th>Commission</th>
                    <th>Documents</th>
                    <th>Status</th>
                    <th>Submitted</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for sub in submissions_list %}
                <tr {% if sub.document_count <= 2 %}style="background: #fff5f5; border-left: 4px solid #dc3545;"{% elif sub.document_count == 3 %}style="background: #fff9e6; border-left: 4px solid #ffc107;"{% else %}style="background: #f0fff4; border-left: 4px solid #28a745;"{% endif %}>
                    <td>#{{ sub.id }}</td>
                    <td>
                        {% if sub.sale_type == 'rental' %}
                        <span class="badge-rental">RENTAL</span>
                        {% else %}
                        <span class="badge-sales">SALE</span>
                        {% endif %}
                    </td>
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
                        {{ (sub.property_address or '')[:25] }}{% if (sub.property_address or '')|length > 25 %}...{% endif %}
                        {% if sub.project_id %}
                        <br>
                        <small style="background: #e8f4ff; color: #0066cc; padding: 2px 6px; border-radius: 3px; font-size: 11px;">Project: {{ sub.project_id }}</small>
                        {% endif %}
                    </td>
                    <td>
                        {% if sub.sale_type == 'rental' %}
                        RM{{ "{:,.2f}".format(sub.sale_price) }}/month
                        {% else %}
                        RM{{ "{:,.2f}".format(sub.sale_price) }}
                        {% endif %}
                    </td>
                    <td>RM{{ "{:,.2f}".format(sub.commission_amount or 0) }}</td>
                    <td>
                        {% if sub.document_count == 0 %}
                            <span style="color: #dc3545; font-weight: bold;">❌ 0</span>
                        {% elif sub.document_count == 1 %}
                            <span style="color: #dc3545; font-weight: bold;">⚠️ 1</span>
                        {% elif sub.document_count == 2 %}
                            <span style="color: #ffc107; font-weight: bold;">⚠️ 2</span>
                        {% elif sub.document_count == 3 %}
                            <span style="color: #28a745; font-weight: bold;">✓ 3</span>
                        {% else %}
                            <span style="color: #28a745; font-weight: bold;">✅ {{ sub.document_count }}</span>
                        {% endif %}
                    </td>
                    <td>
                        <span class="status-badge status-{{ sub.status }}">
                            {{ sub.status|upper }}
                        </span>
                        {% if sub.approved_at %}
                        <br>
                        <small style="color: #666;">{{ sub.approved_at[:10] }}</small>
                        {% endif %}
                    </td>
                    <td>{{ (sub.submitted_at or '')[:10] if sub.submitted_at else 'N/A' }}</td>
                    <td>
                        <div style="display: flex; flex-direction: column; gap: 5px;">
                            <a href="/admin/documents/{{ sub.id }}" class="btn" style="padding: 6px 12px; background: #6f42c1; color: white; text-decoration: none; border-radius: 4px; font-size: 12px; text-align: center;">
                                📎 Docs
                            </a>
                            
                            {% if sub.status == 'submitted' %}
                                {% if sub.document_count >= 3 %}
                                <a href="/admin/approve/{{ sub.id }}" class="btn" style="padding: 6px 12px; background: #28a745; color: white; text-decoration: none; border-radius: 4px; font-size: 12px; text-align: center;">
                                    ✅ Approve
                                </a>
                                {% else %}
                                <button style="padding: 6px 12px; background: #6c757d; color: white; border: none; border-radius: 4px; font-size: 12px; cursor: not-allowed; opacity: 0.5;" disabled>
                                    ❌ Incomplete
                                </button>
                                {% endif %}
                                <a href="/admin/reject/{{ sub.id }}" class="btn" style="padding: 6px 12px; background: #dc3545; color: white; text-decoration: none; border-radius: 4px; font-size: 12px; text-align: center;">
                                    ❌ Reject
                                </a>
                            {% elif sub.status == 'draft' %}
                                <button style="padding: 6px 12px; background: #6c757d; color: white; border: none; border-radius: 4px; font-size: 12px; cursor: not-allowed; opacity: 0.5;" disabled>
                                    Draft
                                </button>
                            {% elif sub.status == 'approved' %}
                                <button style="padding: 6px 12px; background: #28a745; color: white; border: none; border-radius: 4px; font-size: 12px; cursor: not-allowed;" disabled>
                                    ✅ Approved
                                </button>
                            {% elif sub.status == 'rejected' %}
                                <button style="padding: 6px 12px; background: #dc3545; color: white; border: none; border-radius: 4px; font-size: 12px; cursor: not-allowed;" disabled>
                                    ❌ Rejected
                                </button>
                            {% endif %}
                        </div>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <div style="padding: 40px; text-align: center; background: white; border-radius: 10px;">
            <h3>📭 No submissions found</h3>
            <p>No submissions match your filters.</p>
            <a href="/admin/dashboard" class="btn" style="background: #007bff; color: white; margin-top: 15px;">Show All Submissions</a>
        </div>
        {% endif %}
        
        <!-- ============ QUICK ACTIONS ============ -->
        <h2>⚡ Quick Actions</h2>
        <div class="quick-actions">
            <a href="/admin/add-agent" class="action-btn">
                <div style="font-size: 24px;">➕</div>
                <div><strong>Add Agent</strong><br><small>Create new agent account</small></div>
            </a>
            <a href="/admin/commissions" class="action-btn">
                <div style="font-size: 24px;">💰</div>
                <div><strong>Commission Report</strong><br><small>View all commissions</small></div>
            </a>
            <a href="/admin/export-data" class="action-btn">
                <div style="font-size: 24px;">📤</div>
                <div><strong>Export Data</strong><br><small>Export to Excel/CSV</small></div>
            </a>
            <a href="/admin/agent-performance" class="action-btn">
                <div style="font-size: 24px;">📊</div>
                <div><strong>Agent Performance</strong><br><small>View agent statistics</small></div>
            </a>
        </div>
    </body>
    </html>
    '''
    
    return render_template_string(admin_template,
        admin_name=session.get('user_name'),
        submissions_list=submissions_list,
        pending_count=pending_count,
        stats=stats_dict,
        status_filter=status_filter,
        type_filter=type_filter,
        search_query=search_query,
        total_agents=total_agents,
        todays_submissions=todays_submissions)

# ============ ADD THIS TO ADMIN DASHBOARD (in the Pending Submissions table) ============

@app.route('/admin/move-to-draft/<int:listing_id>')
def move_to_draft(listing_id):
    """Admin move submission back to draft so agent can reupload documents"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    try:
        # Get listing details for notification
        cursor.execute('SELECT agent_id FROM property_listings WHERE id = ?', (listing_id,))
        listing = cursor.fetchone()
        
        if not listing:
            conn.close()
            return redirect(f'/admin/documents/{listing_id}?error=Listing+not+found')
        
        agent_id = listing[0]
        
        # Update status to draft
        cursor.execute('''
            UPDATE property_listings 
            SET status = 'draft'
            WHERE id = ?
        ''', (listing_id,))
        
        conn.commit()
        conn.close()
        
        # Send notification to agent
        admin_name = session.get('user_name', 'Admin')
        notify_agent_status_change(listing_id, agent_id, 'draft', admin_name)
        
        return redirect(f'/admin/documents/{listing_id}?success=Submission+moved+to+draft.+Agent+can+now+reupload+documents.')
        
    except Exception as e:
        conn.rollback()
        conn.close()
        return redirect(f'/admin/documents/{listing_id}?error=Error:+{str(e)}')

# ============ ENHANCED DOCUMENT VIEW PAGE WITH ADMIN ACTIONS ============

@app.route('/admin/documents/<int:listing_id>')
def view_documents(listing_id):
    """Admin view documents with status change option"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get listing details with agent name
    cursor.execute('''
        SELECT pl.*, u.name as agent_name, u.email as agent_email
        FROM property_listings pl
        LEFT JOIN users u ON pl.agent_id = u.id
        WHERE pl.id = ?
    ''', (listing_id,))
    listing = cursor.fetchone()
    
    # Get uploaded documents
    cursor.execute('''
        SELECT * FROM documents 
        WHERE listing_id = ? 
        ORDER BY uploaded_at DESC
    ''', (listing_id,))
    documents = cursor.fetchall()
    
    conn.close()
    
    if not listing:
        return "Listing not found", 404
    
    # Get success/error messages
    success_msg = request.args.get('success')
    error_msg = request.args.get('error')
    
    # Prepare document data
    docs_list = []
    for doc in documents:
        docs_list.append({
            'id': doc[0],
            'filename': doc[2],
            'filepath': doc[3],
            'file_type': doc[4].lower() if doc[4] else 'unknown',
            'file_size': doc[5],
            'uploaded_at': doc[7],
            'notes': doc[9]
        })
    
    # Create enhanced document view template with admin actions
    enhanced_doc_template = '''
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
    '''
    
    return render_template_string(enhanced_doc_template,
        listing_id=listing_id,
        customer_name=listing[3] if listing else 'Unknown',
        customer_email=listing[4] if listing else 'Unknown',
        customer_phone=listing[5] if listing else '',
        agent_name=listing[18] if listing and len(listing) > 18 else 'Unknown',
        agent_email=listing[19] if listing and len(listing) > 19 else 'Unknown',
        property_address=listing[6] if listing else 'Unknown',
        status=listing[2] if listing else 'draft',
        sale_price=listing[7] if listing else 0,
        commission_amount=listing[9] if listing else 0,
        created_at=listing[11] if listing else '',
        submitted_at=listing[12] if listing else '',
        approved_at=listing[13] if listing else '',
        documents=docs_list,
        document_count=len(docs_list),
        get_file_icon=get_file_icon,
        format_file_size=format_file_size,
        can_preview_in_browser=can_preview_in_browser,
        success_msg=success_msg,
        error_msg=error_msg)

# ============ UPDATED PENDING SUBMISSIONS TABLE WITH DOCUMENT STATUS ============

admin_dashboard_table_section = '''
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
'''

# ============ UPDATE THE QUERY IN admin_dashboard() ============

# Update the pending submissions query in admin_dashboard() function:

updated_pending_query = '''
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
        '''

# ============ ADD NOTIFICATION TO AGENT WHEN STATUS CHANGED TO DRAFT ============

def notify_agent_status_change(listing_id, agent_id, new_status, admin_name):
    """Notify agent when admin changes their submission status"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get agent details
    cursor.execute('SELECT email, name FROM users WHERE id = ?', (agent_id,))
    agent = cursor.fetchone()
    
    if not agent:
        conn.close()
        return False
    
    agent_email, agent_name = agent
    
    # Get listing details
    cursor.execute('SELECT customer_name FROM property_listings WHERE id = ?', (listing_id,))
    listing = cursor.fetchone()
    customer_name = listing[0] if listing else 'Unknown'
    
    conn.close()
    
    # Create notification email
    subject = f"Submission #{listing_id} Status Updated"
    
    if new_status == 'draft':
        body = f"""
Dear {agent_name},

Your submission #{listing_id} (Customer: {customer_name}) has been updated by admin.

**New Status: DRAFT**

📋 **What This Means:**
- You can now reupload or update documents
- Please review the submission and upload any missing documents
- Resubmit when all documents are complete

🔧 **Required Action:**
1. Go to "My Submissions" page
2. Click on submission #{listing_id}
3. Use the "Add/Replace Documents" button
4. Upload required documents
5. Resubmit for approval

📎 **Document Checklist:**
- ✅ Signed Sales & Purchase Agreement
- ✅ Customer ID Proof
- ✅ Property Title/Deed
- ✅ Commission Agreement (if separate)

**Changed by:** {admin_name}
**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}

Click here to access your submission: [Your Submissions]

If you have any questions, please contact the admin team.

Best regards,
Real Estate Commission System
"""
    else:
        # For other status changes
        body = f"""
Dear {agent_name},

Your submission #{listing_id} (Customer: {customer_name}) has been updated.

**New Status: {new_status.upper()}**

**Changed by:** {admin_name}
**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}

Click here to view your submission: [Your Submissions]

Best regards,
Real Estate Commission System
"""
    
    # Send email
    success, message = send_email(
        recipient_email=agent_email,
        recipient_name=agent_name,
        subject=subject,
        body=body,
        email_type='status_change',
        related_id=listing_id,
        related_type='listing'
    )
    
    return success

# ============ ADD WORKING ADMIN FEATURES ============
@app.route('/admin/agents')
def manage_agents():
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get all agents with multi-level commission info
    cursor.execute('''
        SELECT 
            u.id,
            u.name,
            u.email,
            u.upline_id,
            u.upline2_id,
            u.upline_commission_rate,
            u.upline2_commission_rate,
            u.commission_rate,
            u.total_listings,
            u.total_commission,
            u.created_at,
            upline.name as upline_name,
            upline.email as upline_email,
            upline2.name as upline2_name,
            upline2.email as upline2_email,
            (SELECT SUM(pl.commission_amount) FROM property_listings pl WHERE pl.agent_id = u.id AND pl.status = 'approved') as approved_commission
        FROM users u
        LEFT JOIN users upline ON u.upline_id = upline.id
        LEFT JOIN users upline2 ON u.upline2_id = upline2.id
        WHERE u.role = 'agent'
        ORDER BY u.id
    ''')
    
    agents = cursor.fetchall()
    conn.close()
    
    # Define the template string
    template = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Manage Agents</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .nav a { margin-right: 15px; color: #007bff; text-decoration: none; font-weight: bold; }
            table { width: 100%; background: white; border-radius: 10px; overflow: hidden; margin: 20px 0; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #eee; }
            th { background: #2c3e50; color: white; }
            .btn { padding: 5px 10px; border-radius: 4px; text-decoration: none; font-size: 12px; }
            .btn-edit { background: #17a2b8; color: white; }
            .btn-delete { background: #dc3545; color: white; }
            .upline-info { font-size: 12px; color: #666; }
            .commission-rate { 
                background: #fff3cd; 
                padding: 2px 6px; 
                border-radius: 3px; 
                font-size: 11px;
                color: #856404;
                display: inline-block;
                margin-right: 5px;
            }
            .commission-rate-2 { 
                background: #d1ecf1; 
                padding: 2px 6px; 
                border-radius: 3px; 
                font-size: 11px;
                color: #0c5460;
                display: inline-block;
            }
            .hierarchy-badge {
                display: inline-block;
                padding: 2px 8px;
                border-radius: 12px;
                font-size: 11px;
                font-weight: bold;
                margin-left: 5px;
            }
            .level-1 { background: #007bff; color: white; }
            .level-2 { background: #28a745; color: white; }
            .level-3 { background: #6f42c1; color: white; }
            .multi-level-info {
                font-size: 11px;
                color: #6c757d;
                margin-top: 3px;
            }
            .agent-level {
                font-size: 11px;
                font-weight: bold;
                padding: 1px 6px;
                border-radius: 3px;
                background: #f8f9fa;
                color: #495057;
            }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>👥 Manage Agents</h1>
            <div class="nav">
                <a href="/admin/dashboard">← Dashboard</a>
                <a href="/admin/add-agent">➕ Add New Agent</a>
                <a href="/admin/agent-hierarchy">📊 View Hierarchy</a>
                <a href="/logout">Logout</a>
            </div>
        </div>
        
        <div style="background: white; padding: 15px; border-radius: 10px; margin-bottom: 20px;">
            <h3>📋 Multi-Level Commission System:</h3>
            <div style="display: flex; gap: 20px; margin-top: 10px; flex-wrap: wrap;">
                <div>
                    <span class="hierarchy-badge level-1">L1</span> Top Level (No upline)<br>
                    <small>Earns: 0% from upline</small>
                </div>
                <div>
                    <span class="hierarchy-badge level-2">L2</span> Middle Level<br>
                    <small>Earns: 5% from direct downlines</small>
                </div>
                <div>
                    <span class="hierarchy-badge level-3">L3</span> Bottom Level<br>
                    <small>Pays: 5% to L2 + 2.5% to L1</small>
                </div>
                <div style="margin-left: auto;">
                    <div style="font-size: 12px; color: #666;">
                        <span class="commission-rate">5%</span> = Direct upline commission<br>
                        <span class="commission-rate-2">2.5%</span> = Indirect upline commission
                    </div>
                </div>
            </div>
        </div>
        
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Agent</th>
                    <th>Uplines</th>
                    <th>Commission Rates</th>
                    <th>Total Listings</th>
                    <th>Total Commission</th>
                    <th>Approved Comm</th>
                    <th>Joined</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for agent in agents %}
                <tr>
                    <td>{{ agent[0] }}</td>
                    <td>
                        <strong>{{ agent[1] }}</strong><br>
                        <small>{{ agent[2] }}</small><br>
                        {% if not agent[3] %}
                            <span class="hierarchy-badge level-1">L1</span>
                            <span class="agent-level">Top Level</span>
                        {% elif not agent[4] %}
                            <span class="hierarchy-badge level-2">L2</span>
                            <span class="agent-level">Middle Level</span>
                        {% else %}
                            <span class="hierarchy-badge level-3">L3</span>
                            <span class="agent-level">Bottom Level</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if agent[11] %}
                            <strong>Direct:</strong> {{ agent[11] }}<br>
                            <small class="upline-info">{{ agent[12] }}</small><br>
                        {% else %}
                            <span style="color: #999;">No direct upline</span><br>
                        {% endif %}
                        
                        {% if agent[13] %}
                            <strong>Indirect:</strong> {{ agent[13] }}<br>
                            <small class="upline-info">{{ agent[14] }}</small>
                        {% elif agent[4] %}
                            <span style="color: #999;">No indirect upline</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if agent[5] %}
                            <span class="commission-rate">{{ agent[5] }}%</span> to direct upline<br>
                        {% endif %}
                        {% if agent[6] %}
                            <span class="commission-rate-2">{{ agent[6] }}%</span> to indirect upline<br>
                        {% endif %}
                        <div class="multi-level-info">
                            Own rate: {{ agent[7] or 10 }}%
                        </div>
                    </td>
                    <td>{{ agent[8] or 0 }}</td>
                    <td>RM{{ "{:,.2f}".format(agent[9] or 0) }}</td>
                    <td>RM{{ "{:,.2f}".format(agent[15] or 0) }}</td>
                    <td>{{ agent[10][:10] if agent[10] else 'N/A' }}</td>
                    <td>
                        <a href="/admin/edit-agent/{{ agent[0] }}" class="btn btn-edit">✏️ Edit</a>
                        <a href="/admin/delete-agent/{{ agent[0] }}" class="btn btn-delete" onclick="return confirm('Are you sure you want to delete agent {{ agent[1] }}?')">🗑️ Delete</a>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </body>
    </html>
    '''
    
    return render_template_string(template, agents=agents)

@app.route('/admin/agent-hierarchy')
def agent_hierarchy():
    """View agent hierarchy tree with improved design"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get all agents with their uplines and downline info
    cursor.execute('''
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
            u1.upline_commission_rate,
            u1.upline2_commission_rate,
            u1.commission_rate,
            u1.created_at,
            (SELECT COUNT(*) FROM users u4 WHERE u4.upline_id = u1.id AND u4.role = 'agent') as downline_count,
            (SELECT COUNT(*) FROM property_listings pl WHERE pl.agent_id = u1.id) as total_listings,
            (SELECT SUM(pl.commission_amount) FROM property_listings pl WHERE pl.agent_id = u1.id AND pl.status = 'approved') as total_commission
        FROM users u1
        LEFT JOIN users u2 ON u1.upline_id = u2.id
        LEFT JOIN users u3 ON u1.upline2_id = u3.id
        WHERE u1.role = 'agent'
        ORDER BY u1.upline_id IS NULL DESC, u1.name
    ''')
    
    agents = cursor.fetchall()
    
    # Get all downline relationships
    cursor.execute('''
        SELECT upline_id, GROUP_CONCAT(name) as downline_names
        FROM users 
        WHERE role = 'agent' AND upline_id IS NOT NULL 
        GROUP BY upline_id
    ''')
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
                'id': agent[0],
                'name': agent[1],
                'email': agent[2],
                'upline_id': agent[3],
                'upline2_id': agent[4],
                'upline_name': agent[5],
                'upline_email': agent[6],
                'upline2_name': agent[7],
                'upline2_email': agent[8],
                'upline_commission_rate': agent[9],
                'upline2_commission_rate': agent[10],
                'commission_rate': agent[11],
                'join_date': agent[12],
                'downline_count': agent[13],
                'total_listings': agent[14],
                'total_commission': agent[15] or 0,
                'downlines': []  # Will be filled with child nodes
            }
    
        # Build tree by connecting downlines
        for agent_id, node in nodes.items():
            upline_id = node['upline_id']
            if upline_id and upline_id in nodes:
                nodes[upline_id]['downlines'].append(node)
        
        # Return top-level nodes (no upline)
        top_level = [node for node in nodes.values() if not node['upline_id']]
    
        # Sort by name
        top_level.sort(key=lambda x: x['name'])
    
        # Sort downlines recursively
        def sort_downlines(node):
            node['downlines'].sort(key=lambda x: x['name'])
            for downline in node['downlines']:
                sort_downlines(downline)
    
        for node in top_level:
            sort_downlines(node)
    
        return top_level
    
    hierarchy_tree = build_hierarchy_tree()
    
    # Render HTML tree
    def render_tree_html(agents_list, level=0, parent_id=None):
        html = ''
        for agent in agents_list:
            # Determine level-specific styling
            level_class = f"level-{min(level, 3)}"
            padding_left = level * 40  # Indent based on level
            
            # Calculate statistics
            total_downlines = agent['downline_count']
            commission_rate = agent['commission_rate'] or 0
            total_commission = agent['total_commission'] or 0
            
            # Determine if this agent has downlines
            has_downlines = len(agent['downlines']) > 0
            
            html += f'''
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
            
                        <div class="detail-row">
                            <div class="detail-item">
                                <span class="detail-label">Upline Comm Rate:</span>
                                <span class="detail-value">{agent['upline_commission_rate'] or 0}%</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Upline2 Comm Rate:</span>
                                <span class="detail-value">{agent['upline2_commission_rate'] or 0}%</span>
                            </div>
                        </div>
            
                        <div class="detail-row">
                            <div class="detail-item">
                                <span class="detail-label">Own Comm Rate:</span>
                                <span class="detail-value">{agent['commission_rate'] or 0}%</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Downlines:</span>
                                <span class="detail-value badge-downline">{total_downlines} agent(s)</span>
                            </div>
                        </div>
            
                        <div class="detail-row">
                            <div class="detail-item">
                                <span class="detail-label">Total Commission:</span>
                                <span class="detail-value" style="color: #28a745; font-weight: bold;">RM{float(total_commission):,.2f}</span>
                            </div>
                            <div class="detail-item">
                                <span class="detail-label">Listings:</span>
                                <span class="detail-value badge-listings">{agent['total_listings'] or 0}</span>
                            </div>
                       </div>
            
                       <div class="detail-row">
                           <div class="detail-item">
                               <span class="detail-label">Joined:</span>
                               <span class="detail-value">{agent['join_date'][:10] if agent['join_date'] else 'N/A'}</span>
                           </div>
                       </div>
            
                       {f'<div class="downline-preview" style="background: #e7f3ff;"><strong>Multi-Level Commission:</strong> Earns {agent["upline_commission_rate"] or 0}% from direct downlines + {agent["upline2_commission_rate"] or 0}% from 2nd-level downlines</div>' if agent['upline2_name'] else ''}
            
                       {f'<div class="downline-preview"><strong>Direct Downlines:</strong> {downline_groups.get(agent["id"], "None")}</div>' if downline_groups.get(agent["id"]) else ''}
                   </div>
        
                   {f'<div class="connector-line" style="left: {padding_left + 15}px;"></div>' if has_downlines else ''}
               </div>
            '''
            
            # Recursively render downlines
            if agent['downlines']:
                html += f'<div class="downline-container">'
                html += render_tree_html(agent['downlines'], level + 1, agent['id'])
                html += '</div>'
            
            html += '</div>'
        
        return html
    
    hierarchy_html = render_tree_html(hierarchy_tree) if hierarchy_tree else '''
    <div class="empty-state">
        <div class="empty-icon">👥</div>
        <h3>No Agents Found</h3>
        <p>There are no agents in the system yet. Add your first agent to start building your network.</p>
        <a href="/admin/add-agent" class="btn btn-primary">➕ Add First Agent</a>
    </div>
    '''
    
    # Calculate statistics - WITH TYPE CONVERSION
    total_agents = len(agents)
    top_level_count = sum(1 for agent in agents if agent[3] is None or agent[3] == '')
    with_downlines = sum(1 for agent in agents if agent[13] and int(agent[13]) > 0)
    total_commission = sum(float(agent[15] or 0) for agent in agents)
    
    # Create the template
    hierarchy_template = f'''<!DOCTYPE html>
<html>
<head>
    <title>Agent Hierarchy Network</title>
    <style>
        /* ============ BASE STYLES ============ */
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
            background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
            min-height: 100vh;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        
        /* ============ HEADER STYLES ============ */
        .header {{
            background: white;
            padding: 25px;
            border-radius: 15px;
            margin-bottom: 25px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            border-left: 5px solid #007bff;
        }}
        
        .header h1 {{
            margin: 0;
            color: #2c3e50;
            font-size: 28px;
            display: flex;
            align-items: center;
            gap: 15px;
        }}
        
        .header-actions {{
            display: flex;
            gap: 12px;
            margin-top: 15px;
            flex-wrap: wrap;
        }}
        
        .btn {{
            padding: 10px 20px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 500;
            transition: all 0.3s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            border: none;
            cursor: pointer;
        }}
        
        .btn-primary {{
            background: linear-gradient(135deg, #007bff 0%, #0056b3 100%);
            color: white;
        }}
        
        .btn-secondary {{
            background: #6c757d;
            color: white;
        }}
        
        .btn-success {{
            background: linear-gradient(135deg, #28a745 0%, #1e7e34 100%);
            color: white;
        }}
        
        .btn:hover {{
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
        }}
        
        /* ============ STATS STYLES ============ */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 25px 0;
        }}
        
        .stat-card {{
            background: white;
            padding: 20px;
            border-radius: 12px;
            text-align: center;
            box-shadow: 0 3px 10px rgba(0,0,0,0.1);
            transition: transform 0.3s ease;
        }}
        
        .stat-card:hover {{
            transform: translateY(-5px);
        }}
        
        .stat-icon {{
            font-size: 32px;
            margin-bottom: 10px;
        }}
        
        .stat-value {{
            font-size: 28px;
            font-weight: bold;
            margin: 10px 0;
        }}
        
        .stat-label {{
            color: #6c757d;
            font-size: 14px;
        }}
        
        /* ============ HIERARCHY STYLES ============ */
        .hierarchy-container {{
            background: white;
            padding: 30px;
            border-radius: 15px;
            margin: 25px 0;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            min-height: 500px;
            position: relative;
        }}
        
        .hierarchy-item {{
            margin-bottom: 15px;
            position: relative;
        }}
        
        .agent-card {{
            background: #f8f9fa;
            border-radius: 10px;
            padding: 20px;
            border: 1px solid #e0e0e0;
            transition: all 0.3s ease;
            position: relative;
        }}
        
        .agent-card:hover {{
            background: white;
            border-color: #007bff;
            box-shadow: 0 5px 15px rgba(0,123,255,0.2);
            transform: translateX(5px);
        }}
        
        .level-0 .agent-card {{
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
            border-left: 4px solid #007bff;
        }}
        
        .level-1 .agent-card {{
            background: linear-gradient(135deg, #e8f5e8 0%, #d4edda 100%);
            border-left: 4px solid #28a745;
        }}
        
        .level-2 .agent-card {{
            background: linear-gradient(135deg, #e3f2fd 0%, #bbdefb 100%);
            border-left: 4px solid #17a2b8;
        }}
        
        .level-3 .agent-card {{
            background: linear-gradient(135deg, #f3e5f5 0%, #e1bee7 100%);
            border-left: 4px solid #6f42c1;
        }}
        
        .agent-header {{
            display: flex;
            align-items: center;
            gap: 20px;
            margin-bottom: 15px;
        }}
        
        .agent-avatar {{
            position: relative;
            width: 60px;
            height: 60px;
            background: white;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 3px 10px rgba(0,0,0,0.1);
        }}
        
        .avatar-icon {{
            font-size: 32px;
        }}
        
        .level-badge {{
            position: absolute;
            top: -5px;
            right: -5px;
            background: #007bff;
            color: white;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: bold;
        }}
        
        .agent-info h3 {{
            margin: 0;
            color: #2c3e50;
            font-size: 18px;
        }}
        
        .agent-email {{
            color: #6c757d;
            font-size: 14px;
            margin: 5px 0;
        }}
        
        .agent-id {{
            font-size: 12px;
            color: #6c757d;
            background: #e9ecef;
            padding: 2px 8px;
            border-radius: 10px;
            display: inline-block;
        }}
        
        .agent-actions {{
            margin-left: auto;
            display: flex;
            gap: 8px;
        }}
        
        .btn-edit, .btn-view {{
            padding: 6px 12px;
            border-radius: 5px;
            text-decoration: none;
            font-size: 13px;
        }}
        
        .btn-edit {{
            background: #ffc107;
            color: #000;
        }}
        
        .btn-view {{
            background: #17a2b8;
            color: white;
        }}
        
        .agent-details {{
            border-top: 1px solid #e0e0e0;
            padding-top: 15px;
        }}
        
        .detail-row {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin-bottom: 10px;
        }}
        
        .detail-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 12px;
            background: white;
            border-radius: 6px;
        }}
        
        .detail-label {{
            color: #6c757d;
            font-size: 13px;
        }}
        
        .detail-value {{
            font-weight: 500;
            color: #2c3e50;
        }}
        
        .badge-downline {{
            background: #d1ecf1;
            color: #0c5460;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
        }}
        
        .badge-listings {{
            background: #fff3cd;
            color: #856404;
            padding: 3px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
        }}
        
        .downline-preview {{
            margin-top: 10px;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 6px;
            font-size: 13px;
            color: #495057;
        }}
        
        .connector-line {{
            position: absolute;
            left: -25px;
            top: 50%;
            width: 25px;
            height: 2px;
            background: #adb5bd;
            z-index: 1;
        }}
        
        .downline-container {{
            margin-left: 40px;
            border-left: 2px dashed #dee2e6;
            padding-left: 20px;
            position: relative;
        }}
        
        /* ============ EMPTY STATE ============ */
        .empty-state {{
            text-align: center;
            padding: 60px 20px;
            color: #6c757d;
        }}
        
        .empty-icon {{
            font-size: 64px;
            margin-bottom: 20px;
            opacity: 0.5;
        }}
        
        .empty-state h3 {{
            margin: 10px 0;
            color: #495057;
        }}
        
        /* ============ RESPONSIVE ============ */
        @media (max-width: 768px) {{
            .detail-row {{
                grid-template-columns: 1fr;
            }}
            
            .agent-header {{
                flex-direction: column;
                align-items: flex-start;
                gap: 15px;
            }}
            
            .agent-actions {{
                margin-left: 0;
                width: 100%;
                justify-content: flex-start;
            }}
            
            .stats-grid {{
                grid-template-columns: 1fr;
            }}
            
            .downline-container {{
                margin-left: 20px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <h1>
                <span style="font-size: 32px;">🌳</span>
                Agent Hierarchy Network
            </h1>
            <p style="color: #6c757d; margin: 10px 0;">Visualize your agent network structure and relationships</p>
            
            <div class="header-actions">
                <a href="/admin/agents" class="btn btn-secondary">
                    <span style="font-size: 18px;">👥</span>
                    Back to Agents List
                </a>
                <a href="/admin/add-agent" class="btn btn-success">
                    <span style="font-size: 18px;">➕</span>
                    Add New Agent
                </a>
                <a href="/admin/dashboard" class="btn btn-secondary">
                    <span style="font-size: 18px;">📊</span>
                    Dashboard
                </a>
                <a href="/admin/export-data?type=agents" class="btn" style="background: #6f42c1; color: white;">
                    <span style="font-size: 18px;">📤</span>
                    Export Agents
                </a>
            </div>
        </div>
        
        <!-- Statistics -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-icon">👥</div>
                <div class="stat-value" style="color: #007bff;">{total_agents}</div>
                <div class="stat-label">Total Agents</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🏆</div>
                <div class="stat-value" style="color: #28a745;">{top_level_count}</div>
                <div class="stat-label">Top Level Agents</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">📈</div>
                <div class="stat-value" style="color: #ffc107;">{with_downlines}</div>
                <div class="stat-label">With Downlines</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">💰</div>
                <div class="stat-value" style="color: #6f42c1;">RM{total_commission:,.2f}</div>
                <div class="stat-label">Total Commission</div>
            </div>
        </div>
        
        <!-- Hierarchy Container -->
        <div class="hierarchy-container">
            <div style="margin-bottom: 25px; padding-bottom: 15px; border-bottom: 2px solid #f0f0f0;">
                <h2 style="margin: 0; color: #2c3e50; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 24px;">📊</span>
                    Network Structure
                </h2>
                <p style="color: #6c757d; margin: 5px 0;">Click on any agent card to view/edit details</p>
            </div>
            
            {hierarchy_html}
        </div>
        
        <!-- Legend -->
        <div style="background: white; padding: 20px; border-radius: 12px; margin-top: 25px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">
            <h3 style="margin: 0 0 15px 0; color: #2c3e50;">📋 Hierarchy Legend</h3>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
                <div style="display: flex; align-items: center; gap: 10px; padding: 10px; background: #f8f9fa; border-radius: 8px;">
                    <div style="width: 20px; height: 20px; background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%); border-left: 3px solid #007bff; border-radius: 3px;"></div>
                    <div>
                        <strong>Level 1</strong>
                        <div style="font-size: 12px; color: #6c757d;">Top Level (No upline)</div>
                    </div>
                </div>
                <div style="display: flex; align-items: center; gap: 10px; padding: 10px; background: #f8f9fa; border-radius: 8px;">
                    <div style="width: 20px; height: 20px; background: linear-gradient(135deg, #e8f5e8 0%, #d4edda 100%); border-left: 3px solid #28a745; border-radius: 3px;"></div>
                    <div>
                        <strong>Level 2</strong>
                        <div style="font-size: 12px; color: #6c757d;">Middle Level</div>
                    </div>
                </div>
                <div style="display: flex; align-items: center; gap: 10px; padding: 10px; background: #f8f9fa; border-radius: 8px;">
                    <div style="width: 20px; height: 20px; background: linear-gradient(135deg, #e3f2fd 0%, #bbdefb 100%); border-left: 3px solid #17a2b8; border-radius: 3px;"></div>
                    <div>
                        <strong>Level 3</strong>
                        <div style="font-size: 12px; color: #6c757d;">Base Level</div>
                    </div>
                </div>
                <div style="display: flex; align-items: center; gap: 10px; padding: 10px; background: #f8f9fa; border-radius: 8px;">
                    <div style="width: 20px; height: 20px; background: linear-gradient(135deg, #f3e5f5 0%, #e1bee7 100%); border-left: 3px solid #6f42c1; border-radius: 3px;"></div>
                    <div>
                        <strong>Level 4+</strong>
                        <div style="font-size: 12px; color: #6c757d;">Extended Network</div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- How It Works -->
        <div style="margin-top: 25px; padding: 25px; background: white; border-radius: 12px; box-shadow: 0 3px 10px rgba(0,0,0,0.1);">
            <h3 style="margin: 0 0 15px 0; color: #2c3e50;">💡 How It Works</h3>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px;">
                <div style="padding: 15px; background: #f0f9ff; border-radius: 8px; border-left: 4px solid #007bff;">
                    <h4 style="margin: 0 0 10px 0; color: #0056b3;">📊 Commission Flow</h4>
                    <p style="margin: 0; color: #495057; font-size: 14px;">
                        When an agent makes a sale, their upline earns a percentage of their commission based on the upline commission rate.
                    </p>
                </div>
                <div style="padding: 15px; background: #f0fdf4; border-radius: 8px; border-left: 4px solid #28a745;">
                    <h4 style="margin: 0 0 10px 0; color: #1e7e34;">🌱 Network Growth</h4>
                    <p style="margin: 0; color: #495057; font-size: 14px;">
                        Each agent can recruit downline agents, creating a multi-level network that generates passive income through commission sharing.
                    </p>
                </div>
                <div style="padding: 15px; background: #fff3cd; border-radius: 8px; border-left: 4px solid #ffc107;">
                    <h4 style="margin: 0 0 10px 0; color: #856404;">💰 Earnings Potential</h4>
                    <p style="margin: 0; color: #495057; font-size: 14px;">
                        Top-level agents earn from their direct sales plus commissions from all downline agents in their network.
                    </p>
                </div>
            </div>
        </div>
    </div>
    
    <script>
    document.addEventListener('DOMContentLoaded', function() {{
        // Make agent cards clickable
        document.querySelectorAll('.agent-card').forEach(card => {{
            card.style.cursor = 'pointer';
            card.addEventListener('click', function(e) {{
                // Don't trigger if clicking on a button
                if (!e.target.closest('a') && !e.target.closest('button')) {{
                    const agentId = this.querySelector('.agent-id').textContent.split('#')[1];
                    if (agentId) {{
                        window.location.href = `/admin/edit-agent/RM{{agentId}}`;
                    }}
                }}
            }});
        }});
        
        // Add hover effects to stats cards
        document.querySelectorAll('.stat-card').forEach(card => {{
            card.addEventListener('mouseenter', function() {{
                this.style.transform = 'translateY(-5px) scale(1.02)';
            }});
            card.addEventListener('mouseleave', function() {{
                this.style.transform = 'translateY(0) scale(1)';
            }});
        }});
        
        // Toggle downline visibility (optional feature)
        const downlineContainers = document.querySelectorAll('.downline-container');
        downlineContainers.forEach(container => {{
            const parentCard = container.parentElement.querySelector('.agent-card');
            parentCard.style.cursor = 'pointer';
            parentCard.addEventListener('dblclick', function() {{
                container.style.display = container.style.display === 'none' ? 'block' : 'none';
            }});
        }});
    }});
    </script>
</body>
</html>'''
    
    return hierarchy_template

@app.route('/admin/add-agent', methods=['GET', 'POST'])
def add_agent():
    """Add new agent with upline structure"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get all existing agents for upline selection
    cursor.execute('''
        SELECT id, name, email 
        FROM users 
        WHERE role = 'agent' 
        ORDER BY name
    ''')
    existing_agents = cursor.fetchall()
    conn.close()
    
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        upline_id = request.form.get('upline_id', None)
        
        # Set upline commission rate to 0 (admin will set later)
        upline_commission_rate = 0.00
        
        hashed_pw = generate_password_hash(password)
        
        conn = sqlite3.connect('real_estate.db')
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO users (email, password, name, role, upline_id, upline_commission_rate)
                VALUES (?, ?, ?, 'agent', ?, ?)
            ''', (email, hashed_pw, name, upline_id, upline_commission_rate))
            
            # Get the new agent's ID
            new_agent_id = cursor.lastrowid
            
            # If upline is specified, update the hierarchy
            if upline_id:
                # You can add hierarchy tracking here if needed
                pass
            
            conn.commit()
            conn.close()
            return redirect('/admin/agents?success=Agent added successfully!')
        except Exception as e:
            conn.rollback()
            conn.close()
            return f"Error: {str(e)}"
    
    # GET request - show form
    add_agent_template = '''
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
    '''
    
    return render_template_string(add_agent_template, existing_agents=existing_agents)

@app.route('/admin/edit-agent/<int:agent_id>', methods=['GET', 'POST'])
def edit_agent(agent_id):
    """Edit agent details with upline system - MULTI-LEVEL VERSION"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get agent details with multi-level commission fields - UPDATED QUERY
    cursor.execute('''
        SELECT 
            u.id,
            u.email,
            u.password,
            u.name,
            u.role,
            u.upline_id,
            u.upline_commission_rate,
            u.created_at,
            u.upline2_id,
            u.upline2_commission_rate,
            u.commission_rate,
            u.total_listings,
            u.total_commission
        FROM users u
        WHERE u.id = ? AND u.role = "agent"
    ''', (agent_id,))
    
    agent = cursor.fetchone()
    
    if not agent:
        conn.close()
        return "Agent not found", 404
    
    # Get all agents except current one for upline selection
    cursor.execute('SELECT id, name, email FROM users WHERE role = "agent" AND id != ? ORDER BY name', (agent_id,))
    existing_agents = cursor.fetchall()
    
    # Get upline details
    upline_name = "None"
    if agent[5]:  # upline_id
        cursor.execute('SELECT name FROM users WHERE id = ?', (agent[5],))
        upline_result = cursor.fetchone()
        upline_name = upline_result[0] if upline_result else "None"
    
    # Get upline2 details
    upline2_name = "None"
    if agent[8]:  # upline2_id (index 8)
        cursor.execute('SELECT name FROM users WHERE id = ?', (agent[8],))
        upline2_result = cursor.fetchone()
        upline2_name = upline2_result[0] if upline2_result else "None"
    
    if request.method == 'POST':
        try:
            name = request.form['name']
            email = request.form['email']
            upline_id = request.form.get('upline_id', None)
            upline_commission_rate = float(request.form.get('upline_commission_rate', 0))
            upline2_commission_rate = float(request.form.get('upline2_commission_rate', 0))
            commission_rate = float(request.form.get('commission_rate', 10))
            password = request.form.get('password', '')
            
            # Auto-set upline2 based on upline's upline
            upline2_id = None
            if upline_id:
                # Use the helper function we added earlier
                from app import update_upline_chain
                upline2_id = update_upline_chain(agent_id, upline_id)
            
            # Build update query with multi-level commission fields
            if password:
                hashed_pw = generate_password_hash(password)
                cursor.execute('''
                    UPDATE users 
                    SET name = ?, email = ?, 
                        upline_id = ?, upline_commission_rate = ?,
                        upline2_id = ?, upline2_commission_rate = ?,
                        commission_rate = ?, password = ?
                    WHERE id = ?
                ''', (name, email, upline_id, upline_commission_rate,
                      upline2_id, upline2_commission_rate, commission_rate,
                      hashed_pw, agent_id))
            else:
                cursor.execute('''
                    UPDATE users 
                    SET name = ?, email = ?, 
                        upline_id = ?, upline_commission_rate = ?,
                        upline2_id = ?, upline2_commission_rate = ?,
                        commission_rate = ?
                    WHERE id = ?
                ''', (name, email, upline_id, upline_commission_rate,
                      upline2_id, upline2_commission_rate, commission_rate,
                      agent_id))
            
            conn.commit()
            conn.close()
            return redirect('/admin/agents?success=Agent updated successfully!')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            return f"Error updating agent: {str(e)}"
    
    # GET request - show edit form
    conn.close()
    
    # Use the updated template with multi-level commissions
    edit_agent_template = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Edit Agent</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 700px; margin: 50px auto; padding: 20px; background: #f5f5f5; }
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
            small { color: #666; font-size: 13px; display: block; margin-top: 5px; }
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
            </div>
            
            <form method="POST">
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
                    <h4 style="margin-top: 0; color: #333;">💰 Multi-Level Commission Settings</h4>
                    
                    <div class="commission-grid">
                        <div class="commission-box">
                            <label>Direct Upline Rate (%)</label>
                            <input type="number" name="upline_commission_rate" 
                                   value="{{ upline_commission_rate }}" 
                                   min="0" max="100" step="0.1" required>
                            <small>Commission to direct upline agent</small>
                        </div>
                        
                        <div class="commission-box">
                            <label>Indirect Upline Rate (%)</label>
                            <input type="number" name="upline2_commission_rate" 
                                   value="{{ upline2_commission_rate }}" 
                                   min="0" max="100" step="0.1" required>
                            <small>Commission to upline's upline (Level 2)</small>
                        </div>
                        
                        <div class="commission-box">
                            <label>Agent's Own Rate (%)</label>
                            <input type="number" name="commission_rate" 
                                   value="{{ commission_rate }}" 
                                   min="0" max="100" step="0.1" required>
                            <small>Agent's commission from own sales</small>
                        </div>
                    </div>
                    
                    <div style="margin-top: 15px; padding: 10px; background: #fff3cd; border-radius: 5px;">
                        <strong>💡 Example Commission Flow:</strong><br>
                        • Agent makes a sale → gets {{ commission_rate }}%<br>
                        • Direct upline gets {{ upline_commission_rate }}% of the sale<br>
                        • Indirect upline gets {{ upline2_commission_rate }}% of the sale
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
            // Update commission example when rates change
            function updateExample() {
                const uplineRate = document.querySelector('input[name="upline_commission_rate"]').value || 0;
                const upline2Rate = document.querySelector('input[name="upline2_commission_rate"]').value || 0;
                const selfRate = document.querySelector('input[name="commission_rate"]').value || 0;
                
                const exampleDiv = document.querySelector('.commission-section > div:last-child');
                if (exampleDiv) {
                    exampleDiv.innerHTML = `
                        <strong>💡 Example for RM10,000 sale:</strong><br>
                        • Agent earns: RM10,000 × ${selfRate}% = <strong>RM${(10000 * selfRate/100).toFixed(2)}</strong><br>
                        • Direct upline gets: RM10,000 × ${uplineRate}% = <strong>RM${(10000 * uplineRate/100).toFixed(2)}</strong><br>
                        • Indirect upline gets: RM10,000 × ${upline2Rate}% = <strong>RM${(10000 * upline2Rate/100).toFixed(2)}</strong>
                    `;
                }
            }
            
            // Attach event listeners to commission rate inputs
            document.querySelectorAll('input[name="upline_commission_rate"], input[name="upline2_commission_rate"], input[name="commission_rate"]').forEach(input => {
                input.addEventListener('input', updateExample);
                input.addEventListener('change', updateExample);
            });
            
            // Initial update
            updateExample();
        });
        </script>
    </body>
    </html>
    '''
    
    return render_template_string(edit_agent_template,
        agent_id=agent[0],
        agent_name=agent[3],
        agent_email=agent[1],
        upline_id=agent[5],
        upline_name=upline_name,
        upline2_name=upline2_name,
        upline_commission_rate=agent[6] if agent[6] else 5.0,
        upline2_commission_rate=agent[9] if len(agent) > 9 and agent[9] else 2.5,
        commission_rate=agent[10] if len(agent) > 10 and agent[10] else 10.0,
        join_date=agent[7][:10] if agent[7] else 'Unknown',
        existing_agents=existing_agents)

# Also add the delete agent route (optional but recommended)
@app.route('/admin/delete-agent/<int:agent_id>')
def delete_agent(agent_id):
    """Delete agent (with confirmation)"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Check if agent has any listings
    cursor.execute('SELECT COUNT(*) FROM property_listings WHERE agent_id = ?', (agent_id,))
    listing_count = cursor.fetchone()[0]
    
    if listing_count > 0:
        conn.close()
        return redirect('/admin/agents?error=Cannot delete agent with existing listings. Reassign listings first.')
    
    try:
        cursor.execute('DELETE FROM users WHERE id = ? AND role = "agent"', (agent_id,))
        conn.commit()
        conn.close()
        return redirect('/admin/agents?success=Agent deleted successfully!')
    except Exception as e:
        conn.rollback()
        conn.close()
        return redirect(f'/admin/agents?error=Error deleting agent: {str(e)}')

@app.route('/admin/commissions')
def commission_report():
    """Commission report page - FIXED VERSION"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get commission data - REMOVED property_type
    cursor.execute('''
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
    ''')
    commissions = cursor.fetchall()
    
    # Calculate totals
    cursor.execute('''
        SELECT 
            SUM(commission_amount) as total_paid,
            COUNT(*) as total_approved
        FROM property_listings 
        WHERE status = 'approved'
    ''')
    totals = cursor.fetchone()
    
    conn.close()
    
    # Create a properly formatted commissions list - REMOVED property_type
    commissions_list = []
    for comm in commissions:
        commissions_list.append({
            'id': comm[0],
            'customer_name': comm[1],
            'agent_name': comm[2],
            'sale_price': float(comm[3]) if comm[3] else 0,
            'commission_amount': float(comm[4]) if comm[4] else 0,
            'status': comm[5],
            'approved_at': comm[6]
        })
    
    # Calculate totals safely
    total_paid = float(totals[0]) if totals and totals[0] else 0
    total_approved = totals[1] if totals and totals[1] else 0
    
    commission_template = '''<!DOCTYPE html>
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
</html>'''
    
    return render_template_string(commission_template, 
                                 commissions_list=commissions_list,
                                 total_paid=total_paid,
                                 total_approved=total_approved)

def calculate_upline_commission(listing_id, agent_id, commission_amount):
    """Calculate upline commissions for a sale - MULTI-LEVEL VERSION"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    upline_commissions = []
    
    # Get agent's upline information
    cursor.execute('''
        SELECT upline_id, upline2_id, upline_commission_rate, upline2_commission_rate, name 
        FROM users 
        WHERE id = ?
    ''', (agent_id,))
    
    agent_data = cursor.fetchone()
    
    if not agent_data:
        conn.close()
        return []
    
    upline_id, upline2_id, upline_rate, upline2_rate, agent_name = agent_data
    
    # Get listing info
    cursor.execute('SELECT property_name FROM property_listings WHERE id = ?', (listing_id,))
    listing_name = cursor.fetchone()
    listing_name = listing_name[0] if listing_name else "Unknown Listing"
    
    # 1. Create DIRECT upline commission (5% to John)
    if upline_id and upline_rate and upline_rate > 0:
        direct_commission = commission_amount * (upline_rate / 100)
        
        cursor.execute('''
            INSERT INTO upline_commissions 
            (listing_id, upline_id, amount, status, created_at, 
             commission_type, commission_rate, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (listing_id, upline_id, direct_commission, 'pending',
              datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
              'direct', upline_rate,
              f"Direct upline commission from {agent_name} for {listing_name}"))
        
        upline_commissions.append({
            'upline_id': upline_id,
            'amount': direct_commission,
            'type': 'direct',
            'rate': upline_rate
        })
        
        print(f"Created direct upline commission: RM{direct_commission:,.2f} to agent {upline_id}")
    
    # 2. Create INDIRECT upline commission (2.5% to Edmond) - NEW
    if upline2_id and upline2_rate and upline2_rate > 0:
        indirect_commission = commission_amount * (upline2_rate / 100)
        
        cursor.execute('''
            INSERT INTO upline_commissions 
            (listing_id, upline_id, amount, status, created_at, 
             commission_type, commission_rate, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (listing_id, upline2_id, indirect_commission, 'pending',
              datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
              'indirect', upline2_rate,
              f"Indirect upline commission from {agent_name} for {listing_name}"))
        
        upline_commissions.append({
            'upline_id': upline2_id,
            'amount': indirect_commission,
            'type': 'indirect',
            'rate': upline2_rate
        })
        
        print(f"Created indirect upline commission: RM{indirect_commission:,.2f} to agent {upline2_id}")
    
    conn.commit()
    conn.close()
    return upline_commissions

def get_total_commissions():
    """Get total commissions including upline commissions"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    try:
        # 1. Get total commissions from property_listings (agent commissions)
        cursor.execute('''
            SELECT SUM(commission_amount) 
            FROM property_listings 
            WHERE status = 'approved'
        ''')
        agent_commissions = cursor.fetchone()[0] or 0
        
        # 2. Get total upline commissions from commission_payments
        # Note: These are commissions that uplines earn from their downlines
        cursor.execute('''
            SELECT SUM(commission_amount) 
            FROM commission_payments 
            WHERE payment_status != 'rejected'
        ''')
        all_commissions = cursor.fetchone()[0] or 0
        
        # Total = Agent commissions + Upline commissions
        # But careful: commission_payments includes BOTH agent and upline payments
        # We need to separate them
        
        # 3. Better approach: Get distinct totals
        # Agent's own commissions from their sales
        cursor.execute('''
            SELECT SUM(cp.commission_amount) 
            FROM commission_payments cp
            JOIN property_listings pl ON cp.listing_id = pl.id
            WHERE pl.agent_id = cp.agent_id  # Agent's own commissions
            AND cp.payment_status != 'rejected'
        ''')
        agent_own_commissions = cursor.fetchone()[0] or 0
        
        # Upline commissions (where payment is to upline, not the selling agent)
        cursor.execute('''
            SELECT SUM(cp.commission_amount) 
            FROM commission_payments cp
            JOIN property_listings pl ON cp.listing_id = pl.id
            WHERE cp.agent_id != pl.agent_id  # Upline commissions
            AND cp.payment_status != 'rejected'
        ''')
        upline_commissions = cursor.fetchone()[0] or 0
        
        return {
            'total_all_commissions': agent_own_commissions + upline_commissions,
            'agent_own_commissions': agent_own_commissions,
            'upline_commissions': upline_commissions
        }
        
    except Exception as e:
        print(f"Error calculating total commissions: {e}")
        return {'total_all_commissions': 0, 'agent_own_commissions': 0, 'upline_commissions': 0}
    finally:
        conn.close()

@app.route('/admin/reports')
def reports_dashboard():
    """Reports dashboard"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    reports_template = '''
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
    '''
    
    return render_template_string(reports_template)

@app.route('/admin/settings')
def admin_settings():
    """System settings page"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    # Get current settings
    payment_settings = get_payment_settings()
    notification_settings = get_notification_settings()
    
    settings_template = '''
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
        ''' + '''
        {% if success %}
        <div class="success-message">✅ {{ success }}</div>
        {% endif %}
        
        {% if error %}
        <div class="error-message">❌ {{ error }}</div>
        {% endif %}
        ''' + '''
        
        <!-- ============ PAYMENT SETTINGS ============ -->
        <div class="settings-section">
            <h2>💰 Payment & Payout Settings</h2>
            <form method="POST" action="/admin/update-payment-settings">
                <div class="form-group">
                    <label>Payment Processing Days</label>
                    <input type="number" name="processing_days" value="''' + str(payment_settings['processing_days']) + '''" 
                           min="1" max="60" required>
                    <span class="setting-note">Days until commission is paid after approval</span>
                </div>
                
                <div class="form-group">
                    <label>Minimum Payout Amount (RM)</label>
                    <input type="number" name="min_payout" value="''' + str(payment_settings['min_payout']) + '''" 
                           step="10" min="0" required>
                    <span class="setting-note">Minimum commission balance for payout</span>
                </div>
                
                <div class="form-group">
                    <label>Payout Schedule</label>
                    <select name="payout_schedule" required>
                        <option value="weekly" ''' + ('selected' if payment_settings['payout_schedule'] == 'weekly' else '') + '''>
                            Weekly (Every Friday)
                        </option>
                        <option value="biweekly" ''' + ('selected' if payment_settings['payout_schedule'] == 'biweekly' else '') + '''>
                            Bi-weekly
                        </option>
                        <option value="monthly" ''' + ('selected' if payment_settings['payout_schedule'] == 'monthly' else '') + '''>
                            Monthly (End of month)
                        </option>
                        <option value="immediate" ''' + ('selected' if payment_settings['payout_schedule'] == 'immediate' else '') + '''>
                            Immediate (After approval)
                        </option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Auto-Generate Payment Voucher</label>
                    <select name="auto_generate_voucher" required>
                        <option value="yes" ''' + ('selected' if payment_settings['auto_generate_voucher'] == 'yes' else '') + '''>
                            Yes, auto-generate when marked paid
                        </option>
                        <option value="no" ''' + ('selected' if payment_settings['auto_generate_voucher'] == 'no' else '') + '''>
                            No, generate manually
                        </option>
                    </select>
                    <span class="setting-note">Automatically generate and email payment voucher when commission is marked as paid</span>
                </div>
                
                <div class="form-group">
                    <label>Voucher Email Template</label>
                    <select name="voucher_template" required>
                        <option value="simple" ''' + ('selected' if payment_settings['voucher_template'] == 'simple' else '') + '''>
                            Simple Text
                        </option>
                        <option value="detailed" ''' + ('selected' if payment_settings['voucher_template'] == 'detailed' else '') + '''>
                            Detailed HTML
                        </option>
                        <option value="receipt" ''' + ('selected' if payment_settings['voucher_template'] == 'receipt' else '') + '''>
                            Official Receipt
                        </option>
                    </select>
                    <span class="setting-note">Template for payment voucher emails</span>
                </div>
                
                <div class="form-group">
                    <label>Payment Voucher Prefix</label>
                    <input type="text" name="voucher_prefix" value="''' + payment_settings['voucher_prefix'] + '''" 
                           maxlength="10">
                    <span class="setting-note">Prefix for voucher numbers (e.g., PAY-2024-001)</span>
                </div>
                
                <div class="form-group">
                    <label>Payment Methods Allowed</label>
                    <div class="checkbox-group">
                        <label>
                            <input type="checkbox" name="payment_methods" value="bank_transfer" 
                                   ''' + ('checked' if 'bank_transfer' in payment_settings['payment_methods'] else '') + '''>
                            Bank Transfer
                        </label>
                        <label>
                            <input type="checkbox" name="payment_methods" value="check" 
                                   ''' + ('checked' if 'check' in payment_settings['payment_methods'] else '') + '''>
                            Check
                        </label>
                        <label>
                            <input type="checkbox" name="payment_methods" value="paypal" 
                                   ''' + ('checked' if 'paypal' in payment_settings['payment_methods'] else '') + '''>
                            PayPal
                        </label>
                        <label>
                            <input type="checkbox" name="payment_methods" value="cash" 
                                   ''' + ('checked' if 'cash' in payment_settings['payment_methods'] else '') + '''>
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
                                   ''' + ('checked' if 'submission_received' in notification_settings['notifications'] else '') + '''>
                            New submission received (Admin)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="submission_approved" 
                                   ''' + ('checked' if 'submission_approved' in notification_settings['notifications'] else '') + '''>
                            Submission approved (Agent)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="payment_processed" 
                                   ''' + ('checked' if 'payment_processed' in notification_settings['notifications'] else '') + '''>
                            Payment processed with voucher (Agent)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="monthly_report" 
                                   ''' + ('checked' if 'monthly_report' in notification_settings['notifications'] else '') + '''>
                            Monthly performance report (Agent)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="upline_earnings" 
                                   ''' + ('checked' if 'upline_earnings' in notification_settings['notifications'] else '') + '''>
                            Upline commission earned (Upline Agent)
                        </label>
                        <label>
                            <input type="checkbox" name="notifications" value="reminders" 
                                   ''' + ('checked' if 'reminders' in notification_settings['notifications'] else '') + '''>
                            Pending submission reminders (Agent)
                        </label>
                    </div>
                </div>
                
                <div class="form-group">
                    <label>Auto-Approval Threshold (RM)</label>
                    <input type="number" name="auto_approve_threshold" 
                           value="''' + str(notification_settings['auto_approve_threshold']) + '''" 
                           step="100" min="0">
                    <span class="setting-note">Submissions below this amount auto-approve (0 = disabled)</span>
                </div>
                
                <div class="form-group">
                    <label>Reminder Days</label>
                    <input type="number" name="reminder_days" 
                           value="''' + str(notification_settings['reminder_days']) + '''" 
                           min="1" max="14">
                    <span class="setting-note">Days before sending reminder for pending submissions</span>
                </div>
                
                <div class="form-group">
                    <label>Admin Notification Email</label>
                    <input type="email" name="admin_email" 
                           value="''' + notification_settings['admin_email'] + '''" 
                           required>
                    <span class="setting-note">Email for receiving system notifications</span>
                </div>
                
                <div class="form-group">
                    <label>System From Email</label>
                    <input type="email" name="system_from_email" 
                           value="''' + notification_settings['system_from_email'] + '''" 
                           required>
                    <span class="setting-note">Email address shown as sender</span>
                </div>
                
                <div class="form-group">
                    <label>SMTP Server Configuration</label>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 5px;">
                        <input type="text" name="smtp_server" placeholder="SMTP Server" 
                               value="''' + notification_settings['smtp_server'] + '''">
                        <input type="number" name="smtp_port" placeholder="Port" 
                               value="''' + notification_settings['smtp_port'] + '''">
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px;">
                        <input type="text" name="smtp_username" placeholder="Username" 
                               value="''' + notification_settings['smtp_username'] + '''">
                        <input type="password" name="smtp_password" placeholder="Password" 
                               value="''' + notification_settings['smtp_password'] + '''">
                    </div>
                    <span class="setting-note">Leave blank to use default system mail</span>
                </div>
                
                <div class="form-group">
                    <label>Email Footer Text</label>
                    <textarea name="email_footer" rows="3" placeholder="Email footer text...">''' + notification_settings['email_footer'] + '''</textarea>
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
    '''
    
    # Check for success/error messages in URL parameters
    success_msg = request.args.get('success')
    error_msg = request.args.get('error')
    
    return render_template_string(settings_template,
        success=success_msg,
        error=error_msg)


# ============ SETTINGS MANAGEMENT FUNCTIONS ============
def get_system_setting(setting_type, setting_key, default=None):
    """Get system setting from database"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute(
        'SELECT setting_value FROM system_settings WHERE setting_type = ? AND setting_key = ?',
        (setting_type, setting_key)
    )
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else default

def save_system_setting(setting_type, setting_key, value):
    """Save system setting to database"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO system_settings (setting_type, setting_key, setting_value, updated_at)
        VALUES (?, ?, ?, ?)
    ''', (setting_type, setting_key, value, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

def get_payment_settings():
    """Get all payment settings as dictionary"""
    return {
        'processing_days': int(get_system_setting('payment', 'processing_days', 14)),
        'min_payout': float(get_system_setting('payment', 'min_payout', 100)),
        'payout_schedule': get_system_setting('payment', 'payout_schedule', 'monthly'),
        'auto_generate_voucher': get_system_setting('payment', 'auto_generate_voucher', 'yes'),
        'voucher_template': get_system_setting('payment', 'voucher_template', 'detailed'),
        'voucher_prefix': get_system_setting('payment', 'voucher_prefix', 'PAY'),
        'payment_methods': get_system_setting('payment', 'payment_methods', 'bank_transfer,check').split(',')
    }

def get_notification_settings():
    """Get all notification settings as dictionary"""
    return {
        'notifications': get_system_setting('notification', 'notifications', 'submission_received,submission_approved,payment_processed,reminders').split(','),
        'auto_approve_threshold': float(get_system_setting('notification', 'auto_approve_threshold', 0)),
        'reminder_days': int(get_system_setting('notification', 'reminder_days', 3)),
        'admin_email': get_system_setting('notification', 'admin_email', 'admin@example.com'),
        'system_from_email': get_system_setting('notification', 'system_from_email', 'noreply@realestate.com'),
        'smtp_server': get_system_setting('notification', 'smtp_server', ''),
        'smtp_port': get_system_setting('notification', 'smtp_port', ''),
        'smtp_username': get_system_setting('notification', 'smtp_username', ''),
        'smtp_password': get_system_setting('notification', 'smtp_password', ''),
        'email_footer': get_system_setting('notification', 'email_footer', '© 2024 Real Estate System. All rights reserved.')
    }


@app.route('/admin/update-payment-settings', methods=['POST'])
def update_payment_settings():
    """Update payment settings"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    try:
        data = request.form
        sale_type = data.get('sale_type', 'sales')  # Default to sales
        
        # Save payment settings
        save_system_setting('payment', 'processing_days', data['processing_days'])
        save_system_setting('payment', 'min_payout', data['min_payout'])
        save_system_setting('payment', 'payout_schedule', data['payout_schedule'])
        save_system_setting('payment', 'auto_generate_voucher', data['auto_generate_voucher'])
        save_system_setting('payment', 'voucher_template', data['voucher_template'])
        save_system_setting('payment', 'voucher_prefix', data['voucher_prefix'])
        
        # Handle checkboxes for payment methods
        payment_methods = request.form.getlist('payment_methods')
        save_system_setting('payment', 'payment_methods', ','.join(payment_methods))
        
        return redirect('/admin/settings?success=Payment+settings+updated+successfully')
        
    except Exception as e:
        return redirect(f'/admin/settings?error={str(e)}')

def notify_agent_status_change(listing_id, agent_id, new_status, admin_name):
    """Notify agent when admin changes their submission status"""
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get agent details
    cursor.execute('SELECT email, name FROM users WHERE id = ?', (agent_id,))
    agent = cursor.fetchone()
    
    if not agent:
        conn.close()
        return False
    
    agent_email, agent_name = agent
    
    # Get listing details
    cursor.execute('SELECT customer_name FROM property_listings WHERE id = ?', (listing_id,))
    listing = cursor.fetchone()
    customer_name = listing[0] if listing else 'Unknown'
    
    conn.close()
    
    # Create notification email
    subject = f"Submission #{listing_id} Status Updated"
    
    if new_status == 'draft':
        body = f"""
Dear {agent_name},

Your submission #{listing_id} (Customer: {customer_name}) has been updated by admin.

**New Status: DRAFT**

📋 **What This Means:**
- You can now reupload or update documents
- Please review the submission and upload any missing documents
- Resubmit when all documents are complete

🔧 **Required Action:**
1. Go to "My Submissions" page
2. Click on submission #{listing_id}
3. Use the "Add/Replace Documents" button
4. Upload required documents
5. Resubmit for approval

📎 **Document Checklist:**
- ✅ Signed Sales & Purchase Agreement
- ✅ Customer ID Proof
- ✅ Property Title/Deed
- ✅ Commission Agreement (if separate)

**Changed by:** {admin_name}
**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}

Click here to access your submission: [Your Submissions]

If you have any questions, please contact the admin team.

Best regards,
Real Estate Commission System
"""
    else:
        # For other status changes
        body = f"""
Dear {agent_name},

Your submission #{listing_id} (Customer: {customer_name}) has been updated.

**New Status: {new_status.upper()}**

**Changed by:** {admin_name}
**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}

Click here to view your submission: [Your Submissions]

Best regards,
Real Estate Commission System
"""
    
    # Send email
    success, message = send_email(
        recipient_email=agent_email,
        recipient_name=agent_name,
        subject=subject,
        body=body,
        email_type='status_change',
        related_id=listing_id,
        related_type='listing'
    )
    
    return success


@app.route('/admin/update-notification-settings', methods=['POST'])
def update_notification_settings():
    """Update notification settings"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    try:
        data = request.form
        sale_type = data.get('sale_type', 'sales')  # Default to sales
        
        # Save notification settings
        notifications = request.form.getlist('notifications')
        save_system_setting('notification', 'notifications', ','.join(notifications))
        
        save_system_setting('notification', 'auto_approve_threshold', data['auto_approve_threshold'])
        save_system_setting('notification', 'reminder_days', data['reminder_days'])
        save_system_setting('notification', 'admin_email', data['admin_email'])
        save_system_setting('notification', 'system_from_email', data['system_from_email'])
        save_system_setting('notification', 'smtp_server', data['smtp_server'])
        save_system_setting('notification', 'smtp_port', data['smtp_port'])
        save_system_setting('notification', 'smtp_username', data['smtp_username'])
        save_system_setting('notification', 'smtp_password', data['smtp_password'])
        save_system_setting('notification', 'email_footer', data['email_footer'])
        
        return redirect('/admin/settings?success=Notification+settings+updated+successfully')
        
    except Exception as e:
        return redirect(f'/admin/settings?error={str(e)}')


# ============ EMAIL SYSTEM FUNCTIONS ============
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import random
import string


def generate_voucher_number(prefix='PAY'):
    """Generate unique voucher number"""
    timestamp = datetime.now().strftime('%Y%m%d')
    random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{prefix}-{timestamp}-{random_str}"


def create_payment_voucher(payment_id, agent_id, amount, payment_date, payment_method):
    """Create payment voucher record - FIXED VERSION"""
    conn = None
    cursor = None
    
    try:
        # Use the existing connection function
        conn = get_db_connection(timeout=30)
        cursor = conn.cursor()
        
        voucher_number = generate_voucher_number(
            get_system_setting('payment', 'voucher_prefix', 'PAY')
        )
        
        cursor.execute('''
            INSERT INTO payment_vouchers 
            (voucher_number, payment_id, agent_id, amount, payment_date, payment_method, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        ''', (voucher_number, payment_id, agent_id, amount, payment_date, payment_method))
        
        voucher_id = cursor.lastrowid
        conn.commit()
        
        return voucher_id, voucher_number
        
    except sqlite3.OperationalError as e:
        print(f" Database error in create_payment_voucher: {e}")
        if conn:
            conn.rollback()
        
        # Retry once with a fresh connection
        try:
            conn = sqlite3.connect('real_estate.db', timeout=30)
            cursor = conn.cursor()
            
            voucher_number = generate_voucher_number(
                get_system_setting('payment', 'voucher_prefix', 'PAY')
            )
            
            cursor.execute('''
                INSERT INTO payment_vouchers 
                (voucher_number, payment_id, agent_id, amount, payment_date, payment_method, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
            ''', (voucher_number, payment_id, agent_id, amount, payment_date, payment_method))
            
            voucher_id = cursor.lastrowid
            conn.commit()
            
            return voucher_id, voucher_number
            
        except Exception as retry_error:
            print(f"❌ Retry also failed: {retry_error}")
            raise retry_error
            
    except Exception as e:
        print(f"❌ Error in create_payment_voucher: {e}")
        if conn:
            conn.rollback()
        raise e
        
    finally:
        # Always close connections
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def send_payment_voucher_email(voucher_id):
    """Send payment voucher email to agent - FIXED VERSION"""
    conn = None
    cursor = None
    
    try:
        conn = get_db_connection(timeout=30)
        cursor = conn.cursor()
        
        # Get voucher details
        cursor.execute('''
            SELECT pv.*, u.email, u.name as agent_name, 
                   cp.transaction_id, cp.notes as payment_notes,
                   pl.customer_name, pl.property_address, pl.sale_price
            FROM payment_vouchers pv
            JOIN users u ON pv.agent_id = u.id
            JOIN commission_payments cp ON pv.payment_id = cp.id
            JOIN property_listings pl ON cp.listing_id = pl.id
            WHERE pv.id = ?
        ''', (voucher_id,))
        
        voucher = cursor.fetchone()
        
        if not voucher:
            return False, "Voucher not found"
        
        # Get email template based on settings
        template_type = get_system_setting('payment', 'voucher_template', 'detailed')
        email_subject = f"Payment Voucher #{voucher[1]} - Real Estate Commission"
    
        # Create email content
        email_body = create_voucher_email_body(voucher, template_type)
    
        # Send email
        success, message = send_email(
            recipient_email=voucher[12] if len(voucher) > 12 else '',  # agent email
            recipient_name=voucher[13] if len(voucher) > 13 else '',   # agent name
            subject=email_subject,
            body=email_body,
            email_type='payment_voucher',
            related_id=voucher_id,
            related_type='voucher'
        )
        
        # Update voucher status
        success = True  # Assuming email sent successfully
        if success:
            cursor.execute('''
                UPDATE payment_vouchers 
                SET status = 'sent', 
                    email_sent_at = ?,
                    email_status = 'success'
                WHERE id = ?
            ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), voucher_id))
        else:
            cursor.execute('''
                UPDATE payment_vouchers 
                SET status = 'failed',
                    email_status = ?
                WHERE id = ?
            ''', ("Email sending failed", voucher_id))
        
        conn.commit()
        
        return success, "Email sent successfully"
        
    except Exception as e:
        print(f"❌ Error in send_payment_voucher_email: {e}")
        if conn:
            conn.rollback()
        return False, f"Failed to send email: {str(e)}"
        
    finally:
        if cursor:
            cursor.close()


def create_voucher_email_body(voucher, template_type='detailed'):
    """Create email body for payment voucher"""
    
    # Simple text template
    if template_type == 'simple':
        return f"""
Payment Voucher: {voucher[1]}
Amount: RM{voucher[4]:,.2f}
Date: {voucher[5]}
Payment Method: {voucher[6] or 'Bank Transfer'}

Transaction ID: {voucher[14] or 'N/A'}
Customer: {voucher[16]}
Property: {voucher[17]}
Sale Price: RM{voucher[18]:,.2f}

This payment has been processed and credited to your account.

Thank you for your hard work!

{get_system_setting('notification', 'email_footer', '© 2024 Real Estate System')}
"""
    
    # Detailed HTML template
    elif template_type == 'detailed':
        return f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; text-align: center; border-radius: 10px 10px 0 0; }}
        .voucher-box {{ border: 2px solid #28a745; padding: 25px; margin: 20px 0; border-radius: 10px; background: #f8fff9; }}
        .amount {{ font-size: 32px; font-weight: bold; color: #28a745; text-align: center; margin: 15px 0; }}
        .details-table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        .details-table td {{ padding: 10px; border-bottom: 1px solid #eee; }}
        .details-table tr:last-child td {{ border-bottom: none; }}
        .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; color: #666; font-size: 12px; text-align: center; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>💰 Payment Voucher</h1>
        <p>Your commission payment has been processed</p>
    </div>
    
    <div class="voucher-box">
        <div style="text-align: center; margin-bottom: 20px;">
            <div style="font-size: 14px; color: #666;">Voucher Number</div>
            <div style="font-size: 24px; font-weight: bold; color: #2c3e50;">{voucher[1]}</div>
        </div>
        
        <div class="amount">RM{voucher[4]:,.2f}</div>
        
        <table class="details-table">
            <tr>
                <td><strong>Payment Date:</strong></td>
                <td>{voucher[5]}</td>
            </tr>
            <tr>
                <td><strong>Payment Method:</strong></td>
                <td>{voucher[6] or 'Bank Transfer'}</td>
            </tr>
            <tr>
                <td><strong>Transaction ID:</strong></td>
                <td>{voucher[14] or 'N/A'}</td>
            </tr>
            <tr>
                <td><strong>Agent Name:</strong></td>
                <td>{voucher[13]}</td>
            </tr>
            <tr>
                <td><strong>Customer:</strong></td>
                <td>{voucher[16]}</td>
            </tr>
            <tr>
                <td><strong>Property:</strong></td>
                <td>{voucher[17]}</td>
            </tr>
            <tr>
                <td><strong>Sale Price:</strong></td>
                <td>RM{voucher[18]:,.2f}</td>
            </tr>
            <tr>
                <td><strong>Status:</strong></td>
                <td><span style="color: #28a745; font-weight: bold;">PAID</span></td>
            </tr>
        </table>
        
        {f'<div style="margin-top: 20px; padding: 15px; background: #f8f9fa; border-radius: 5px;"><strong>Notes:</strong><br>{voucher[15]}</div>' if voucher[15] else ''}
    </div>
    
    <div style="text-align: center; margin: 25px 0;">
        <p>This payment has been processed and will reflect in your account according to your bank's processing time.</p>
        <p>Thank you for your excellent work!</p>
    </div>
    
    <div class="footer">
        {get_system_setting('notification', 'email_footer', '© 2024 Real Estate System. All rights reserved.')}
        <br>
        <small>This is an automated email, please do not reply.</small>
    </div>
</body>
</html>
"""
    
    # Official receipt template
    else:  # receipt template
        return f"""
OFFICIAL PAYMENT RECEIPT
========================

Voucher: {voucher[1]}
Date: {datetime.now().strftime('%d %B %Y')}

PAYMENT TO:
{voucher[13]}
{voucher[12]}

AMOUNT PAID: RM{voucher[4]:,.2f}

PAYMENT DETAILS:
----------------
Payment Method: {voucher[6] or 'Bank Transfer'}
Transaction ID: {voucher[14] or 'N/A'}
Payment Date: {voucher[5]}

FOR SERVICES RENDERED:
----------------------
Customer: {voucher[16]}
Property: {voucher[17]}
Sale Price: RM{voucher[18]:,.2f}

STATUS: PAID IN FULL

Authorized Signature:
____________________
Real Estate Commission System

{get_system_setting('notification', 'email_footer', '© 2024 Real Estate System')}
"""
def send_email(recipient_email, recipient_name, subject, body, email_type, related_id=None, related_type=None):
    """Send email using configured SMTP settings"""
    
    # Get email settings
    notification_settings = get_notification_settings()
    
    conn = None
    try:
        # Log email attempt - use a separate connection
        conn = sqlite3.connect('real_estate.db', timeout=30)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO email_logs 
            (recipient_email, recipient_name, subject, email_type, status, related_id, related_type)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
        ''', (recipient_email, recipient_name, subject, email_type, related_id, related_type))
        
        email_log_id = cursor.lastrowid
        conn.commit()
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = notification_settings['system_from_email']
        msg['To'] = recipient_email
        
        # Check if body is HTML
        if '<html>' in body.lower():
            msg.attach(MIMEText(body, 'html'))
        else:
            msg.attach(MIMEText(body, 'plain'))
        
        # Send email
        if notification_settings['smtp_server'] and notification_settings['smtp_username']:
            # Use custom SMTP
            server = smtplib.SMTP(notification_settings['smtp_server'], 
                                  int(notification_settings['smtp_port'] or 587))
            server.starttls()
            server.login(notification_settings['smtp_username'], 
                        notification_settings['smtp_password'])
            server.send_message(msg)
            server.quit()
        else:
            # Use default (development - prints to console)
            print(f"\n" + "="*50)
            print(f"EMAIL SENT TO: {recipient_email}")
            print(f"SUBJECT: {subject}")
            print(f"BODY:\n{body}")
            print("="*50 + "\n")
        
        # Update email log - use same connection
        cursor.execute('''
            UPDATE email_logs 
            SET status = 'sent', sent_at = ?
            WHERE id = ?
        ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), email_log_id))
        
        conn.commit()
        
        return True, "Email sent successfully"
        
    except Exception as e:
        # Update email log with error
        if conn:
            try:
                cursor.execute('''
                    UPDATE email_logs 
                    SET status = 'failed', error_message = ?
                    WHERE id = ?
                ''', (str(e), email_log_id))
                conn.commit()
            except:
                pass
        
        return False, f"Failed to send email: {str(e)}"
        
    finally:
        if conn:
            conn.close()


# ============ TEST EMAIL ROUTES ============
@app.route('/admin/test-email')
def test_email_system():
    """Test email system"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    # Send test email to admin
    admin_email = get_system_setting('notification', 'admin_email', 'admin@example.com')
    
    success, message = send_email(
        recipient_email=admin_email,
        recipient_name="Admin",
        subject="Test Email - Real Estate System",
        body=f"""
This is a test email from your Real Estate System.

System Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
SMTP Configured: {'Yes' if get_system_setting('notification', 'smtp_server') else 'No (using console)'}

If you received this email, your email system is working correctly.
        """,
        email_type='test',
        related_id=session['user_id'],
        related_type='admin'
    )
    
    if success:
        return redirect('/admin/settings?success=Test+email+sent+successfully')
    else:
        return redirect(f'/admin/settings?error=Test+email+failed:+{message}')


@app.route('/admin/send-test-voucher')
def send_test_voucher():
    """Send test payment voucher"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = get_db_connection()
    
    try:
        cursor = conn.cursor()
        
        # Get admin user
        cursor.execute('SELECT id, email, name FROM users WHERE id = ?', (session['user_id'],))
        admin_user = cursor.fetchone()
        
        if admin_user:
            # Create test payment record
            cursor.execute('''
                INSERT INTO commission_payments 
                (listing_id, agent_id, commission_amount, payment_status, payment_date, payment_method, transaction_id)
                VALUES (?, ?, ?, 'paid', ?, ?, ?)
            ''', (0, admin_user[0], 1000.00, 
                  datetime.now().strftime('%Y-%m-%d'),
                  'test',
                  'TEST-12345'))
            
            payment_id = cursor.lastrowid
            
            # Create voucher
            voucher_id, voucher_number = create_payment_voucher(
                payment_id=payment_id,
                agent_id=admin_user[0],
                amount=1000.00,
                payment_date=datetime.now().strftime('%Y-%m-%d'),
                payment_method='test'
            )
            
            conn.commit()
            
            # Send voucher email
            success, message = send_payment_voucher_email(voucher_id)
            
            if success:
                return redirect('/admin/settings?success=Test+voucher+email+sent+successfully')
            else:
                return redirect(f'/admin/settings?error=Test+voucher+failed:+{message}')
        
        return redirect('/admin/settings?error=Cannot+find+user+account')
        
    except Exception as e:
        print(f"Error in send_test_voucher: {e}")
        import traceback
        traceback.print_exc()
        return redirect(f'/admin/settings?error=Database+error:+{str(e)}')
        
    finally:
        conn.close()

@app.route('/admin/approve/<int:listing_id>')
def approve_listing(listing_id):
    """Approve listing and create commission records"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    try:
        conn = sqlite3.connect('real_estate.db')
        cursor = conn.cursor()
        
        # Get listing details including agent's upline AND project name
        cursor.execute('''
            SELECT 
                pl.*, 
                u.name as agent_name, 
                u.upline_id,
                (SELECT name FROM users WHERE id = u.upline_id) as upline_name,
                p.project_name
            FROM property_listings pl
            JOIN users u ON pl.agent_id = u.id
            LEFT JOIN projects p ON pl.project_id = p.id
            WHERE pl.id = ?
        ''', (listing_id,))
        
        listing = cursor.fetchone()
        
        if not listing:
            # Use flash properly (imported from flask)
            flash('❌ Listing not found', 'error')
            return redirect('/admin/documents')
        
        if listing[8] == 'approved':  # status column
            flash(' Listing already approved', 'warning')
            return redirect(f'/admin/documents/{listing_id}')
        
        # Update listing status
        cursor.execute('''
            UPDATE property_listings 
            SET status = 'approved', 
                approved_at = ?,
                approved_by = ?,
                commission_status = 'pending'
            WHERE id = ?
        ''', (
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            session['user_id'],
            listing_id
        ))
        
        commission_amount = listing[9]  # commission_amount column
        project_name = listing[25] if len(listing) > 25 else None  # project_name from join
        
        # Determine what to show in notes
        if project_name:
            agent_notes = f'Agent commission for {project_name} - 95% of RM{commission_amount:,.2f}'
            upline_notes = f'Upline commission from agent {listing[20]} for {project_name} - 5% of RM{commission_amount:,.2f}'
            notification_message = f'Your submission #{listing_id} for {project_name} has been approved. Commission: RM{commission_amount:,.2f}'
        else:
            # Fallback to customer name if no project
            customer_name = listing[4] if listing[4] else f'listing #{listing_id}'
            agent_notes = f'Agent commission for {customer_name} - 95% of RM{commission_amount:,.2f}'
            upline_notes = f'Upline commission from agent {listing[20]} - 5% of RM{commission_amount:,.2f}'
            notification_message = f'Your submission #{listing_id} has been approved. Commission: RM{commission_amount:,.2f}'
        
        # Create AGENT commission payment record
        cursor.execute('''
            INSERT INTO commission_payments
            (listing_id, agent_id, commission_amount, payment_status, 
             payment_date, payment_method, transaction_id, notes)
            VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, ?)
        ''', (
            listing_id,
            listing[1],  # agent_id
            commission_amount * 0.95,  # Agent gets 95%
            agent_notes
        ))
        
        # Create UPLINE commission record if upline exists
        upline_id = listing[22] if len(listing) > 22 else None  # upline_id from join
        
        if upline_id:
            cursor.execute('''
                INSERT INTO commission_payments
                (listing_id, agent_id, commission_amount, payment_status,
                 payment_date, payment_method, transaction_id, notes)
                VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, ?)
            ''', (
                listing_id,
                upline_id,
                commission_amount * 0.05,  # Upline gets 5%
                upline_notes
            ))
            
            # Also create upline_commissions table record
            cursor.execute('''
                INSERT INTO upline_commissions
                (listing_id, agent_id, upline_id, amount, status, notes, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
            ''', (
                listing_id,
                listing[1],  # agent_id
                upline_id,
                commission_amount * 0.05,  # 5% of commission
                upline_notes,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ))
        
        # Create commission distribution record
        cursor.execute('''
            INSERT INTO commission_distributions
            (listing_id, agent_id, upline_id, level, sale_price,
             agent_commission_rate, agent_gross_commission,
             upline_commission_rate, upline_commission,
             agent_net_commission, payment_status, distribution_date)
            VALUES (?, ?, ?, 1, ?, 100, ?, 
                    CASE WHEN ? IS NOT NULL THEN 5 ELSE 0 END,
                    CASE WHEN ? IS NOT NULL THEN ? * 0.05 ELSE 0 END,
                    ? * 0.95, 'pending', ?)
        ''', (
            listing_id,
            listing[1],  # agent_id
            upline_id,
            listing[6],  # sale_price
            commission_amount,
            upline_id,
            upline_id,
            commission_amount,
            commission_amount,
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        
        # Create notification for agent
        cursor.execute('''
            INSERT INTO agent_notifications
            (agent_id, notification_type, title, message, 
             related_id, related_type, priority, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            listing[1],  # agent_id
            'listing_approved',
            '✅ Listing Approved',
            notification_message,
            listing_id,
            'listing',
            'high',
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        
        conn.commit()
        conn.close()
        
        # Use flash for success message
        flash(f'✅ Listing #{listing_id} approved! Commissions calculated.', 'success')
        return redirect(f'/admin/documents/{listing_id}')
        
    except Exception as e:
        # Log the error
        print(f"Error approving listing {listing_id}: {str(e)}")
        
        # Return an error response
        error_html = f'''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Approval Error</title>
            <style>
                body {{ font-family: Arial, sans-serif; padding: 20px; }}
                .error {{ color: #dc3545; padding: 15px; border: 1px solid #dc3545; border-radius: 5px; }}
            </style>
        </head>
        <body>
            <div class="error">
                <h2>❌ Approval Failed</h2>
                <p><strong>Error:</strong> {str(e)}</p>
                <p>Please check if flash is imported properly:</p>
                <pre>from flask import Flask, ..., flash</pre>
                <div style="margin-top: 20px;">
                    <a href="/admin/documents/{listing_id}">← Back to Documents</a>
                </div>
            </div>
        </body>
        </html>
        '''
        return error_html

@app.route('/admin/reject/<int:listing_id>', methods=['GET', 'POST'])
def reject_listing(listing_id):
    """Reject listing with reason"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    if request.method == 'POST':
        rejection_reason = request.form.get('rejection_reason', '')
        
        conn = sqlite3.connect('real_estate.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE property_listings 
            SET status = 'rejected', 
                commission_status = 'rejected',
                rejection_reason = ?
            WHERE id = ?
        ''', (rejection_reason, listing_id))
        
        conn.commit()
        conn.close()
        
        return redirect('/admin/dashboard')
    
    # GET request - show rejection form - FIXED VERSION
    rejection_template = '''
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
            <h2>❌ Reject Submission #''' + str(listing_id) + '''</h2>
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
    '''
    
    return render_template_string(rejection_template)

@app.route('/admin/payments')
def admin_payments():
    """Payment management page with BOTH agent and upline payments"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get payment status filter
    status_filter = request.args.get('status', 'all')
    agent_filter = request.args.get('agent', 'all')
    info_message = request.args.get('info', '')
    success_message = request.args.get('success', '')
    error_message = request.args.get('error', '')
    
    # ============ 1. AGENT PAYMENTS (Agent's own commissions) ============
    # First check what columns exist in projects table
    cursor.execute("PRAGMA table_info(projects)")
    project_columns = [col[1] for col in cursor.fetchall()]
    print(f"Projects table columns: {project_columns}")
    
    # Use appropriate column name for project name
    project_name_column = 'name' if 'name' in project_columns else 'project_name' if 'project_name' in project_columns else 'title'
    
    query_agent = f'''
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
    '''
    
    params_agent = []
    
    # Apply filters
    if status_filter != 'all':
        query_agent += ' AND cp.payment_status = ?'
        params_agent.append(status_filter)
    
    if agent_filter != 'all':
        query_agent += ' AND cp.agent_id = ?'
        params_agent.append(agent_filter)
    
    query_agent += ' ORDER BY cp.created_at DESC'
    
    print(f"Agent query: {query_agent}")
    
    cursor.execute(query_agent, params_agent)
    all_payments = cursor.fetchall()
    
    # Filter agent payments (those where agent is the listing agent)
    agent_payments = []
    for payment in all_payments:
        listing_id = payment[1]
        agent_id = payment[2]
        
        # Check if this agent is the listing agent
        cursor.execute('SELECT agent_id FROM property_listings WHERE id = ?', (listing_id,))
        listing_result = cursor.fetchone()
        
        if listing_result and listing_result[0] == agent_id:
            # This is an agent's own commission
            agent_payments.append(payment)
    
    # ============ 2. UPLINE PAYMENTS ============
    # Get upline commissions with correct column structure
    query_upline = f'''
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
    '''
    
    params_upline = []
    
    # Apply filters
    if status_filter != 'all':
        query_upline += ' AND uc.status = ?'
        params_upline.append(status_filter)
    
    if agent_filter != 'all':
        query_upline += ' AND uc.upline_id = ?'
        params_upline.append(agent_filter)
    
    query_upline += ' ORDER BY uc.created_at DESC'
    
    print(f"Upline query: {query_upline}")
    
    try:
        cursor.execute(query_upline, params_upline)
        upline_payments = cursor.fetchall()
        print(f"Found {len(upline_payments)} upline payments")
    except Exception as e:
        print(f"Error fetching upline payments: {e}")
        # Try without project name
        query_upline_simple = '''
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
                COALESCE(uc.commission_type, 'direct') as commission_type,  -- NEW
                uc.commission_rate  -- NEW
            FROM upline_commissions uc
            LEFT JOIN users uu ON uc.upline_id = uu.id
            LEFT JOIN property_listings pl ON uc.listing_id = pl.id
            LEFT JOIN users ua ON pl.agent_id = ua.id
            WHERE 1=1
        '''
        
        if status_filter != 'all':
            query_upline_simple += ' AND uc.status = ?'
        
        if agent_filter != 'all':
            query_upline_simple += ' AND uc.upline_id = ?'
        
        query_upline_simple += ' ORDER BY uc.created_at DESC'
        
        cursor.execute(query_upline_simple, params_upline)
        upline_payments = cursor.fetchall()
        # Add None for project_name column
        # No need to add extra columns
    
    # ============ 3. CORRECTED COMBINED PAYMENTS STATS ============
    # Get stats from commission_payments table
    query_cp_stats = '''
        SELECT 
            COUNT(*) as total_agent_payments,
            SUM(CASE WHEN payment_status = 'paid' THEN commission_amount ELSE 0 END) as total_agent_paid,
            SUM(CASE WHEN payment_status = 'pending' THEN commission_amount ELSE 0 END) as total_agent_pending,
            SUM(CASE WHEN payment_status = 'processing' THEN commission_amount ELSE 0 END) as total_agent_processing
        FROM commission_payments
    '''

    cursor.execute(query_cp_stats)
    cp_stats = cursor.fetchone()

    # Get stats from upline_commissions table
    query_uc_stats = '''
        SELECT 
            COUNT(*) as total_upline_payments,
            SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) as total_upline_paid,
            SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END) as total_upline_pending
        FROM upline_commissions
    '''

    cursor.execute(query_uc_stats)
    uc_stats = cursor.fetchone()

    # Combine stats CORRECTLY
    total_payments = (cp_stats[0] or 0) + (uc_stats[0] or 0)
    total_paid = (cp_stats[1] or 0) + (uc_stats[1] or 0)
    total_pending = (cp_stats[2] or 0) + (uc_stats[2] or 0)
    total_processing = cp_stats[3] or 0

    stats = (total_payments, total_paid, total_pending, total_processing)
    
    # Get all agents for filter dropdown
    cursor.execute('SELECT id, name FROM users WHERE role = "agent" ORDER BY name')
    agents = cursor.fetchall()
    
    conn.close()
    
    # ============ 4. CALCULATE SEPARATE STATS ============
    total_agent_amount = sum(p[5] or 0 for p in agent_payments if p[6] == 'pending')
    total_upline_amount = sum(p[5] or 0 for p in upline_payments if p[6] == 'pending')
    
    # ============ 5. RENDER TEMPLATE ============
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>Payment Management</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 1800px; margin: 0 auto; }
        .header { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .stats-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 15px; margin-bottom: 20px; }
        .stat-card { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); text-align: center; }
        .stat-value { font-size: 24px; font-weight: bold; margin: 10px 0; }
        .filter-card { background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .filter-form { display: grid; grid-template-columns: auto auto auto 1fr; gap: 10px; align-items: center; }
        select, input { padding: 8px; border: 1px solid #ddd; border-radius: 5px; }
        .btn { background: #007bff; color: white; padding: 8px 15px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn:hover { background: #0056b3; }
        .table-container { background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 30px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #eee; }
        th { background: #f8f9fa; font-weight: bold; }
        tr:hover { background: #f8f9fa; }
        .status-badge { padding: 4px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .status-pending { background: #fff3cd; color: #856404; }
        .status-paid { background: #d4edda; color: #155724; }
        .status-processing { background: #cce5ff; color: #004085; }
        .actions { display: flex; gap: 5px; }
        .action-btn { padding: 4px 8px; border-radius: 4px; text-decoration: none; font-size: 12px; }
        .view-btn { background: #17a2b8; color: white; }
        .pay-btn { background: #28a745; color: white; }
        .payment-type { font-size: 11px; padding: 2px 6px; border-radius: 10px; margin-left: 5px; }
        .agent-payment { background: #e3f2fd; color: #1565c0; }
        .upline-payment { background: #f3e5f5; color: #7b1fa2; }
        .upline-direct { background: #e3f2fd; color: #1565c0; }
        .upline-indirect { background: #fff3cd; color: #856404; }
        .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; }
        .section-title { display: flex; align-items: center; gap: 10px; }
        .badge-count { background: #6c757d; color: white; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
        .database-info { background: #e8f4fd; padding: 10px; border-radius: 5px; margin-bottom: 10px; font-size: 12px; color: #004085; }
        .info-message { background: #d4edda; color: #155724; padding: 10px; border-radius: 5px; margin-bottom: 15px; border: 1px solid #c3e6cb; }
        .success-message { background: #d1ecf1; color: #0c5460; padding: 10px; border-radius: 5px; margin-bottom: 15px; border: 1px solid #bee5eb; }
        .error-message { background: #f8d7da; color: #721c24; padding: 10px; border-radius: 5px; margin-bottom: 15px; border: 1px solid #f5c6cb; }
        .header-links { display: flex; gap: 10px; margin-top: 15px; flex-wrap: wrap; }
        .listing-info { font-size: 12px; color: #666; }
        .project-name { font-weight: bold; color: #0056b3; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>💰 Payment Management</h1>
            <div class="header-links">
                <a href="/admin/dashboard" class="btn" style="background: #6c757d;">← Dashboard</a>
                <a href="/admin/export-data?type=payments" class="btn" style="background: #20c997;">📤 Export</a>
                <a href="/admin/sync-payments" class="btn" style="background: #17a2b8;">💳 Sync & Create Payments</a>
            </div>
        </div>
        
        {% if info_message %}
        <div class="info-message">
            ℹ️ {{ info_message }}
        </div>
        {% endif %}
        
        {% if success_message %}
        <div class="success-message">
            ✅ {{ success_message }}
        </div>
        {% endif %}
        
        {% if error_message %}
        <div class="error-message">
            ❌ {{ error_message }}
        </div>
        {% endif %}
        
        <!-- Database Info -->
        <div class="database-info">
            <strong>Database Status:</strong> 
            Agent Payments: {{ agent_payments|length }} | 
            Upline Payments: {{ upline_payments|length }} |
            Total Payments: {{ stats[0] or 0 }}
        </div>
        
        <!-- Enhanced Summary Stats -->
        <div class="stats-grid">
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Displayed Payments</div>
                <div class="stat-value" style="color: #6f42c1;">{{ agent_payments|length + upline_payments|length }}</div>
                <div style="font-size: 12px; color: #999;">
                    {{ agent_payments|length }} agent + {{ upline_payments|length }} upline
                </div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Total Paid</div>
                <div class="stat-value" style="color: #28a745;">RM{{ "{:,.2f}".format(stats[1] or 0) }}</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Pending Payment</div>
                <div class="stat-value" style="color: #fd7e14;">RM{{ "{:,.2f}".format(stats[2] or 0) }}</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Processing</div>
                <div class="stat-value" style="color: #17a2b8;">RM{{ "{:,.2f}".format(stats[3] or 0) }}</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Agent Pending</div>
                <div class="stat-value" style="color: #007bff;">RM{{ "{:,.2f}".format(total_agent_amount or 0) }}</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Upline Pending</div>
                <div class="stat-value" style="color: #6f42c1;">RM{{ "{:,.2f}".format(total_upline_amount or 0) }}</div>
            </div>
        </div>
        
        <!-- Filter Section -->
        <div class="filter-card">
            <h3 style="margin-top: 0;">Filter Payments</h3>
            <form method="get" class="filter-form">
                <select name="status">
                    <option value="all" {% if status_filter == 'all' %}selected{% endif %}>All Status</option>
                    <option value="pending" {% if status_filter == 'pending' %}selected{% endif %}>Pending</option>
                    <option value="processing" {% if status_filter == 'processing' %}selected{% endif %}>Processing</option>
                    <option value="paid" {% if status_filter == 'paid' %}selected{% endif %}>Paid</option>
                </select>
                
                <select name="agent">
                    <option value="all" {% if agent_filter == 'all' %}selected{% endif %}>All Agents</option>
                    {% for agent in agents %}
                    <option value="{{ agent[0] }}" {% if agent_filter == agent[0]|string %}selected{% endif %}>{{ agent[1] }}</option>
                    {% endfor %}
                </select>
                
                <button type="submit" class="btn">🔍 Filter</button>
                <a href="/admin/payments" class="btn" style="background: #6c757d; margin-left: auto;">Clear</a>
            </form>
        </div>
        
        <!-- Agent Payments Table -->
        <div class="table-container">
            <div class="section-header">
                <div class="section-title">
                    <h2 style="margin: 0;">👤 Agent Commission Payments</h2>
                    <span class="badge-count">{{ agent_payments|length }} payments</span>
                </div>
                <span class="payment-type agent-payment">Agent's Own Commissions</span>
            </div>
            
            {% if agent_payments %}
            <table>
                <thead>
                    <tr>
                        <th>Payment ID</th>
                        <th>Listing</th>
                        <th>Agent</th>
                        <th>Amount</th>
                        <th>Status</th>
                        <th>Payment Date</th>
                        <th>Created</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for payment in agent_payments %}
                    <tr>
                        <td><strong>#{{ payment[0] }}</strong></td>
                        <td>
                            <div class="listing-info">
                                <strong>#{{ payment[1] }}</strong>
                                {% if payment[12] %}
                                <div class="project-name">{{ payment[12] }}</div>
                                {% endif %}
                                {% if payment[10] %}
                                <div>{{ payment[10] }}</div>
                                {% endif %}
                            </div>
                        </td>
                        <td>
                            {{ payment[3] }}
                            <div style="font-size: 12px; color: #666;">{{ payment[4] }}</div>
                        </td>
                        <td><strong>RM{{ "{:,.2f}".format(payment[5] or 0) }}</strong></td>
                        <td>
                            {% if payment[6] == 'pending' %}
                            <span class="status-badge status-pending">Pending</span>
                            {% elif payment[6] == 'processing' %}
                            <span class="status-badge status-processing">Processing</span>
                            {% elif payment[6] == 'paid' %}
                            <span class="status-badge status-paid">Paid</span>
                            {% else %}
                            <span class="status-badge">{{ payment[6] }}</span>
                            {% endif %}
                        </td>
                        <td>{{ payment[7] if payment[7] else 'Not paid' }}</td>
                        <td>{{ payment[8].split()[0] if payment[8] else '' }}</td>
                        <td class="actions">
                            <a href="/admin/payment/{{ payment[0] }}" class="action-btn view-btn">👁️ View</a>
                            {% if payment[6] != 'paid' %}
                            <a href="/admin/payment/{{ payment[0] }}/mark-paid" class="action-btn pay-btn" onclick="return confirm('Mark agent payment #{{ payment[0] }} as paid?')">💰 Mark Paid</a>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <p>No agent commission payments found.</p>
            {% endif %}
        </div>
        
        <!-- Upline Payments Table -->
        <div class="table-container">
            <div class="section-header">
                <div class="section-title">
                    <h2 style="margin: 0;">👥 Upline Commission Payments</h2>
                    <span class="badge-count">{{ upline_payments|length }} payments</span>
                </div>
                <span class="payment-type upline-payment">Commissions from Downline Agents</span>
            </div>
            
            {% if upline_payments %}
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Listing</th>
                        <th>Upline Agent</th>
                        <th>From Agent</th>
                        <th>Type</th>
                        <th>Rate</th>
                        <th>Amount</th>
                        <th>Status</th>
                        <th>Payment Date</th>
                        <th>Created</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for payment in upline_payments %}
                    <tr>
                        <td><strong>UC-{{ payment[0] }}</strong></td>
                        <td>
                            <div class="listing-info">
                                <strong>#{{ payment[1] }}</strong>
                                {% if payment[14] %}
                                <div class="project-name">{{ payment[14] }}</div>
                                {% endif %}
                                {% if payment[9] %}
                                <div>{{ payment[9] }}</div>
                                {% endif %}
                            </div>
                        </td>
                        <td>
                            {{ payment[3] if payment[3] else 'Unknown' }}
                            <div style="font-size: 12px; color: #666;">{{ payment[4] if payment[4] else '' }}</div>
                        </td>
                        <td>
                            {{ payment[11] if payment[11] else 'N/A' }}
                            <div style="font-size: 12px; color: #666;">{{ payment[12] if payment[12] else '' }}</div>
                        </td>
                        <td>
                            {% if payment[15] == 'indirect' %}
                                <span class="payment-type upline-indirect">Indirect</span>
                            {% else %}
                                <span class="payment-type upline-direct">Direct</span>
                            {% endif %}
                        </td>
                        <td>
                            {{ payment[16] or 0 }}%
                        </td>
                        <td><strong>RM{{ "{:,.2f}".format(payment[5] or 0) }}</strong></td>
                        <td>
                            {% if payment[6] == 'pending' %}
                            <span class="status-badge status-pending">Pending</span>
                            {% elif payment[6] == 'paid' %}
                            <span class="status-badge status-paid">Paid</span>
                            {% elif payment[6] == 'processing' %}
                            <span class="status-badge status-processing">Processing</span>
                            {% else %}
                            <span class="status-badge">{{ payment[6] }}</span>
                            {% endif %}
                        </td>
                        <td>
                            {% if payment[8] %}
                            {{ payment[8].split()[0] if payment[8] else '' }}
                            {% else %}
                            Not paid
                            {% endif %}
                        </td>
                        <td>
                            {% if payment[7] %}
                            {{ payment[7].split()[0] if payment[7] else '' }}
                            {% else %}
                            N/A
                            {% endif %}
                        </td>
                        <td class="actions">
                            {% if payment[6] == 'pending' %}
                            <a href="/admin/pay-upline/{{ payment[0] }}" class="action-btn pay-btn" onclick="return confirm('Mark upline commission UC-{{ payment[0] }} as paid? Amount: RM{{ "{:,.2f}".format(payment[5] or 0) }}')">💰 Mark Paid</a>
                            {% elif payment[6] == 'paid' %}
                            <span class="status-badge status-paid" style="font-size: 11px; padding: 4px 8px;">✓ Paid</span>
                            {% else %}
                            <span class="status-badge">{{ payment[6] }}</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div style="background: #f8f9fa; padding: 20px; border-radius: 5px; text-align: center;">
                <h3 style="color: #6c757d; margin-bottom: 10px;">No Upline Commission Payments Found</h3>
                <p style="color: #666; margin-bottom: 15px;">This could be because:</p>
                <ul style="text-align: left; display: inline-block; color: #666; margin-bottom: 20px;">
                    <li>No upline relationships have been set up</li>
                    <li>Agents don't have uplines assigned</li>
                    <li>No commissions have been approved yet</li>
                    <li>All upline commissions have already been paid</li>
                </ul>
                <div style="margin-top: 15px;">
                    <a href="/admin/set-upline" class="btn" style="background: #17a2b8; margin: 5px;">👥 Set Upline Relationships</a>
                    <a href="/admin/agents" class="btn" style="background: #20c997; margin: 5px;">📋 Manage Agents</a>
                </div>
            </div>
            {% endif %}
        </div>
    </div>
    
    <script>
        document.addEventListener('DOMContentLoaded', function() {
            // Add payment type indicators
            const agentRows = document.querySelectorAll('.table-container:first-child tbody tr');
            agentRows.forEach(row => {
                const typeCell = row.querySelector('td:nth-child(3)');
                if (typeCell) {
                    const typeBadge = document.createElement('span');
                    typeBadge.className = 'payment-type agent-payment';
                    typeBadge.textContent = 'Agent';
                    typeBadge.style.marginLeft = '5px';
                    typeBadge.style.fontSize = '10px';
                    typeCell.appendChild(typeBadge);
                }
            });
            
            const uplineRows = document.querySelectorAll('.table-container:last-child tbody tr');
            uplineRows.forEach(row => {
                const typeCell = row.querySelector('td:nth-child(3)');
                if (typeCell) {
                    const commissionTypeCell = row.querySelector('td:nth-child(5)');
                    if (commissionTypeCell) {
                        const commissionType = commissionTypeCell.textContent.trim();
                        const typeBadge = document.createElement('span');
                        typeBadge.className = 'payment-type';
                        typeBadge.textContent = commissionType === 'Indirect' ? 'Upline (Indirect)' : 'Upline (Direct)';
                        typeBadge.style.marginLeft = '5px';
                        typeBadge.style.fontSize = '10px';
                        typeBadge.style.background = commissionType === 'Indirect' ? '#ffc107' : '#e3f2fd';
                        typeBadge.style.color = commissionType === 'Indirect' ? '#000' : '#1565c0';
                        typeBadge.style.padding = '2px 6px';
                        typeBadge.style.borderRadius = '10px';
                        typeCell.appendChild(typeBadge);
                    }
                }
            });
        });
    </script>
</body>
</html>
    ''', 
    agent_payments=agent_payments,
    upline_payments=upline_payments,
    stats=stats,
    agents=agents,
    status_filter=status_filter,
    agent_filter=agent_filter,
    total_agent_amount=total_agent_amount,
    total_upline_amount=total_upline_amount,
    info_message=info_message,
    success_message=success_message,
    error_message=error_message
    )

@app.route('/admin/set-upline')
def set_upline():
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get all agents
    cursor.execute("SELECT id, name FROM users WHERE role = 'agent' ORDER BY name")
    agents = cursor.fetchall()
    
    # Get potential uplines
    cursor.execute("SELECT id, name FROM users WHERE role IN ('admin', 'agent') ORDER BY name")
    uplines = cursor.fetchall()
    
    html = '''
    <h1>Set Upline Relationships</h1>
    <p><a href="/admin/dashboard">← Back</a></p>
    
    <form action="/admin/update-upline" method="post">
    <table border="1" style="width: 100%;">
        <tr>
            <th>Agent</th>
            <th>Current Upline</th>
            <th>Set New Upline</th>
        </tr>'''
    
    for agent in agents:
        agent_id, agent_name = agent
        
        # Get current upline
        cursor.execute('''
            SELECT upline_id, (SELECT name FROM users WHERE id = users.upline_id) 
            FROM users WHERE id = ?
        ''', (agent_id,))
        current = cursor.fetchone()
        current_upline = current[1] if current and current[0] else "None"
        
        html += f'''
        <tr>
            <td>{agent_name} (ID: {agent_id})</td>
            <td>{current_upline}</td>
            <td>
                <select name="upline_{agent_id}">
                    <option value="">-- No Upline --</option>'''
        
        for upline in uplines:
            upline_id, upline_name = upline
            if upline_id != agent_id:  # Can't be own upline
                selected = "selected" if current and current[0] == upline_id else ""
                html += f'<option value="{upline_id}" {selected}>{upline_name} (ID: {upline_id})</option>'
        
        html += '''
                </select>
            </td>
        </tr>'''
    
    html += '''
    </table>
    <br>
    <button type="submit">Update All Upline Relationships</button>
    </form>'''
    
    conn.close()
    return html

@app.route('/admin/update-upline', methods=['POST'])
def update_upline():
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get all agents
    cursor.execute("SELECT id FROM users WHERE role = 'agent'")
    agents = cursor.fetchall()
    
    for agent in agents:
        agent_id = agent[0]
        upline_id = request.form.get(f'upline_{agent_id}')
        
        if upline_id == "":
            upline_id = None
        
        cursor.execute('UPDATE users SET upline_id = ? WHERE id = ?', (upline_id, agent_id))
    
    conn.commit()
    conn.close()
    
    return redirect('/admin/set-upline')

@app.route('/admin/upline-payments')
def upline_payments():
    """Admin page to view and pay upline commissions"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get all pending upline commissions - FIXED QUERY
    cursor.execute('''
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
    ''')
    
    pending_commissions = cursor.fetchall()
    
    # Get statistics
    cursor.execute('SELECT COUNT(*), SUM(amount) FROM upline_commissions WHERE status = "pending"')
    stats = cursor.fetchone()
    
    conn.close()
    
    # Debug: Print commission structure
    print(f"Number of pending commissions: {len(pending_commissions)}")
    if pending_commissions:
        print(f"First commission structure: {pending_commissions[0]}")
        print(f"Number of columns: {len(pending_commissions[0])}")
    
    return render_template_string('''
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
                    <div style="font-size: 24px; font-weight: bold; color: #dc3545;">''' + str(stats[0] or 0) + '''</div>
                    <div style="color: #666; font-size: 14px;">Pending Payments</div>
                </div>
                <div>
                    <div style="font-size: 24px; font-weight: bold; color: #28a745;">RM''' + ("{:,.2f}".format(stats[1] or 0)) + '''</div>
                    <div style="color: #666; font-size: 14px;">Total Amount</div>
                </div>
            </div>
        </div>
        
        <h2 style="color: #333; margin-bottom: 20px;">📋 Pending Upline Commissions</h2>
        
        ''' + ('''
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
        ''' if pending_commissions else '') + '''
        
        ''' + (''.join(f'''
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
        ''' for c in pending_commissions) if pending_commissions else '''
                <tr>
                    <td colspan="9">
                        <div class="empty-state">
                            <h3>🎉 No pending upline commissions!</h3>
                            <p>All upline commissions have been paid.</p>
                        </div>
                    </td>
                </tr>
        ''') + '''
        
        ''' + ('''
            </tbody>
        </table>
        ''' if pending_commissions else '') + '''
        
        <a href="/admin/dashboard" class="back-link" style="font-weight: bold; color: #000; font-size: 16px; text-decoration: none; padding: 10px 0; display: inline-block; margin-top: 20px;">← Back to Dashboard</a>
        
        <script>
            // Confirmation for payment
            function confirmPayment(commissionId, amount, uplineName) {
                return confirm(`Pay RMRM{amount.toFixed(2)} to RM{uplineName}?`);
            }
        </script>
    </body>
    </html>
    ''')
    
@app.route('/admin/pay-upline/<int:commission_id>', methods=['GET', 'POST'])
@app.route('/admin/pay-upline/<int:commission_id>', methods=['GET', 'POST'])
def pay_upline_commission(commission_id):
    """Mark upline commission as paid - MATCHES YOUR TABLE STRUCTURE"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    if request.method == 'POST':
        payment_method = request.form.get('payment_method', '')
        transaction_id = request.form.get('transaction_id', '')
        notes = request.form.get('notes', '')
        
        conn = sqlite3.connect('real_estate.db')
        cursor = conn.cursor()
        
        try:
            # Get commission details
            cursor.execute('''
                SELECT uc.*, uu.name as upline_name, uu.email, 
                       pl.property_address, pl.customer_name
                FROM upline_commissions uc
                JOIN users uu ON uc.upline_id = uu.id
                LEFT JOIN property_listings pl ON uc.listing_id = pl.id
                WHERE uc.id = ?
            ''', (commission_id,))
            
            commission = cursor.fetchone()
            
            if not commission:
                conn.close()
                return "Commission not found", 404
            
            if commission[5] == 'paid':  # status column (index 5)
                conn.close()
                return redirect(f'/admin/payments?error=Commission+UC-{commission_id}+already+paid')
            
            # Update upline commission (YOUR TABLE STRUCTURE: id, listing_id, agent_id, upline_id, amount, status, notes, created_at, paid_at)
            cursor.execute('''
                UPDATE upline_commissions 
                SET status = 'paid', 
                    paid_at = ?,
                    notes = ?
                WHERE id = ?
            ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  f"Payment method: {payment_method}, Transaction: {transaction_id}",
                  commission_id))
            
            conn.commit()
            conn.close()
            
            return redirect(f'/admin/payments?success=Upline+commission+UC-{commission_id}+marked+as+paid+successfully!')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            print(f"Error paying upline commission: {e}")
            return redirect(f'/admin/payments?error=Failed+to+pay+upline+commission:+{str(e)}')
    
    # GET request - show payment form
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get commission details for display
    cursor.execute('''
        SELECT uc.amount, uc.upline_id, uu.name as upline_name, uu.email,
               pl.property_address, pl.customer_name
        FROM upline_commissions uc
        JOIN users uu ON uc.upline_id = uu.id
        LEFT JOIN property_listings pl ON uc.listing_id = pl.id
        WHERE uc.id = ?
    ''', (commission_id,))
    
    commission = cursor.fetchone()
    conn.close()
    
    if not commission:
        return "Commission not found", 404
    
    amount, upline_id, upline_name, upline_email, property_address, customer_name = commission
    
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Pay Upline Commission UC-{{ commission_id }}</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto; padding: 20px; background: #f5f5f5; }
            .form-box { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            h2 { margin-top: 0; color: #6f42c1; text-align: center; }
            .commission-details { 
                background: #f8f9fa; 
                padding: 15px; 
                border-radius: 5px; 
                margin: 15px 0; 
                border-left: 4px solid #6f42c1;
            }
            .amount { 
                text-align: center; 
                font-size: 28px; 
                font-weight: bold; 
                color: #6f42c1;
                margin: 15px 0;
            }
            .detail-row { display: flex; justify-content: space-between; margin: 8px 0; }
            .detail-label { font-weight: bold; color: #666; }
            label { display: block; margin-top: 15px; font-weight: bold; color: #333; }
            input, select, textarea { 
                width: 100%; padding: 10px; margin: 5px 0 15px 0; 
                border: 1px solid #ddd; border-radius: 5px; box-sizing: border-box;
            }
            button { 
                width: 100%; padding: 12px; background: #6f42c1; color: white; 
                border: none; border-radius: 5px; cursor: pointer; margin-top: 20px;
                font-size: 16px; font-weight: bold;
            }
            button:hover { background: #59359a; }
            .cancel-link { 
                display: block; text-align: center; margin-top: 15px; 
                color: #6c757d; text-decoration: none; padding: 10px;
            }
        </style>
    </head>
    <body>
        <div class="form-box">
            <h2>💰 Pay Upline Commission</h2>
            <h3 style="text-align: center; color: #6f42c1; margin-bottom: 5px;">UC-{{ commission_id }}</h3>
            
            <div class="commission-details">
                <div class="amount">RM{{ "{:,.2f}".format(amount or 0) }}</div>
                
                <div class="detail-row">
                    <span class="detail-label">Upline Agent:</span>
                    <span>{{ upline_name }} ({{ upline_email }})</span>
                </div>
                
                {% if property_address %}
                <div class="detail-row">
                    <span class="detail-label">Property:</span>
                    <span>{{ property_address }}</span>
                </div>
                {% endif %}
                
                {% if customer_name %}
                <div class="detail-row">
                    <span class="detail-label">Customer:</span>
                    <span>{{ customer_name }}</span>
                </div>
                {% endif %}
            </div>
            
            <form method="POST" onsubmit="return confirm('Confirm payment of RM{{ "{:,.2f}".format(amount or 0) }} to {{ upline_name }}?')">
                <label for="payment_method">Payment Method *</label>
                <select name="payment_method" id="payment_method" required>
                    <option value="">-- Select Payment Method --</option>
                    <option value="bank_transfer">🏦 Bank Transfer</option>
                    <option value="cash">💵 Cash</option>
                    <option value="check">📄 Check</option>
                    <option value="online_payment">🌐 Online Payment</option>
                    <option value="other">📝 Other</option>
                </select>
                
                <label for="transaction_id">Transaction/Reference ID</label>
                <input type="text" name="transaction_id" id="transaction_id" placeholder="e.g., Bank Reference, Check Number">
                
                <button type="submit">✅ PAY UPLINE COMMISSION</button>
                
                <a href="/admin/payments" class="cancel-link">← Back to Payments</a>
            </form>
        </div>
    </body>
    </html>
    ''', commission_id=commission_id, amount=amount, upline_name=upline_name, 
        upline_email=upline_email, property_address=property_address, customer_name=customer_name)

@app.route('/admin/payment/<int:payment_id>')
def payment_details(payment_id):
    """View payment details"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    try:
        # First check what columns exist in projects table
        cursor.execute("PRAGMA table_info(projects)")
        project_columns = [col[1] for col in cursor.fetchall()]
        print(f"Projects table columns: {project_columns}")
        
        # Use appropriate column name for project name
        project_name_column = 'name' if 'name' in project_columns else 'project_name' if 'project_name' in project_columns else 'title'
        
        # Get payment details with proper joins
        query = f'''
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
        '''
        
        print(f"Payment details query: {query}")
        
        cursor.execute(query, (payment_id,))
        
        payment = cursor.fetchone()
        
        if not payment:
            conn.close()
            return "Payment not found", 404
        
        print(f"Payment data fetched: {len(payment) if payment else 0} columns")
        print(f"Payment columns: {payment}")
        
        # Get additional listing details
        cursor.execute('''
            SELECT pl.customer_email, pl.customer_phone, pl.closing_date, 
                   pl.status, pl.submitted_at, pl.approved_at, pl.commission_status
            FROM property_listings pl
            WHERE pl.id = ?
        ''', (payment[1],))  # listing_id
        
        listing_details = cursor.fetchone()
        
        conn.close()
        
        # Prepare payment data dictionary
        payment_data = {
            'id': payment[0],
            'listing_id': payment[1],
            'agent_id': payment[2],
            'commission_amount': payment[3],
            'payment_status': payment[4],
            'payment_date': payment[5],
            'payment_method': payment[6],
            'transaction_id': payment[7],
            'paid_by': payment[8],
            'updated_at': payment[9],
            'notes': payment[10],
            'created_at': payment[11],
            'agent_name': payment[12],
            'agent_email': payment[13],
            'customer_name': payment[14],
            'property_address': payment[15],
            'sale_price': payment[16],
            'commission_rate': payment[17],
            'project_name': payment[18]
        }
        
        # Add listing details if available
        if listing_details:
            payment_data.update({
                'customer_email': listing_details[0],
                'customer_phone': listing_details[1],
                'closing_date': listing_details[2],
                'listing_status': listing_details[3],
                'submitted_at': listing_details[4],
                'approved_at': listing_details[5],
                'commission_status': listing_details[6]
            })
        
        # Format the commission rate
        if payment_data['commission_rate']:
            payment_data['commission_rate'] = f"{payment_data['commission_rate']}%"
        
        # Debug: Print what we have
        print(f"Payment data prepared:")
        for key, value in payment_data.items():
            print(f"  {key}: {value}")
        
        return render_template_string('''
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
        ''', payment_data=payment_data)
        
    except Exception as e:
        conn.close()
        print(f"Error fetching payment details: {e}")
        return f"Error loading payment details: {str(e)}", 500

@app.route('/admin/payment/<int:payment_id>/mark-paid', methods=['GET', 'POST'])
def mark_payment_paid(payment_id):
    """Mark payment as paid - SIMPLIFIED VERSION without voucher"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    if request.method == 'POST':
        payment_method = request.form.get('payment_method', '')
        transaction_id = request.form.get('transaction_id', '')
        notes = request.form.get('notes', '')
        
        conn = sqlite3.connect('real_estate.db')
        cursor = conn.cursor()
        
        try:
            # Get payment details
            cursor.execute('''
                SELECT cp.agent_id, cp.commission_amount, u.email, u.name
                FROM commission_payments cp
                JOIN users u ON cp.agent_id = u.id
                WHERE cp.id = ?
            ''', (payment_id,))
            payment_info = cursor.fetchone()
            
            if not payment_info:
                conn.close()
                return "Payment not found", 404
            
            # Update payment record
            cursor.execute('''
                UPDATE commission_payments 
                SET payment_status = 'paid',
                    payment_date = ?,
                    payment_method = ?,
                    transaction_id = ?,
                    notes = ?,
                    updated_at = ?,
                    paid_by = ?
                WHERE id = ?
            ''', (datetime.now().strftime('%Y-%m-%d'),
                  payment_method,
                  transaction_id,
                  notes,
                  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  session['user_id'],
                  payment_id))
            
            # Update the property listing commission status
            cursor.execute('''
                UPDATE property_listings 
                SET commission_status = 'paid'
                WHERE id = (SELECT listing_id FROM commission_payments WHERE id = ?)
            ''', (payment_id,))
            
            conn.commit()
            conn.close()
            
            # Redirect with success message
            return redirect(f'/admin/payments?success=Payment+#{payment_id}+marked+as+paid+successfully!')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            print(f"Error marking payment as paid: {e}")
            return redirect(f'/admin/payments?error=Payment+failed:+{str(e)}')
    
    # GET request - show payment form (simplified without voucher option)
    mark_paid_template = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Mark Payment as Paid</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 500px; margin: 50px auto; padding: 20px; background: #f5f5f5; }
            .form-box { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            h2 { margin-top: 0; color: #28a745; text-align: center; }
            .payment-info { 
                background: #f8f9fa; 
                padding: 15px; 
                border-radius: 5px; 
                margin: 15px 0; 
                border-left: 4px solid #28a745;
                font-size: 14px;
                color: #555;
            }
            .payment-info strong { color: #28a745; }
            label { display: block; margin-top: 15px; font-weight: bold; color: #333; }
            input, select, textarea { 
                width: 100%; 
                padding: 10px; 
                margin: 5px 0 15px 0; 
                border: 1px solid #ddd; 
                border-radius: 5px; 
                box-sizing: border-box;
                font-size: 14px;
            }
            input:focus, select:focus, textarea:focus { 
                border-color: #28a745; 
                outline: none; 
                box-shadow: 0 0 5px rgba(40, 167, 69, 0.2);
            }
            button { 
                width: 100%; 
                padding: 12px; 
                background: #28a745; 
                color: white; 
                border: none; 
                border-radius: 5px; 
                cursor: pointer; 
                margin-top: 20px;
                font-size: 16px;
                font-weight: bold;
            }
            button:hover { background: #218838; }
            .cancel-link { 
                display: block; 
                text-align: center; 
                margin-top: 15px; 
                color: #6c757d; 
                text-decoration: none;
                padding: 10px;
            }
            .cancel-link:hover { 
                text-decoration: underline;
            }
            .required::after { 
                content: " *"; 
                color: #dc3545; 
            }
        </style>
    </head>
    <body>
        <div class="form-box">
            <h2>💰 Mark Payment as Paid</h2>
            <h3 style="text-align: center; color: #333; margin-bottom: 20px;">Payment #{{ payment_id }}</h3>
            
            <div class="payment-info">
                <p><strong>⚠️ Important:</strong> This action will:</p>
                <ol>
                    <li>Mark payment #{{ payment_id }} as <strong style="color: #28a745;">PAID</strong></li>
                    <li>Update commission status in the original listing</li>
                    <li>Record payment date and method</li>
                    <li>This action cannot be undone</li>
                </ol>
            </div>
            
            <form method="POST" onsubmit="return confirm('Are you sure you want to mark Payment #{{ payment_id }} as PAID?')">
                <label for="payment_method" class="required">Payment Method</label>
                <select name="payment_method" id="payment_method" required>
                    <option value="">-- Select Payment Method --</option>
                    <option value="bank_transfer">🏦 Bank Transfer</option>
                    <option value="cash">💵 Cash</option>
                    <option value="check">📄 Check</option>
                    <option value="online_payment">🌐 Online Payment</option>
                    <option value="credit_card">💳 Credit Card</option>
                    <option value="other">📝 Other</option>
                </select>
                
                <label for="transaction_id">Transaction/Reference ID</label>
                <input type="text" name="transaction_id" id="transaction_id" 
                       placeholder="e.g., BANK-REF-12345, Check #789, TxnID-ABC123">
                
                <label for="notes">Payment Notes (Optional)</label>
                <textarea name="notes" id="notes" rows="3" 
                          placeholder="Any additional notes about this payment..."></textarea>
                
                <button type="submit">
                    ✅ CONFIRM & MARK AS PAID
                </button>
                
                <a href="/admin/payment/{{ payment_id }}" class="cancel-link">
                    ← Cancel and Return to Payment Details
                </a>
            </form>
        </div>
        
        <script>
            document.addEventListener('DOMContentLoaded', function() {
                // Add placeholder hints based on payment method
                const paymentMethod = document.getElementById('payment_method');
                const transactionId = document.getElementById('transaction_id');
                
                paymentMethod.addEventListener('change', function() {
                    const method = this.value;
                    switch(method) {
                        case 'bank_transfer':
                            transactionId.placeholder = 'e.g., Bank Reference Number, Transaction ID';
                            break;
                        case 'check':
                            transactionId.placeholder = 'e.g., Check Number, Bank Name';
                            break;
                        case 'online_payment':
                            transactionId.placeholder = 'e.g., PayPal ID, Stripe Charge ID';
                            break;
                        case 'credit_card':
                            transactionId.placeholder = 'e.g., Last 4 digits, Authorization Code';
                            break;
                        default:
                            transactionId.placeholder = 'e.g., Reference Number, Transaction ID';
                    }
                });
            });
        </script>
    </body>
    </html>
    '''
    
    # Get allowed payment methods (simple list)
    payment_methods = ['bank_transfer', 'cash', 'check', 'online_payment', 'credit_card', 'other']
    
    return render_template_string(mark_paid_template, 
                                 payment_id=payment_id,
                                 payment_methods=payment_methods)

# ============ BATCH PAYMENT PROCESSING ============

@app.route('/admin/batch-payments', methods=['GET', 'POST'])
def batch_payments():
    """Batch process multiple payments - SIMPLER WORKING VERSION"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    if request.method == 'POST':
        # Get selected payment IDs
        selected_payments = request.form.getlist('payment_ids')
        payment_method = request.form.get('payment_method', 'bank_transfer')
        transaction_id = request.form.get('transaction_id', '')
        notes = request.form.get('notes', '')
        
        if not selected_payments:
            conn.close()
            return redirect('/admin/batch-payments?error=No payments selected')
        
        # Process each selected payment
        processed_count = 0
        today = datetime.now().strftime('%Y-%m-%d')
        
        for payment_id in selected_payments:
            try:
                # Update payment record
                cursor.execute('''
                    UPDATE commission_payments 
                    SET payment_status = 'paid',
                        payment_date = ?,
                        payment_method = ?,
                        transaction_id = ?,
                        notes = COALESCE(notes || ' | ', '') || ?,
                        updated_at = ?,
                        paid_by = ?
                    WHERE id = ? AND payment_status = 'pending'
                ''', (today,
                      payment_method,
                      transaction_id,
                      f"Batch processed on {today}",
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      session['user_id'],
                      payment_id))
                
                # Update the property listing commission status
                cursor.execute('''
                    UPDATE property_listings 
                    SET commission_status = 'paid'
                    WHERE id = (
                        SELECT listing_id FROM commission_payments WHERE id = ?
                    )
                ''', (payment_id,))
                
                processed_count += 1
                
            except Exception as e:
                print(f"Error processing payment {payment_id}: {e}")
                continue
        
        conn.commit()
        conn.close()
        
        if processed_count > 0:
            return redirect(f'/admin/payments?success={processed_count} payments processed successfully')
        else:
            return redirect('/admin/payments?error=No payments were processed')
    
    # GET request - show batch payment page
    # Get all pending payments
    cursor.execute('''
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
    ''')
    
    pending_payments = cursor.fetchall()
    
    # Calculate totals
    total_amount = sum([p[1] for p in pending_payments]) if pending_payments else 0
    total_count = len(pending_payments)
    
    # Get today's date for default transaction ID
    today_str = datetime.now().strftime('%Y%m%d')
    
    conn.close()
    
    # Create a simple HTML string without complex template syntax
    html_content = f'''
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
    '''
    
    if pending_payments:
        html_content += f'''
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
        '''
        
        for payment in pending_payments:
            html_content += f'''
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
            '''
        
        html_content += f'''
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
        '''
    else:
        html_content += f'''
        <div class="empty-state">
            <h3>✅ No Pending Payments!</h3>
            <p>All commission payments have been processed. Great job!</p>
            <div style="margin-top: 20px;">
                <a href="/admin/payments" class="btn">Back to Payments</a>
                <a href="/admin/dashboard" class="btn btn-secondary">Go to Dashboard</a>
            </div>
        </div>
        '''
    
    html_content += '''
    </body>
    </html>
    '''
    
    return html_content


@app.route('/download/<int:doc_id>')
def download_document(doc_id):
    """Download document"""
    if 'user_id' not in session:
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM documents WHERE id = ?', (doc_id,))
    doc = cursor.fetchone()
    conn.close()
    
    if doc and os.path.exists(doc[3]):
        return send_file(doc[3], as_attachment=True, download_name=doc[2])
    else:
        return "File not found", 404

@app.route('/admin/sync-payments')
def sync_payments():
    """Create payment records for approved but unpaid commissions (BOTH agent and upline)"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    try:
        messages = []
        total_created = 0
        
        # ============ 1. AGENT COMMISSION PAYMENTS ============
        cursor.execute('''
            SELECT pl.id, pl.agent_id, pl.commission_amount, pl.approved_at
            FROM property_listings pl
            LEFT JOIN commission_payments cp ON pl.id = cp.listing_id AND cp.agent_id = pl.agent_id
            WHERE pl.status = 'approved'
              AND (pl.commission_status IS NULL OR pl.commission_status != 'paid')
              AND cp.id IS NULL
        ''')
        
        pending_agent_commissions = cursor.fetchall()
        agent_created = 0
        
        for listing_id, agent_id, commission_amount, approved_at in pending_agent_commissions:
            # Check if payment already exists
            cursor.execute('SELECT id FROM commission_payments WHERE listing_id = ? AND agent_id = ?', (listing_id, agent_id))
            if cursor.fetchone():
                continue
                
            # Get project name for notes
            cursor.execute('''
                SELECT p.project_name, pl.customer_name
                FROM property_listings pl
                LEFT JOIN projects p ON pl.project_id = p.id
                WHERE pl.id = ?
            ''', (listing_id,))
            project_info = cursor.fetchone()
            
            project_name = project_info[0] if project_info else None
            customer_name = project_info[1] if project_info and project_info[1] else f'listing #{listing_id}'
            
            # Create agent notes
            if project_name:
                agent_notes = f'Agent commission for {project_name} - 95% of RM{commission_amount:,.2f}'
            else:
                agent_notes = f'Agent commission for {customer_name} - 95% of RM{commission_amount:,.2f}'
            
            # Create agent payment record
            cursor.execute('''
                INSERT INTO commission_payments 
                (listing_id, agent_id, commission_amount, payment_status, created_at, updated_at, notes)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
            ''', (listing_id, agent_id, commission_amount * 0.95, 
                  approved_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                  agent_notes))
            
            agent_created += 1
        
        if agent_created > 0:
            messages.append(f"Created {agent_created} agent payment(s)")
            total_created += agent_created
        
        # ============ 2. UPLINE COMMISSION PAYMENTS ============
        # Find approved listings where agent has an upline, but no upline commission exists
        # FIXED: Using NOT EXISTS to properly check for duplicates
        cursor.execute('''
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
        ''')
        
        pending_upline_commissions = cursor.fetchall()
        upline_created = 0
        
        for (listing_id, agent_id, upline_id, commission_amount, 
             approved_at, agent_name, upline_name, project_name) in pending_upline_commissions:
            
            # Calculate upline commission (5% of agent's commission)
            upline_commission = commission_amount * 0.05  # 5% upline share
            
            # Create upline notes
            if project_name:
                upline_notes = f'Upline commission from agent {agent_name} for {project_name} - 5% of RM{commission_amount:,.2f}'
            else:
                upline_notes = f'Upline commission from agent {agent_name} - 5% of RM{commission_amount:,.2f}'
            
            # Create upline commission record
            cursor.execute('''
                INSERT INTO upline_commissions 
                (listing_id, agent_id, upline_id, amount, status, notes, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
            ''', (listing_id, agent_id, upline_id, upline_commission,
                  upline_notes,
                  approved_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            
            # Also create commission_payments record for upline
            cursor.execute('''
                SELECT id FROM commission_payments 
                WHERE listing_id = ? AND agent_id = ? AND commission_amount = ?
            ''', (listing_id, upline_id, upline_commission))
            
            if not cursor.fetchone():
                # Create commission payment for upline
                cursor.execute('''
                    INSERT INTO commission_payments
                    (listing_id, agent_id, commission_amount, payment_status, created_at, updated_at, notes)
                    VALUES (?, ?, ?, 'pending', ?, ?, ?)
                ''', (listing_id, upline_id, upline_commission,
                      approved_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      upline_notes))
            
            upline_created += 1
        
        if upline_created > 0:
            messages.append(f"Created {upline_created} upline payment(s)")
            total_created += upline_created
        
        # ============ 3. UPDATE COMMISSION STATUSES ============
        # Update commission status for approved listings
        cursor.execute('''
            UPDATE property_listings 
            SET commission_status = 'pending'
            WHERE status = 'approved' 
              AND (commission_status IS NULL OR commission_status = 'approved')
        ''')
        
        conn.commit()
        conn.close()
        
        if total_created > 0:
            message = " | ".join(messages)
            return redirect(f'/admin/payments?success={message}')
        else:
            return redirect('/admin/payments?info=No pending payments need to be created. All approved listings already have payment records.')
            
    except Exception as e:
        conn.rollback()
        conn.close()
        print(f"Error in sync_payments: {e}")
        return redirect(f'/admin/payments?error=Sync failed: {str(e)}')


@app.route('/admin/fix-payments')
def fix_payments():
    """Quick fix for payment synchronization"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    try:
        # SQL to create missing payment records
        cursor.execute('''
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
        ''')
        
        created = cursor.rowcount
        
        # Update commission status
        cursor.execute('''
            UPDATE property_listings 
            SET commission_status = 'pending' 
            WHERE status = 'approved' AND (commission_status IS NULL OR commission_status = 'approved')
        ''')
        
        conn.commit()
        conn.close()
        
        return redirect(f'/admin/payments?success={created} payment records created. Refresh the batch payments page.')
        
    except Exception as e:
        conn.rollback()
        conn.close()
        return redirect(f'/admin/payments?error=Fix failed: {str(e)}')

@app.route('/admin/create-project', methods=['GET', 'POST'])
def create_project():
    """Create a new project"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    if request.method == 'POST':
        # Get form data
        data = request.form
        sale_type = data.get('sale_type', 'sales')  # Default to sales
        print(f"DEBUG: Form data received: {dict(request.form)}")
        
        try:
            conn = sqlite3.connect('real_estate.db')
            cursor = conn.cursor()
            
            # Debug check for table structure
            cursor.execute("PRAGMA table_info(projects)")
            columns = [col[1] for col in cursor.fetchall()]
            print(f"DEBUG: Current columns in 'projects' table: {columns}")
            
            # Check if project_sale_type column exists
            if 'project_sale_type' not in columns:
                print("DEBUG: Adding 'project_sale_type' column to projects table...")
                try:
                    cursor.execute('ALTER TABLE projects ADD COLUMN project_sale_type TEXT DEFAULT "sales"')
                    conn.commit()
                    print("DEBUG: Column added successfully!")
                except Exception as alter_error:
                    print(f"DEBUG: Error adding column: {alter_error}")
            
            # Get project_sale_type (default to 'sales' if not provided)
            project_sale_type = data.get('project_sale_type', 'sales')
            print(f"DEBUG: Project sale type: {project_sale_type}")
            
            # Get all required fields with defaults
            project_name = data.get('name', '').strip()
            description = data.get('description', '').strip()
            location = data.get('location', '').strip()
            project_type = data.get('project_type', 'residential')
            category = data.get('category', 'condo')
            commission_rate = float(data.get('project_commission', 3.0))
            
            # Validate required fields
            if not project_name or not location:
                flash('❌ Project Name and Location are required', 'error')
                return redirect('/admin/create-project')
            
            print(f"DEBUG: Inserting project: {project_name}")
            print(f"DEBUG: Commission rate: {commission_rate}")
            print(f"DEBUG: Sale type: {project_sale_type}")
            
            # Insert project - FIXED: using project_name instead of name
            cursor.execute('''
                INSERT INTO projects 
                (project_name, description, location, project_type, category, 
                 commission_rate, project_sale_type, created_by, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                project_name,
                description,
                location,
                project_type,
                category,
                commission_rate,
                project_sale_type,
                session['user_id'],
                'active'
            ))
            
            project_id = cursor.lastrowid
            print(f"DEBUG: Project created with ID: {project_id}")
            
            # Handle units
            unit_counter = 1
            units_added = 0
            while f'unit_code_{unit_counter}' in data:
                unit_code = data.get(f'unit_code_{unit_counter}', '').strip()
                unit_type = data.get(f'unit_type_{unit_counter}', '').strip()
                unit_price = data.get(f'unit_price_{unit_counter}', '').strip()
                unit_size = data.get(f'unit_size_{unit_counter}', '').strip()
                unit_commission = data.get(f'unit_commission_{unit_counter}', '').strip()
    
                if unit_code:
                    # Convert empty strings to None
                    price = float(unit_price) if unit_price else None
                    size = float(unit_size) if unit_size else None
                    commission = float(unit_commission) if unit_commission else None
                
                    # ✅ UPDATED: Use correct column names that match database
                    cursor.execute('''
                        INSERT INTO project_units 
                        (project_id, unit_code, unit_type, base_price, square_feet, 
                        commission_rate, quantity, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        project_id,
                        unit_code,
                        unit_type,
                        price,        # Maps to base_price column
                        size,         # Maps to square_feet column
                        commission,   # commission_rate column already exists
                        1,
                        'available'
                    ))
                    units_added += 1
                    print(f"DEBUG: Added unit: {unit_code}")
                
                unit_counter += 1
            
            print(f"DEBUG: Total units added: {units_added}")
            
            conn.commit()
            conn.close()
            print("DEBUG: Database changes committed successfully")
            
            flash(f'✅ Project "{project_name}" created successfully!', 'success')
            return redirect('/admin/projects')
            
        except Exception as e:
            print(f"DEBUG: ERROR occurred: {str(e)}")
            import traceback
            print(f"DEBUG: Full traceback:\n{traceback.format_exc()}")
            flash(f'❌ Error creating project: {str(e)}', 'error')
            return redirect('/admin/create-project')
    
    # GET request - show form
    # ✅ FIX 3: Updated HTML with Sales/Rental dropdown
    return '''
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
    '''

@app.route('/admin/edit-project/<int:project_id>', methods=['GET', 'POST'])
def edit_project(project_id):
    """Edit existing project"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get project details
    cursor.execute('SELECT * FROM projects WHERE id = ?', (project_id,))
    project = cursor.fetchone()
    
    if not project:
        conn.close()
        return "Project not found", 404
    
    # Get existing units
    cursor.execute('SELECT * FROM project_units WHERE project_id = ? ORDER BY unit_type', (project_id,))
    existing_units = cursor.fetchall()
    
    if request.method == 'POST':
        try:
            data = request.form
            sale_type = data.get('sale_type', 'sales')  # Default to sales
            
            # Update main project
            cursor.execute('''
                UPDATE projects 
                SET project_name = ?,
                    category = ?,
                    project_type = ?,
                    location = ?,
                    description = ?,
                    commission_rate = ?,
                    updated_at = ?
                WHERE id = ?
            ''', (
                data['project_name'],
                data['category'],
                data['project_type'],
                data.get('location', ''),
                data.get('description', ''),
                float(data.get('project_commission', 0)),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                project_id
            ))
            
            # Delete existing units (we'll recreate them)
            cursor.execute('DELETE FROM project_units WHERE project_id = ?', (project_id,))
            
            # Handle unit types - dynamic form fields
            unit_counter = 0
            while f'unit_type_{unit_counter}' in data:
                unit_type = data.get(f'unit_type_{unit_counter}')
                square_feet = data.get(f'square_feet_{unit_counter}')
                base_price = data.get(f'base_price_{unit_counter}')
                rental_price = data.get(f'rental_price_{unit_counter}')
                unit_commission = data.get(f'unit_commission_{unit_counter}')
                quantity = data.get(f'quantity_{unit_counter}', 1)
                
                if unit_type:  # Only insert if unit type is provided
                    cursor.execute('''
                        INSERT INTO project_units 
                        (project_id, unit_type, square_feet, base_price, rental_price, 
                         commission_rate, quantity, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'available')
                    ''', (
                        project_id,
                        unit_type,
                        int(square_feet) if square_feet else None,
                        float(base_price) if base_price else None,
                        float(rental_price) if rental_price else None,
                        float(unit_commission) if unit_commission else None,
                        int(quantity) if quantity else 1
                    ))
                
                unit_counter += 1
            
            conn.commit()
            conn.close()
            
            return redirect(f'/admin/project/{project_id}?success=Project updated successfully!')
            
        except Exception as e:
            conn.rollback()
            conn.close()
            return f'''
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
            '''
    
    # GET request - show edit form
    # Build existing units JavaScript data
    units_js_data = []
    for unit in existing_units:
        units_js_data.append({
            'unit_type': unit[2],
            'square_feet': unit[3] or '',
            'base_price': unit[4] or '',
            'rental_price': unit[5] or '',
            'commission_rate': unit[6] or '',
            'quantity': unit[7] or 1
        })
    
    conn.close()
    
    edit_project_template = f'''
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
    '''
    
    return edit_project_template

@app.route('/admin/projects')
def list_projects():
    """List all projects"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get all projects
    cursor.execute('''
        SELECT p.*, u.name as created_by_name, 
               COUNT(pu.id) as unit_count,
               SUM(pu.quantity) as total_units
        FROM projects p
        LEFT JOIN users u ON p.created_by = u.id
        LEFT JOIN project_units pu ON p.id = pu.project_id
        GROUP BY p.id
        ORDER BY p.created_at DESC
    ''')
    
    projects = cursor.fetchall()
    
    conn.close()
    
    projects_template = '''
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
            .btn-delete { background: #dc3545; color: white; }
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
                <div class="stat-value" style="color: #007bff;">{{ projects|length }}</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Active Projects</div>
                <div class="stat-value" style="color: #28a745;">{{ active_count }}</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Total Units</div>
                <div class="stat-value" style="color: #6f42c1;">{{ total_units }}</div>
            </div>
            <div class="stat-card">
                <div style="font-size: 14px; color: #666;">Sales Projects</div>
                <div class="stat-value" style="color: #fd7e14;">{{ sales_count }}</div>
            </div>
        </div>
        
        {% if projects %}
        <div class="project-grid">
            {% for project in projects %}
            <div class="project-card">
                <div class="project-header">
                    <h3 style="margin: 0;">{{ project[1] }}</h3>
                    <div style="margin-top: 5px; font-size: 14px;">
                        <span class="badge badge-{{ project[2] }}">{{ project[2]|title }}</span>
                        <span class="badge badge-{{ project[3] }}">{{ project[3]|title }}</span>
                    </div>
                </div>
                <div class="project-body">
                    <div class="project-meta">
                        <div>
                            <strong>Location:</strong><br>
                            {{ project[4] or 'Not specified' }}
                        </div>
                        <div>
                            <strong>Commission:</strong><br>
                            {{ project[7] or 'N/A' }}%
                        </div>
                    </div>
                    
                    <div class="project-meta">
                        <div>
                            <strong>Units:</strong><br>
                            {{ project[12] or 0 }} types<br>
                            {{ project[13] or 0 }} total
                        </div>
                        <div>
                            <strong>Created:</strong><br>
                            {{ project[10][:10] }}<br>
                            by {{ project[11] }}
                        </div>
                    </div>
                    
                    <div style="margin-top: 15px;">
                        <a href="/admin/project/{{ project[0] }}" class="btn btn-view">👁️ View Details</a>
                        <a href="/admin/edit-project/{{ project[0] }}" class="btn btn-edit">✏️ Edit</a>
                        <a href="/admin/delete-project/{{ project[0] }}" class="btn btn-delete" onclick="return confirm('Delete this project?')">🗑️ Delete</a>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        {% else %}
        <div style="padding: 40px; text-align: center; background: white; border-radius: 10px;">
            <h3>No projects found</h3>
            <p>You haven't created any projects yet.</p>
            <a href="/admin/create-project" class="btn" style="background: #28a745; color: white; padding: 10px 20px; margin-top: 15px;">Create Your First Project</a>
        </div>
        {% endif %}
    </body>
    </html>
    '''
    
    # Calculate stats
    active_count = sum(1 for p in projects if p[6] == 'active')
    total_units = sum(p[13] or 0 for p in projects)
    sales_count = sum(1 for p in projects if p[2] == 'sales')
    
    return render_template_string(projects_template, 
        projects=projects, 
        active_count=active_count,
        total_units=total_units,
        sales_count=sales_count)

@app.route('/admin/project/<int:project_id>')
def view_project(project_id):
    """View project details"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get project details
    cursor.execute('''
        SELECT p.*, u.name as created_by_name
        FROM projects p
        LEFT JOIN users u ON p.created_by = u.id
        WHERE p.id = ?
    ''', (project_id,))
    
    project = cursor.fetchone()
    
    if not project:
        conn.close()
        return "Project not found", 404
    
    # Get project units
    cursor.execute('SELECT * FROM project_units WHERE project_id = ? ORDER BY unit_type', (project_id,))
    units = cursor.fetchall()
    
    conn.close()
    
    # Determine price label based on project_sale_type
    price_label = "Sale Price" if project[11] == 'sales' else "Monthly Rent"
    
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
            if project[11] == 'rental':
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
            
            status_color = '#28a745' if unit[8] == 'available' else '#dc3545'
            
            units_html += f'''
            <tr>
                <td>{unit[11] or 'N/A'}</td>
                <td>{unit[2] or 'N/A'}</td>
                <td>{unit[3] or 'N/A'}</td>
                <td>{formatted_price}</td>
                <td>{formatted_commission}</td>
                <td>{unit[7] or 1}</td>
                <td><span style="color: {status_color}">{unit[8].title()}</span></td>
            </tr>
            '''
    else:
        units_html = '''
        <tr>
            <td colspan="7" style="text-align: center; padding: 40px; color: #666;">
                <h3>No units defined yet</h3>
                <p>Add unit types to this project by editing it.</p>
            </td>
        </tr>
        '''
    
    # Create the template with corrected price_label
    detail_template = f'''<!DOCTYPE html>
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
</html>'''
    
    return detail_template

@app.route('/admin/export-data')
def export_data():
    """Export data to CSV/Excel"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    export_type = request.args.get('type', 'csv')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    if export_type == 'commissions':
        # Export commission data
        cursor.execute('''
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
        ''')
        data = cursor.fetchall()
        filename = f"commissions_export_{datetime.now().strftime('%Y%m%d')}.csv"
        csv_content = "Listing ID,Customer Name,Customer Email,Property Address,Property Type,Sale Price,Commission,Status,Submitted Date,Approved Date,Agent Name,Agent Tier\n"
        
        for row in data:
            # Escape commas in CSV
            row_escaped = []
            for item in row:
                if item and ',' in str(item):
                    row_escaped.append(f'"{item}"')
                else:
                    row_escaped.append(str(item) if item else '')
            csv_content += ','.join(row_escaped) + '\n'
        
        response = app.response_class(
            response=csv_content,
            status=200,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        return response
    
    elif export_type == 'agents':
        # Export agent data
        cursor.execute('''
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
        ''')
        data = cursor.fetchall()
        filename = f"agents_export_{datetime.now().strftime('%Y%m%d')}.csv"
        csv_content = "Agent ID,Name,Email,Tier,Created Date,Total Listings,Total Commission\n"
        
        for row in data:
            row_escaped = []
            for item in row:
                if item and ',' in str(item):
                    row_escaped.append(f'"{item}"')
                else:
                    row_escaped.append(str(item) if item else '')
            csv_content += ','.join(row_escaped) + '\n'
        
        response = app.response_class(
            response=csv_content,
            status=200,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        return response
    
    elif export_type == 'payments':
        # Export payment data
        cursor.execute('''
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
        ''')
        data = cursor.fetchall()
        filename = f"payments_export_{datetime.now().strftime('%Y%m%d')}.csv"
        csv_content = "Payment ID,Listing ID,Agent ID,Amount,Status,Payment Date,Payment Method,Transaction ID,Created Date,Agent Name,Customer Name\n"
        
        for row in data:
            row_escaped = []
            for item in row:
                if item and ',' in str(item):
                    row_escaped.append(f'"{item}"')
                else:
                    row_escaped.append(str(item) if item else '')
            csv_content += ','.join(row_escaped) + '\n'
        
        response = app.response_class(
            response=csv_content,
            status=200,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        return response
    
    conn.close()
    
    # If no export type specified, show export options page
    export_template = '''
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
    '''
    
    return render_template_string(export_template)

@app.route('/admin/agent-performance')
def agent_performance_admin():
    """Admin view of agent performance analytics"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
    cursor = conn.cursor()
    
    # Get agent performance data - REMOVED agent_tier
    cursor.execute('''
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
    ''')
    
    agents_data = cursor.fetchall()
    
    # Get monthly performance data
    cursor.execute('''
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
    ''')
    
    monthly_data = cursor.fetchall()
    
    conn.close()
    
    # Prepare data for template - REMOVED tier references
    performance_template = '''
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
    '''
    
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
    
    avg_success_rate = round(sum(success_rates) / max(len(success_rates), 1)) if success_rates else 0
    
    return render_template_string(performance_template,
        agents_data=agents_data,
        monthly_data=monthly_data,
        agent_count=agent_count,
        total_commissions=total_commissions,
        total_sales=total_sales,
        avg_success_rate=avg_success_rate)

@app.route('/admin/export-full-db')
def export_full_database():
    """Export complete database as SQL and CSV files"""
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    try:
        # Create export directory
        export_dir = 'database_exports'
        if not os.path.exists(export_dir):
            os.makedirs(export_dir)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        export_filename = f'real_estate_db_export_{timestamp}'
        
        conn = sqlite3.connect('real_estate.db')
        cursor = conn.cursor()
        
        # Get all table names
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]
        
        # Create SQL dump
        sql_dump = f'-- Real Estate Database Export\n-- Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n-- Tables: {len(tables)}\n\n'
        
        # Create ZIP file in memory
        from io import BytesIO
        import zipfile
        
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            
            # 1. Export as SQL
            for table in tables:
                # Get table schema
                cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
                schema = cursor.fetchone()[0]
                
                sql_dump += f'--\n-- Table: {table}\n--\n\n'
                sql_dump += f'{schema};\n\n'
                
                # Get table data
                cursor.execute(f"SELECT * FROM {table}")
                rows = cursor.fetchall()
                
                if rows:
                    # Get column names
                    cursor.execute(f"PRAGMA table_info({table})")
                    columns = [col[1] for col in cursor.fetchall()]
                    
                    sql_dump += f'-- Data for table {table} ({len(rows)} rows)\n'
                    
                    for row in rows:
                        values = []
                        for value in row:
                            if value is None:
                                values.append('NULL')
                            elif isinstance(value, (int, float)):
                                values.append(str(value))
                            else:
                                # Escape single quotes in strings
                                escaped = str(value).replace("'", "''")
                                values.append(f"'{escaped}'")
                        
                        sql_dump += f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)});\n"
                
                sql_dump += '\n'
            
            # Add SQL file to zip
            zip_file.writestr(f'{export_filename}.sql', sql_dump)
            
            # 2. Export each table as CSV
            for table in tables:
                cursor.execute(f"SELECT * FROM {table}")
                rows = cursor.fetchall()
                
                if rows:
                    # Get column names
                    cursor.execute(f"PRAGMA table_info({table})")
                    columns = [col[1] for col in cursor.fetchall()]
                    
                    # Create CSV content
                    csv_content = ','.join(columns) + '\n'
                    
                    for row in rows:
                        row_data = []
                        for value in row:
                            if value is None:
                                row_data.append('')
                            elif isinstance(value, (int, float)):
                                row_data.append(str(value))
                            else:
                                # Escape commas and quotes in CSV
                                escaped = str(value).replace('"', '""')
                                if ',' in escaped or '"' in escaped or '\n' in escaped:
                                    escaped = f'"{escaped}"'
                                row_data.append(escaped)
                        
                        csv_content += ','.join(row_data) + '\n'
                    
                    # Add CSV file to zip
                    zip_file.writestr(f'{export_filename}/{table}.csv', csv_content)
            
            # 3. Create README file
            readme_content = f'''Real Estate Database Export
===============================

Export Details:
- Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
- Database: real_estate.db
- Tables exported: {len(tables)}
- Export ID: {export_filename}

Table Information:
{'-' * 40}

'''
            
            for table in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                readme_content += f'{table}: {count} rows\n'
            
            readme_content += f'''

Export Contents:
{'-' * 40}
1. {export_filename}.sql - Complete SQL dump of database
2. {export_filename}/ - Folder containing CSV files for each table

Usage:
- SQL file: Can be imported into any SQLite database
- CSV files: Can be opened in Excel, Google Sheets, or any spreadsheet software

Tables:
{'-' * 40}
'''
            
            for table in tables:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = cursor.fetchall()
                readme_content += f'\n{table}:\n'
                for col in columns:
                    col_name = col[1]
                    col_type = col[2]
                    col_notnull = 'NOT NULL' if col[3] else 'NULL'
                    col_pk = 'PRIMARY KEY' if col[5] else ''
                    readme_content += f'  - {col_name} ({col_type}) {col_notnull} {col_pk}\n'
            
            readme_content += f'''

Generated by Real Estate Sales System
Admin: {session.get('user_name', 'Unknown')}
'''

            zip_file.writestr(f'{export_filename}/README.txt', readme_content)
        
        conn.close()
        
        # Prepare response
        zip_buffer.seek(0)
        response = app.response_class(
            response=zip_buffer.getvalue(),
            status=200,
            mimetype='application/zip',
            headers={
                'Content-Disposition': f'attachment; filename={export_filename}.zip',
                'Content-Type': 'application/zip'
            }
        )
        
        return response
        
    except Exception as e:
        error_template = '''
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
        '''
        return render_template_string(error_template, error=str(e))

@app.route('/admin/check-db-structure')
def check_db_structure():
    if 'user_id' not in session or session['user_role'] != 'admin':
        return redirect('/login')
    
    conn = sqlite3.connect('real_estate.db')
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

# TEMPORARY: reset admin password
@app.route("/reset-admin")
def reset_admin():
    from werkzeug.security import generate_password_hash
    import sqlite3

    # Connect to the live SQLite database
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # Hash the new password
    hashed_pw = generate_password_hash("admin456***")

    # Update admin password using the admin email
    cursor.execute("UPDATE users SET password=? WHERE email=?", (hashed_pw, "admin@example.com"))

    conn.commit()
    conn.close()
    return "✅ Admin password updated!"

# ============ RUN APPLICATION ============
if __name__ == '__main__':
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