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


# ===================== TAIKO EA RANK & COMMISSION SYSTEM =====================

# ------------------------------------------------------------
# RANK THRESHOLDS — based on agent's personal cumulative gross
# ------------------------------------------------------------
TAIKO_RANKS = [
    {"rank": "REN",        "payout_pct": 70.0, "cumulative_target": 0},
    {"rank": "Assoc REN",  "payout_pct": 75.0, "cumulative_target": 30_000},
    {"rank": "Elite REN",  "payout_pct": 80.0, "cumulative_target": 90_000},
    {"rank": "TL",         "payout_pct": 85.0, "cumulative_target": 210_000},
    {"rank": "ATL",        "payout_pct": 90.0, "cumulative_target": 450_000},
]

def get_rank_for_cumulative(cumulative_gross):
    """Return the correct rank dict for a given cumulative gross amount."""
    current_rank = TAIKO_RANKS[0]
    for rank in TAIKO_RANKS:
        if cumulative_gross >= rank["cumulative_target"]:
            current_rank = rank
    return current_rank

def get_next_rank(current_rank_name):
    """Return the next rank dict above the current rank, or None if already ATL."""
    for i, rank in enumerate(TAIKO_RANKS):
        if rank["rank"] == current_rank_name and i + 1 < len(TAIKO_RANKS):
            return TAIKO_RANKS[i + 1]
    return None

# ------------------------------------------------------------
# RANK PROMOTION CHECK — called after each approved personal deal
# Q1: Only personal deals count toward cumulative gross
# Q2: Old rate applies to current deal; new rate from next deal
# ------------------------------------------------------------
def check_and_promote_agent(agent_id, conn=None):
    """
    Check if agent qualifies for a rank promotion after a personal deal is approved.
    Returns dict with promotion info if promoted, else None.
    Rule Q1: Only personal (own) deals count.
    Rule Q2: Promotion takes effect AFTER current deal — new rate from next deal.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT agent_rank, commission_rate, cumulative_gross, name, email
            FROM users WHERE id = ? AND role = 'agent'
            """,
            (agent_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None

        current_rank, current_rate, cumulative_gross, agent_name, agent_email = row
        cumulative_gross = float(cumulative_gross or 0)

        # Determine what rank they SHOULD be at
        new_rank_info = get_rank_for_cumulative(cumulative_gross)
        new_rank = new_rank_info["rank"]
        new_pct  = new_rank_info["payout_pct"]

        # Only act if rank has changed
        if new_rank == current_rank:
            return None

        # Update user's rank and commission rate
        cursor.execute(
            """
            UPDATE users
            SET agent_rank = ?, commission_rate = ?
            WHERE id = ?
            """,
            (new_rank, new_pct, agent_id)
        )

        # Log the promotion
        cursor.execute(
            """
            INSERT INTO rank_promotion_log
            (agent_id, old_rank, new_rank, old_pct, new_pct, cumulative_gross_at_promotion)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (agent_id, current_rank, new_rank, current_rate, new_pct, cumulative_gross)
        )

        conn.commit()

        promotion_info = {
            "promoted": True,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "old_rank": current_rank,
            "new_rank": new_rank,
            "old_pct": float(current_rate or 70),
            "new_pct": new_pct,
            "cumulative_gross": cumulative_gross,
        }

        # Notify agent
        create_agent_notification(
            agent_id=agent_id,
            notification_type="rank_promotion",
            title=f"🎉 Congratulations! You've been promoted to {new_rank}!",
            message=(
                f"You have been promoted from {current_rank} to {new_rank}! "
                f"Your new commission payout rate is {new_pct}%. "
                f"This new rate applies to your next deal onwards. "
                f"Total cumulative gross: RM {cumulative_gross:,.2f}"
            ),
            related_id=agent_id,
            related_type="agent",
            priority="high",
        )

        return promotion_info

    except Exception as e:
        print(f"Error in check_and_promote_agent: {e}")
        return None
    finally:
        if close_conn:
            conn.close()


# ------------------------------------------------------------
# ADMIN RANK OVERRIDE — manually set any agent to any rank
# Admin can promote OR demote, supply custom cumulative_gross
# and a reason. Logs as 'admin' in rank_promotion_log.
# ------------------------------------------------------------
def admin_set_agent_rank(agent_id, new_rank, new_cumulative_gross, reason, admin_id):
    """
    Admin manually assigns a rank (and custom cumulative_gross) to any agent.
    - Can promote OR demote freely
    - cumulative_gross set to admin-supplied value
    - Logs promoted_by = 'admin' with admin_id and reason
    - Notifies agent with an admin-assigned message
    Returns dict with result info, or raises on error.
    """
    valid_ranks = [r["rank"] for r in TAIKO_RANKS]
    if new_rank not in valid_ranks:
        raise ValueError(f"Invalid rank '{new_rank}'. Must be one of: {valid_ranks}")

    new_pct = next(r["payout_pct"] for r in TAIKO_RANKS if r["rank"] == new_rank)
    new_cumulative_gross = float(new_cumulative_gross or 0)

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT name, email, agent_rank, commission_rate, cumulative_gross
            FROM users WHERE id = ? AND role = 'agent'
            """,
            (agent_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Agent {agent_id} not found or is not an active agent.")

        agent_name, agent_email, old_rank, old_pct, old_cumulative = row
        old_pct = float(old_pct or 70)

        old_rank_idx = next((i for i, r in enumerate(TAIKO_RANKS) if r["rank"] == old_rank), 0)
        new_rank_idx = next((i for i, r in enumerate(TAIKO_RANKS) if r["rank"] == new_rank), 0)
        is_promotion = new_rank_idx > old_rank_idx
        is_demotion  = new_rank_idx < old_rank_idx

        cursor.execute(
            """
            UPDATE users
            SET agent_rank       = ?,
                commission_rate  = ?,
                cumulative_gross = ?
            WHERE id = ?
            """,
            (new_rank, new_pct, new_cumulative_gross, agent_id)
        )

        cursor.execute(
            """
            INSERT INTO rank_promotion_log
            (agent_id, old_rank, new_rank, old_pct, new_pct,
             cumulative_gross_at_promotion, promoted_by, admin_id, reason)
            VALUES (?, ?, ?, ?, ?, ?, 'admin', ?, ?)
            """,
            (agent_id, old_rank, new_rank, old_pct, new_pct,
             new_cumulative_gross, admin_id, reason)
        )

        conn.commit()

        if is_promotion:
            title   = f"\U0001f389 You have been promoted to {new_rank}!"
            message = (
                f"Your rank has been updated by the admin from {old_rank} to {new_rank}. "
                f"Your new commission payout rate is {new_pct:.0f}%. "
                f"This rate applies to your next deal onwards."
            )
        elif is_demotion:
            title   = f"\U0001f4cb Your rank has been updated to {new_rank}"
            message = (
                f"Your rank has been adjusted by the admin from {old_rank} to {new_rank}. "
                f"Your commission payout rate is now {new_pct:.0f}%. "
                f"Please contact your admin if you have any questions."
            )
        else:
            title   = f"\U0001f4cb Your rank record has been updated"
            message = (
                f"Your rank remains {new_rank} ({new_pct:.0f}%). "
                f"Your cumulative gross has been updated to RM {new_cumulative_gross:,.2f}."
            )

        if reason:
            message += f" Reason: {reason}"

        create_agent_notification(
            agent_id=agent_id,
            notification_type="rank_admin_update",
            title=title,
            message=message,
            related_id=agent_id,
            related_type="agent",
            priority="high",
        )

        return {
            "success":              True,
            "agent_id":             agent_id,
            "agent_name":           agent_name,
            "old_rank":             old_rank,
            "new_rank":             new_rank,
            "old_pct":              old_pct,
            "new_pct":              new_pct,
            "new_cumulative_gross": new_cumulative_gross,
            "action":               "promoted" if is_promotion else ("demoted" if is_demotion else "updated"),
            "reason":               reason,
        }

    except Exception as e:
        conn.rollback()
        print(f"Error in admin_set_agent_rank: {e}")
        raise e
    finally:
        conn.close()


@app.route("/admin/set-agent-rank", methods=["POST"])
def admin_set_agent_rank_route():
    """Admin POST route to change any agent rank + cumulative gross."""
    if "user_id" not in session or session.get("user_role") != "admin":
        return redirect("/login")

    agent_id       = request.form.get("agent_id", type=int)
    new_rank       = request.form.get("new_rank", "").strip()
    new_cumulative = request.form.get("cumulative_gross", type=float, default=0)
    reason         = request.form.get("reason", "").strip()
    admin_id       = session["user_id"]
    redirect_to    = request.form.get("redirect_to", "/admin/agents")

    if not agent_id or not new_rank:
        flash("Agent ID and new rank are required.", "error")
        return redirect(redirect_to)

    try:
        result = admin_set_agent_rank(
            agent_id=agent_id,
            new_rank=new_rank,
            new_cumulative_gross=new_cumulative,
            reason=reason or "Admin manual assignment",
            admin_id=admin_id,
        )
        flash(
            f"\u2705 {result['agent_name']} has been {result['action']} "
            f"from {result['old_rank']} ({result['old_pct']:.0f}%) "
            f"to {result['new_rank']} ({result['new_pct']:.0f}%). "
            f"Cumulative gross set to RM {result['new_cumulative_gross']:,.2f}.",
            "success"
        )
    except ValueError as ve:
        flash(f"\u274c {str(ve)}", "error")
    except Exception as e:
        flash(f"\u274c Unexpected error: {str(e)}", "error")

    return redirect(redirect_to)


@app.route("/admin/agent-rank-history/<int:agent_id>")
def admin_agent_rank_history(agent_id):
    """Returns rank promotion/change history for an agent as JSON."""
    if "user_id" not in session or session.get("user_role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT
                rpl.id, rpl.old_rank, rpl.new_rank, rpl.old_pct, rpl.new_pct,
                rpl.cumulative_gross_at_promotion, rpl.promoted_by,
                rpl.reason, rpl.promoted_at,
                u.name as admin_name
            FROM rank_promotion_log rpl
            LEFT JOIN users u ON rpl.admin_id = u.id
            WHERE rpl.agent_id = ?
            ORDER BY rpl.promoted_at DESC
            """,
            (agent_id,)
        )
        rows = cursor.fetchall()
        cols = ["id","old_rank","new_rank","old_pct","new_pct",
                "cumulative_gross_at_promotion","promoted_by","reason",
                "promoted_at","admin_name"]
        history = [dict(zip(cols, r)) for r in rows]
        return jsonify({"history": history})
    finally:
        conn.close()


# ------------------------------------------------------------
# TEAM GROSS CALCULATOR — recursive, handles any depth & branching
# Returns total gross commission generated by agent + all downlines
# ------------------------------------------------------------
def get_team_gross(agent_id, listing_id, cursor):
    """
    Recursively calculate the total gross commission generated by an agent's team
    for a specific listing (deal). Used to compute override amounts.
    
    'Team gross' = sum of gross_commission for all deals closed by agent
                   and everyone below them in the hierarchy.
    """
    total = 0.0

    # Get this agent's own commission on this listing (if any)
    cursor.execute(
        """
        SELECT gross_commission_base FROM taiko_commission_entries
        WHERE listing_id = ? AND agent_id = ? AND entry_type = 'personal'
        """,
        (listing_id, agent_id)
    )
    row = cursor.fetchone()
    if row:
        total += float(row[0])

    # Get all direct downlines of this agent
    cursor.execute(
        "SELECT id FROM users WHERE upline_id = ? AND role = 'agent'",
        (agent_id,)
    )
    downlines = cursor.fetchall()
    for (dl_id,) in downlines:
        total += get_team_gross(dl_id, listing_id, cursor)

    return total


# ------------------------------------------------------------
# MAIN TAIKO EA COMMISSION ENGINE
# Override model: upline earns (own% - downline%) × downline team gross
# WTP kicks in when gap = 0: Gen1 +2%, Gen2 +1%
# ------------------------------------------------------------
def calculate_taiko_commission(listing_id, selling_agent_id, gross_commission):
    """
    Calculate and persist the full commission distribution for a deal
    using the TAIKO EA override model.

    Parameters:
    - listing_id       : ID of the property_listing
    - selling_agent_id : ID of the agent who personally closed the deal
    - gross_commission : Total gross commission amount (RM) paid by developer

    Returns list of commission entry dicts for all parties.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    entries = []

    try:
        # ── Step 1: Get selling agent's current rank/rate ──
        cursor.execute(
            "SELECT agent_rank, commission_rate FROM users WHERE id = ?",
            (selling_agent_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Agent {selling_agent_id} not found")

        agent_rank, agent_pct = row[0], float(row[1] or 70)

        # ── Step 2: Record selling agent's personal entry ──
        agent_amount = gross_commission * (agent_pct / 100)
        cursor.execute(
            """
            INSERT INTO taiko_commission_entries
            (listing_id, agent_id, entry_type, rank_at_time, pct_at_time,
             gross_commission_base, amount, level, description)
            VALUES (?, ?, 'personal', ?, ?, ?, ?, 0, ?)
            """,
            (listing_id, selling_agent_id, agent_rank, agent_pct,
             gross_commission, agent_amount,
             f"{agent_rank} personal deal: {agent_pct}% × RM {gross_commission:,.2f}")
        )
        entries.append({
            "agent_id":   selling_agent_id,
            "type":       "personal",
            "rank":       agent_rank,
            "pct":        agent_pct,
            "base":       gross_commission,
            "amount":     agent_amount,
            "level":      0,
        })

        # ── Step 3: Update agent's cumulative gross (personal only — Q1) ──
        cursor.execute(
            """
            UPDATE users
            SET cumulative_gross = COALESCE(cumulative_gross, 0) + ?,
                total_commission  = COALESCE(total_commission, 0) + ?
            WHERE id = ?
            """,
            (gross_commission, agent_amount, selling_agent_id)
        )
        conn.commit()

        # ── Step 4: Check for rank promotion (Q2 — takes effect next deal) ──
        promotion = check_and_promote_agent(selling_agent_id, conn)

        # ── Step 5: Walk up the upline chain and distribute overrides ──
        current_downline_id   = selling_agent_id
        current_downline_pct  = agent_pct
        level = 1

        while True:
            # Get direct upline of current node
            cursor.execute(
                "SELECT id, agent_rank, commission_rate, upline_id FROM users WHERE id = (SELECT upline_id FROM users WHERE id = ?)",
                (current_downline_id,)
            )
            upline_row = cursor.fetchone()
            if not upline_row:
                break  # Reached top of chain

            upline_id, upline_rank, upline_pct_raw, upline_upline_id = upline_row
            upline_pct = float(upline_pct_raw or 70)

            # Get downline's full team gross for this listing
            downline_team_gross = get_team_gross(current_downline_id, listing_id, cursor)

            gap = round(upline_pct - current_downline_pct, 4)

            if gap > 0:
                # ── Standard override ──
                override_amount = downline_team_gross * (gap / 100)
                cursor.execute(
                    """
                    INSERT INTO taiko_commission_entries
                    (listing_id, agent_id, entry_type, rank_at_time, pct_at_time,
                     gross_commission_base, amount, level, description, ref_downline_id)
                    VALUES (?, ?, 'override', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (listing_id, upline_id, upline_rank, upline_pct,
                     downline_team_gross, override_amount, level,
                     f"{upline_rank} override on {current_downline_pct}% downline: "
                     f"({upline_pct}%−{current_downline_pct}%) × RM {downline_team_gross:,.2f}",
                     current_downline_id)
                )
                cursor.execute(
                    """
                    UPDATE users
                    SET total_commission = COALESCE(total_commission, 0) + ?
                    WHERE id = ?
                    """,
                    (override_amount, upline_id)
                )
                entries.append({
                    "agent_id":     upline_id,
                    "type":         "override",
                    "rank":         upline_rank,
                    "pct":          upline_pct,
                    "downline_pct": current_downline_pct,
                    "gap_pct":      gap,
                    "base":         downline_team_gross,
                    "amount":       override_amount,
                    "level":        level,
                })

            elif gap == 0:
                # ── WTP Gen1: upline is same % as direct downline ──
                wtp_gen1_pct = 2.0
                wtp_gen1_amount = downline_team_gross * (wtp_gen1_pct / 100)
                cursor.execute(
                    """
                    INSERT INTO taiko_commission_entries
                    (listing_id, agent_id, entry_type, rank_at_time, pct_at_time,
                     gross_commission_base, amount, level, description, ref_downline_id)
                    VALUES (?, ?, 'wtp_gen1', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (listing_id, upline_id, upline_rank, upline_pct,
                     downline_team_gross, wtp_gen1_amount, level,
                     f"WTP Gen1: {upline_rank} same level as downline — "
                     f"2% × RM {downline_team_gross:,.2f}",
                     current_downline_id)
                )
                cursor.execute(
                    "UPDATE users SET total_commission = COALESCE(total_commission, 0) + ? WHERE id = ?",
                    (wtp_gen1_amount, upline_id)
                )
                entries.append({
                    "agent_id": upline_id,
                    "type":     "wtp_gen1",
                    "rank":     upline_rank,
                    "pct":      upline_pct,
                    "wtp_pct":  wtp_gen1_pct,
                    "base":     downline_team_gross,
                    "amount":   wtp_gen1_amount,
                    "level":    level,
                })

                # ── WTP Gen2: indirect upline (upline's upline) also gets 1% ──
                if upline_upline_id:
                    cursor.execute(
                        "SELECT agent_rank, commission_rate FROM users WHERE id = ?",
                        (upline_upline_id,)
                    )
                    gen2_row = cursor.fetchone()
                    if gen2_row:
                        gen2_rank, gen2_pct_raw = gen2_row
                        gen2_pct = float(gen2_pct_raw or 70)
                        wtp_gen2_pct = 1.0
                        wtp_gen2_amount = downline_team_gross * (wtp_gen2_pct / 100)
                        cursor.execute(
                            """
                            INSERT INTO taiko_commission_entries
                            (listing_id, agent_id, entry_type, rank_at_time, pct_at_time,
                             gross_commission_base, amount, level, description, ref_downline_id)
                            VALUES (?, ?, 'wtp_gen2', ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (listing_id, upline_upline_id, gen2_rank, gen2_pct,
                             downline_team_gross, wtp_gen2_amount, level + 1,
                             f"WTP Gen2: {gen2_rank} indirect upline of same-level event — "
                             f"1% × RM {downline_team_gross:,.2f}",
                             current_downline_id)
                        )
                        cursor.execute(
                            "UPDATE users SET total_commission = COALESCE(total_commission, 0) + ? WHERE id = ?",
                            (wtp_gen2_amount, upline_upline_id)
                        )
                        entries.append({
                            "agent_id": upline_upline_id,
                            "type":     "wtp_gen2",
                            "rank":     gen2_rank,
                            "pct":      gen2_pct,
                            "wtp_pct":  wtp_gen2_pct,
                            "base":     downline_team_gross,
                            "amount":   wtp_gen2_amount,
                            "level":    level + 1,
                        })

            # Move up the chain
            current_downline_id  = upline_id
            current_downline_pct = upline_pct
            level += 1

            # Safety guard — max 20 levels to prevent infinite loop
            if level > 20:
                print(f"Warning: chain depth exceeded 20 levels at agent {upline_id}")
                break

        conn.commit()

        # ── Step 6: Store a summary record in commission_calculations ──
        total_distributed = sum(e["amount"] for e in entries)
        cursor.execute(
            """
            INSERT INTO commission_calculations
            (listing_id, agent_id, sale_price, base_rate, commission, calculation_details)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                selling_agent_id,
                gross_commission,
                agent_pct,
                agent_amount,
                json.dumps({
                    "method":            "taiko_override",
                    "gross_commission":  gross_commission,
                    "selling_agent_pct": agent_pct,
                    "total_distributed": total_distributed,
                    "entries":           entries,
                    "promotion":         promotion,
                    "calculated_at":     datetime.now().isoformat(),
                })
            )
        )
        conn.commit()

    except Exception as e:
        conn.rollback()
        print(f"Error in calculate_taiko_commission: {e}")
        import traceback; traceback.print_exc()
        raise e
    finally:
        conn.close()

    return entries


# ------------------------------------------------------------
# COMMISSION PREVIEW — no DB writes, just returns breakdown
# Useful for showing agents what they'd earn before submitting
# ------------------------------------------------------------
def preview_taiko_commission(gross_commission, selling_agent_id):
    """
    Preview commission distribution without writing to DB.
    Returns a breakdown list showing what each party would earn.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    preview = []

    try:
        cursor.execute(
            "SELECT id, name, agent_rank, commission_rate, upline_id FROM users WHERE id = ?",
            (selling_agent_id,)
        )
        row = cursor.fetchone()
        if not row:
            return []

        agent_id, agent_name, agent_rank, agent_pct_raw, _ = row
        agent_pct    = float(agent_pct_raw or 70)
        agent_amount = gross_commission * (agent_pct / 100)

        preview.append({
            "name":   agent_name,
            "rank":   agent_rank,
            "type":   "personal",
            "pct":    agent_pct,
            "base":   gross_commission,
            "amount": agent_amount,
            "formula": f"{agent_pct}% × RM {gross_commission:,.2f}",
        })

        # Walk up chain
        current_downline_id  = selling_agent_id
        current_downline_pct = agent_pct
        # For preview, treat entire gross as "team gross" of selling agent
        running_team_gross   = gross_commission

        level = 1
        while level <= 20:
            cursor.execute(
                """
                SELECT u.id, u.name, u.agent_rank, u.commission_rate, u.upline_id
                FROM users u
                WHERE u.id = (SELECT upline_id FROM users WHERE id = ?)
                """,
                (current_downline_id,)
            )
            upline_row = cursor.fetchone()
            if not upline_row:
                break

            upline_id, upline_name, upline_rank, upline_pct_raw, upline_upline_id = upline_row
            upline_pct = float(upline_pct_raw or 70)
            gap = round(upline_pct - current_downline_pct, 4)

            if gap > 0:
                amount = running_team_gross * (gap / 100)
                preview.append({
                    "name":    upline_name,
                    "rank":    upline_rank,
                    "type":    "override",
                    "pct":     upline_pct,
                    "gap_pct": gap,
                    "base":    running_team_gross,
                    "amount":  amount,
                    "formula": f"({upline_pct}%−{current_downline_pct}%) × RM {running_team_gross:,.2f}",
                })
            elif gap == 0:
                wtp1 = running_team_gross * 0.02
                preview.append({
                    "name":    upline_name,
                    "rank":    upline_rank,
                    "type":    "wtp_gen1",
                    "pct":     upline_pct,
                    "wtp_pct": 2.0,
                    "base":    running_team_gross,
                    "amount":  wtp1,
                    "formula": f"WTP Gen1: 2% × RM {running_team_gross:,.2f}",
                })
                if upline_upline_id:
                    cursor.execute(
                        "SELECT name, agent_rank, commission_rate FROM users WHERE id = ?",
                        (upline_upline_id,)
                    )
                    g2 = cursor.fetchone()
                    if g2:
                        wtp2 = running_team_gross * 0.01
                        preview.append({
                            "name":    g2[0],
                            "rank":    g2[1],
                            "type":    "wtp_gen2",
                            "pct":     float(g2[2] or 70),
                            "wtp_pct": 1.0,
                            "base":    running_team_gross,
                            "amount":  wtp2,
                            "formula": f"WTP Gen2: 1% × RM {running_team_gross:,.2f}",
                        })

            current_downline_id  = upline_id
            current_downline_pct = upline_pct
            level += 1

    finally:
        conn.close()

    return preview


# ------------------------------------------------------------
# RANK PROGRESS HELPER — for agent dashboard display
# ------------------------------------------------------------
def get_agent_rank_progress(agent_id):
    """
    Returns rank info and progress toward next promotion for dashboard display.
    WTP Rule: cumulative_gross includes submitted+approved commission_amount.
    Rejected/draft excluded. Only deducted when admin rejects.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT agent_rank, commission_rate, cumulative_gross FROM users WHERE id = ?",
            (agent_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None

        rank_name, comm_rate, _ = row

        # WTP rule: gross = sum of commission_amount for submitted+approved
        cursor.execute(
            """SELECT COALESCE(SUM(commission_amount), 0)
               FROM property_listings
               WHERE agent_id = ? AND status IN ('submitted', 'approved')""",
            (agent_id,)
        )
        gross_row = cursor.fetchone()
        cumulative_gross = float(gross_row[0] or 0) if gross_row else 0.0

        next_rank = get_next_rank(rank_name)
        current_rank_info = get_rank_for_cumulative(cumulative_gross)

        progress_pct = 0
        remaining    = 0
        if next_rank:
            current_target = current_rank_info["cumulative_target"]
            next_target    = next_rank["cumulative_target"]
            span  = next_target - current_target
            done  = cumulative_gross - current_target
            progress_pct = min(100, round((done / span) * 100, 1)) if span > 0 else 100
            remaining    = max(0, next_target - cumulative_gross)

        return {
            "agent_id":        agent_id,
            "current_rank":    rank_name,
            "commission_pct":  float(comm_rate or 70),
            "cumulative_gross": cumulative_gross,
            "next_rank":       next_rank["rank"] if next_rank else None,
            "next_rank_pct":   next_rank["payout_pct"] if next_rank else None,
            "next_target":     next_rank["cumulative_target"] if next_rank else None,
            "remaining_to_next": remaining,
            "progress_pct":    progress_pct,
            "is_top_rank":     next_rank is None,
            "all_ranks":       TAIKO_RANKS,
        }
    finally:
        conn.close()


# ===================== MULTI-LEVEL COMMISSION HELPERS (legacy — kept for reference) =====================
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

        # ============ TAIKO EA RANK & COMMISSION TABLES ============

        # Individual commission entries per listing (one row per party who earns)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS taiko_commission_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                entry_type TEXT NOT NULL,
                rank_at_time TEXT NOT NULL,
                pct_at_time DECIMAL(5,2) NOT NULL,
                gross_commission_base DECIMAL(12,2) NOT NULL,
                amount DECIMAL(12,2) NOT NULL,
                level INTEGER DEFAULT 0,
                description TEXT,
                ref_downline_id INTEGER NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (listing_id) REFERENCES property_listings(id),
                FOREIGN KEY (agent_id) REFERENCES users(id)
            )
        """)

        # Rank promotion audit log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rank_promotion_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL,
                old_rank TEXT NOT NULL,
                new_rank TEXT NOT NULL,
                old_pct DECIMAL(5,2),
                new_pct DECIMAL(5,2),
                cumulative_gross_at_promotion DECIMAL(12,2),
                promoted_by TEXT DEFAULT 'system',
                admin_id INTEGER NULL,
                reason TEXT NULL,
                promoted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (agent_id) REFERENCES users(id),
                FOREIGN KEY (admin_id) REFERENCES users(id)
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

        # ── WTP Unified Submissions table ──
        conn.commit()
        conn.close()
        conn = None
        init_submissions_table()
        print("✅ Unified submissions table ready!")

        conn = get_db_connection()
        conn.execute("PRAGMA journal_mode=WAL")
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

        # ============ TAIKO EA — Add new columns to users if missing ============
        cursor.execute("PRAGMA table_info(users)")
        user_columns = [col[1] for col in cursor.fetchall()]

        if "agent_rank" not in user_columns:
            print("🔄 Adding agent_rank column to users table...")
            cursor.execute("ALTER TABLE users ADD COLUMN agent_rank TEXT DEFAULT 'REN'")
            conn.commit()
            print("✅ agent_rank column added!")

        if "commission_rate" not in user_columns:
            print("🔄 Adding commission_rate column to users table...")
            cursor.execute("ALTER TABLE users ADD COLUMN commission_rate DECIMAL(5,2) DEFAULT 70.00")
            conn.commit()
            print("✅ commission_rate column added!")

        if "cumulative_gross" not in user_columns:
            print("🔄 Adding cumulative_gross column to users table...")
            cursor.execute("ALTER TABLE users ADD COLUMN cumulative_gross DECIMAL(12,2) DEFAULT 0.00")
            conn.commit()
            print("✅ cumulative_gross column added!")

        # ============ TAIKO EA — Migrate rank_promotion_log columns if missing ============
        cursor.execute("PRAGMA table_info(rank_promotion_log)")
        rpl_cols = [col[1] for col in cursor.fetchall()]

        if rpl_cols:  # table exists — check for new columns
            if "promoted_by" not in rpl_cols:
                print("🔄 Adding promoted_by column to rank_promotion_log...")
                cursor.execute(
                    "ALTER TABLE rank_promotion_log ADD COLUMN promoted_by TEXT DEFAULT 'system'"
                )
                conn.commit()
                print("✅ promoted_by added!")
            if "admin_id" not in rpl_cols:
                print("🔄 Adding admin_id column to rank_promotion_log...")
                cursor.execute(
                    "ALTER TABLE rank_promotion_log ADD COLUMN admin_id INTEGER NULL"
                )
                conn.commit()
                print("✅ admin_id added!")
            if "reason" not in rpl_cols:
                print("🔄 Adding reason column to rank_promotion_log...")
                cursor.execute(
                    "ALTER TABLE rank_promotion_log ADD COLUMN reason TEXT NULL"
                )
                conn.commit()
                print("✅ reason added!")

        # Backfill commission_rate from existing total_commission data if possible
        cursor.execute("""
            UPDATE users SET commission_rate = 70.00
            WHERE commission_rate IS NULL AND role = 'agent'
        """)
        cursor.execute("""
            UPDATE users SET agent_rank = 'REN'
            WHERE agent_rank IS NULL AND role = 'agent'
        """)
        conn.commit()

        # Create TAIKO tables if they don't exist yet
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS taiko_commission_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                entry_type TEXT NOT NULL,
                rank_at_time TEXT NOT NULL,
                pct_at_time DECIMAL(5,2) NOT NULL,
                gross_commission_base DECIMAL(12,2) NOT NULL,
                amount DECIMAL(12,2) NOT NULL,
                level INTEGER DEFAULT 0,
                description TEXT,
                ref_downline_id INTEGER NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (listing_id) REFERENCES property_listings(id),
                FOREIGN KEY (agent_id) REFERENCES users(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rank_promotion_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL,
                old_rank TEXT NOT NULL,
                new_rank TEXT NOT NULL,
                old_pct DECIMAL(5,2),
                new_pct DECIMAL(5,2),
                cumulative_gross_at_promotion DECIMAL(12,2),
                promoted_by TEXT DEFAULT 'system',
                admin_id INTEGER NULL,
                reason TEXT NULL,
                promoted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (agent_id) REFERENCES users(id),
                FOREIGN KEY (admin_id) REFERENCES users(id)
            )
        """)
        conn.commit()
        print("✅ TAIKO EA rank & commission tables ready!")

        # ── WTP COMMISSION CONFIG — add comm config columns to projects ──
        cursor.execute("PRAGMA table_info(projects)")
        proj_cols = [col[1] for col in cursor.fetchall()]
        new_proj_cols = {
            "comm_dev_rate":     "DECIMAL(5,2) DEFAULT 2.0",
            "comm_sst_rate":     "DECIMAL(5,2) DEFAULT 8.0",
            "comm_company_pct":  "DECIMAL(5,2) DEFAULT 15.0",
            "comm_pic_pct":      "DECIMAL(5,2) DEFAULT 10.0",
            "comm_agents_pct":   "DECIMAL(5,2) DEFAULT 75.0",
            "comm_pic_rank":     "TEXT DEFAULT 'REN'",
            "comm_pic_agent_id": "INTEGER NULL",
            "comm_pic_name":     "TEXT NULL",
        }
        for col, coldef in new_proj_cols.items():
            if col not in proj_cols:
                print(f"🔄 Adding {col} to projects table...")
                cursor.execute(f"ALTER TABLE projects ADD COLUMN {col} {coldef}")
                conn.commit()
                print(f"✅ {col} added!")

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
    <meta charset="UTF-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>Login - WTP Real Estate</title>
    <style>
        *,*::before,*::after{box-sizing:border-box}
        html{-webkit-text-size-adjust:100%}
        body{
            font-family:Arial,sans-serif;
            margin:0;
            min-height:100vh;
            background:#f0f2f5;
            display:flex;
            align-items:center;
            justify-content:center;
            padding:16px;
        }
        .login-wrap{
            width:100%;
            max-width:420px;
        }
        .login-logo{
            text-align:center;
            margin-bottom:24px;
        }
        .login-logo .icon{
            font-size:48px;
            display:block;
            margin-bottom:8px;
        }
        .login-logo h1{
            color:#1a2a3a;
            font-size:1.4rem;
            margin:0 0 4px;
            font-weight:800;
            letter-spacing:.5px;
        }
        .login-logo p{
            color:#888;
            font-size:13px;
            margin:0;
        }
        .login-box{
            background:white;
            border:1px solid #e0e0e0;
            border-radius:16px;
            padding:32px 28px;
            box-shadow:0 4px 24px rgba(0,0,0,.10);
        }
        .form-group{
            margin-bottom:16px;
        }
        label{
            display:block;
            color:#444;
            font-size:12px;
            font-weight:700;
            text-transform:uppercase;
            letter-spacing:.08em;
            margin-bottom:6px;
        }
        input[type=email],
        input[type=password]{
            width:100%;
            padding:12px 14px;
            background:#f8f9fa;
            border:1.5px solid #dee2e6;
            border-radius:8px;
            color:#1a2a3a;
            font-size:15px;
            outline:none;
            transition:border-color .2s,background .2s;
        }
        input[type=email]::placeholder,
        input[type=password]::placeholder{
            color:#aaa;
        }
        input[type=email]:focus,
        input[type=password]:focus{
            border-color:#2563eb;
            background:white;
            outline:none;
        }
        .btn-login{
            width:100%;
            padding:13px;
            background:linear-gradient(135deg,#2563eb,#1d4ed8);
            color:white;
            border:none;
            border-radius:8px;
            font-size:15px;
            font-weight:700;
            cursor:pointer;
            margin-top:8px;
            letter-spacing:.3px;
            transition:opacity .2s,transform .1s;
        }
        .btn-login:hover{opacity:.9}
        .btn-login:active{transform:scale(.99)}
        .error-msg{
            background:rgba(220,53,69,.2);
            border:1px solid rgba(220,53,69,.4);
            color:#ff8a94;
            border-radius:8px;
            padding:10px 14px;
            font-size:13px;
            text-align:center;
            margin-bottom:16px;
        }
        .info-box{
            margin-top:20px;
            background:#f8f9fa;
            border:1px solid #e0e0e0;
            border-radius:8px;
            padding:14px 16px;
            font-size:12px;
            color:#666;
            line-height:1.8;
        }
        .info-box strong{
            color:#333;
        }
        @media(max-width:480px){
            .login-box{padding:24px 18px}
            .login-logo .icon{font-size:40px}
            .login-logo h1{font-size:1.2rem}
        }
    </style>
</head>
<body>
    <div class="login-wrap">
        <div class="login-logo">
            <span class="icon">&#127968;</span>
            <h1>WTP Real Estate</h1>
            <p>Commission Management System</p>
        </div>
        <div class="login-box">
            {% if error %}
            <div class="error-msg">&#9888; {{ error }}</div>
            {% endif %}
            <form method="POST">
                <div class="form-group">
                    <label>Email Address</label>
                    <input type="email" name="email" placeholder="your@email.com" required autofocus>
                </div>
                <div class="form-group">
                    <label>Password</label>
                    <input type="password" name="password" placeholder="&#9679;&#9679;&#9679;&#9679;&#9679;&#9679;&#9679;&#9679;" required>
                </div>
                <button type="submit" class="btn-login">&#128274; Sign In</button>
            </form>
            <div class="info-box">
                <strong>Admin:</strong> admin@xxxxx.com<br>
                Agent accounts are created by admin
            </div>
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
    # Retired — redirect all agents to unified submission form
    return redirect("/agent/unified-submit")
    # ── OLD CODE BELOW — kept for reference only ──
    if False:
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
            -- Commission only counts for APPROVED listings
            COALESCE(SUM(CASE WHEN pl.status = 'approved'
                THEN COALESCE(tce.amount, pl.commission_amount) ELSE 0 END), 0) as total_commission,
            SUM(CASE WHEN pl.status = 'submitted' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN pl.status = 'draft' THEN 1 ELSE 0 END) as drafts,
            SUM(CASE WHEN pl.status = 'rejected' THEN 1 ELSE 0 END) as rejected
        FROM property_listings pl
        LEFT JOIN taiko_commission_entries tce
            ON tce.listing_id = pl.id
            AND tce.agent_id = pl.agent_id
            AND tce.entry_type = 'personal'
        WHERE pl.agent_id = ?
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

    # ============ 6. GET RECENT SALES — use TAIKO personal payout ============
    cursor.execute(
        """
        SELECT 
            pl.id,
            pl.customer_name,
            pl.sale_price,
            COALESCE(tce.amount, pl.commission_amount) as agent_payout,
            pl.status,
            pl.created_at,
            COALESCE(p.project_name, '') as project_name
        FROM property_listings pl
        LEFT JOIN projects p ON pl.project_id = p.id
        LEFT JOIN taiko_commission_entries tce
            ON tce.listing_id = pl.id
            AND tce.agent_id = pl.agent_id
            AND tce.entry_type = 'personal'
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

    # ============ 11. GET RANK PROGRESS ============
    rank_progress = get_agent_rank_progress(user_id)

    # ============ 12. RENDER TEMPLATE ============
    return render_template(
        "agent/dashboard.html",
        user_name=session.get("user_name", "Agent"),
        total_sales=total_sales,
        total_commission=total_commission,
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
        upline_earnings=upline_earnings,
        upline_payments_count=upline_payments_count,
        total_paid=total_paid,
        total_payments=total_payments,
        rank_progress=rank_progress,
    )

@app.route("/agent/my-downline")
def agent_downline():
    """Agent view of their downline network - FIXED PENDING COMMISSIONS"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    agent_id = session["user_id"]

    # ========== GET CURRENT AGENT'S TAIKO RANK INFO ==========
    cursor.execute(
        "SELECT agent_rank, commission_rate, cumulative_gross FROM users WHERE id = ?",
        (agent_id,),
    )
    agent_info = cursor.fetchone()
    my_rank = agent_info[0] if agent_info else 'REN'
    my_pct  = float(agent_info[1] or 70) if agent_info else 70.0
    my_cumul = float(agent_info[2] or 0) if agent_info else 0.0

    # WTP RULE: Cumulative Gross = commission amount tracker for rank progression
    # Uses commission_amount (not sale_price)
    # Counts: submitted + approved
    # Excluded: rejected, draft (if admin rejects, amount is deducted from rank progress)
    cursor.execute(
        """SELECT COALESCE(SUM(commission_amount), 0)
           FROM property_listings
           WHERE agent_id = ? AND status IN ('submitted', 'approved')""",
        (agent_id,)
    )
    gross_row = cursor.fetchone()
    my_cumul_display = float(gross_row[0] or 0) if gross_row else 0.0

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

    # Helper: get downline agent's rank info + override earned/pending for current agent
    def get_downline_override_stats(dl_id, entry_types):
        # Rank info
        cursor.execute(
            "SELECT agent_rank, commission_rate FROM users WHERE id = ?", (dl_id,)
        )
        r = cursor.fetchone()
        dl_rank = r[0] if r else 'REN'
        dl_pct  = float(r[1] or 70) if r else 70.0
        # Override gap
        gap = round(my_pct - dl_pct, 4)
        if gap > 0:
            override_label = f"{int(my_pct)}% − {int(dl_pct)}% = {gap:.4g}% override"
        elif gap == 0:
            override_label = "WTP 2% (same rank)"
        else:
            override_label = f"Upline has lower rank — no override"

        # Earned from taiko_commission_entries (paid via upline_commissions)
        cursor.execute(
            """SELECT COALESCE(SUM(amount),0), COUNT(*)
               FROM upline_commissions
               WHERE upline_id=? AND agent_id=?
               AND commission_type IN ({})
               AND status IN ('paid','approved','completed')""".format(
                   ','.join('?'*len(entry_types))
               ),
            (agent_id, dl_id, *entry_types)
        )
        res = cursor.fetchone()
        earned, earned_count = float(res[0] or 0), int(res[1] or 0)

        # Pending
        cursor.execute(
            """SELECT COALESCE(SUM(amount),0), COUNT(*)
               FROM upline_commissions
               WHERE upline_id=? AND agent_id=?
               AND commission_type IN ({})
               AND status = 'pending'""".format(
                   ','.join('?'*len(entry_types))
               ),
            (agent_id, dl_id, *entry_types)
        )
        res = cursor.fetchone()
        pending, pending_count = float(res[0] or 0), int(res[1] or 0)

        return dl_rank, dl_pct, gap, override_label, earned, earned_count, pending, pending_count

    # Process direct downlines
    for agent in direct_downlines:
        agent_id_val = agent[0]
        dl_rank, dl_pct, gap, override_label, earned, earned_count, pending, pending_count =             get_downline_override_stats(agent_id_val, ('override','wtp_gen1'))

        direct_downline_list.append({
            "id": agent_id_val,
            "name": agent[1],
            "email": agent[2],
            "agent_rank": dl_rank,
            "commission_pct": dl_pct,
            "join_date": agent[3][:10] if agent[3] else "",
            "commission_percentage": override_label,
            "relationship": "direct",
            "earned_from_agent": earned,
            "earned_count": earned_count,
            "pending_from_agent": pending,
            "pending_count": pending_count,
        })
        total_direct_earnings += earned
        total_direct_pending += pending

    # Process indirect downlines
    for agent in indirect_downlines:
        agent_id_val = agent[0]
        dl_rank, dl_pct, gap, override_label, earned, earned_count, pending, pending_count =             get_downline_override_stats(agent_id_val, ('override','wtp_gen2'))

        indirect_downline_list.append({
            "id": agent_id_val,
            "name": agent[1],
            "email": agent[2],
            "agent_rank": dl_rank,
            "commission_pct": dl_pct,
            "join_date": agent[3][:10] if agent[3] else "",
            "commission_percentage": override_label,
            "relationship": "indirect",
            "direct_upline_name": agent[5] if len(agent) > 5 else "",
            "earned_from_agent": earned,
            "earned_count": earned_count,
            "pending_from_agent": pending,
            "pending_count": pending_count,
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
    total_pending  = total_direct_pending  + total_indirect_pending
    total_earnings = total_direct_earnings + total_indirect_earnings

    stats_dict = {
        "total_downline":           len(direct_downline_list) + len(indirect_downline_list),
        "direct_downline_count":    len(direct_downline_list),
        "indirect_downline_count":  len(indirect_downline_list),
        "total_direct_earnings":    total_direct_earnings,
        "total_indirect_earnings":  total_indirect_earnings,
        "total_direct_pending":     total_direct_pending,
        "total_indirect_pending":   total_indirect_pending,
        "total_your_earnings":      total_earnings,
        "total_your_pending":       total_pending,
        "my_rank":                  my_rank,
        "my_pct":                   my_pct,
        "my_cumul":                 my_cumul_display,  # includes submitted+approved
    }

    rank_progress = get_agent_rank_progress(agent_id)

    conn.close()

    return render_template(
        "agent/downline.html",
        direct_downline_agents=direct_downline_list,
        indirect_downline_agents=indirect_downline_list,
        stats=stats_dict,
        rank_progress=rank_progress,
        search_query=request.args.get('search', ''),
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
        
    /* ── ADMIN TOPBAR ── */
    body{margin:0;background:#f0f2f5;font-family:Arial,sans-serif}
    .atb{background:#2c3e50;color:white;padding:12px 16px;display:flex;
         align-items:center;justify-content:space-between;position:sticky;top:0;z-index:200}
    .atb-title{font-size:1rem;font-weight:700}
    .atb button{background:none;border:none;color:white;font-size:24px;cursor:pointer;padding:0;line-height:1}
    .anav{background:white;max-height:0;overflow:hidden;transition:max-height .3s ease;
          box-shadow:0 2px 6px rgba(0,0,0,.1)}
    .anav.open{max-height:600px}
    .anav a{display:block;padding:11px 16px;color:#007bff;text-decoration:none;
            font-weight:600;font-size:14px;border-bottom:1px solid #f0f0f0}
    .anav a:last-child{border-bottom:none}
    .anav a:hover{background:#f0f7ff}
    .anav a.nl{color:#dc3545}
    .pwrap{max-width:1400px;margin:0 auto;padding:16px}
    @media(min-width:641px){
        .atb button{display:none}
        .anav{max-height:none!important;overflow:visible;display:flex;flex-wrap:wrap;
              gap:4px;align-items:center;padding:8px 16px}
        .anav a{display:inline-block;padding:5px 10px;border-bottom:none;border-radius:6px;font-size:13px}
    }
    @media(max-width:640px){.pwrap{padding:10px}}
        body{font-family:Arial,sans-serif}
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

    return render_template('agent/notifications.html', notifications=notifications)


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
    """Redirects to unified submissions page"""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")
    return redirect("/agent/unified-submissions")
    # ── OLD CODE BELOW — kept for reference only ──
    if False:
        pass

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



@app.route("/view-document/u<int:doc_id>")
def view_unified_document(doc_id):
    """View/download a unified submission document"""
    if "user_id" not in session:
        return redirect("/login")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    if session["user_role"] == "admin":
        doc = conn.execute(
            "SELECT * FROM unified_documents WHERE id=?",
            (doc_id,)
        ).fetchone()
    else:
        # Agent can only view their own submission docs
        doc = conn.execute(
            """SELECT ud.* FROM unified_documents ud
               JOIN unified_submissions us ON us.id = ud.sub_id
               WHERE ud.id=? AND us.agent_id=?""",
            (doc_id, session["user_id"])
        ).fetchone()
    conn.close()

    if not doc:
        return "Document not found or access denied", 404

    filepath_db = doc["filepath"]
    app_root    = os.path.dirname(os.path.abspath(__file__))
    filepath    = os.path.normpath(os.path.join(app_root, filepath_db))
    filename    = os.path.basename(filepath)

    if not os.path.exists(filepath):
        # Try relative path directly
        if os.path.exists(filepath_db):
            filepath = filepath_db
        else:
            return f"File not found: {filename}", 404

    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    types = {
        "pdf":  "application/pdf",
        "jpg":  "image/jpeg", "jpeg": "image/jpeg",
        "png":  "image/png",
        "doc":  "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    content_type  = types.get(ext, "application/octet-stream")
    as_attachment = request.args.get("download", "0") == "1"

    return send_file(filepath, mimetype=content_type,
                     as_attachment=as_attachment, download_name=filename)

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

        # Store full gross commission — TAIKO engine splits at approval time
        commission_to_store = total_commission

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
                round(commission_to_store, 2),  # Gross commission (TAIKO splits at approval)
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
            "commission_method": "taiko_override",
            "gross_commission": round(commission_to_store, 2),  # Full gross before TAIKO split
            "fund_allocation": {
                "note": "Split calculated by TAIKO engine at approval"
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
                round(commission_to_store, 2),  # Gross commission
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
            commission=commission_to_store,  # Gross commission
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
    
    # Get paginated commissions — show agent's actual TAIKO payout not gross
    cursor.execute(f"""
        SELECT 
            pl.id,
            pl.customer_name,
            pl.sale_price,
            COALESCE(tce.amount, pl.commission_amount) as agent_payout,
            pl.status,
            pl.approved_at,
            pl.created_at,
            COALESCE(p.project_name, '') as project_name
        FROM property_listings pl
        LEFT JOIN projects p ON pl.project_id = p.id
        LEFT JOIN taiko_commission_entries tce
            ON tce.listing_id = pl.id
            AND tce.agent_id = pl.agent_id
            AND tce.entry_type = 'personal'
        WHERE {where_sql}
        ORDER BY {order_by}
        LIMIT ? OFFSET ?
    """, params + [items_per_page, offset])
    
    commissions = cursor.fetchall()

    # Calculate totals for current filter — use TAIKO personal payout
    cursor.execute(f"""
        SELECT 
            COALESCE(SUM(COALESCE(tce.amount, pl.commission_amount)), 0) as total_approved,
            COUNT(*) as total_count,
            COALESCE(AVG(COALESCE(tce.amount, pl.commission_amount)), 0) as avg_commission
        FROM property_listings pl
        LEFT JOIN taiko_commission_entries tce
            ON tce.listing_id = pl.id
            AND tce.agent_id = pl.agent_id
            AND tce.entry_type = 'personal'
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
        try:
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
        except Exception:
            pass  # upline_commissions table may not exist yet
    
    # Sort and limit payments
    recent_payments_list.sort(key=lambda x: x["payment_date"] or "", reverse=True)
    recent_payments_list = recent_payments_list[:10]

    # ===== 4. GET RECENT SALES (all statuses, last 10) =====
    cursor.execute("""
        SELECT 
            pl.id,
            pl.customer_name,
            pl.sale_price,
            COALESCE(tce.amount, pl.commission_amount) as agent_payout,
            pl.status,
            pl.created_at,
            COALESCE(p.project_name, '') as project_name
        FROM property_listings pl
        LEFT JOIN projects p ON pl.project_id = p.id
        LEFT JOIN taiko_commission_entries tce
            ON tce.listing_id = pl.id
            AND tce.agent_id = pl.agent_id
            AND tce.entry_type = 'personal'
        WHERE pl.agent_id = ?
        ORDER BY pl.created_at DESC
        LIMIT 10
    """, (user_id,))
    recent_sales = cursor.fetchall()

    # ===== 5. GET TOTAL STATS (unfiltered) =====
    # Total approved commissions (for stats card) — use TAIKO personal payout
    cursor.execute("""
        SELECT 
            COALESCE(SUM(COALESCE(tce.amount, pl.commission_amount)), 0) as total_approved_amount,
            COUNT(*) as total_approved_count
        FROM property_listings pl
        LEFT JOIN taiko_commission_entries tce
            ON tce.listing_id = pl.id
            AND tce.agent_id = pl.agent_id
            AND tce.entry_type = 'personal'
        WHERE pl.agent_id = ? AND pl.status = 'approved'
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
    try:
        cursor.execute("""
            SELECT 
                COALESCE(SUM(amount), 0) as total_upline_paid
            FROM upline_commissions 
            WHERE upline_id = ? AND status = 'paid'
        """, (user_id,))
        total_upline_paid_result = cursor.fetchone()
        total_upline_paid = float(total_upline_paid_result[0]) if total_upline_paid_result and total_upline_paid_result[0] else 0
    except Exception:
        total_upline_paid = 0

    # Total pending (approved but not paid) — use TAIKO personal payout
    cursor.execute("""
        SELECT 
            COALESCE(SUM(COALESCE(tce.amount, pl.commission_amount)), 0) as total_pending
        FROM property_listings pl
        LEFT JOIN taiko_commission_entries tce
            ON tce.listing_id = pl.id
            AND tce.agent_id = pl.agent_id
            AND tce.entry_type = 'personal'
        WHERE pl.agent_id = ? AND pl.status = 'approved'
        AND pl.id NOT IN (
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
               (
                   (SELECT COUNT(*) FROM documents d WHERE d.listing_id = pl.id)
                   +
                   COALESCE((SELECT COUNT(*) FROM unified_documents ud
                    WHERE pl.notes LIKE '%unified:' || ud.sub_id || '%'), 0)
               ) as document_count
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
            -- Total Sales and Commissions: APPROVED only
            COALESCE(SUM(CASE WHEN status='approved' THEN sale_price ELSE 0 END), 0) as total_sales,
            COALESCE(SUM(CASE WHEN status='approved' THEN commission_amount ELSE 0 END), 0) as total_commissions,
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

    # Stats: total_listings = approved only; pending/rejected shown separately
    approved_count = stats[3] if stats else 0

    stats_dict = {
        "total_listings": approved_count,          # approved only
        "total_sales": stats[1] if stats else 0,   # approved sale prices only
        "total_rentals": 0,
        "total_commissions": total_commissions,    # approved commissions only
        "agent_commissions": agent_commissions,
        "upline_commissions": upline_commissions,
        "total_paid": total_paid,
        "balance": balance,
        "approved": approved_count,
        "pending": stats[4] if stats else 0,
        "rejected": stats[5] if stats else 0,
        "draft": stats[6] if stats else 0,
        "sales_count": approved_count,
        "rentals_count": 0,
    }

    # Agent rank snapshot for dashboard
    agents_snapshot = []
    try:
        conn2 = get_db_connection()
        snap_rows = conn2.execute("""
            SELECT u.id, u.name, u.agent_rank, u.commission_rate, u.cumulative_gross,
                (SELECT COALESCE(SUM(pl.commission_amount),0) FROM property_listings pl
                 WHERE pl.agent_id=u.id AND pl.status IN ('submitted','approved')) AS display_gross,
                (SELECT COUNT(*) FROM property_listings pl2
                 WHERE pl2.agent_id=u.id AND pl2.status='submitted') AS pending_count
            FROM users u WHERE u.role='agent'
            ORDER BY display_gross DESC
            LIMIT 5
        """).fetchall()
        conn2.close()
        RANK_NEXT = {'REN':('Assoc REN',30000),'Assoc REN':('Elite REN',90000),
                     'Elite REN':('TL',210000),'TL':('ATL',450000),'ATL':('ATL',450000)}
        for r in snap_rows:
            nxt = RANK_NEXT.get(r[2] or 'REN', ('Assoc REN', 30000))
            agents_snapshot.append({
                'name':           r[1],
                'agent_rank':     r[2] or 'REN',
                'commission_rate': float(r[3] or 70),
                'cumulative_gross': float(r[4] or 0),
                'display_gross':  float(r[5] or 0),
                'pending_count':  int(r[6] or 0),
                'next_rank':      nxt[0],
                'next_threshold': nxt[1],
            })
    except Exception:
        agents_snapshot = []

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
        agents_snapshot=agents_snapshot,
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

    # Get uploaded documents from old documents table
    cursor.execute(
        """
        SELECT * FROM documents 
        WHERE listing_id = ? 
        ORDER BY uploaded_at DESC
    """,
        (listing_id,),
    )
    documents = cursor.fetchall()

    # Also get unified_documents linked via notes field
    unified_docs = []
    try:
        import re as _re
        notes_row = cursor.execute(
            "SELECT notes FROM property_listings WHERE id=?", (listing_id,)
        ).fetchone()
        if notes_row and notes_row[0] and "unified:" in (notes_row[0] or ""):
            m = _re.search(r"unified:([a-f0-9-]+)", notes_row[0])
            if m:
                sub_id = m.group(1)
                cursor.execute(
                    "SELECT id, filename, filepath, file_type, file_size, doc_label, uploaded_at FROM unified_documents WHERE sub_id=? ORDER BY uploaded_at DESC",
                    (sub_id,)
                )
                unified_docs = cursor.fetchall()
    except Exception:
        unified_docs = []

    conn.close()

    if not listing:
        return "Listing not found", 404

    # Get success/error messages
    success_msg = request.args.get("success")
    error_msg = request.args.get("error")

    # Combine old + unified docs
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

    # Append unified documents
    for ud in unified_docs:
        docs_list.append({
            "id":          "u" + str(ud[0]),
            "filename":    ud[1],
            "filepath":    ud[2],
            "file_type":   (ud[3] or "unknown").lower(),
            "file_size":   ud[4],
            "uploaded_at": ud[6],
            "notes":       ud[5] or "Supporting Document",
        })

    return render_template(
        "admin/documents.html",
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
                            
                            <a href="/admin/approve/{{ sub.id }}" class="btn" style="padding: 6px 12px; background: #28a745; color: white; text-decoration: none; border-radius: 4px; font-size: 12px; text-align: center;">
                                ✅ Approve{% if sub.document_count < 3 %} *{% endif %}
                            </a>
                            {% if sub.document_count < 3 %}
                            <span style="font-size:10px;color:#856404">* Low docs ({{ sub.document_count }})</span>
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
    """Display all agents with their TAIKO rank and commission structures"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")
    
    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT
            u.id,
            u.email,
            u.name,
            u.role,
            u.upline_id,
            u.created_at,
            (SELECT COUNT(*) FROM property_listings pl WHERE pl.agent_id = u.id) AS total_listings,
            (SELECT COALESCE(SUM(pl.commission_amount),0) FROM property_listings pl WHERE pl.agent_id = u.id AND pl.status='approved') AS total_commission,
            u.agent_rank,
            u.commission_rate,
            u.cumulative_gross,
            ul.name AS upline_name,
            ul.email AS upline_email,
            ul.agent_rank AS upline_rank,
            (SELECT COALESCE(SUM(pl2.commission_amount),0) FROM property_listings pl2 WHERE pl2.agent_id = u.id AND pl2.status IN ('submitted','approved')) AS pending_gross,
            (SELECT COUNT(*) FROM property_listings pl3 WHERE pl3.agent_id = u.id AND pl3.status = 'submitted') AS pending_count
        FROM users u
        LEFT JOIN users ul ON u.upline_id = ul.id
        WHERE u.role = 'agent'
        ORDER BY u.cumulative_gross DESC, u.created_at DESC
    """)
    
    agents_data = cursor.fetchall()
    conn.close()

    # WTP rank thresholds (cumulative gross commission)
    RANK_THRESHOLDS = [
        ('REN',       0,      70),
        ('Assoc REN', 30000,  75),
        ('Elite REN', 90000,  80),
        ('TL',        210000, 85),
        ('ATL',       450000, 90),
    ]

    def get_next_rank(gross):
        for i, (rank, threshold, pct) in enumerate(RANK_THRESHOLDS):
            if gross < threshold:
                return rank, threshold, pct
            if i + 1 < len(RANK_THRESHOLDS):
                next_rank, next_thresh, next_pct = RANK_THRESHOLDS[i+1]
                if gross < next_thresh:
                    return next_rank, next_thresh, next_pct
        return 'ATL', 450000, 90

    agents = []
    for a in agents_data:
        pending_gross  = float(a[14] or 0)   # idx 14 = pending_gross
        pending_count  = int(a[15] or 0)    # idx 15 = pending_count
        approved_gross = float(a[10] or 0)  # idx 10 = cumulative_gross
        # Display gross = submitted + approved commission
        display_gross  = pending_gross

        # Next rank info based on display_gross
        next_rank_info = get_next_rank(display_gross)

        agents.append({
            'id':              a[0],
            'email':           a[1],
            'name':            a[2],
            'role':            a[3],
            'upline_id':       a[4],
            'created_at':      a[5],
            'total_listings':  a[6],
            'total_commission': float(a[7] or 0),
            'agent_rank':      a[8] or 'REN',
            'commission_rate': float(a[9] or 70),
            'cumulative_gross': approved_gross,    # DB approved value
            'display_gross':   display_gross,      # submitted+approved (rank tracker)
            'pending_gross':   pending_gross - approved_gross,  # submitted only portion
            'pending_count':   pending_count,
            'upline_name':     a[11],
            'upline_email':    a[12],
            'upline_rank':     a[13],
            'next_rank':       next_rank_info[0],
            'next_threshold':  next_rank_info[1],
            'next_pct':        next_rank_info[2],
            'progress_pct':    min(100, round(display_gross / next_rank_info[1] * 100, 1)) if next_rank_info[1] > 0 else 100,
        })
    
    return render_template("admin/manage_agents.html", agents=agents)

@app.route("/admin/agent-hierarchy")
def agent_hierarchy():
    """View agent hierarchy tree with improved design - UPDATED FOR FUND-BASED COMMISSIONS"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    # UPDATED QUERY: Get all agents with TAIKO rank fields
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
            -- TAIKO rank fields
            u1.agent_rank,
            u1.commission_rate,
            u1.cumulative_gross,
            -- Legacy fields (kept for backward compat)
            u1.upline_commission_rate,
            NULL as upline2_commission_rate,  -- column removed
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
        # New column index map:
        # 0=id, 1=name, 2=email, 3=upline_id, 4=upline2_id,
        # 5=upline_name, 6=upline_email, 7=upline2_name, 8=upline2_email,
        # 9=created_at, 10=agent_rank, 11=commission_rate, 12=cumulative_gross,
        # 13=upline_commission_rate, 14=upline2_commission_rate,
        # 15=downline_count, 16=total_listings, 17=total_commission
        nodes = {}
        for agent in agents:
            agent_id = agent[0]
            nodes[agent_id] = {
                "id":                    agent[0],
                "name":                  agent[1],
                "email":                 agent[2],
                "upline_id":             agent[3],
                "upline2_id":            agent[4],
                "upline_name":           agent[5],
                "upline_email":          agent[6],
                "upline2_name":          agent[7],
                "upline2_email":         agent[8],
                "join_date":             agent[9],
                "agent_rank":            agent[10] or "REN",
                "commission_rate":       agent[11] or 70.0,
                "cumulative_gross":      agent[12] or 0.0,
                "upline_commission_rate":  agent[13],
                "upline2_commission_rate": agent[14],
                "downline_count":        agent[15] or 0,
                "total_listings":        agent[16] or 0,
                "total_commission":      agent[17] or 0,
                "downlines": [],
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

    # Render horizontal org-chart tree
    RANK_ICONS = {"REN":"🟣","Assoc REN":"🔵","Elite REN":"⭐","TL":"🏅","ATL":"👑"}
    RANK_COLORS = {
        "REN":       "#4a1ea8",
        "Assoc REN": "#0055b3",
        "Elite REN": "#7a4f00",
        "TL":        "#0a5c30",
        "ATL":       "#7a3300",
    }
    RANK_BG = {
        "REN":       "#ede8ff",
        "Assoc REN": "#dbeeff",
        "Elite REN": "#fff4cc",
        "TL":        "#d6f5e3",
        "ATL":       "#fff0e0",
    }
    THRESHOLDS = [
        ("REN",       0,       30000),
        ("Assoc REN", 30000,   90000),
        ("Elite REN", 90000,   210000),
        ("TL",        210000,  450000),
        ("ATL",       450000,  None),
    ]

    def render_node(agent):
        rank  = agent.get("agent_rank", "REN")
        pct   = float(agent.get("commission_rate") or 70)
        cumul = float(agent.get("cumulative_gross") or 0)
        icon  = RANK_ICONS.get(rank, "🟣")
        color = RANK_COLORS.get(rank, "#333")
        bg    = RANK_BG.get(rank, "#f8f9fa")
        aid   = agent["id"]
        has_children = bool(agent["downlines"])

        # Progress
        prog = 0
        next_label = "🏆 Top"
        for rname, rmin, rmax in THRESHOLDS:
            if rank == rname:
                if rmax:
                    prog = min(100, int((cumul - rmin) / (rmax - rmin) * 100))
                    next_label = f"RM {max(0,rmax-cumul):,.0f} → next"
                else:
                    prog = 100
                break

        toggle_btn = f'<button class="toggle-btn" onclick="toggleNode({aid})" title="Collapse/Expand">▾</button>' if has_children else ''

        # Build children HTML
        children_html = ""
        if has_children:
            children_html = f'<div class="children" id="children-{aid}">'
            for child in agent["downlines"]:
                children_html += render_node(child)
            children_html += '</div>'

        return f'''
<div class="tree-node" id="node-{aid}">
  <div class="node-card" style="border-top:3px solid {color}; background:{bg};"
       onclick="showDetail({aid})"
       data-id="{aid}"
       data-name="{agent['name']}"
       data-email="{agent['email']}"
       data-rank="{rank}"
       data-pct="{int(pct)}"
       data-cumul="{cumul:,.2f}"
       data-upline="{agent['upline_name'] or 'Top Level'}"
       data-downlines="{agent['downline_count'] or 0}"
       data-listings="{agent['total_listings'] or 0}"
       data-commission="{float(agent['total_commission'] or 0):,.2f}"
       data-joined="{str(agent['join_date'] or '')[:10]}"
       data-prog="{prog}"
       data-next="{next_label}">
    <div class="node-top">
      <span class="node-rank-badge" style="background:{color}; color:#fff;">{icon} {rank}</span>
      {toggle_btn}
    </div>
    <div class="node-name">{agent['name']}</div>
    <div class="node-pct" style="color:{color};">{int(pct)}%</div>
    <div class="node-bar-bg"><div class="node-bar-fg" style="width:{prog}%; background:{color};"></div></div>
    <div class="node-sub">#{aid} · {agent['total_listings'] or 0} listings</div>
  </div>
  {children_html}
</div>'''

    def render_tree_html(agents_list, level=0, parent_id=None):
        if not agents_list:
            return ""
        html = ""
        for agent in agents_list:
            html += render_node(agent)
        return html

    hierarchy_html = render_tree_html(hierarchy_tree) if hierarchy_tree else ""

    # Calculate statistics
    total_agents = len(agents)
    top_level_count = sum(1 for agent in agents if agent[3] is None or agent[3] == "")
    with_downlines = sum(1 for agent in agents if agent[15] and int(agent[15]) > 0)
    total_commission = sum(float(agent[17] or 0) for agent in agents)

    # Count agents per TAIKO rank (index 10 = agent_rank)
    rank_counts = {'REN': 0, 'Assoc REN': 0, 'Elite REN': 0, 'TL': 0, 'ATL': 0}
    for agent in agents:
        r = agent[10] or 'REN'
        if r in rank_counts:
            rank_counts[r] += 1
        else:
            rank_counts['REN'] += 1

    return render_template(
        "admin/agent_hierarchy.html",
        hierarchy_tree=hierarchy_tree,
        hierarchy_html=hierarchy_html,
        total_agents=total_agents,
        top_level_count=top_level_count,
        with_downlines=with_downlines,
        total_commission=total_commission,
        rank_counts=rank_counts,
    )


@app.route("/admin/add-agent", methods=["GET", "POST"])
def add_agent():
    """Add new agent with TAIKO rank selection"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, email, agent_rank FROM users WHERE role='agent' ORDER BY name"
    )
    existing_agents = [
        {"id": r[0], "name": r[1], "email": r[2], "agent_rank": r[3] or "REN"}
        for r in cursor.fetchall()
    ]
    conn.close()

    if request.method == "POST":
        name             = request.form.get("name", "").strip()
        email            = request.form.get("email", "").strip()
        password         = request.form.get("password", "")
        upline_id        = request.form.get("upline_id") or None
        initial_rank     = request.form.get("initial_rank", "REN")
        cumulative_gross = float(request.form.get("cumulative_gross") or 0)
        reason           = request.form.get("reason", "").strip() or "Admin new agent creation"

        # Validate rank and get payout %
        valid_ranks = [r["rank"] for r in TAIKO_RANKS]
        if initial_rank not in valid_ranks:
            initial_rank = "REN"
        commission_rate = next(r["payout_pct"] for r in TAIKO_RANKS if r["rank"] == initial_rank)

        hashed_pw = generate_password_hash(password)

        conn = sqlite3.connect("real_estate.db")
        cursor = conn.cursor()
        try:
            # Auto-resolve upline2 from upline's upline
            upline2_id = None
            if upline_id:
                cursor.execute("SELECT upline_id FROM users WHERE id = ?", (upline_id,))
                row = cursor.fetchone()
                upline2_id = row[0] if row else None

            cursor.execute(
                """
                INSERT INTO users
                    (email, password, name, role, upline_id, upline2_id,
                     agent_rank, commission_rate, cumulative_gross, upline_commission_rate)
                VALUES (?, ?, ?, 'agent', ?, ?, ?, ?, ?, 0)
                """,
                (email, hashed_pw, name, upline_id, upline2_id,
                 initial_rank, commission_rate, cumulative_gross),
            )
            new_agent_id = cursor.lastrowid

            # Log to rank_promotion_log if non-default rank was assigned
            if initial_rank != "REN" or cumulative_gross > 0:
                cursor.execute(
                    """
                    INSERT INTO rank_promotion_log
                        (agent_id, old_rank, new_rank, old_pct, new_pct,
                         cumulative_gross_at_promotion, promoted_by, admin_id, reason)
                    VALUES (?, 'REN', ?, 70, ?, ?, 'admin', ?, ?)
                    """,
                    (new_agent_id, initial_rank, commission_rate,
                     cumulative_gross, session["user_id"], reason),
                )

            conn.commit()
            conn.close()
            flash(
                f"✅ Agent {name} created successfully as {initial_rank} ({int(commission_rate)}%).",
                "success"
            )
            return redirect("/admin/agents")
        except Exception as e:
            conn.rollback()
            conn.close()
            flash(f"❌ Error creating agent: {str(e)}", "error")
            return redirect("/admin/add-agent")

    # GET — render external template
    return render_template("admin/add_agent.html", existing_agents=existing_agents)


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
            u.agent_rank,
            u.commission_rate,
            u.cumulative_gross
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
            name      = request.form["name"]
            email     = request.form["email"]
            upline_id = request.form.get("upline_id") or None
            password  = request.form.get("password", "")

            # Auto-resolve upline2 from upline's upline
            upline2_id = None
            if upline_id:
                cursor.execute("SELECT upline_id FROM users WHERE id = ?", (upline_id,))
                row = cursor.fetchone()
                upline2_id = row[0] if row else None

            if password:
                hashed_pw = generate_password_hash(password)
                cursor.execute(
                    """
                    UPDATE users
                    SET name = ?, email = ?, upline_id = ?, upline2_id = ?, password = ?
                    WHERE id = ?
                    """,
                    (name, email, upline_id, upline2_id, hashed_pw, agent_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE users
                    SET name = ?, email = ?, upline_id = ?, upline2_id = ?
                    WHERE id = ?
                    """,
                    (name, email, upline_id, upline2_id, agent_id),
                )

            conn.commit()
            conn.close()
            flash(f"\u2705 Agent {name} updated successfully.", "success")
            return redirect("/admin/agents")

        except Exception as e:
            conn.rollback()
            conn.close()
            flash(f"\u274c Error updating agent: {str(e)}", "error")
            return redirect(f"/admin/edit-agent/{agent_id}")
    
    # GET request — build a clean agent dict and render the external template
    conn.close()

    # Build agent dict (index: 0=id,1=email,2=pw,3=name,4=role,5=upline_id,
    #  6=created_at,7=upline2_id,8=total_listings,9=total_commission,
    #  10=agent_rank,11=commission_rate,12=cumulative_gross)
    agent_dict = {
        "id":              agent[0],
        "email":           agent[1],
        "name":            agent[3],
        "upline_id":       agent[5],
        "created_at":      agent[6],
        "total_listings":  agent[8],
        "total_commission": agent[9],
        "agent_rank":      agent[10] or "REN",
        "commission_rate": agent[11] or 70.0,
        "cumulative_gross": agent[12] or 0.0,
        "upline_name":     upline_name if upline_name != "None" else None,
    }

    # Enrich existing_agents list with rank for the dropdown
    conn2 = sqlite3.connect("real_estate.db")
    cur2  = conn2.cursor()
    cur2.execute(
        "SELECT id, name, email, agent_rank FROM users WHERE role='agent' AND id != ? ORDER BY name",
        (agent_id,)
    )
    all_agents = [{"id": r[0], "name": r[1], "email": r[2], "agent_rank": r[3] or "REN"}
                  for r in cur2.fetchall()]
    conn2.close()

    return render_template(
        "admin/edit_agent.html",
        agent=agent_dict,
        all_agents=all_agents,
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

    return render_template(
        "admin/commissions.html",
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
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>System Settings</title>
<style>
        *,*::before,*::after { box-sizing: border-box; }
        body { font-family: Arial, sans-serif; margin: 0; background: #f0f2f5; color: #1a2a3a; }
        .topbar { background:#2c3e50; color:white; padding:12px 16px; display:flex; align-items:center; justify-content:space-between; gap:8px; position:sticky; top:0; z-index:100; }
        .topbar-title { font-size:1rem; font-weight:700; }
        .topbar-right { display:flex; align-items:center; gap:10px; }
        .topbar-right a { color:#a8c8ff; text-decoration:none; font-size:13px; }
        .hamburger { display:none; background:none; border:none; color:white; font-size:22px; cursor:pointer; padding:2px 6px; }
        .nav-bar { background:white; padding:10px 16px; display:flex; flex-wrap:wrap; gap:4px; align-items:center; box-shadow:0 2px 6px rgba(0,0,0,.08); }
        .nav-bar a { color:#007bff; text-decoration:none; font-weight:600; font-size:13px; padding:5px 10px; border-radius:6px; white-space:nowrap; }
        .nav-bar a:hover { background:#f0f7ff; }
        .nav-bar a.nav-btn { background:#2563eb; color:white; }
        .nav-bar a.nav-logout { color:#dc3545; }
        .wrap { max-width:1400px; margin:0 auto; padding:16px; }
        .card { background:white; border-radius:10px; padding:20px; box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px; }
        .stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; margin-bottom:16px; }
        .scard { background:white; border-radius:10px; padding:14px 16px; box-shadow:0 1px 4px rgba(0,0,0,.08); border-top:3px solid #ddd; }
        .scard h3 { margin:0 0 6px; font-size:12px; color:#888; font-weight:600; text-transform:uppercase; }
        .scard-val { font-size:1.4rem; font-weight:800; margin-bottom:2px; }
        .tbl-wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px; }
        table { width:100%; border-collapse:collapse; background:white; min-width:500px; }
        th { background:#2c3e50; color:white; padding:10px 12px; text-align:left; font-size:12px; white-space:nowrap; }
        td { padding:10px 12px; border-bottom:1px solid #f0f0f0; font-size:13px; vertical-align:top; }
        tr:last-child td { border-bottom:none; } tr:hover td { background:#fafbfc; }
        .badge { padding:3px 8px; border-radius:10px; font-size:11px; font-weight:700; }
        .act-btn { display:inline-block; padding:5px 10px; border:none; border-radius:5px; font-size:12px; font-weight:600; cursor:pointer; text-decoration:none; white-space:nowrap; margin:2px 0; }
        .act-green{background:#28a745;color:white} .act-blue{background:#007bff;color:white}
        .act-red{background:#dc3545;color:white} .act-grey{background:#6c757d;color:white}
        .act-orange{background:#fd7e14;color:white} .act-purple{background:#6f42c1;color:white}
        .filter-wrap { background:white; border-radius:10px; padding:14px 16px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,.07); }
        .filter-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
        .filter-row select,.filter-row input { padding:7px 10px; border:1px solid #ddd; border-radius:6px; font-size:13px; flex:1; min-width:120px; }
        .btn-go { padding:7px 14px; background:#007bff; color:white; border:none; border-radius:6px; cursor:pointer; font-size:13px; font-weight:600; }
        .btn-clr { padding:7px 12px; background:#6c757d; color:white; border:none; border-radius:6px; font-size:13px; text-decoration:none; display:inline-block; }
        .sec-hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
        .sec-hdr h2 { margin:0; font-size:15px; }
        .empty { padding:30px; text-align:center; background:white; border-radius:10px; }
        .form-group { margin-bottom:16px; }
        label { display:block; margin-bottom:5px; font-weight:600; color:#444; font-size:13px; }
        input[type=text],input[type=email],input[type=password],input[type=number],select,textarea { width:100%; padding:9px 11px; border:1px solid #ccc; border-radius:6px; font-size:14px; background:#fafafa; }
        input:focus,select:focus,textarea:focus { outline:none; border-color:#007bff; background:white; }
        .btn-primary { background:#007bff; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; font-size:14px; font-weight:600; }
        .btn-secondary { background:#6c757d; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; font-size:14px; font-weight:600; text-decoration:none; display:inline-block; }
        .flash-s { background:#d4edda; color:#155724; padding:10px 14px; border-radius:6px; margin-bottom:14px; border-left:4px solid #28a745; font-size:13px; }
        .flash-e { background:#f8d7da; color:#721c24; padding:10px 14px; border-radius:6px; margin-bottom:14px; border-left:4px solid #dc3545; font-size:13px; }
        @media(max-width:640px) {
            .hamburger { display:block; }
            .nav-bar { display:none; flex-direction:column; align-items:stretch; padding:8px 12px; gap:2px; }
            .nav-bar.open { display:flex; }
            .nav-bar a { padding:10px 12px; font-size:14px; border-bottom:1px solid #f0f0f0; }
            .nav-bar a:last-child { border-bottom:none; }
            .wrap { padding:10px; }
            .stats-grid { grid-template-columns:repeat(2,1fr); gap:8px; }
            .scard { padding:12px; } .scard-val { font-size:1.2rem; }
            .filter-row { flex-direction:column; align-items:stretch; }
            .filter-row select,.filter-row input,.btn-go,.btn-clr { width:100%; }
            td,th { padding:8px 10px!important; font-size:12px; }
        }
</style>
</head>
<body>
<div class="topbar">
    <div class="topbar-title">&#9881; System Settings</div>
    <div class="topbar-right"><button class="hamburger" onclick="toggleNav()">&#9776;</button></div>
</div>
<div class="nav-bar" id="mainNav">
  <a href="/admin/dashboard" class="nav-active">&#128202; Dashboard</a>
  <a href="/admin/projects">&#127962; Projects</a>
  <a href="/admin/create-project" class="nav-btn">&#10010; New Project</a>
  <a href="/admin/agents">&#128101; Agents</a>
  <a href="/admin/agent-hierarchy">&#128279; Hierarchy</a>
  <a href="/admin/payments">&#9993; Payments</a>
  <a href="/admin/commissions">&#128176; Commissions</a>
  <a href="/admin/unified-submissions">&#128203; Unified Submissions</a>
  <a href="/admin/agent-performance">&#128200; Performance</a>
  <a href="/admin/commission-calculator">&#9889; Calc</a>
  <a href="/admin/settings">&#9881; Settings</a>
  <a href="/admin/export-data">&#128228; Export</a>
</div>
<div class="wrap">
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

    return render_template(
        "admin/settings.html",
        success=success_msg,
        error=error_msg,
        payment_settings=payment_settings,
        notification_settings=notification_settings,
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
    """Approve listing and run TAIKO EA override commission engine."""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = None
    try:
        conn = sqlite3.connect("real_estate.db")
        cursor = conn.cursor()

        # ── 1. Fetch listing + agent basics ──
        cursor.execute(
            """
            SELECT pl.id, pl.agent_id, pl.status, pl.sale_price,
                   pl.commission_amount, pl.project_id, pl.unit_id,
                   u.name as agent_name, u.agent_rank, u.commission_rate
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

        if listing[2] == "approved":
            flash("⚠️ Listing already approved", "warning")
            return redirect(f"/admin/documents/{listing_id}")

        agent_id        = listing[1]
        sale_price      = float(listing[3] or 0)
        project_id      = listing[5]
        unit_id         = listing[6]
        agent_name      = listing[7]
        agent_rank      = listing[8] or "REN"
        agent_rate      = float(listing[9] or 70)

        # ── 2. Determine gross commission from project/unit rate ──
        commission_rate_pct = 2.0          # default 2%
        commission_source   = "default"

        if project_id:
            cursor.execute("SELECT commission_rate FROM projects WHERE id = ?", (project_id,))
            row = cursor.fetchone()
            if row and row[0]:
                commission_rate_pct = float(row[0])
                commission_source   = "project"

        if unit_id:
            cursor.execute("SELECT commission_rate FROM project_units WHERE id = ?", (unit_id,))
            row = cursor.fetchone()
            if row and row[0]:
                commission_rate_pct = float(row[0])
                commission_source   = "unit"

        # Gross commission = full developer commission (before any split)
        gross_commission = sale_price * (commission_rate_pct / 100)
        gross_commission = max(1000.0, min(gross_commission, 50000.0))  # same caps as before

        # ── 3. Mark listing as approved ──
        cursor.execute(
            """
            UPDATE property_listings
            SET status            = 'approved',
                approved_at       = ?,
                approved_by       = ?,
                commission_status = 'pending',
                commission_amount = ?
            WHERE id = ?
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                session["user_id"],
                gross_commission,
                listing_id,
            ),
        )
        conn.commit()
        conn.close()
        conn = None

        # ── 4. Run TAIKO commission engine ──
        #    This function opens its own connection, writes taiko_commission_entries,
        #    updates cumulative_gross + total_commission for all parties,
        #    checks for auto-promotion, and logs to commission_calculations.
        entries = calculate_taiko_commission(listing_id, agent_id, gross_commission)

        # ── 5. Mirror agent's personal payout into commission_payments table
        #    (used by existing payment-tracking UI) ──
        agent_entry = next((e for e in entries if e["type"] == "personal"), None)
        agent_payout = agent_entry["amount"] if agent_entry else gross_commission * (agent_rate / 100)

        conn2 = sqlite3.connect("real_estate.db")
        cur2  = conn2.cursor()

        cur2.execute(
            """
            INSERT OR IGNORE INTO commission_payments
            (listing_id, agent_id, commission_amount, payment_status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (listing_id, agent_id, agent_payout,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

        # ── 6. Mirror upline overrides into upline_commissions table
        #    (used by existing upline payout UI) ──
        for e in entries:
            if e["type"] in ("override", "wtp_gen1", "wtp_gen2") and e.get("amount", 0) > 0:
                cur2.execute(
                    """
                    INSERT INTO upline_commissions
                    (listing_id, agent_id, upline_id, amount, status,
                     commission_type, commission_rate, created_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        listing_id,
                        agent_id,
                        e["agent_id"],
                        e["amount"],
                        e["type"],                          # override / wtp_gen1 / wtp_gen2
                        e.get("gap_pct") or e.get("wtp_pct") or 0,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )

        # ── 7. Notify selling agent ──
        #    Check if a promotion happened (check_and_promote_agent already ran
        #    inside calculate_taiko_commission — read the updated rank back)
        cur2.execute("SELECT agent_rank FROM users WHERE id = ?", (agent_id,))
        new_rank_row = cur2.fetchone()
        new_rank     = new_rank_row[0] if new_rank_row else agent_rank
        promoted     = new_rank != agent_rank

        if promoted:
            notif_title = "🎉 Listing Approved + Rank Promotion!"
            notif_msg   = (
                f"Your listing #{listing_id} has been approved. "
                f"Congratulations — you have been promoted from {agent_rank} to {new_rank}! "
                f"Your new payout rate applies from your next deal."
            )
        else:
            notif_title = "✅ Listing Approved"
            notif_msg   = (
                f"Your listing #{listing_id} has been approved. "
                f"Commission of RM {agent_payout:,.2f} is pending payout."
            )

        cur2.execute(
            """
            INSERT INTO agent_notifications
            (agent_id, title, message, notification_type,
             related_id, related_type, priority, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id,
                notif_title,
                notif_msg,
                "listing_approved",
                listing_id,
                "listing",
                "high",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )

        conn2.commit()
        conn2.close()

        # ── 8. Build a readable summary for the flash message ──
        total_distributed = sum(e["amount"] for e in entries)
        override_count    = sum(1 for e in entries if e["type"] in ("override","wtp_gen1","wtp_gen2"))
        promo_note        = f" | 🎉 {agent_name} promoted to {new_rank}!" if promoted else ""
        flash(
            f"✅ Listing #{listing_id} approved! "
            f"Gross: RM {gross_commission:,.2f} | "
            f"Agent payout: RM {agent_payout:,.2f} ({agent_rank} {int(agent_rate)}%) | "
            f"{override_count} override(s) distributed.{promo_note}",
            "success"
        )
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
        try:
            cursor.execute(query_upline, params_upline)
        except Exception:
            cursor.execute("SELECT 1 WHERE 0")  # empty result
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

        try:
            cursor.execute(query_upline_simple, params_upline)
        except Exception:
            cursor.execute("SELECT 1 WHERE 0")  # empty result
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

    # Get stats from upline_commissions table (may not exist)
    uc_stats = None
    try:
        cursor.execute("""
            SELECT 
                COUNT(*) as total_upline_payments,
                SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) as total_upline_paid_db,
                SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END) as total_upline_pending_db
            FROM upline_commissions
        """)
        uc_stats = cursor.fetchone()
    except Exception:
        uc_stats = (0, 0, 0)

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
                        <a href="/admin/mark-commission-paid/CP-{{ payment_data.id }}" class="btn" style="background: #28a745;">✅ Mark as Paid</a>
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

            # WTP commission config fields
            comm_dev_rate     = float(data.get("comm_dev_rate", 2.0))
            comm_sst_rate     = float(data.get("comm_sst_rate", 8.0))
            comm_company_pct  = float(data.get("comm_company_pct", 15.0))
            comm_pic_pct      = float(data.get("comm_pic_pct", 10.0))
            comm_agents_pct   = float(data.get("comm_agents_pct", 75.0))
            comm_pic_rank     = data.get("comm_pic_rank", "REN")
            comm_pic_agent_id = int(data.get("comm_pic_agent_id", 0)) or None
            comm_pic_name     = data.get("comm_pic_name", "").strip() or None

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
                 commission_rate, project_sale_type, created_by, status,
                 comm_dev_rate, comm_sst_rate, comm_company_pct,
                 comm_pic_pct, comm_agents_pct, comm_pic_rank,
                 comm_pic_agent_id, comm_pic_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    project_name, description, location, project_type, category,
                    commission_rate, project_sale_type, session["user_id"], "active",
                    comm_dev_rate, comm_sst_rate, comm_company_pct,
                    comm_pic_pct, comm_agents_pct, comm_pic_rank,
                    comm_pic_agent_id, comm_pic_name,
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
                    # unit_code stored in unit_type field (no unit_code column in schema)
                    combined_type = (unit_code + ' – ' + unit_type).strip(' –') if unit_type else unit_code
                    cursor.execute(
                        """
                        INSERT INTO project_units 
                        (project_id, unit_type, base_price, square_feet, 
                        commission_rate, quantity, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            project_id,
                            combined_type,
                            price,
                            size,
                            commission,
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
    _conn = get_db_connection()
    _cur  = _conn.cursor()
    _cur.execute("""SELECT id, name, agent_rank, commission_rate FROM users
                    WHERE role='agent' ORDER BY name""")
    _agents = [{"id": r[0], "name": r[1], "rank": r[2] or "REN", "rate": float(r[3] or 70)}
               for r in _cur.fetchall()]
    _conn.close()

    _rank_label = {"ATL":"ATL (90%)", "TL":"TL (85%)", "ELITE":"Elite REN (80%)",
                   "ASSOC":"Assoc REN (75%)", "REN":"REN (70%)"}
    _agent_opts = '<option value="">-- Select PIC Agent --</option>'
    for _ag in _agents:
        _rd = _rank_label.get(_ag["rank"], _ag["rank"])
        _agent_opts += (f'<option value="{_ag["id"]}" data-rank="{_ag["rank"]}" '
                        f'data-name="{_ag["name"]}">{_ag["name"]} — {_rd}</option>')

    return render_template("admin/create_project.html", agent_opts=_agent_opts)


@app.route("/admin/edit-project/<int:project_id>", methods=["GET", "POST"])
def edit_project(project_id):
    """Edit project — renders templates/admin/edit_project.html"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, project_name, category, project_type, location, description,
               status, commission_rate, created_by, created_at, updated_at,
               project_sale_type,
               COALESCE(comm_dev_rate, 2.0), COALESCE(comm_sst_rate, 8.0),
               COALESCE(comm_company_pct, 15.0), COALESCE(comm_pic_pct, 10.0),
               COALESCE(comm_agents_pct, 75.0), COALESCE(comm_pic_rank, 'REN'),
               comm_pic_agent_id, COALESCE(comm_pic_name, '')
        FROM projects WHERE id = ?
    """, (project_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return "Project not found", 404

    cursor.execute(
        "SELECT * FROM project_units WHERE project_id = ? ORDER BY unit_type",
        (project_id,)
    )
    existing_units = cursor.fetchall()

    if request.method == "POST":
        try:
            data = request.form
            cursor.execute("""
                UPDATE projects SET
                    project_name=?, category=?, project_type=?,
                    location=?, description=?, commission_rate=?,
                    comm_dev_rate=?, comm_sst_rate=?, comm_company_pct=?,
                    comm_pic_pct=?, comm_agents_pct=?, comm_pic_rank=?,
                    comm_pic_agent_id=?, comm_pic_name=?, updated_at=?
                WHERE id=?
            """, (
                data["project_name"], data["category"], data["project_type"],
                data.get("location", ""), data.get("description", ""),
                float(data.get("project_commission", 0)),
                float(data.get("comm_dev_rate", 2.0)),
                float(data.get("comm_sst_rate", 8.0)),
                float(data.get("comm_company_pct", 15.0)),
                float(data.get("comm_pic_pct", 10.0)),
                float(data.get("comm_agents_pct", 75.0)),
                data.get("comm_pic_rank", "REN"),
                int(data.get("comm_pic_agent_id", 0)) or None,
                data.get("comm_pic_name", "").strip() or None,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), project_id,
            ))
            cursor.execute("DELETE FROM project_units WHERE project_id = ?", (project_id,))
            n = 0
            while f"unit_type_{n}" in data:
                ut = data.get(f"unit_type_{n}")
                if ut:
                    sf  = data.get(f"square_feet_{n}")
                    bp  = data.get(f"base_price_{n}")
                    rp  = data.get(f"rental_price_{n}")
                    cr  = data.get(f"unit_commission_{n}")
                    qty = data.get(f"quantity_{n}", 1)
                    cursor.execute("""
                        INSERT INTO project_units
                        (project_id, unit_type, square_feet, base_price, rental_price,
                         commission_rate, quantity, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'available')
                    """, (
                        project_id, ut,
                        int(sf) if sf else None,
                        float(bp) if bp else None,
                        float(rp) if rp else None,
                        float(cr) if cr else None,
                        int(qty) if qty else 1,
                    ))
                n += 1
            conn.commit()
            conn.close()
            return redirect(f"/admin/project/{project_id}?success=Project updated successfully!")
        except Exception as e:
            conn.rollback()
            conn.close()
            return f"<h2>Error: {str(e)}</h2><a href='/admin/edit-project/{project_id}'>Try Again</a>"

    # GET — load agents for PIC selector
    agent_conn = get_db_connection()
    agent_cur  = agent_conn.cursor()
    agent_cur.execute("""SELECT id, name, agent_rank, commission_rate FROM users
                         WHERE role='agent'
                         ORDER BY name""")
    agents = [{"id":r[0],"name":r[1],"rank":r[2] or "REN","rate":float(r[3] or 70)}
              for r in agent_cur.fetchall()]
    agent_conn.close()

    curr_pic_id = int(row[18]) if row[18] else 0
    rank_labels = {"ATL":"ATL (90%)","TL":"TL (85%)","ELITE":"Elite REN (80%)",
                   "ASSOC":"Assoc REN (75%)","REN":"REN (70%)"}
    agent_opts = '<option value="">-- Select PIC Agent --</option>'
    for ag in agents:
        sel = ' selected' if curr_pic_id and ag["id"] == curr_pic_id else ''
        agent_opts += (f'<option value="{ag["id"]}" data-rank="{ag["rank"]}" '
                       f'data-name="{ag["name"]}"{sel}>'
                       f'{ag["name"]} — {rank_labels.get(ag["rank"], ag["rank"])}</option>')

    units_data = []
    for u in existing_units:
        units_data.append({
            "unit_type":       u[2] or "",
            "square_feet":     u[3] or "",
            "base_price":      u[4] or "",
            "rental_price":    u[5] or "",
            "commission_rate": u[6] or "",
            "quantity":        u[7] or 1,
        })

    conn.close()

    project = {
        "id":               row[0],
        "name":             row[1] or "",
        "category":         row[2] or "condo",
        "project_type":     row[3] or "residential",
        "location":         row[4] or "",
        "description":      row[5] or "",
        "commission_rate":  row[7] or 3.0,
        "comm_dev_rate":    row[12],
        "comm_sst_rate":    row[13],
        "comm_company_pct": row[14],
        "comm_pic_pct":     row[15],
        "comm_agents_pct":  row[16],
        "comm_pic_rank":    row[17],
        "comm_pic_name":    row[19] or "",
    }

    return render_template(
        "admin/edit_project.html",
        project=project,
        agent_opts=agent_opts,
        units_json=json.dumps(units_data),
        curr_pic_id=curr_pic_id,
    )

@app.route("/admin/projects")
def list_projects():
    """List all projects — renders templates/admin/projects.html"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.project_name, p.category, p.project_type, p.location,
               p.description, p.status, p.commission_rate, p.project_sale_type,
               p.created_at, p.updated_at, u.name AS created_by_name,
               COUNT(DISTINCT pu.id) AS unit_count,
               CASE WHEN p.status = 'active' THEN 1 ELSE 0 END AS is_active,
               SUM(pu.quantity) AS total_units
        FROM projects p
        LEFT JOIN users u ON p.created_by = u.id
        LEFT JOIN project_units pu ON p.id = pu.project_id
        GROUP BY p.id
        ORDER BY p.status DESC, p.created_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    projects = []
    active_count = 0
    total_units_sum = 0
    sales_count = 0

    for r in rows:
        is_active = bool(r[13]) if r[13] is not None else True
        created_at = r[9]
        created_date = (created_at[:10] if isinstance(created_at, str) and len(created_at) >= 10
                        else str(created_at)[:10] if created_at else "N/A")
        total_units = int(r[14]) if r[14] else 0
        category = (r[2] or "N/A").lower()
        project_type = (r[3] or "N/A").lower()

        if is_active:
            active_count += 1
        total_units_sum += total_units
        if r[8] == "sales":
            sales_count += 1

        projects.append({
            "id":           r[0],
            "name":         r[1] or "Unnamed",
            "category":     category,
            "project_type": project_type,
            "location":     r[4] or "Not specified",
            "commission":   r[7] or "N/A",
            "sale_type":    r[8] or "sales",
            "created_date": created_date,
            "created_by":   r[11] or "Unknown",
            "unit_count":   r[12] or 0,
            "total_units":  total_units,
            "is_active":    is_active,
        })

    return render_template(
        "admin/projects.html",
        projects=projects,
        total_projects=len(projects),
        active_count=active_count,
        total_units_sum=total_units_sum,
        sales_count=sales_count,
    )

@app.route("/admin/project/<int:project_id>")
def view_project(project_id):
    """View project details — renders templates/admin/project_detail.html"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.id, p.project_name, p.category, p.project_type, p.location,
               p.description, p.status, p.commission_rate, p.project_sale_type,
               p.created_at, p.updated_at, u.name AS created_by_name,
               p.is_active,
               COALESCE(p.comm_dev_rate, 2.0)     AS comm_dev_rate,
               COALESCE(p.comm_sst_rate, 8.0)     AS comm_sst_rate,
               COALESCE(p.comm_company_pct, 15.0) AS comm_company_pct,
               COALESCE(p.comm_pic_pct, 10.0)     AS comm_pic_pct,
               COALESCE(p.comm_agents_pct, 75.0)  AS comm_agents_pct,
               COALESCE(p.comm_pic_rank, 'REN')   AS comm_pic_rank,
               COALESCE(p.comm_pic_name, '')       AS comm_pic_name
        FROM projects p
        LEFT JOIN users u ON p.created_by = u.id
        WHERE p.id = ?
    """, (project_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return "Project not found", 404

    cursor.execute(
        "SELECT * FROM project_units WHERE project_id = ? ORDER BY unit_type",
        (project_id,)
    )
    unit_rows = cursor.fetchall()
    conn.close()

    project = {
        "id":               row[0],
        "name":             row[1] or "Unnamed",
        "category":         (row[2] or "N/A").lower(),
        "project_type":     (row[3] or "N/A").lower(),
        "location":         row[4] or "",
        "description":      row[5] or "",
        "status":           row[6] or "active",
        "commission_rate":  row[7] or 0,
        "sale_type":        row[8] or "sales",
        "created_at":       (row[9] or "")[:10],
        "updated_at":       (row[10] or "")[:10] if row[10] else "",
        "created_by":       row[11] or "Unknown",
        "is_active":        bool(row[12]) if row[12] is not None else True,
        "comm_dev_rate":    row[13],
        "comm_sst_rate":    row[14],
        "comm_company_pct": row[15],
        "comm_pic_pct":     row[16],
        "comm_agents_pct":  row[17],
        "comm_pic_rank":    row[18],
        "comm_pic_name":    row[19],
    }

    price_label = "Sale Price" if project["sale_type"] == "sales" else "Monthly Rent"

    units = []
    for u in unit_rows:
        price_val = u[5] if project["sale_type"] == "rental" and u[5] else u[4]
        comm_val  = u[6] if u[6] else project["commission_rate"]
        units.append({
            "code":             u[11] if len(u) > 11 else "",
            "unit_type":        u[2] or "",
            "square_feet":      u[3] or "",
            "price_display":    f"RM{price_val:,.2f}" if price_val else "N/A",
            "commission_display": f"{comm_val}%" if comm_val else "N/A",
            "quantity":         u[7] or 1,
            "status":           u[8] or "available",
        })

    success = request.args.get("success", "")
    return render_template(
        "admin/project_detail.html",
        project=project,
        units=units,
        price_label=price_label,
        success=success,
    )

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
    # Build stats and agent list for the export page
    conn2 = get_db_connection()
    conn2.row_factory = sqlite3.Row
    try:
        stats_row = conn2.execute("""
            SELECT
                COUNT(*) as approved,
                COALESCE(SUM(CASE WHEN status='approved' THEN sale_price END),0) as total_sales,
                COALESCE(SUM(CASE WHEN status='approved' THEN commission_amount END),0) as total_commissions
            FROM property_listings
        """).fetchone()
        stats_dict = dict(stats_row) if stats_row else {}
        stats_dict['total_agents'] = conn2.execute("SELECT COUNT(*) FROM users WHERE role='agent'").fetchone()[0]

        agents_rows = conn2.execute("""
            SELECT u.name, u.email, u.agent_rank,
                (SELECT COALESCE(SUM(pl.commission_amount),0) FROM property_listings pl WHERE pl.agent_id=u.id AND pl.status='approved') as total_commission,
                (SELECT COALESCE(SUM(pl.commission_amount),0) FROM property_listings pl WHERE pl.agent_id=u.id AND pl.status IN ('submitted','approved')) as cumulative_gross,
                (SELECT COUNT(*) FROM property_listings pl WHERE pl.agent_id=u.id) as total_listings
            FROM users u WHERE u.role='agent'
            ORDER BY cumulative_gross DESC
        """).fetchall()
        agents_data = [dict(r) for r in agents_rows]
    except Exception:
        stats_dict = {'total_agents':0,'approved':0,'total_sales':0,'total_commissions':0}
        agents_data = []
    finally:
        conn2.close()

    return render_template("admin/export.html", stats=stats_dict, agents=agents_data)

    export_template = """
    <!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Export Data</title>
<style>
        *,*::before,*::after { box-sizing: border-box; }
        body { font-family: Arial, sans-serif; margin: 0; background: #f0f2f5; color: #1a2a3a; }
        .topbar { background:#2c3e50; color:white; padding:12px 16px; display:flex; align-items:center; justify-content:space-between; gap:8px; position:sticky; top:0; z-index:100; }
        .topbar-title { font-size:1rem; font-weight:700; }
        .topbar-right { display:flex; align-items:center; gap:10px; }
        .topbar-right a { color:#a8c8ff; text-decoration:none; font-size:13px; }
        .hamburger { display:none; background:none; border:none; color:white; font-size:22px; cursor:pointer; padding:2px 6px; }
        .nav-bar { background:white; padding:10px 16px; display:flex; flex-wrap:wrap; gap:4px; align-items:center; box-shadow:0 2px 6px rgba(0,0,0,.08); }
        .nav-bar a { color:#007bff; text-decoration:none; font-weight:600; font-size:13px; padding:5px 10px; border-radius:6px; white-space:nowrap; }
        .nav-bar a:hover { background:#f0f7ff; }
        .nav-bar a.nav-btn { background:#2563eb; color:white; }
        .nav-bar a.nav-logout { color:#dc3545; }
        .wrap { max-width:1400px; margin:0 auto; padding:16px; }
        .card { background:white; border-radius:10px; padding:20px; box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px; }
        .stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; margin-bottom:16px; }
        .scard { background:white; border-radius:10px; padding:14px 16px; box-shadow:0 1px 4px rgba(0,0,0,.08); border-top:3px solid #ddd; }
        .scard h3 { margin:0 0 6px; font-size:12px; color:#888; font-weight:600; text-transform:uppercase; }
        .scard-val { font-size:1.4rem; font-weight:800; margin-bottom:2px; }
        .tbl-wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px; }
        table { width:100%; border-collapse:collapse; background:white; min-width:500px; }
        th { background:#2c3e50; color:white; padding:10px 12px; text-align:left; font-size:12px; white-space:nowrap; }
        td { padding:10px 12px; border-bottom:1px solid #f0f0f0; font-size:13px; vertical-align:top; }
        tr:last-child td { border-bottom:none; } tr:hover td { background:#fafbfc; }
        .badge { padding:3px 8px; border-radius:10px; font-size:11px; font-weight:700; }
        .act-btn { display:inline-block; padding:5px 10px; border:none; border-radius:5px; font-size:12px; font-weight:600; cursor:pointer; text-decoration:none; white-space:nowrap; margin:2px 0; }
        .act-green{background:#28a745;color:white} .act-blue{background:#007bff;color:white}
        .act-red{background:#dc3545;color:white} .act-grey{background:#6c757d;color:white}
        .act-orange{background:#fd7e14;color:white} .act-purple{background:#6f42c1;color:white}
        .filter-wrap { background:white; border-radius:10px; padding:14px 16px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,.07); }
        .filter-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
        .filter-row select,.filter-row input { padding:7px 10px; border:1px solid #ddd; border-radius:6px; font-size:13px; flex:1; min-width:120px; }
        .btn-go { padding:7px 14px; background:#007bff; color:white; border:none; border-radius:6px; cursor:pointer; font-size:13px; font-weight:600; }
        .btn-clr { padding:7px 12px; background:#6c757d; color:white; border:none; border-radius:6px; font-size:13px; text-decoration:none; display:inline-block; }
        .sec-hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
        .sec-hdr h2 { margin:0; font-size:15px; }
        .empty { padding:30px; text-align:center; background:white; border-radius:10px; }
        .form-group { margin-bottom:16px; }
        label { display:block; margin-bottom:5px; font-weight:600; color:#444; font-size:13px; }
        input[type=text],input[type=email],input[type=password],input[type=number],select,textarea { width:100%; padding:9px 11px; border:1px solid #ccc; border-radius:6px; font-size:14px; background:#fafafa; }
        input:focus,select:focus,textarea:focus { outline:none; border-color:#007bff; background:white; }
        .btn-primary { background:#007bff; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; font-size:14px; font-weight:600; }
        .btn-secondary { background:#6c757d; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; font-size:14px; font-weight:600; text-decoration:none; display:inline-block; }
        .flash-s { background:#d4edda; color:#155724; padding:10px 14px; border-radius:6px; margin-bottom:14px; border-left:4px solid #28a745; font-size:13px; }
        .flash-e { background:#f8d7da; color:#721c24; padding:10px 14px; border-radius:6px; margin-bottom:14px; border-left:4px solid #dc3545; font-size:13px; }
        @media(max-width:640px) {
            .hamburger { display:block; }
            .nav-bar { display:none; flex-direction:column; align-items:stretch; padding:8px 12px; gap:2px; }
            .nav-bar.open { display:flex; }
            .nav-bar a { padding:10px 12px; font-size:14px; border-bottom:1px solid #f0f0f0; }
            .nav-bar a:last-child { border-bottom:none; }
            .wrap { padding:10px; }
            .stats-grid { grid-template-columns:repeat(2,1fr); gap:8px; }
            .scard { padding:12px; } .scard-val { font-size:1.2rem; }
            .filter-row { flex-direction:column; align-items:stretch; }
            .filter-row select,.filter-row input,.btn-go,.btn-clr { width:100%; }
            td,th { padding:8px 10px!important; font-size:12px; }
        }
</style>
</head>
<body>
<div class="topbar">
    <div class="topbar-title">&#128228; Export Data</div>
    <div class="topbar-right"><a href="/admin/dashboard">&#128202; Dashboard</a>
        <button class="hamburger" onclick="toggleNav()">&#9776;</button></div>
</div>
<div class="nav-bar" id="mainNav">
  <a href="/admin/dashboard" class="nav-active">&#128202; Dashboard</a>
  <a href="/admin/projects">&#127962; Projects</a>
  <a href="/admin/create-project" class="nav-btn">&#10010; New Project</a>
  <a href="/admin/agents">&#128101; Agents</a>
  <a href="/admin/agent-hierarchy">&#128279; Hierarchy</a>
  <a href="/admin/payments">&#9993; Payments</a>
  <a href="/admin/commissions">&#128176; Commissions</a>
  <a href="/admin/unified-submissions">&#128203; Unified Submissions</a>
  <a href="/admin/agent-performance">&#128200; Performance</a>
  <a href="/admin/commission-calculator">&#9889; Calc</a>
  <a href="/admin/settings">&#9881; Settings</a>
  <a href="/admin/export-data">&#128228; Export</a>
</div>
<div class="wrap">

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

    return render_template(
        "admin/export.html",
        stats=stats_dict,
        agents=agents_data,
    )


@app.route("/admin/agent-performance")
def agent_performance_admin():
    """Admin view of agent performance analytics — WTP aware"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    conn = sqlite3.connect("real_estate.db")
    cursor = conn.cursor()

    cursor.execute("""
        SELECT u.id, u.name, u.email, u.agent_rank, u.commission_rate,
               u.cumulative_gross, u.created_at, u.upline_id, up.name AS upline_name,
               COUNT(DISTINCT pl.id) AS total_listings,
               SUM(CASE WHEN pl.status='approved' THEN 1 ELSE 0 END) AS approved,
               SUM(CASE WHEN pl.status='rejected' THEN 1 ELSE 0 END) AS rejected,
               SUM(CASE WHEN pl.status='approved' THEN pl.sale_price ELSE 0 END) AS total_sales,
               SUM(CASE WHEN pl.status='approved' THEN pl.commission_amount ELSE 0 END) AS total_comm
        FROM users u
        LEFT JOIN users up ON u.upline_id = up.id
        LEFT JOIN property_listings pl ON u.id = pl.agent_id
        WHERE u.role = 'agent'
        GROUP BY u.id
        ORDER BY total_comm DESC
    """)
    agents_raw = cursor.fetchall()

    cursor.execute("""
        SELECT agent_id,
               SUM(CASE WHEN entry_type='personal'  THEN amount ELSE 0 END),
               SUM(CASE WHEN entry_type='override'  THEN amount ELSE 0 END),
               SUM(CASE WHEN entry_type='wtp_gen1'  THEN amount ELSE 0 END),
               SUM(CASE WHEN entry_type='wtp_gen2'  THEN amount ELSE 0 END),
               COUNT(DISTINCT listing_id)
        FROM taiko_commission_entries GROUP BY agent_id
    """)
    taiko_map = {r[0]: r for r in cursor.fetchall()}

    cursor.execute("""
        SELECT strftime('%Y-%m', pl.approved_at) AS month, u.name, u.agent_rank,
               COUNT(pl.id), SUM(pl.commission_amount)
        FROM property_listings pl
        JOIN users u ON pl.agent_id = u.id
        WHERE pl.status='approved' AND pl.approved_at IS NOT NULL
        GROUP BY month, u.id ORDER BY month DESC, 5 DESC LIMIT 60
    """)
    monthly_data = cursor.fetchall()

    cursor.execute("""
        SELECT rpl.promoted_at, u.name, rpl.old_rank, rpl.new_rank,
               rpl.cumulative_gross_at_promotion
        FROM rank_promotion_log rpl
        JOIN users u ON rpl.agent_id = u.id
        ORDER BY rpl.promoted_at DESC LIMIT 10
    """)
    recent_promotions = cursor.fetchall()
    conn.close()

    RANK_TARGETS = {
        "REN":      {"next":"Assoc REN","cur_min":0,"next_min":30000},
        "Assoc REN":{"next":"Elite REN","cur_min":30000,"next_min":90000},
        "Elite REN":{"next":"TL","cur_min":90000,"next_min":210000},
        "TL":       {"next":"ATL","cur_min":210000,"next_min":450000},
        "ATL":      {"next":None,"cur_min":450000,"next_min":None},
    }
    agents = []
    for r in agents_raw:
        aid,name,email,rank,comm_rate,cumgross,joined,upline_id,upline_name,\
            total_l,approved,rejected,total_sales,total_comm = r
        cumgross=float(cumgross or 0); total_sales=float(total_sales or 0)
        total_comm=float(total_comm or 0); rank=rank or "REN"
        rt=RANK_TARGETS.get(rank,RANK_TARGETS["REN"])
        if rt["next_min"]:
            span=rt["next_min"]-rt["cur_min"]; done=max(0,cumgross-rt["cur_min"])
            prog_pct=min(100,round(done/span*100,1)) if span else 100
            remaining=max(0,rt["next_min"]-cumgross)
        else:
            prog_pct=100; remaining=0
        tk=taiko_map.get(aid)
        agents.append({"id":aid,"name":name,"email":email,"rank":rank,
            "comm_rate":float(comm_rate or 70),"cumgross":cumgross,
            "joined":(joined or "")[:10],"upline":upline_name or "—",
            "total_l":total_l or 0,"approved":approved or 0,"rejected":rejected or 0,
            "success_rate":round((approved or 0)/(total_l)*100) if total_l else 0,
            "total_sales":total_sales,"total_comm":total_comm,
            "personal_e":float(tk[1] or 0) if tk else 0,
            "override_e":float(tk[2] or 0) if tk else 0,
            "wtp1_e":float(tk[3] or 0) if tk else 0,
            "wtp2_e":float(tk[4] or 0) if tk else 0,
            "taiko_deals":tk[5] if tk else 0,
            "prog_pct":prog_pct,"remaining":remaining,"next_rank":rt["next"]})

    rank_counts={}
    for a in agents: rank_counts[a["rank"]]=rank_counts.get(a["rank"],0)+1

    return render_template_string(AGENT_PERF_TEMPLATE,
        agents=agents, monthly_data=monthly_data,
        recent_promotions=recent_promotions,
        total_agents=len(agents),
        total_commissions=sum(a["total_comm"] for a in agents),
        total_sales=sum(a["total_sales"] for a in agents),
        avg_success=round(sum(a["success_rate"] for a in agents)/max(len(agents),1)),
        rank_counts=rank_counts,
    )


AGENT_PERF_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Agent Performance</title>
<style>
        *,*::before,*::after { box-sizing: border-box; }
        body { font-family: Arial, sans-serif; margin: 0; background: #f0f2f5; color: #1a2a3a; }
        .topbar { background:#2c3e50; color:white; padding:12px 16px; display:flex;
                  align-items:center; justify-content:space-between; gap:8px;
                  position:sticky; top:0; z-index:100; }
        .topbar-title { font-size:1rem; font-weight:700; }
        .topbar-right { display:flex; align-items:center; gap:10px; }
        .topbar-right a { color:#a8c8ff; text-decoration:none; font-size:13px; }
        .hamburger { display:none; background:none; border:none; color:white; font-size:22px; cursor:pointer; padding:2px 6px; }
        .nav-bar { background:white; padding:10px 16px; display:flex; flex-wrap:wrap; gap:4px; align-items:center; box-shadow:0 2px 6px rgba(0,0,0,.08); }
        .nav-bar a { color:#007bff; text-decoration:none; font-weight:600; font-size:13px; padding:5px 10px; border-radius:6px; white-space:nowrap; }
        .nav-bar a:hover { background:#f0f7ff; }
        .nav-bar a.nav-btn { background:#2563eb; color:white; }
        .nav-bar a.nav-logout { color:#dc3545; }
        .wrap { max-width:1400px; margin:0 auto; padding:16px; }
        .card { background:white; border-radius:10px; padding:20px; box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px; }
        .card-title { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.1em; color:#7a8fa0; margin-bottom:14px; padding-bottom:8px; border-bottom:1px solid #eee; }
        .stats-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px; margin-bottom:16px; }
        .scard { background:white; border-radius:10px; padding:14px 16px; box-shadow:0 1px 4px rgba(0,0,0,.08); border-top:3px solid #ddd; }
        .scard h3 { margin:0 0 6px; font-size:12px; color:#888; font-weight:600; text-transform:uppercase; letter-spacing:.05em; }
        .scard-val { font-size:1.4rem; font-weight:800; margin-bottom:2px; }
        .scard small { font-size:11px; color:#888; }
        .tbl-wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); margin-bottom:16px; }
        table { width:100%; border-collapse:collapse; background:white; min-width:500px; }
        th { background:#2c3e50; color:white; padding:10px 12px; text-align:left; font-size:12px; white-space:nowrap; }
        td { padding:10px 12px; border-bottom:1px solid #f0f0f0; font-size:13px; vertical-align:top; }
        tr:last-child td { border-bottom:none; }
        tr:hover td { background:#fafbfc; }
        .badge { padding:3px 8px; border-radius:10px; font-size:11px; font-weight:700; }
        .act-btn { display:inline-block; padding:5px 10px; border:none; border-radius:5px; font-size:12px; font-weight:600; cursor:pointer; text-decoration:none; white-space:nowrap; margin:2px 0; }
        .act-green { background:#28a745; color:white; }
        .act-blue  { background:#007bff; color:white; }
        .act-red   { background:#dc3545; color:white; }
        .act-grey  { background:#6c757d; color:white; }
        .act-purple{ background:#6f42c1; color:white; }
        .filter-wrap { background:white; border-radius:10px; padding:14px 16px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,.07); }
        .filter-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
        .filter-row select,.filter-row input { padding:7px 10px; border:1px solid #ddd; border-radius:6px; font-size:13px; flex:1; min-width:120px; }
        .btn-go  { padding:7px 14px; background:#007bff; color:white; border:none; border-radius:6px; cursor:pointer; font-size:13px; font-weight:600; white-space:nowrap; }
        .btn-clr { padding:7px 12px; background:#6c757d; color:white; border:none; border-radius:6px; font-size:13px; text-decoration:none; white-space:nowrap; display:inline-block; }
        .sec-hdr { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; flex-wrap:wrap; gap:8px; }
        .sec-hdr h2 { margin:0; font-size:15px; }
        .empty { padding:30px; text-align:center; background:white; border-radius:10px; }
        .form-group { margin-bottom:16px; }
        label { display:block; margin-bottom:5px; font-weight:600; color:#444; font-size:13px; }
        input[type=text],input[type=email],input[type=password],input[type=number],select,textarea {
            width:100%; padding:9px 11px; border:1px solid #ccc; border-radius:6px; font-size:14px; background:#fafafa; }
        input:focus,select:focus,textarea:focus { outline:none; border-color:#007bff; background:white; }
        .btn-primary   { background:#007bff; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; font-size:14px; font-weight:600; }
        .btn-secondary { background:#6c757d; color:white; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; font-size:14px; font-weight:600; text-decoration:none; display:inline-block; }
        .flash-s { background:#d4edda; color:#155724; padding:10px 14px; border-radius:6px; margin-bottom:14px; font-size:13px; border-left:4px solid #28a745; }
        .flash-e { background:#f8d7da; color:#721c24; padding:10px 14px; border-radius:6px; margin-bottom:14px; font-size:13px; border-left:4px solid #dc3545; }
        @media(max-width:640px) {
            .hamburger { display:block; }
            .nav-bar { display:none; flex-direction:column; align-items:stretch; padding:8px 12px; gap:2px; }
            .nav-bar.open { display:flex; }
            .nav-bar a { padding:10px 12px; font-size:14px; border-bottom:1px solid #f0f0f0; }
            .nav-bar a:last-child { border-bottom:none; }
            .wrap { padding:10px; }
            .stats-grid { grid-template-columns:repeat(2,1fr); gap:8px; }
            .scard { padding:12px; }
            .scard-val { font-size:1.2rem; }
            .filter-row { flex-direction:column; align-items:stretch; }
            .filter-row select,.filter-row input,.btn-go,.btn-clr { width:100%; }
            td,th { padding:8px 10px!important; font-size:12px; }
        }
        .rank-chip{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700}
        .rc-REN{background:#ede8ff;color:#4a1ea8}.rc-AssocREN{background:#dbeeff;color:#0055b3}
        .rc-EliteREN{background:#fff4cc;color:#7a4f00}.rc-TL{background:#d6f5e3;color:#0a5c30}
        .rc-ATL{background:#fff0e0;color:#7a3300}
        .prog-bg{background:#e9ecef;border-radius:999px;height:5px;margin:3px 0}
        .prog-fg{height:5px;border-radius:999px;background:linear-gradient(90deg,#007bff,#6f42c1)}
        .earn-chip{padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600}
        .ep{background:#d4edda;color:#155724}.eo{background:#cce5ff;color:#004085}.ew{background:#fff3cd;color:#856404}
        .promo-row{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid #f0f0f0;font-size:13px}
        .promo-row:last-child{border-bottom:none}
        .promo-date{font-size:11px;color:#888;margin-left:auto;white-space:nowrap}
        .perf-bottom{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
        @media(max-width:640px){
            .perf-bottom{grid-template-columns:1fr}
            #agentTable th:nth-child(6),#agentTable td:nth-child(6),
            #agentTable th:nth-child(8),#agentTable td:nth-child(8){display:none}
        }
</style>
</head>
<body>
<div class="topbar">
    <div class="topbar-title">&#128200; Agent Performance</div>
    <div class="topbar-right"><a href="/admin/export-data?type=agents">&#128228; Export</a><button class="hamburger" onclick="toggleNav()">&#9776;</button></div>
</div>
<div class="nav-bar" id="mainNav">
  <a href="/admin/dashboard" class="nav-active">&#128202; Dashboard</a>
  <a href="/admin/projects">&#127962; Projects</a>
  <a href="/admin/create-project" class="nav-btn">&#10010; New Project</a>
  <a href="/admin/agents">&#128101; Agents</a>
  <a href="/admin/agent-hierarchy">&#128279; Hierarchy</a>
  <a href="/admin/payments">&#9993; Payments</a>
  <a href="/admin/commissions">&#128176; Commissions</a>
  <a href="/admin/unified-submissions">&#128203; Unified Submissions</a>
  <a href="/admin/agent-performance">&#128200; Performance</a>
  <a href="/admin/commission-calculator">&#9889; Calc</a>
  <a href="/admin/settings">&#9881; Settings</a>
  <a href="/admin/export-data">&#128228; Export</a>
</div>
<div class="wrap">
<div class="stats-grid">
  <div class="scard" style="border-top-color:#007bff"><h3>Total Agents</h3><div class="scard-val" style="color:#007bff">{{ total_agents }}</div></div>
  <div class="scard" style="border-top-color:#28a745"><h3>Commissions</h3><div class="scard-val" style="color:#28a745">RM{{ "{:,.0f}".format(total_commissions) }}</div></div>
  <div class="scard" style="border-top-color:#6f42c1"><h3>Total Sales</h3><div class="scard-val" style="color:#6f42c1">RM{{ "{:,.0f}".format(total_sales) }}</div></div>
  <div class="scard" style="border-top-color:#17a2b8"><h3>Avg Success</h3><div class="scard-val" style="color:#17a2b8">{{ avg_success }}%</div></div>
</div>
<div class="card" style="padding:12px 16px;margin-bottom:14px">
  <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
    <strong style="font-size:13px">&#127942; Ranks:</strong>
    {% set rs={"REN":"rc-REN","Assoc REN":"rc-AssocREN","Elite REN":"rc-EliteREN","TL":"rc-TL","ATL":"rc-ATL"} %}
    {% for rn in ["REN","Assoc REN","Elite REN","TL","ATL"] %}{% if rank_counts.get(rn,0)>0 %}
    <span class="rank-chip {{ rs.get(rn,'rc-REN') }}">{{ rn }} &bull; {{ rank_counts.get(rn,0) }}</span>
    {% endif %}{% endfor %}
    <span style="font-size:11px;color:#888;margin-left:auto">RM30k&#8594;Assoc|RM90k&#8594;Elite|RM210k&#8594;TL|RM450k&#8594;ATL</span>
  </div>
</div>
<div class="filter-wrap">
  <div class="filter-row">
    <input type="text" id="searchInput" placeholder="Search agent..." oninput="filterTable()"/>
    <select id="rankFilter" onchange="filterTable()"><option value="">All Ranks</option><option>REN</option><option>Assoc REN</option><option>Elite REN</option><option>TL</option><option>ATL</option></select>
    <select id="sortBy" onchange="sortTable()"><option value="comm">Commission</option><option value="sales">Sales</option><option value="approved">Deals</option><option value="name">Name</option></select>
    <span id="agentCount" style="font-size:12px;color:#666"></span>
  </div>
</div>
<div class="tbl-wrap"><table id="agentTable">
  <thead><tr><th>#</th><th>Agent</th><th>Rank &amp; Progress</th><th>Deals</th><th>Success</th><th>Earnings</th><th>Commission</th><th>Cum. Gross</th></tr></thead>
  <tbody id="agentTbody">
  {% for a in agents %}
  <tr data-name="{{ a.name|lower }}" data-email="{{ a.email|lower }}" data-rank="{{ a.rank }}"
      data-comm="{{ a.total_comm }}" data-sales="{{ a.total_sales }}" data-approved="{{ a.approved }}" data-cumgross="{{ a.cumgross }}">
    <td style="color:#888;font-weight:700">{{ loop.index }}</td>
    <td><strong>{{ a.name }}</strong><br><small style="color:#888">{{ a.email }}</small><br><small style="color:#555">&#8679; {{ a.upline }}</small></td>
    <td style="min-width:140px">
      {% set rs2={"REN":"rc-REN","Assoc REN":"rc-AssocREN","Elite REN":"rc-EliteREN","TL":"rc-TL","ATL":"rc-ATL"} %}
      <span class="rank-chip {{ rs2.get(a.rank,'rc-REN') }}">{{ a.rank }} &bull; {{ a.comm_rate|int }}%</span>
      <div class="prog-bg"><div class="prog-fg" style="width:{{ a.prog_pct }}%"></div></div>
      {% if a.next_rank %}<small style="color:#888">{{ a.prog_pct }}% &#8594; {{ a.next_rank }}</small>{% else %}<small style="color:#28a745;font-weight:700">&#127942; ATL</small>{% endif %}
    </td>
    <td><div>{{ a.total_l }}</div><div style="color:#28a745;font-size:11px">&#10003;{{ a.approved }}</div><div style="color:#dc3545;font-size:11px">&#10007;{{ a.rejected }}</div></td>
    <td>{% if a.total_l>0 %}<strong style="color:{% if a.success_rate>=80 %}#28a745{% elif a.success_rate>=50 %}#ffc107{% else %}#dc3545{% endif %}">{{ a.success_rate }}%</strong>{% else %}<span style="color:#aaa">—</span>{% endif %}</td>
    <td>{% if a.personal_e>0 %}<span class="earn-chip ep">P RM{{ "{:,.0f}".format(a.personal_e) }}</span> {% endif %}{% if a.override_e>0 %}<span class="earn-chip eo">O RM{{ "{:,.0f}".format(a.override_e) }}</span> {% endif %}{% if a.wtp1_e+a.wtp2_e>0 %}<span class="earn-chip ew">W RM{{ "{:,.0f}".format(a.wtp1_e+a.wtp2_e) }}</span>{% endif %}{% if a.personal_e==0 and a.override_e==0 and a.wtp1_e==0 %}<span style="color:#aaa;font-size:11px">—</span>{% endif %}</td>
    <td><strong style="color:#28a745">RM{{ "{:,.2f}".format(a.total_comm) }}</strong></td>
    <td><strong>RM{{ "{:,.0f}".format(a.cumgross) }}</strong></td>
  </tr>
  {% else %}<tr><td colspan="8" style="text-align:center;padding:30px;color:#888">No agents found.</td></tr>
  {% endfor %}
  </tbody>
</table></div>
<div class="perf-bottom">
  <div>
    <div class="sec-hdr"><h2>&#127942; Recent Promotions</h2></div>
    {% if recent_promotions %}<div class="card" style="padding:0">{% for p in recent_promotions %}
    <div class="promo-row">&#11014; <strong>{{ p[1] }}</strong> <span style="color:#28a745;font-weight:700">{{ p[2] }} &#8594; {{ p[3] }}</span>{% if p[4] %} <span style="font-size:11px;color:#666">RM{{ "{:,.0f}".format(p[4]) }}</span>{% endif %}<span class="promo-date">{{ p[0][:10] if p[0] else '' }}</span></div>
    {% endfor %}</div>{% else %}<div class="empty"><p style="color:#888">No promotions yet.</p></div>{% endif %}
  </div>
  <div>
    <div class="sec-hdr"><h2>&#128197; Monthly Activity</h2></div>
    {% if monthly_data %}<div class="tbl-wrap" style="margin-bottom:0"><table><thead><tr><th>Month</th><th>Agent</th><th>Rank</th><th>Deals</th><th>Commission</th></tr></thead><tbody>
    {% set rs3={"REN":"rc-REN","Assoc REN":"rc-AssocREN","Elite REN":"rc-EliteREN","TL":"rc-TL","ATL":"rc-ATL"} %}
    {% for m in monthly_data %}<tr><td>{{ m[0] }}</td><td>{{ m[1] }}</td><td><span class="rank-chip {{ rs3.get(m[2],'rc-REN') }}">{{ m[2] }}</span></td><td>{{ m[3] }}</td><td><strong style="color:#28a745">RM{{ "{:,.2f}".format(m[4] or 0) }}</strong></td></tr>{% endfor %}
    </tbody></table></div>{% else %}<div class="empty"><p style="color:#888">No data yet.</p></div>{% endif %}
  </div>
</div>
</div>
<script>
function filterTable(){const q=document.getElementById('searchInput').value.toLowerCase();const rank=document.getElementById('rankFilter').value;let v=0;document.querySelectorAll('#agentTbody tr').forEach(function(row){if(!row.dataset.name)return;const show=(!q||row.dataset.name.includes(q)||row.dataset.email.includes(q))&&(!rank||row.dataset.rank===rank);row.style.display=show?'':'none';if(show)v++;});let i=1;document.querySelectorAll('#agentTbody tr').forEach(function(row){if(row.style.display!=='none'&&row.cells[0])row.cells[0].textContent=i++;});document.getElementById('agentCount').textContent='Showing '+v+' agent'+(v!==1?'s':'');}
function sortTable(){const key=document.getElementById('sortBy').value;const tbody=document.getElementById('agentTbody');const rows=Array.from(tbody.querySelectorAll('tr')).filter(r=>r.dataset.name);rows.sort(function(a,b){if(key==='name')return a.dataset.name.localeCompare(b.dataset.name);return(parseFloat(b.dataset[key])||0)-(parseFloat(a.dataset[key])||0);});rows.forEach(r=>tbody.appendChild(r));filterTable();}
function toggleNav(){document.getElementById('mainNav').classList.toggle('open');}
document.addEventListener('DOMContentLoaded',function(){filterTable();document.querySelectorAll('#mainNav a').forEach(function(a){a.addEventListener('click',function(){document.getElementById('mainNav').classList.remove('open');});});});
</script>
</body>
</html>"""



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



# ============================================================
# WTP COMMISSION CALCULATOR — Python engine (no DB writes)
# ============================================================

WTP_RANKS = {
    "ATL":   {"label": "ATL",       "pct": 90.0},
    "TL":    {"label": "TL",        "pct": 85.0},
    "ELITE": {"label": "Elite REN", "pct": 80.0},
    "ASSOC": {"label": "Assoc REN", "pct": 75.0},
    "REN":   {"label": "REN",       "pct": 70.0},
}
WTP_RANK_KEYS = ["ATL", "TL", "ELITE", "ASSOC", "REN"]


def wtp_calculate(payload):
    try:
        sp          = float(payload.get("sales_price", 0))
        dev_rate    = float(payload.get("dev_rate", 2))
        sst_rate    = float(payload.get("sst_rate", 8))
        company_pct = float(payload.get("company_pct", 15))
        pic_pct     = float(payload.get("pic_pct", 10))
        agents_pct  = float(payload.get("agents_pct", 75))
        pic_name    = payload.get("pic_name", "PIC")
        pic_rank_k  = payload.get("pic_rank", "REN")
        company_name= payload.get("company_name", "Company")
        edmond_name = payload.get("edmond_name", "ATL")
        edmond_deal = bool(payload.get("edmond_deal", False))
        branches    = payload.get("branches", [])

        split_total = company_pct + pic_pct + agents_pct
        if abs(split_total - 100) > 0.001:
            return {"ok": False, "error":
                f"Company ({company_pct}%) + PIC ({pic_pct}%) + Agents ({agents_pct}%) = "
                f"{split_total:.2f}%. Must sum to 100%."}

        gross = sp * dev_rate / 100
        net   = gross / (1 + sst_rate / 100) if sst_rate > 0 else gross
        sst_amt = gross - net

        pic_rank_pct  = WTP_RANKS.get(pic_rank_k, WTP_RANKS["REN"])["pct"]
        pic_rank_lbl  = WTP_RANKS.get(pic_rank_k, WTP_RANKS["REN"])["label"]
        agent_pool_pd = net * agents_pct / 100
        company_pd    = net * company_pct / 100
        pic_pool_pd   = net * pic_pct / 100
        pic_earn_pd   = pic_pool_pd * pic_rank_pct / 100
        pic_rem_pd    = pic_pool_pd - pic_earn_pd

        total_deals = 1 if edmond_deal else 0
        for b in branches:
            for a in b.get("agents", []):
                if a.get("hasDeal"):
                    total_deals += 1

        total_gross    = total_deals * gross
        total_net      = total_deals * net
        total_company  = total_deals * company_pd
        total_pic_pool = total_deals * pic_pool_pd
        total_pic_earn = total_deals * pic_earn_pd
        total_pic_rem  = total_deals * pic_rem_pd

        lines = []
        all_payouts     = {}
        cobroke_payouts = {}
        branch_results  = []
        ep = {"personal": 0, "overrides": 0, "wtp1": 0, "wtp2": 0, "total": 0}

        def new_p(): return {"personal": 0, "overrides": 0, "wtp1": 0, "wtp2": 0, "total": 0}

        if edmond_deal:
            earn = agent_pool_pd * 0.90
            ep["personal"] = earn
            lines.append({"label": f"{edmond_name} (personal deal)",
                "formula": f"90% × RM {agent_pool_pd:,.2f}",
                "exp": "ATL 90% of agent pool",
                "amount": earn, "isWTP": False, "isPIC": False, "branch": "Edmond"})

        for bi, b in enumerate(branches):
            ags = b.get("agents", [])
            n = len(ags)
            btag = f"Branch {bi+1}"
            bp = {}
            for a in ags:
                k = (bi, a.get("name","") or f"a{bi}")
                bp[k] = new_p(); all_payouts[k] = bp[k]

            def akey(a): return (bi, a.get("name","") or f"a{bi}")

            for ai, a in enumerate(ags):
                if not a.get("hasDeal"): continue
                cbs = a.get("cobroke", [])
                cb_tot = sum(float(c.get("pct",0)) for c in cbs)
                if cb_tot > 100:
                    return {"ok": False, "error": f"{a.get('name','Agent')} co-broke total > 100%"}
                rank_pct = WTP_RANKS.get(a.get("rank","REN"), WTP_RANKS["REN"])["pct"]
                rank_lbl = WTP_RANKS.get(a.get("rank","REN"), WTP_RANKS["REN"])["label"]
                sa   = agent_pool_pd * (1 - cb_tot/100)
                earn = sa * rank_pct / 100
                bp[akey(a)]["personal"] = earn
                sfx  = f" (keeps {100-cb_tot:.0f}% of pool)" if cb_tot > 0 else ""
                lines.append({"label": f"{a.get('name') or rank_lbl} (personal deal{sfx})",
                    "formula": f"{rank_pct}% × RM {sa:,.2f}", "exp": f"Stream A: {rank_lbl} {rank_pct}%",
                    "amount": earn, "isWTP": False, "isPIC": False, "branch": btag})

                for cp in cbs:
                    cp_pct  = float(cp.get("pct",0))
                    if cp_pct <= 0: continue
                    cp_name = cp.get("name") or f"Co-broke {ai+1}"
                    sb      = agent_pool_pd * cp_pct / 100
                    is_wtp  = bool(cp.get("isWTP"))
                    chain   = cp.get("wtpChain",[])
                    if is_wtp and chain:
                        cb_earn = sb * rank_pct / 100
                        lines.append({"label": f"{cp_name} co-broke ({cp_pct:.0f}%)",
                            "formula": f"{rank_pct}% × RM {sb:,.2f}", "exp": f"WTP agent Stream B",
                            "amount": cb_earn, "isWTP": False, "isPIC": False,
                            "isCobroke": True, "isCobrokeWTP": True, "branch": btag})
                        if cp_name not in cobroke_payouts:
                            cobroke_payouts[cp_name] = {"earn":0,"overrides":0,"wtp1":0,"isWTP":True}
                        cobroke_payouts[cp_name]["earn"] += cb_earn
                        prev = rank_pct
                        for ul in reversed(chain):
                            up = WTP_RANKS.get(ul.get("rank","TL"), WTP_RANKS["TL"])["pct"]
                            un = ul.get("name") or WTP_RANKS.get(ul.get("rank","TL"))["label"]
                            gap = up - prev
                            if gap > 0:
                                ue = sb * gap / 100
                                lines.append({"label": f"{un} override on {cp_name}",
                                    "formula": f"({up}%−{prev}%) × RM {sb:,.2f}",
                                    "exp": f"{gap}% gap on Stream B",
                                    "amount": ue, "isWTP": False, "isPIC": False,
                                    "isCobroke": True, "isCobrokeWTP": True, "branch": btag})
                                if un not in cobroke_payouts:
                                    cobroke_payouts[un] = {"earn":0,"overrides":0,"wtp1":0,"isWTP":True}
                                cobroke_payouts[un]["overrides"] += ue
                            prev = up
                    else:
                        lines.append({"label": f"{cp_name} co-broke ({cp_pct:.0f}% ext.)",
                            "formula": f"RM {sb:,.2f} flat", "exp": "External agent",
                            "amount": sb, "isWTP": False, "isPIC": False,
                            "isCobroke": True, "isCobrokeWTP": False, "branch": btag})
                        if cp_name not in cobroke_payouts:
                            cobroke_payouts[cp_name] = {"earn":0,"overrides":0,"wtp1":0,"isWTP":False}
                        cobroke_payouts[cp_name]["earn"] += sb

            tg = [0.0]*n
            for ai in range(n-1,-1,-1):
                a = ags[ai]
                if a.get("hasDeal"):
                    cb_t = sum(float(c.get("pct",0)) for c in a.get("cobroke",[]))
                    sa   = agent_pool_pd*(1-cb_t/100)
                else:
                    sa   = 0.0
                tg[ai] = sa + (tg[ai+1] if ai < n-1 else 0)

            for ai in range(n-1):
                up   = ags[ai]; dn = ags[ai+1]
                up_p = WTP_RANKS.get(up.get("rank","REN"),WTP_RANKS["REN"])["pct"]
                dn_p = WTP_RANKS.get(dn.get("rank","REN"),WTP_RANKS["REN"])["pct"]
                gap  = up_p - dn_p; t = tg[ai+1]
                if t == 0: continue
                uname = up.get("name") or WTP_RANKS.get(up.get("rank","REN"))["label"]
                dname = dn.get("name") or WTP_RANKS.get(dn.get("rank","REN"))["label"]
                if gap > 0:
                    earn = t*gap/100; bp[akey(up)]["overrides"] += earn
                    lines.append({"label": f"{uname} override on {dname}",
                        "formula": f"({up_p}%−{dn_p}%) × RM {t:,.2f}",
                        "exp": f"{gap}% gap on pool RM {t:,.2f}",
                        "amount": earn, "isWTP": False, "isPIC": False, "branch": btag})
                else:
                    w1 = t*0.02; bp[akey(up)]["wtp1"] += w1
                    lines.append({"label": f"{uname} WTP Gen1 on {dname}",
                        "formula": f"2% × RM {t:,.2f}", "exp": "Same rank — WTP Gen1",
                        "amount": w1, "isWTP": True, "isPIC": False, "branch": btag})
                    if ai > 0:
                        g2 = ags[ai-1]; g2n = g2.get("name") or WTP_RANKS.get(g2.get("rank","REN"))["label"]
                        w2 = t*0.01; bp[akey(g2)]["wtp2"] += w2
                        lines.append({"label": f"{g2n} WTP Gen2",
                            "formula": f"1% × RM {t:,.2f}", "exp": "Indirect upline 1% WTP",
                            "amount": w2, "isWTP": True, "isPIC": False, "branch": btag})

            if ags:
                fa = ags[0]; fp = WTP_RANKS.get(fa.get("rank","REN"),WTP_RANKS["REN"])["pct"]
                bp0 = tg[0]
                if bp0 > 0:
                    gap = 90 - fp
                    fname = fa.get("name") or WTP_RANKS.get(fa.get("rank","REN"))["label"]
                    if gap > 0:
                        ee = bp0*gap/100; ep["overrides"] += ee
                        lines.append({"label": f"{edmond_name} override on {fname} ({btag})",
                            "formula": f"(90%−{fp}%) × RM {bp0:,.2f}",
                            "exp": f"{gap}% gap on {btag} pool",
                            "amount": ee, "isWTP": False, "isPIC": False, "branch": btag})
                        edmond_earn = ee
                    else:
                        w1 = bp0*0.02; ep["wtp1"] += w1
                        lines.append({"label": f"{edmond_name} WTP Gen1 on {fname} ({btag})",
                            "formula": f"2% × RM {bp0:,.2f}", "exp": "Same rank — WTP Gen1",
                            "amount": w1, "isWTP": True, "isPIC": False, "branch": btag})
                        edmond_earn = w1
                    deals_b = sum(1 for a in ags if a.get("hasDeal"))
                    branch_results.append({"id":bi+1,"name":btag,"agents":n,"deals":deals_b,"gross":bp0,"edmondEarn":edmond_earn})
                else:
                    branch_results.append({"id":bi+1,"name":btag,"agents":n,"deals":0,"gross":0,"edmondEarn":0})

        lines.append({"label": f"{company_name} (Company {company_pct}% of net)",
            "formula": f"{company_pct}% × RM {total_net:,.2f} ({total_deals} deal(s))",
            "exp": f"Company earns {company_pct}% of total net",
            "amount": total_company, "isWTP": False, "isPIC": False, "isCompany": True, "branch": "Company"})
        lines.append({"label": f"{pic_name} pool ({pic_pct}% of net)",
            "formula": f"{pic_pct}% × RM {total_net:,.2f} ({total_deals} deal(s))",
            "exp": f"PIC pool. Rank: {pic_rank_lbl} {pic_rank_pct}%. Rem RM {total_pic_rem:,.2f} → co.",
            "amount": total_pic_pool, "isWTP": False, "isPIC": True, "isPICpool": True, "branch": "PIC"})
        lines.append({"label": f"{pic_name} ({pic_rank_lbl} {pic_rank_pct}% of pool)",
            "formula": f"{pic_rank_pct}% × RM {total_pic_pool:,.2f}",
            "exp": f"{pic_name} earns {pic_rank_lbl} ({pic_rank_pct}%) of pool",
            "amount": total_pic_earn, "isWTP": False, "isPIC": True, "branch": "PIC"})

        ep["total"] = ep["personal"]+ep["overrides"]+ep["wtp1"]+ep["wtp2"]
        for k,p in all_payouts.items():
            p["total"] = p["personal"]+p["overrides"]+p["wtp1"]+p["wtp2"]

        return {
            "ok": True,
            "lines": lines,
            "branch_results": branch_results,
            "edmond_payout": ep,
            "all_payouts": {str(k): v for k,v in all_payouts.items()},
            "cobroke_payouts": cobroke_payouts,
            "summary": {
                "total_deals": total_deals,
                "gross_per_deal": round(gross,2), "sst_per_deal": round(sst_amt,2),
                "net_per_deal": round(net,2), "total_gross": round(total_gross,2),
                "total_net": round(total_net,2), "total_company": round(total_company,2),
                "total_pic_pool": round(total_pic_pool,2), "total_pic_earn": round(total_pic_earn,2),
                "total_pic_rem": round(total_pic_rem,2), "agent_pool_per_deal": round(agent_pool_pd,2),
                "pic_rank_lbl": pic_rank_lbl, "pic_rank_pct": pic_rank_pct,
            }
        }
    except Exception as e:
        import traceback
        return {"ok": False, "error": str(e), "tb": traceback.format_exc()}


@app.route("/admin/commission-calculator/calculate", methods=["POST"])
def wtp_commission_calculate():
    if "user_id" not in session or session.get("user_role") != "admin":
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    try:
        result = wtp_calculate(request.get_json(force=True))
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/commission-calculator")
def wtp_commission_calculator():
    if "user_id" not in session or session.get("user_role") != "admin":
        return redirect("/login")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""SELECT id,name,agent_rank,commission_rate FROM users
                      WHERE role='agent'
                      ORDER BY name""")
    agents = [{"id":r[0],"name":r[1],"rank":r[2] or "REN","rate":float(r[3] or 70)}
              for r in cursor.fetchall()]
    cursor.execute("""SELECT id,project_name,
                      COALESCE(comm_dev_rate,2),COALESCE(comm_sst_rate,8),
                      COALESCE(comm_company_pct,15),COALESCE(comm_pic_pct,10),
                      COALESCE(comm_agents_pct,75),COALESCE(comm_pic_rank,'REN')
                      FROM projects WHERE status='active' ORDER BY project_name""")
    projects = [{"id":r[0],"name":r[1],"comm_dev_rate":float(r[2]),"comm_sst_rate":float(r[3]),
                 "comm_company_pct":float(r[4]),"comm_pic_pct":float(r[5]),
                 "comm_agents_pct":float(r[6]),"comm_pic_rank":r[7]}
                for r in cursor.fetchall()]
    conn.close()
    return render_template_string(
        WTP_CALCULATOR_TEMPLATE,
        agents=agents, projects=projects,
        agents_json=json.dumps(agents), projects_json=json.dumps(projects),
        admin_name=session.get("user_name","Admin"),
    )


@app.route("/admin/project/<int:project_id>/save-comm-config", methods=["POST"])
def save_project_comm_config(project_id):
    if "user_id" not in session or session.get("user_role") != "admin":
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    data = request.get_json(force=True)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""UPDATE projects SET
            comm_dev_rate=?,comm_sst_rate=?,comm_company_pct=?,
            comm_pic_pct=?,comm_agents_pct=?,comm_pic_rank=?
            WHERE id=?""",
            (float(data.get("comm_dev_rate",2)),float(data.get("comm_sst_rate",8)),
             float(data.get("comm_company_pct",15)),float(data.get("comm_pic_pct",10)),
             float(data.get("comm_agents_pct",75)),data.get("comm_pic_rank","REN"),
             project_id))
        conn.commit(); conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



WTP_CALCULATOR_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>WTP Commission Calculator</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:Arial,sans-serif;margin:0;background:#f0f2f5;color:#1a2a3a}
.topbar{background:#2c3e50;color:white;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:8px;position:sticky;top:0;z-index:100}
.topbar-title{font-size:1rem;font-weight:700}
.topbar-right{display:flex;align-items:center;gap:10px}
.topbar-right a{color:#a8c8ff;text-decoration:none;font-size:13px}
.hamburger{display:none;background:none;border:none;color:white;font-size:22px;cursor:pointer;padding:2px 6px}
.nav-bar{background:white;padding:10px 16px;display:flex;flex-wrap:wrap;gap:4px;align-items:center;box-shadow:0 2px 6px rgba(0,0,0,.08)}
.nav-bar a{color:#007bff;text-decoration:none;font-weight:600;font-size:13px;padding:5px 10px;border-radius:6px;white-space:nowrap;transition:background .15s}
.nav-bar a:hover{background:#f0f7ff}
.nav-bar a.nav-btn{background:#2563eb;color:white}
.nav-bar a.nav-active{background:#eff6ff;color:#1d4ed8}
@media(max-width:640px){.hamburger{display:block}.nav-bar{display:none;flex-direction:column;align-items:stretch;padding:8px 12px;gap:2px}.nav-bar.open{display:flex}.nav-bar a{padding:10px 12px;font-size:14px;border-bottom:1px solid #f0f0f0}}
.wrap{max-width:1100px;margin:0 auto;padding:16px}

/* cards */
.card{background:white;border-radius:10px;padding:20px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:14px}
.card-title{font-size:13px;font-weight:700;color:#2c3e50;margin-bottom:16px;padding-bottom:8px;border-bottom:2px solid #f0f0f0;display:flex;align-items:center;gap:8px}
.step-num{background:#2563eb;color:white;border-radius:50%;width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0}

/* formula banner */
.formula-banner{background:#eff6ff;border-left:4px solid #2563eb;padding:10px 14px;border-radius:0 8px 8px 0;margin-bottom:14px;font-size:12px;color:#1e40af;font-weight:600}

/* rank chips */
.rank-bar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:14px;background:white;padding:12px 16px;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.rank-bar span{font-size:11px;font-weight:700}
.rchip{padding:4px 12px;border-radius:999px;font-size:12px;font-weight:700;border:2px solid transparent}
.rc-atl{background:#fff0e0;color:#7a3300}.rc-tl{background:#d6f5e3;color:#0a5c30}
.rc-er{background:#fff4cc;color:#7a4f00}.rc-as{background:#dbeeff;color:#0055b3}
.rc-ren{background:#ede8ff;color:#4a1ea8}.rc-arr{color:#ccc;font-size:14px}

/* form layout */
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-group{margin-bottom:0}
.form-group label{display:block;font-size:11px;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.04em;margin-bottom:5px}
.form-group input,.form-group select{width:100%;padding:9px 11px;border:1.5px solid #e5e7eb;border-radius:7px;font-size:14px;background:#fafafa;transition:border .15s}
.form-group input:focus,.form-group select:focus{outline:none;border-color:#2563eb;background:white}
.form-group input[readonly]{background:#f5f5f5;color:#666;cursor:default}
.ipw{position:relative}
.ipw .ipl{position:absolute;left:10px;top:50%;transform:translateY(-50%);font-size:12px;font-weight:700;color:#888;pointer-events:none}
.ipw input{padding-left:28px}
@media(max-width:640px){.grid3{grid-template-columns:1fr 1fr}.grid2{grid-template-columns:1fr}}

/* rate-check bar */
.rate-check{background:#f8f9fa;border-radius:7px;padding:9px 14px;font-size:12px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:12px}
.rate-check strong{font-weight:700}
.ok{color:#16a34a}.bad{color:#dc3545}
.small-note{font-size:11px;color:#666;margin-top:3px}

/* project picker */
.proj-card{background:white;border-radius:10px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.proj-card label{font-size:12px;font-weight:700;color:#444;white-space:nowrap}
.proj-card select{flex:1;min-width:220px;padding:8px 11px;border:1.5px solid #e5e7eb;border-radius:7px;font-size:13px;background:#fafafa}
.proj-hint{font-size:11px;color:#888;flex-basis:100%}

/* ATL section */
.atl-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:#fff8f0;border:1.5px solid #fcd34d;border-radius:8px;padding:12px 14px;margin-bottom:12px}
.atl-badge{background:#7a3300;color:#fff0e0;padding:4px 10px;border-radius:6px;font-size:11px;font-weight:700;white-space:nowrap}
.tog{position:relative;display:inline-block;width:38px;height:20px}
.tog input{opacity:0;width:0;height:0}
.tog-sl{position:absolute;cursor:pointer;inset:0;background:#ccc;border-radius:20px;transition:.3s}
.tog input:checked+.tog-sl{background:#16a34a}
.tog-sl:before{content:'';position:absolute;width:14px;height:14px;left:3px;bottom:3px;background:white;border-radius:50%;transition:.3s}
.tog input:checked+.tog-sl:before{left:21px}
.tog-lbl{font-size:12px;font-weight:700;color:#666}
.tw{display:flex;align-items:center;gap:6px}

/* branches */
.branches-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:4px}
.branches-row{display:flex;gap:10px;min-width:max-content;padding:2px}
.branch-col{min-width:240px;background:#f8f9fa;border-radius:8px;padding:12px;border:1.5px solid #e5e7eb}
.branch-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.branch-title{font-size:11px;font-weight:700;text-transform:uppercase;color:#888;letter-spacing:.06em}
.btn-del-branch{background:none;border:none;color:#dc3545;cursor:pointer;font-size:16px;padding:0 4px}
.btn-add-branch{background:#eff6ff;border:1.5px dashed #2563eb;color:#2563eb;border-radius:8px;padding:10px 14px;font-size:13px;font-weight:700;cursor:pointer;white-space:nowrap;min-width:200px;text-align:center}
.btn-add-branch:hover{background:#dbeafe}
.agent-row{display:flex;align-items:center;gap:8px;padding:8px;background:white;border-radius:6px;margin-bottom:6px;border:1.5px solid #e5e7eb;cursor:pointer}
.agent-row.selected{border-color:#2563eb;background:#eff6ff}
.agent-row:hover{border-color:#93c5fd}
.agent-rank-sel{padding:4px 6px;border:1px solid #ddd;border-radius:5px;font-size:12px;font-weight:700;background:#f8f9fa}
.agent-name-inp{flex:1;padding:5px 7px;border:1px solid #ddd;border-radius:5px;font-size:12px}
.agent-deal-chk{width:15px;height:15px;cursor:pointer}
.agent-pct{font-family:monospace;font-size:13px;font-weight:800;color:#2563eb;margin-left:auto;white-space:nowrap}
.btn-del-agent{background:none;border:none;color:#dc3545;cursor:pointer;font-size:14px;padding:0 2px}
.btn-add-agent{width:100%;background:none;border:1.5px dashed #ccc;color:#888;border-radius:5px;padding:6px;font-size:12px;cursor:pointer;margin-top:4px}
.btn-add-agent:hover{border-color:#2563eb;color:#2563eb}

/* cobroke */
.cobroke-tag{display:inline-block;background:#f0fdf4;border:1px solid #86efac;color:#166534;border-radius:4px;font-size:9px;padding:0 4px;font-weight:700;margin-left:4px}
.cobroke-section{border-top:1px solid #e5e7eb;margin-top:6px;padding-top:6px}
.cobroke-row{display:flex;align-items:center;gap:6px;margin-bottom:4px}
.cobroke-row input{padding:4px 6px;border:1px solid #ddd;border-radius:4px;font-size:11px}
.cobroke-row input[type=number]{width:60px}
.cobroke-wtpchk{width:13px;height:13px}
.btn-cobroke{background:none;border:1.5px dashed #86efac;color:#166534;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer;margin-top:4px;width:100%}

/* calculate button */
.btn-calc{background:#2563eb;color:white;border:none;border-radius:8px;padding:13px 28px;font-size:15px;font-weight:700;cursor:pointer;width:100%;margin-top:8px;transition:background .15s}
.btn-calc:hover{background:#1d4ed8}

/* alert */
.flash-err{background:#f8d7da;color:#721c24;padding:10px 14px;border-radius:7px;margin-bottom:12px;border-left:4px solid #dc3545;font-size:13px;display:none}

/* results */
.result-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-bottom:14px}
.rc{background:white;border-radius:10px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.08);border-top:4px solid #ddd}
.rc-atl-b{border-top-color:#f97316}.rc-tl-b{border-top-color:#22c55e}.rc-er-b{border-top-color:#eab308}
.rc-as-b{border-top-color:#3b82f6}.rc-ren-b{border-top-color:#a855f7}.rc-co-b{border-top-color:#10b981}
.rc-comp-b{border-top-color:#8b5cf6}.rc-pic-b{border-top-color:#f43f5e}
.rc-name{font-weight:800;font-size:14px;margin-bottom:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-role{font-size:10px;color:#888;margin-bottom:8px;font-weight:600}
.rc-line{display:flex;justify-content:space-between;font-size:12px;padding:3px 0;border-bottom:1px solid #f5f5f5}
.rc-lbl{color:#666}.rc-val{font-weight:700}.rc-val.wtp{color:#0d9488}.rc-val.pic{color:#e11d48}.rc-val.company{color:#6d28d9}
.rc-total{display:flex;justify-content:space-between;align-items:center;margin-top:8px;padding-top:8px;border-top:2px solid #f0f0f0;background:#f8f9fa;border-radius:6px;padding:8px 10px}
.rc-total-lbl{font-size:10px;font-weight:700;color:#888;text-transform:uppercase}
.rc-total-val{font-size:16px;font-weight:900;color:#1a2a3a}

/* breakdown table */
.bk{background:white;border-radius:10px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:14px}
.bk-head{display:grid;grid-template-columns:2fr 1.5fr 2fr 1fr;gap:6px;padding:9px 14px;background:#2c3e50;color:white;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.bk-row{display:grid;grid-template-columns:2fr 1.5fr 2fr 1fr;gap:6px;padding:8px 14px;font-size:12px;border-bottom:1px solid #f5f5f5;align-items:center}
.bk-row:hover{background:#fafbfc}
.bk-sec{display:grid;grid-template-columns:2fr 1.5fr 2fr 1fr;gap:6px;padding:7px 14px;background:#eff6ff;font-weight:700;font-size:12px;border-top:2px solid #2563eb;color:#1e40af}
.bk-amt{text-align:right;font-weight:700}
.bk-amt.wtp{color:#0d9488}.bk-amt.pic{color:#e11d48}.bk-amt.company{color:#6d28d9}
.is-wtp{background:#f0fdfa}
@media(max-width:640px){.bk-head,.bk-row,.bk-sec{grid-template-columns:1fr 1fr;}.bk-head div:nth-child(3),.bk-row div:nth-child(3),.bk-sec div:nth-child(3){display:none}}

/* summary box */
.sum-box{background:white;border-radius:10px;padding:16px 20px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:14px}
.sum-items{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;margin-bottom:14px}
.sum-item{background:#f8f9fa;border-radius:7px;padding:10px 12px}
.sum-lbl{display:block;font-size:10px;font-weight:700;color:#888;text-transform:uppercase;margin-bottom:3px}
.sum-val{font-size:15px;font-weight:800;color:#1a2a3a}
.sum-grand{display:flex;justify-content:space-between;align-items:center;background:#1a3a2a;border-radius:8px;padding:14px 18px}
.sg-lbl{font-size:12px;font-weight:700;color:#86efac;text-transform:uppercase}
.sg-val{font-size:22px;font-weight:900;color:white}
.sg-note{font-size:11px;color:#86efac;margin-top:2px}

.info-note{background:#f8f9fa;border-radius:8px;padding:10px 14px;font-size:11px;color:#666;margin-bottom:14px;line-height:1.6}
.info-note strong{color:#1a2a3a}
</style>
</head>
<body>
<div class="topbar">
  <div class="topbar-title">⚡ WTP Commission Calculator</div>
  <div class="topbar-right">
    <a href="/logout">🔒 Logout</a>
    <button class="hamburger" onclick="toggleNav()">☰</button>
  </div>
</div>
<div class="nav-bar" id="mainNav">
  <a href="/admin/dashboard">&#128202; Dashboard</a>
  <a href="/admin/projects">&#127962; Projects</a>
  <a href="/admin/create-project" class="nav-btn">&#10010; New Project</a>
  <a href="/admin/agents">&#128101; Agents</a>
  <a href="/admin/agent-hierarchy">&#128279; Hierarchy</a>
  <a href="/admin/payments">&#9993; Payments</a>
  <a href="/admin/commissions">&#128176; Commissions</a>
  <a href="/admin/unified-submissions">&#128203; Unified Submissions</a>
  <a href="/admin/agent-performance">&#128200; Performance</a>
  <a href="/admin/commission-calculator" class="nav-active">&#9889; Calc</a>
  <a href="/admin/settings">&#9881; Settings</a>
  <a href="/admin/export-data">&#128228; Export</a>
</div>

<div class="wrap">

<div class="formula-banner">
  ⚡ <strong>WTP Formula:</strong> Sale Price × Dev Rate → Gross ÷ (1 + SST%) → Net → Company + PIC + Agents splits → Rank override chain
</div>

<!-- Rank reference -->
<div class="rank-bar">
  <span style="font-size:10px;font-weight:700;color:#888;text-transform:uppercase;letter-spacing:.06em">Ranks:</span>
  <span class="rchip rc-ren">🟣 REN · 70%</span>
  <span class="rc-arr">→</span>
  <span class="rchip rc-as">🔵 Assoc · 75%</span>
  <span class="rc-arr">→</span>
  <span class="rchip rc-er">⭐ Elite · 80%</span>
  <span class="rc-arr">→</span>
  <span class="rchip rc-tl">🏠 TL · 85%</span>
  <span class="rc-arr">→</span>
  <span class="rchip rc-atl">👑 ATL · 90%</span>
</div>

<!-- Project selector -->
<div class="proj-card">
  <label>📋 Load from Project:</label>
  <select id="projectPicker" onchange="loadProjectConfig()">
    <option value="">— Select project to auto-fill rates —</option>
    {% for p in projects %}
    <option value="{{ p.id }}"
      data-dev="{{ p.comm_dev_rate }}" data-sst="{{ p.comm_sst_rate }}"
      data-co="{{ p.comm_company_pct }}" data-pic="{{ p.comm_pic_pct }}"
      data-ag="{{ p.comm_agents_pct }}" data-prank="{{ p.comm_pic_rank }}"
      data-name="{{ p.name }}">{{ p.name }}</option>
    {% endfor %}
  </select>
  <div class="proj-hint">ℹ Selecting a project auto-fills all commission rates below.</div>
</div>

<div id="alertBox" class="flash-err"></div>

<!-- STEP 1: Deal & Rates -->
<div class="card">
  <div class="card-title"><span class="step-num">1</span> Deal & Commission Rates</div>
  <div class="grid3" style="gap:14px;margin-bottom:14px">
    <div class="form-group">
      <label>Project Name</label>
      <input type="text" id="projName" value="Project-A"/>
    </div>
    <div class="form-group">
      <label>Sale Price (RM)</label>
      <div class="ipw"><span class="ipl">RM</span>
        <input type="number" id="salesPrice" value="1000000" min="0" oninput="updateGross()"/>
      </div>
    </div>
    <div class="form-group">
      <label>Developer Comm Rate (%)</label>
      <div class="ipw"><span class="ipl">%</span>
        <input type="number" id="devCommRate" value="2" min="0" max="100" step="0.1" oninput="updateGross()"/>
      </div>
    </div>
  </div>
  <div class="grid3" style="gap:14px;margin-bottom:14px">
    <div class="form-group">
      <label>Gross Commission (auto)</label>
      <div class="ipw"><span class="ipl">RM</span>
        <input type="number" id="grossComm" readonly value="20000"/>
      </div>
    </div>
    <div class="form-group">
      <label>SST Rate (%) — 0 if dev absorbs</label>
      <div class="ipw"><span class="ipl">%</span>
        <input type="number" id="sstRate" value="8" min="0" max="100" step="0.1" oninput="updateNet()"/>
      </div>
    </div>
    <div class="form-group">
      <label>Net After SST (auto)</label>
      <div class="ipw"><span class="ipl">RM</span>
        <input type="number" id="netComm" readonly/>
      </div>
      <div id="sstNote" class="small-note"></div>
    </div>
  </div>
  <div class="grid3" style="gap:14px">
    <div class="form-group">
      <label>Company Split %</label>
      <div class="ipw"><span class="ipl">%</span>
        <input type="number" id="companyRate" value="15" min="0" max="100" step="0.1" oninput="updateRateCheck()"/>
      </div>
    </div>
    <div class="form-group">
      <label>PIC Split %</label>
      <div class="ipw"><span class="ipl">%</span>
        <input type="number" id="picRate" value="10" min="0" max="100" step="0.1" oninput="updateRateCheck()"/>
      </div>
    </div>
    <div class="form-group">
      <label>Agents Split %</label>
      <div class="ipw"><span class="ipl">%</span>
        <input type="number" id="agentsRate" value="75" min="0" max="100" step="0.1" oninput="updateRateCheck()"/>
      </div>
    </div>
  </div>
  <div class="rate-check">
    Gross: <strong id="rcGross">RM 20,000</strong>
    &nbsp;SST: <strong id="rcSST">None</strong>
    &nbsp;Net: <strong id="rcNet">RM 0</strong>
    &nbsp;|&nbsp;
    Co: <strong id="rcCo">15%</strong>
    PIC: <strong id="rcPIC">10%</strong>
    Agents: <strong id="rcAg">75%</strong>
    Total: <strong id="rcTot" class="ok">100%</strong>
    <strong id="rcSt" class="ok">✓ OK</strong>
  </div>
</div>

<!-- STEP 2: Company & PIC -->
<div class="card">
  <div class="card-title"><span class="step-num">2</span> Company & PIC Details</div>
  <div class="grid3" style="gap:14px;margin-bottom:14px">
    <div class="form-group">
      <label>Company Name</label>
      <input type="text" id="companyName" value="Worldtree Properties"/>
    </div>
    <div class="form-group">
      <label>Company Commission (auto)</label>
      <div class="ipw"><span class="ipl">RM</span>
        <input type="number" id="companyComm" readonly/>
      </div>
    </div>
    <div class="form-group">
      <label>Project (ref)</label>
      <input type="text" id="picProject" readonly style="color:#888"/>
    </div>
  </div>
  <div class="grid3" style="gap:14px">
    <div class="form-group">
      <label>PIC Name</label>
      <input type="text" id="picName" value="Erwin"/>
    </div>
    <div class="form-group">
      <label>PIC Rank</label>
      <select id="picRank" onchange="updatePicPreview()">
        <option value="ATL">ATL (90%)</option>
        <option value="TL">TL (85%)</option>
        <option value="ELITE">Elite REN (80%)</option>
        <option value="ASSOC">Assoc REN (75%)</option>
        <option value="REN" selected>REN (70%)</option>
      </select>
    </div>
    <div class="form-group">
      <label>PIC Earns (auto)</label>
      <div class="ipw"><span class="ipl">RM</span>
        <input type="number" id="picComm" readonly/>
      </div>
      <div id="picNote" class="small-note"></div>
    </div>
  </div>
</div>

<!-- STEP 3: Team Hierarchy -->
<div class="card">
  <div class="card-title"><span class="step-num">3</span> Team Hierarchy</div>

  <!-- ATL root -->
  <div class="atl-row">
    <span class="atl-badge">👑 ATL · 90%</span>
    <div style="flex:1;min-width:140px">
      <div class="form-group">
        <label>Top Leader (ATL) Name</label>
        <input type="text" id="edmondName" value="Edmond" placeholder="ATL name"/>
      </div>
    </div>
    <div>
      <div class="form-group">
        <label>Personal Deal</label>
        <div class="tw">
          <label class="tog"><input type="checkbox" id="edmondDeal" onchange="renderBranches()"/><span class="tog-sl"></span></label>
          <span class="tog-lbl" id="edmondDealLbl">OFF</span>
        </div>
      </div>
    </div>
  </div>

  <div id="alertInner" class="flash-err"></div>

  <div class="branches-scroll">
    <div class="branches-row" id="branchesRow"></div>
  </div>

  <button class="btn-calc" onclick="runCalc()">⚡ Calculate Commission Split</button>
</div>

<!-- RESULTS (hidden until calculated) -->
<div id="results" style="display:none">
  <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#7a8fa0;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #ddd">📊 Results</div>
  <div class="result-grid" id="resultCards"></div>

  <div class="bk">
    <div style="padding:12px 16px;font-weight:800;font-size:14px;color:#1a2a3a;border-bottom:1px solid #eee">Full Calculation Breakdown</div>
    <div class="bk-head">
      <div>Agent</div><div>Formula</div><div>Explanation</div><div style="text-align:right">Amount</div>
    </div>
    <div id="bkRows"></div>
  </div>

  <div class="sum-box">
    <div class="sum-items" id="sumItems"></div>
    <div class="sum-grand">
      <div>
        <div class="sg-lbl">Total Agent Payout</div>
        <div class="sg-note" id="sgNote"></div>
      </div>
      <div class="sg-val" id="sgVal">RM 0</div>
    </div>
  </div>

  <div class="info-note">
    ⚡ <strong>WTP Rule:</strong> Same rank as downline = 0 override. Earn 2% WTP Gen1 on their pool; indirect upline earns 1% WTP Gen2.
    &nbsp;|&nbsp; <strong>SST:</strong> Reverse method — Net = Gross ÷ (1 + SST%).
    &nbsp;|&nbsp; <strong>Co-broke WTP</strong> chains flow independently from main hierarchy.
  </div>
</div>

</div><!-- /wrap -->

<script>
var RANKS = {
    ATL:  {label:'ATL',      pct:90, pill:'rp-atl'},
    TL:   {label:'TL',       pct:85, pill:'rp-tl'},
    ELITE:{label:'Elite REN',pct:80, pill:'rp-er'},
    ASSOC:{label:'Assoc REN',pct:75, pill:'rp-as'},
    REN:  {label:'REN',      pct:70, pill:'rp-ren'}
};
var RANK_KEYS = ['ATL','TL','ELITE','ASSOC','REN'];
var PROJECTS  = {{ projects_json|safe }};
var branchCount=0, agentCount=0, branches=[];

function fmtRM(n){ return 'RM '+parseFloat(n||0).toLocaleString('en-MY',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

window.onload = function(){
    updateGross(); updateRateCheck();
    document.getElementById('picProject').value = document.getElementById('projName').value;
    document.getElementById('projName').oninput = function(){ document.getElementById('picProject').value = this.value; };
    addBranchData([{rank:'TL',name:'Sarah',hasDeal:true}]);
    addBranchData([{rank:'TL',name:'Ahmad',hasDeal:true},{rank:'REN',name:'Barry',hasDeal:false}]);
};

function loadProjectConfig(){
    var sel = document.getElementById('projectPicker');
    var opt = sel.options[sel.selectedIndex];
    if(!opt || !opt.value) return;
    document.getElementById('projName').value    = opt.dataset.name  || '';
    document.getElementById('picProject').value  = opt.dataset.name  || '';
    document.getElementById('devCommRate').value = opt.dataset.dev   || 2;
    document.getElementById('sstRate').value     = opt.dataset.sst   || 8;
    document.getElementById('companyRate').value = opt.dataset.co    || 15;
    document.getElementById('picRate').value     = opt.dataset.pic   || 10;
    document.getElementById('agentsRate').value  = opt.dataset.ag    || 75;
    document.getElementById('picRank').value     = opt.dataset.prank || 'REN';
    updateGross(); updateRateCheck(); updatePicPreview();
}

function updateGross(){
    var sp = parseFloat(document.getElementById('salesPrice').value)||0;
    var dr = parseFloat(document.getElementById('devCommRate').value)||0;
    document.getElementById('grossComm').value = (sp*dr/100).toFixed(2);
    updateNet();
}
function updateNet(){
    var g   = parseFloat(document.getElementById('grossComm').value)||0;
    var sst = parseFloat(document.getElementById('sstRate').value)||0;
    var net = sst>0 ? g/(1+sst/100) : g;
    var sa  = g - net;
    document.getElementById('netComm').value = net.toFixed(2);
    var nt = document.getElementById('sstNote');
    if(sst>0){
        nt.textContent = fmtRM(g)+' \u00f7 1.'+(sst<10?'0':'')+sst+' = '+fmtRM(net)+' (SST: '+fmtRM(sa)+')';
        nt.style.color = '#0369a1';
    } else {
        nt.textContent = 'No SST \u2014 full gross flows through.';
        nt.style.color = '#28a745';
    }
    updateRateCheck(); updatePicPreview();
}
function updateRateCheck(){
    var g   = parseFloat(document.getElementById('grossComm').value)||0;
    var sst = parseFloat(document.getElementById('sstRate').value)||0;
    var net = sst>0 ? g/(1+sst/100) : g;
    var sa  = g - net;
    var co  = parseFloat(document.getElementById('companyRate').value)||0;
    var pic = parseFloat(document.getElementById('picRate').value)||0;
    var ag  = parseFloat(document.getElementById('agentsRate').value)||0;
    var tot = co+pic+ag;
    var ok  = Math.abs(tot-100)<0.001;
    document.getElementById('rcGross').textContent = fmtRM(g);
    document.getElementById('rcSST').textContent   = sst>0 ? '-'+fmtRM(sa) : 'None';
    document.getElementById('rcSST').style.color   = sst>0 ? '#dc3545' : '#28a745';
    document.getElementById('rcNet').textContent   = fmtRM(net);
    document.getElementById('rcCo').textContent    = co+'%';
    document.getElementById('rcPIC').textContent   = pic+'%';
    document.getElementById('rcAg').textContent    = ag+'%';
    var te = document.getElementById('rcTot');
    te.textContent = tot.toFixed(2).replace(/\.?0+$/,'')+'%';
    te.className = ok ? 'ok' : 'bad';
    var se = document.getElementById('rcSt');
    if(ok){ se.textContent='\u2713 OK'; se.className='ok'; }
    else  { se.textContent=(tot>100?'Over':'Under')+' by '+Math.abs(tot-100).toFixed(2)+'%'; se.className='bad'; }
    updateCompanyPreview();
}
function updatePicPreview(){
    var net  = parseFloat(document.getElementById('netComm').value)||0;
    var pic  = parseFloat(document.getElementById('picRate').value)||0;
    var rank = document.getElementById('picRank').value||'REN';
    var rp   = {ATL:90,TL:85,ELITE:80,ASSOC:75,REN:70}[rank]||70;
    var pool = net*pic/100; var earn = pool*rp/100; var rem = pool-earn;
    document.getElementById('picComm').value = earn.toFixed(2);
    document.getElementById('picNote').textContent = pool>0 ? (fmtRM(pool)+' \u00d7 '+rp+'% = '+fmtRM(earn)+' (rem '+fmtRM(rem)+' \u2192 co)') : '';
}
function updateCompanyPreview(){
    var net = parseFloat(document.getElementById('netComm').value)||0;
    var co  = parseFloat(document.getElementById('companyRate').value)||0;
    document.getElementById('companyComm').value = (net*co/100).toFixed(2);
}

function getAgent(bid,aid){ for(var i=0;i<branches.length;i++){ if(branches[i].id===bid){ for(var j=0;j<branches[i].agents.length;j++){ if(branches[i].agents[j].id===aid) return branches[i].agents[j]; } } } return null; }
function addBranchData(arr){ branchCount++; var b={id:branchCount,agents:[]}; for(var i=0;i<arr.length;i++){agentCount++;b.agents.push({id:agentCount,rank:arr[i].rank,name:arr[i].name||'',hasDeal:arr[i].hasDeal===true,cobroke:[]});} branches.push(b); renderBranches(); }
function addBranch(){ branchCount++;agentCount++; branches.push({id:branchCount,agents:[{id:agentCount,rank:'REN',name:'',hasDeal:false,cobroke:[]}]}); renderBranches(); }
function removeBranch(bid){ branches=branches.filter(function(b){return b.id!==bid;}); renderBranches(); }
function addAgent(bid){ agentCount++; for(var i=0;i<branches.length;i++){ if(branches[i].id===bid){ branches[i].agents.push({id:agentCount,rank:'REN',name:'',hasDeal:false,cobroke:[]}); break; } } renderBranches(); }
function removeAgent(bid,aid){ for(var i=0;i<branches.length;i++){ if(branches[i].id===bid){ branches[i].agents=branches[i].agents.filter(function(a){return a.id!==aid;}); if(branches[i].agents.length===0){ branches=branches.filter(function(b){return b.id!==bid;}); } break; } } renderBranches(); }
function syncAgent(bid,aid,field,val){ var a=getAgent(bid,aid); if(a) a[field]=val; if(field==='rank') renderBranches(); }
function addCobroke(bid,aid){ var a=getAgent(bid,aid); if(a){ a.cobroke=a.cobroke||[]; a.cobroke.push({name:'',pct:50,isWTP:false}); renderBranches(); } }
function removeCobroke(bid,aid,ci){ var a=getAgent(bid,aid); if(a&&a.cobroke){ a.cobroke.splice(ci,1); renderBranches(); } }
function syncCobroke(bid,aid,ci,field,val){ var a=getAgent(bid,aid); if(a&&a.cobroke&&a.cobroke[ci]!=null) a.cobroke[ci][field]=val; }

function renderBranches(){
    var edDeal=document.getElementById('edmondDeal').checked;
    document.getElementById('edmondDealLbl').textContent=edDeal?'ON':'OFF';
    var row=document.getElementById('branchesRow'); row.innerHTML='';
    for(var bi=0;bi<branches.length;bi++){
        var b=branches[bi]; var bc=document.createElement('div'); bc.className='branch-col';
        var bh='<div class="branch-header"><span class="branch-title">Branch '+(bi+1)+'</span><button class="btn-del-branch" onclick="removeBranch('+b.id+')" title="Remove branch">✕</button></div>';
        for(var ai=0;ai<b.agents.length;ai++){
            var a=b.agents[ai]; var r=RANKS[a.rank]||RANKS.REN;
            var rankOpts=''; for(var ri=0;ri<RANK_KEYS.length;ri++){ var rk=RANK_KEYS[ri]; rankOpts+='<option value="'+rk+'"'+(a.rank===rk?' selected':'')+'>'+RANKS[rk].label+' '+RANKS[rk].pct+'%</option>'; }
            bh+='<div class="agent-row" id="ar-'+b.id+'-'+a.id+'">'
               +'<select class="agent-rank-sel" onchange="syncAgent('+b.id+','+a.id+',\'rank\',this.value)">'+rankOpts+'</select>'
               +'<input class="agent-name-inp" type="text" placeholder="Name" value="'+esc(a.name||'')+'" oninput="syncAgent('+b.id+','+a.id+',\'name\',this.value)"/>'
               +'<label title="Has deal"><input class="agent-deal-chk" type="checkbox"'+(a.hasDeal?' checked':'')+' onchange="syncAgent('+b.id+','+a.id+',\'hasDeal\',this.checked)"/> Deal</label>'
               +'<span class="agent-pct">'+r.pct+'%</span>'
               +'<button class="btn-del-agent" onclick="removeAgent('+b.id+','+a.id+')" title="Remove">✕</button>'
               +'</div>';
            if(a.cobroke&&a.cobroke.length>0){
                bh+='<div class="cobroke-section">';
                for(var ci=0;ci<a.cobroke.length;ci++){
                    var cb=a.cobroke[ci];
                    bh+='<div class="cobroke-row">'
                       +'<span class="cobroke-tag">Co-broke</span>'
                       +'<input type="text" placeholder="Name" value="'+esc(cb.name||'')+'" oninput="syncCobroke('+b.id+','+a.id+','+ci+',\'name\',this.value)" style="flex:1;padding:4px 6px;border:1px solid #ddd;border-radius:4px;font-size:11px"/>'
                       +'<input type="number" value="'+esc(String(cb.pct||50))+'" min="0" max="100" step="1" oninput="syncCobroke('+b.id+','+a.id+','+ci+',\'pct\',parseFloat(this.value)||0)" style="width:55px;padding:4px 6px;border:1px solid #ddd;border-radius:4px;font-size:11px"/>%'
                       +'<label style="font-size:11px;white-space:nowrap"><input class="cobroke-wtpchk" type="checkbox"'+(cb.isWTP?' checked':'')+' onchange="syncCobroke('+b.id+','+a.id+','+ci+',\'isWTP\',this.checked)"/> WTP</label>'
                       +'<button onclick="removeCobroke('+b.id+','+a.id+','+ci+')" style="background:none;border:none;color:#dc3545;cursor:pointer;font-size:13px">✕</button>'
                       +'</div>';
                }
                bh+='</div>';
            }
            bh+='<button class="btn-cobroke" onclick="addCobroke('+b.id+','+a.id+')">+ Add Co-broke</button>';
        }
        bh+='<button class="btn-add-agent" onclick="addAgent('+b.id+')">+ Add Agent</button>';
        bc.innerHTML=bh; row.appendChild(bc);
    }
    var addBtn=document.createElement('button'); addBtn.className='btn-add-branch'; addBtn.textContent='+ Add Branch'; addBtn.onclick=addBranch; row.appendChild(addBtn);
}

function runCalc(){
    var payload={
        sales_price:  parseFloat(document.getElementById('salesPrice').value)||0,
        dev_rate:     parseFloat(document.getElementById('devCommRate').value)||0,
        sst_rate:     parseFloat(document.getElementById('sstRate').value)||0,
        company_pct:  parseFloat(document.getElementById('companyRate').value)||0,
        pic_pct:      parseFloat(document.getElementById('picRate').value)||0,
        agents_pct:   parseFloat(document.getElementById('agentsRate').value)||0,
        pic_name:     document.getElementById('picName').value||'PIC',
        pic_rank:     document.getElementById('picRank').value||'REN',
        company_name: document.getElementById('companyName').value||'Company',
        edmond_name:  document.getElementById('edmondName').value||'ATL',
        edmond_deal:  document.getElementById('edmondDeal').checked,
        branches:     branches.map(function(b){return {agents:b.agents.map(function(a){return {rank:a.rank,name:a.name,hasDeal:a.hasDeal,cobroke:a.cobroke};});};})
    };
    if(payload.sales_price<=0){ showAlert('Sales price must be greater than 0.'); return; }
    var ab=document.getElementById('alertBox'); ab.style.display='none';
    fetch('/admin/commission-calculator/calculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
    .then(function(r){return r.json();})
    .then(function(d){ if(!d.ok){showAlert(d.error||'Calculation error.');return;} renderResults(d,payload); })
    .catch(function(e){ showAlert('Network error: '+e.message); });
}
function showAlert(msg){
    var a=document.getElementById('alertBox'); a.textContent=msg; a.style.display='block';
    var b=document.getElementById('alertInner'); b.textContent=msg; b.style.display='block';
}

function renderResults(data,payload){
    var lines=data.lines||[]; var ep=data.edmond_payout||{}; var summ=data.summary||{}; var cobroke=data.cobroke_payouts||{};
    var edName=payload.edmond_name||'ATL'; var coName=payload.company_name||'Company'; var picName=payload.pic_name||'PIC';
    var grid=document.getElementById('resultCards'); grid.innerHTML='';
    var ec=mRC('rc-atl-b');
    ec.innerHTML='<div class="rc-name">'+esc(edName)+'</div><div class="rc-role">ATL &bull; 90%</div><div>'+(ep.personal?rcL('Personal deal',ep.personal,false):'')+(ep.overrides?rcL('Override income',ep.overrides,false):'')+(ep.wtp1?rcL('WTP Gen1 (2%)',ep.wtp1,true):'')+(ep.wtp2?rcL('WTP Gen2 (1%)',ep.wtp2,true):'')+'</div><div class="rc-total"><span class="rc-total-lbl">Total</span><span class="rc-total-val">'+fmtRM(ep.total||0)+'</span></div>';
    grid.appendChild(ec);
    var barMap={ATL:'rc-atl-b',TL:'rc-tl-b',ELITE:'rc-er-b',ASSOC:'rc-as-b',REN:'rc-ren-b'};
    for(var bi=0;bi<branches.length;bi++){
        for(var ai=0;ai<branches[bi].agents.length;ai++){
            var a=branches[bi].agents[ai]; var r=RANKS[a.rank];
            var ac=mRC(barMap[a.rank]||'rc-ren-b');
            var p=findP(data.all_payouts,bi,a.name);
            ac.innerHTML='<div class="rc-name">'+esc(a.name||r.label)+'</div><div class="rc-role">'+r.label+' &bull; '+r.pct+'% &mdash; Branch '+(bi+1)+'</div><div>'+(p&&p.personal?rcL('Personal deal',p.personal,false):'')+(p&&p.overrides?rcL('Override income',p.overrides,false):'')+(p&&p.wtp1?rcL('WTP Gen1',p.wtp1,true):'')+(p&&p.wtp2?rcL('WTP Gen2',p.wtp2,true):'')+(!p||(!p.personal&&!p.overrides&&!p.wtp1&&!p.wtp2)?'<div style="color:#888;font-size:12px;padding:6px 0">No deal / no override</div>':'')+'</div><div class="rc-total"><span class="rc-total-lbl">Total</span><span class="rc-total-val">'+fmtRM(p?p.total:0)+'</span></div>';
            grid.appendChild(ac);
        }
    }
    for(var cpn in cobroke){
        var cpd=cobroke[cpn]; var cptot=(cpd.earn||0)+(cpd.overrides||0)+(cpd.wtp1||0);
        var cc=mRC('rc-co-b');
        cc.innerHTML='<div class="rc-name">'+esc(cpn)+'</div><div class="rc-role">'+(cpd.isWTP?'WTP Co-broke Agent':'External Co-broke')+'</div><div>'+(cpd.earn?rcL(cpd.isWTP?'Commission':'Referral share',cpd.earn,false):'')+(cpd.overrides?rcL('Override income',cpd.overrides,false):'')+(cpd.wtp1?rcL('WTP Gen1',cpd.wtp1,true):'')+'</div><div class="rc-total" style="background:#fffbeb;border-color:#fde68a"><span class="rc-total-lbl" style="color:#d97706">Total</span><span class="rc-total-val" style="color:#b45309">'+fmtRM(cptot)+'</span></div>';
        grid.appendChild(cc);
    }
    var compc=mRC('rc-comp-b');
    compc.innerHTML='<div class="rc-name">'+esc(coName)+'</div><div class="rc-role">Company Commission</div><div>'+rcL('Company comm',summ.total_company||0,false)+(summ.total_pic_rem?rcL('PIC remainder \u2192 co.',summ.total_pic_rem,false):'')+'</div><div class="rc-total" style="background:#f5f3ff;border-color:#a78bfa"><span class="rc-total-lbl" style="color:#6d28d9">Total</span><span class="rc-total-val" style="color:#6d28d9">'+fmtRM((summ.total_company||0)+(summ.total_pic_rem||0))+'</span></div>';
    grid.appendChild(compc);
    var picc=mRC('rc-pic-b');
    picc.innerHTML='<div class="rc-name">'+esc(picName)+'</div><div class="rc-role">Project PIC &bull; '+esc(summ.pic_rank_lbl||'REN')+' '+esc(String(summ.pic_rank_pct||70))+'%</div><div>'+rcL('PIC pool ('+payload.pic_pct+'% of net)',summ.total_pic_pool||0,false)+rcL((summ.pic_rank_lbl||'REN')+' '+(summ.pic_rank_pct||70)+'% of pool',summ.total_pic_earn||0,false)+'<div class="rc-line"><span class="rc-lbl">Remainder \u2192 company</span><span class="rc-val" style="color:#888">('+fmtRM(summ.total_pic_rem||0)+')</span></div></div><div class="rc-total" style="background:#fff5f7;border-color:#fda4af"><span class="rc-total-lbl" style="color:#e11d48">PIC Receives</span><span class="rc-total-val" style="color:#be185d">'+fmtRM(summ.total_pic_earn||0)+'</span></div>';
    grid.appendChild(picc);
    var bkR=document.getElementById('bkRows'); bkR.innerHTML='';
    var curB='';
    for(var li=0;li<lines.length;li++){
        var l=lines[li];
        if(l.branch!==curB){
            curB=l.branch;
            var sh=document.createElement('div'); sh.className='bk-sec';
            sh.innerHTML='<div>'+esc(l.branch)+'</div><div></div><div></div><div></div>';
            bkR.appendChild(sh);
        }
        var rw=document.createElement('div');
        rw.className='bk-row'+(l.isWTP?' is-wtp':'');
        var ac2='bk-amt'+(l.isWTP?' wtp':l.isPIC&&!l.isPICpool?' pic':l.isCompany?' company':'');
        rw.innerHTML='<div class="bk-agent">'+esc(l.label)+'</div><div class="bk-formula">'+esc(l.formula)+'</div><div class="bk-exp">'+esc(l.exp)+'</div><div class="'+ac2+'">'+fmtRM(l.amount)+'</div>';
        bkR.appendChild(rw);
    }
    var si=document.getElementById('sumItems'); si.innerHTML='';
    var items=[
        {lbl:'Total Deals',   val:String(summ.total_deals||0)},
        {lbl:'Gross/deal',    val:fmtRM(summ.gross_per_deal||0)},
        {lbl:'SST/deal',      val:(summ.sst_per_deal>0?'-'+fmtRM(summ.sst_per_deal):'None'), style:'color:#dc3545'},
        {lbl:'Net/deal',      val:fmtRM(summ.net_per_deal||0), style:'color:#17a2b8'},
        {lbl:coName+' total', val:fmtRM(summ.total_company||0), style:'color:#a78bfa'},
        {lbl:'PIC receives',  val:fmtRM(summ.total_pic_earn||0), style:'color:#fb7185'},
        {lbl:'PIC remainder', val:fmtRM(summ.total_pic_rem||0), style:'color:#aaa'}
    ];
    for(var i=0;i<items.length;i++){
        var d=document.createElement('div'); d.className='sum-item';
        d.innerHTML='<span class="sum-lbl">'+esc(items[i].lbl)+'</span><span class="sum-val"'+(items[i].style?' style="'+items[i].style+'"':'')+'>'+items[i].val+'</span>';
        si.appendChild(d);
    }
    var tap=(ep.total||0);
    for(var bpk in (data.all_payouts||{})){ tap+=(data.all_payouts[bpk].total||0); }
    for(var cpk in cobroke){ tap+=((cobroke[cpk].earn||0)+(cobroke[cpk].overrides||0)+(cobroke[cpk].wtp1||0)); }
    document.getElementById('sgVal').textContent = fmtRM(tap);
    document.getElementById('sgNote').textContent = 'Total across '+summ.total_deals+' deal(s) | Net cap: '+fmtRM(summ.total_net||0);
    document.getElementById('results').style.display='block';
    setTimeout(function(){document.getElementById('results').scrollIntoView({behavior:'smooth',block:'start'});},80);
}
function mRC(cls){ var c=document.createElement('div'); c.className='rc '+cls; return c; }
function rcL(lbl,val,wtp){ return '<div class="rc-line"><span class="rc-lbl">'+esc(lbl)+'</span><span class="rc-val'+(wtp?' wtp':'')+'">'+fmtRM(val)+'</span></div>'; }
function findP(all,bi,name){
    var k='('+bi+', '+JSON.stringify(name)+')';
    if(all&&all[k]) return all[k];
    if(all){ for(var key in all){ if(key.indexOf('"'+name+'"')!==-1) return all[key]; } }
    return null;
}
function toggleNav(){document.getElementById('mainNav').classList.toggle('open');}
document.addEventListener('DOMContentLoaded',function(){
    document.querySelectorAll('#mainNav a').forEach(function(a){
        a.addEventListener('click',function(){document.getElementById('mainNav').classList.remove('open');});
    });
});
</script>
</body>
</html>
"""
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

# ============================================================
# WTP UNIFIED SUBMISSION SYSTEM
# Paste this entire block into app.py
# Place it BEFORE the  

# ── ADMIN MAINTENANCE ROUTES ────────────────────────────────

@app.route("/admin/clear-notifications")
def admin_clear_notifications():
    """Clear expired/old notifications (older than 30 days)"""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")
    conn = get_db_connection()
    try:
        result = conn.execute(
            "DELETE FROM notifications WHERE created_at < datetime('now', '-30 days')"
        )
        conn.commit()
        deleted = result.rowcount
        flash(f"✅ Cleared {deleted} expired notification(s).", "success")
    except Exception as e:
        flash(f"Error: {str(e)}", "error")
    finally:
        conn.close()
    return redirect("/admin/settings")


@app.route("/admin/recalculate-ranks")
def admin_recalculate_ranks():
    """Recalculate all agent ranks based on cumulative gross commission."""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    RANK_THRESHOLDS = [
        (450000, 'ATL',       90.0),
        (210000, 'TL',        85.0),
        (90000,  'Elite REN', 80.0),
        (30000,  'Assoc REN', 75.0),
        (0,      'REN',       70.0),
    ]

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    try:
        agents = conn.execute(
            "SELECT id FROM users WHERE role = 'agent'"
        ).fetchall()

        updated = 0
        for ag in agents:
            # Get live gross (submitted + approved commission)
            gross_row = conn.execute(
                """SELECT COALESCE(SUM(commission_amount), 0)
                   FROM property_listings
                   WHERE agent_id = ? AND status IN ('submitted', 'approved')""",
                (ag['id'],)
            ).fetchone()
            gross = float(gross_row[0] or 0)

            # Determine rank
            new_rank, new_rate = 'REN', 70.0
            for threshold, rank, rate in RANK_THRESHOLDS:
                if gross >= threshold:
                    new_rank, new_rate = rank, rate
                    break

            conn.execute(
                "UPDATE users SET agent_rank=?, commission_rate=?, cumulative_gross=? WHERE id=?",
                (new_rank, new_rate, gross, ag['id'])
            )
            updated += 1

        conn.commit()
        flash(f"✅ Recalculated ranks for {updated} agent(s).", "success")
    except Exception as e:
        flash(f"Error recalculating ranks: {str(e)}", "error")
    finally:
        conn.close()
    return redirect("/admin/settings")

if __name__ == "__main__":  line
# ============================================================
# Covers:
#   - init_submissions_table()  → call inside init_database()
#   - 8 Flask routes
#   - 3 HTML templates (agent form, agent list, admin list)
# ============================================================

import uuid

# ── HTML TEMPLATES ──────────────────────────────────────────

UNIFIED_SUBMIT_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>New Submission – WTP</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:Arial,sans-serif;margin:0;background:#f0f2f5;color:#1a2a3a}
.topbar{background:#1a3a2a;color:white;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:8px;position:sticky;top:0;z-index:100}
.topbar-title{font-size:1rem;font-weight:700;display:flex;align-items:center;gap:8px}
.topbar-right{display:flex;align-items:center;gap:10px}
.topbar-right a{color:#86efac;text-decoration:none;font-size:13px}
.hamburger{display:none;background:none;border:none;color:white;font-size:22px;cursor:pointer;padding:2px 6px}
.nav-bar{background:white;padding:10px 16px;display:flex;flex-wrap:wrap;gap:4px;align-items:center;box-shadow:0 2px 6px rgba(0,0,0,.08)}
.nav-bar a{color:#16a34a;text-decoration:none;font-weight:600;font-size:13px;padding:5px 10px;border-radius:6px;white-space:nowrap;transition:background .15s}
.nav-bar a:hover{background:#f0fdf4}
.nav-bar a.nav-btn{background:#16a34a;color:white}
.nav-bar a.nav-btn:hover{background:#15803d}
.nav-bar a.nav-active{background:#f0fdf4;color:#15803d}
.nav-bar a.nav-logout{color:#dc3545}
@media(max-width:640px){.hamburger{display:block}.nav-bar{display:none;flex-direction:column;align-items:stretch;padding:8px 12px;gap:2px}.nav-bar.open{display:flex}.nav-bar a{padding:10px 12px;font-size:14px;border-bottom:1px solid #f0f0f0}.nav-bar a:last-child{border-bottom:none}}
.wrap{max-width:860px;margin:0 auto;padding:16px}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13px;font-weight:600}
.flash-ok{background:#dcfce7;color:#166534;border:1px solid #86efac}
.flash-err{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5}
.flash-warn{background:#fef9c3;color:#854d0e;border:1px solid #fde68a}

/* ── TYPE SELECTOR ── */
.type-bar{background:white;border-radius:10px;padding:16px;margin-bottom:14px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.type-bar h3{margin:0 0 10px;font-size:13px;font-weight:700;color:#444}
.type-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.tc{border:2px solid #e5e7eb;border-radius:10px;padding:14px 12px;text-align:center;cursor:pointer;transition:all .18s;background:#fff}
.tc:hover{border-color:#16a34a;background:#f0fdf4}
.tc.selected{border-color:#16a34a;background:#f0fdf4}
.tc-icon{font-size:26px;margin-bottom:5px}
.tc-name{font-weight:700;font-size:13px;color:#1a2a3a}
.tc-desc{font-size:10px;color:#888;margin-top:2px}
.tc-ref{font-size:9px;font-weight:700;color:#16a34a;background:#dcfce7;padding:2px 8px;border-radius:8px;display:inline-block;margin-top:5px}
@media(max-width:540px){.type-cards{grid-template-columns:1fr}}

/* ── STEP CARDS ── */
.step-card{background:white;border-radius:10px;padding:18px 20px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:12px}
.step-title{font-size:13px;font-weight:700;color:#1a2a3a;margin-bottom:14px;padding-bottom:8px;border-bottom:2px solid #f0f0f0;display:flex;align-items:center;gap:8px}
.step-num{background:#16a34a;color:white;border-radius:50%;width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0}
.step-card.sec-np{border-top:3px solid #1a4a6a}
.step-card.sec-ss{border-top:3px solid #3a2a6a}
.step-card.sec-rn{border-top:3px solid #6a2a1a}
.smart-sec{display:none}

/* ── FORM ELEMENTS ── */
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-row.three{grid-template-columns:1fr 1fr 1fr}
.form-group{margin-bottom:12px}
.form-group label{display:block;margin-bottom:4px;font-weight:600;color:#444;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.form-group input,.form-group select,.form-group textarea{width:100%;padding:9px 11px;border:1px solid #ccc;border-radius:6px;font-size:14px;background:#fafafa;transition:border-color .15s}
.form-group input:focus,.form-group select:focus,.form-group textarea:focus{outline:none;border-color:#16a34a;background:white}
.form-group input.readonly-field{background:#f5f5f5;color:#555;cursor:not-allowed;border-color:#e0e0e0}
.form-group .field-note{font-size:11px;color:#888;margin-top:3px}
.form-group textarea{resize:vertical;min-height:60px}
.span2{grid-column:span 2}
.span3{grid-column:span 3}
@media(max-width:600px){.form-row,.form-row.three{grid-template-columns:1fr}.span2,.span3{grid-column:span 1}}

/* ── REF NUMBER ── */
.ref-row{display:flex;align-items:center;gap:8px;margin-bottom:14px;padding:10px 14px;background:#f0fdf4;border:1px solid #86efac;border-radius:8px}
.ref-label{font-size:12px;font-weight:700;color:#166534}
.ref-value{font-family:monospace;font-size:16px;font-weight:800;color:#15803d;flex:1}
.ref-edit-btn{background:none;border:1px solid #86efac;color:#16a34a;padding:4px 10px;border-radius:5px;font-size:11px;cursor:pointer;transition:all .13s}
.ref-edit-btn:hover{background:#dcfce7}
.ref-locked{font-size:10px;color:#888;font-style:italic}

/* ── SIG BLOCKS ── */
.sig-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px}
.sig-block{border:1.5px dashed #d1d5db;border-radius:8px;padding:12px;background:white;position:relative}
.sig-block.sig-active{border-color:#16a34a;box-shadow:0 0 0 3px rgba(22,163,74,.12);animation:pulse 2s infinite}
.sig-block.sig-signed{border:1.5px solid #16a34a;background:#f0fdf4}
.sig-block.sig-locked{background:#f9fafb;border-color:#e5e7eb}
@keyframes pulse{0%,100%{box-shadow:0 0 0 3px rgba(22,163,74,.12)}50%{box-shadow:0 0 0 7px rgba(22,163,74,.04)}}
.sig-role{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#1a3a2a;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.sig-canvas{width:100%;height:68px;border:1px solid #e5e7eb;border-radius:4px;background:#fff;cursor:crosshair;display:block;touch-action:none}
.sig-locked .sig-canvas{cursor:default;background:#f9fafb}
.sig-hint{font-size:9px;color:#16a34a;text-align:center;margin:4px 0;font-style:italic}
.sig-locked .sig-hint,.sig-signed .sig-hint{display:none}
.sig-flds{margin-top:6px;display:flex;flex-direction:column;gap:3px}
.sig-fr{display:flex;align-items:baseline;gap:4px;font-size:10px}
.sig-fl{color:#888;min-width:36px}
.sig-fv{flex:1;border-bottom:1px solid #e5e7eb;padding:0 3px 1px;font-size:10px;color:#1a2a3a;cursor:pointer}
.sig-fv:empty::before{content:attr(data-ph);color:#9ca3af;font-style:italic;font-size:9px}
.sig-locked .sig-fv{cursor:default;border-bottom-color:#e5e7eb}
.sig-stamp{display:none;position:absolute;top:6px;right:8px;background:#dcfce7;color:#166534;font-size:7px;padding:1px 6px;border-radius:8px;border:1px solid #86efac;font-weight:700}
.sig-signed .sig-stamp{display:block}
.sig-clr{background:none;border:1px solid #e5e7eb;color:#9ca3af;font-size:8px;padding:1px 6px;border-radius:8px;cursor:pointer;float:right;margin-top:-3px}
.sig-locked .sig-clr,.sig-signed .sig-clr{display:none}
@media(max-width:540px){.sig-grid{grid-template-columns:1fr}}

/* ── WF STRIP ── */
.wf-strip{background:#1a3a2a;padding:8px 16px;display:flex;align-items:center;gap:4px;flex-wrap:wrap;overflow-x:auto}
.wf-step{display:flex;align-items:center;gap:4px;font-size:9px;color:rgba(255,255,255,.4);white-space:nowrap}
.wf-step.wf-done{color:#d4b04a}
.wf-step.wf-now{color:white;font-weight:700}
.wf-dot{width:17px;height:17px;border-radius:50%;border:1.5px solid rgba(255,255,255,.2);display:flex;align-items:center;justify-content:center;font-size:7px;font-weight:700;flex-shrink:0}
.wf-step.wf-done .wf-dot{background:#d4b04a;border-color:#d4b04a;color:#1a3a2a}
.wf-step.wf-now  .wf-dot{background:#b8952a;border-color:#b8952a;color:white}
.wf-arr{color:rgba(255,255,255,.15);font-size:8px;margin:0 1px}
.wf-tag{margin-left:auto;background:rgba(255,255,255,.1);color:rgba(255,255,255,.85);font-size:8px;padding:2px 9px;border-radius:8px;letter-spacing:.4px;white-space:nowrap}

/* ── NOTICE BANNER ── */
.nb{border-radius:6px;padding:9px 12px;margin-bottom:12px;font-size:11.5px;line-height:1.55;display:flex;gap:7px;align-items:flex-start}
.nb-info{background:#e8f4fd;border:1px solid #b3d4f0;color:#1a4a6a}
.nb-ok{background:#dcfce7;border:1px solid #86efac;color:#166534}
.nb-warn{background:#fef9c3;border:1px solid #fde68a;color:#854d0e}

/* ── ACTION BAR ── */
.act-bar{display:flex;gap:8px;justify-content:flex-end;padding-top:12px;flex-wrap:wrap}
.btn-submit{background:#16a34a;color:white;padding:11px 26px;border:none;border-radius:7px;cursor:pointer;font-size:14px;font-weight:700}
.btn-draft{background:#6c757d;color:white;padding:11px 20px;border:none;border-radius:7px;cursor:pointer;font-size:14px;font-weight:600}
.btn-send{background:#b8952a;color:white;padding:11px 22px;border:none;border-radius:7px;cursor:pointer;font-size:14px;font-weight:700}
.btn-done{background:#1a3a2a;color:white;padding:11px 22px;border:none;border-radius:7px;cursor:pointer;font-size:14px;font-weight:700}
@media(max-width:540px){.act-bar{flex-direction:column}.btn-submit,.btn-draft,.btn-send,.btn-done{width:100%;text-align:center}}

/* ── SEND MODAL ── */
.mo{position:fixed;inset:0;background:rgba(26,58,42,.52);backdrop-filter:blur(3px);z-index:1000;display:flex;align-items:center;justify-content:center;padding:16px;opacity:0;pointer-events:none;transition:opacity .2s}
.mo.open{opacity:1;pointer-events:all}
.mb{background:#faf8f3;border-radius:10px;box-shadow:0 16px 50px rgba(0,0,0,.22);width:100%;max-width:400px;padding:20px;transform:translateY(14px);transition:transform .2s;border-top:4px solid #1a3a2a}
.mo.open .mb{transform:translateY(0)}
.mb h3{font-size:14px;font-weight:700;color:#1a3a2a;margin:0 0 4px}
.mb p{font-size:10.5px;color:#6a6a6a;margin:0 0 14px}
.ch-card{border:1.5px solid #e5e7eb;border-radius:7px;padding:10px 12px;cursor:pointer;transition:all .13s;display:flex;align-items:center;gap:10px;margin-bottom:8px}
.ch-card:hover{border-color:#16a34a;background:#f0fdf4}
.ch-ico{width:30px;height:30px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0}
.ch-wa-bg{background:#dcf8e7}.ch-tg-bg{background:#daf0fb}
.ch-name{font-size:11.5px;font-weight:700;color:#1a2a3a}
.ch-desc{font-size:9px;color:#888}
.ph-step{display:none}.ph-step.open{display:block}
.back-lk{font-size:10px;color:#16a34a;cursor:pointer;margin-bottom:10px;display:inline-flex;align-items:center;gap:3px}
.back-lk:hover{text-decoration:underline}
.msg-prev{background:#f0fdf4;border:1px solid #86efac;border-radius:5px;padding:8px 10px;font-size:9.5px;color:#444;line-height:1.6;font-style:italic;max-height:72px;overflow:auto;margin-bottom:10px}
.m-grp{margin-bottom:10px}
.m-lbl{display:block;font-size:9px;font-weight:700;color:#555;margin-bottom:3px;text-transform:uppercase;letter-spacing:.3px}
.m-inp{width:100%;border:1.5px solid #e5e7eb;border-radius:5px;padding:7px 9px;font-size:12px;outline:none;font-family:Arial}
.m-inp:focus{border-color:#16a34a}
.mo-acts{display:flex;gap:7px;justify-content:flex-end;margin-top:12px}
.btn-sm{font-size:10.5px;padding:6px 14px;border-radius:5px;border:none;cursor:pointer;font-weight:600;font-family:Arial}
.btn-sm-gh{background:transparent;border:1.5px solid #16a34a;color:#16a34a}
.btn-sm-wa{background:#25D366;color:white}
.btn-sm-tg{background:#2AABEE;color:white}

/* ── SIG MODAL ── */
.sig-mo-cv{width:100%;height:110px;border:1.5px solid #e5e7eb;border-radius:5px;background:white;display:block;cursor:crosshair;touch-action:none;margin-bottom:4px}
.sig-mo-hint{font-size:9px;color:#888;text-align:center;margin-bottom:9px}
.sig-mo-clr{background:none;border:1px solid #e5e7eb;color:#9ca3af;font-size:8.5px;padding:2px 7px;border-radius:8px;cursor:pointer;float:right;margin-bottom:5px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:2px}
@media(max-width:400px){.two-col{grid-template-columns:1fr}}

/* ── RETURN LINK ── */
.ret-lnk-box{background:white;border:1.5px solid #e5e7eb;border-radius:5px;padding:7px 10px;font-size:9.5px;color:#555;word-break:break-all;line-height:1.5;margin:8px 0}
.ret-btn{display:block;width:100%;padding:8px;border:none;border-radius:4px;cursor:pointer;font-size:10.5px;text-align:center;margin-bottom:5px;font-family:Arial;font-weight:600;text-decoration:none}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-title">📋 New Submission</div>
  <div class="topbar-right">
    <a href="/agent/unified-submissions">📄 My Submissions</a>
    <button class="hamburger" onclick="toggleNav()" aria-label="Menu">☰</button>
  </div>
</div>
<div class="nav-bar" id="mainNav">
  <a href="/admin/dashboard" class="nav-active">&#128202; Dashboard</a>
  <a href="/admin/projects">&#127962; Projects</a>
  <a href="/admin/create-project" class="nav-btn">&#10010; New Project</a>
  <a href="/admin/agents">&#128101; Agents</a>
  <a href="/admin/agent-hierarchy">&#128279; Hierarchy</a>
  <a href="/admin/payments">&#9993; Payments</a>
  <a href="/admin/commissions">&#128176; Commissions</a>
  <a href="/admin/unified-submissions">&#128203; Unified Submissions</a>
  <a href="/admin/agent-performance">&#128200; Performance</a>
  <a href="/admin/commission-calculator">&#9889; Calc</a>
  <a href="/admin/settings">&#9881; Settings</a>
  <a href="/admin/export-data">&#128228; Export</a>
</div>

<!-- WF STRIP -->
<div class="wf-strip" id="wfStrip">
  <div class="wf-step wf-now" id="ws0"><div class="wf-dot">1</div><span>Agent Fills</span></div>
  <div class="wf-arr">›</div>
  <div class="wf-step" id="ws1"><div class="wf-dot">2</div><span id="ws1lbl">Party A Signs</span></div>
  <div class="wf-arr">›</div>
  <div class="wf-step" id="ws2"><div class="wf-dot">3</div><span id="ws2lbl">Party B Signs</span></div>
  <div class="wf-arr">›</div>
  <div class="wf-step" id="ws3"><div class="wf-dot">4</div><span>Complete</span></div>
  <div class="wf-tag" id="wfTag">Draft</div>
</div>

<div class="wrap">

{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat,msg in messages %}
<div class="flash flash-{{ 'ok' if cat=='success' else 'err' if cat=='error' else 'warn' }}">{{ msg }}</div>
{% endfor %}{% endif %}{% endwith %}

<div id="banner"></div>

<!-- ── TYPE SELECTOR ── -->
<div class="type-bar" id="typeBar">
  <h3>Select Submission Type</h3>
  <div class="type-cards">
    <div class="tc np-tc" id="tc-np" onclick="setType('np')">
      <div class="tc-icon">🏢</div>
      <div class="tc-name">New Project</div>
      <div class="tc-desc">Developer booking form</div>
      <div class="tc-ref">NP-XXXX</div>
    </div>
    <div class="tc ss-tc selected" id="tc-ss" onclick="setType('ss')">
      <div class="tc-icon">🏠</div>
      <div class="tc-name">Sub Sales</div>
      <div class="tc-desc">Offer to Purchase (OTP)</div>
      <div class="tc-ref">SC-XXXX</div>
    </div>
    <div class="tc rn-tc" id="tc-rn" onclick="setType('rn')">
      <div class="tc-icon">🔑</div>
      <div class="tc-name">Rental</div>
      <div class="tc-desc">Tenancy Agreement</div>
      <div class="tc-ref">RN-XXXX</div>
    </div>
  </div>
</div>

<form method="POST" action="/agent/unified-submit" id="uForm">
  <input type="hidden" name="sub_type"  id="h_type"  value="ss">
  <input type="hidden" name="sub_ref"   id="h_ref"   value="">
  <input type="hidden" name="sub_id"    id="h_id"    value="{{ sub_id or '' }}">
  <input type="hidden" name="sub_stage" id="h_stage" value="{{ sub_stage or 0 }}">
  <input type="hidden" name="sig_data"  id="h_sigs"  value="{{ sig_data or '{}' }}">
  <input type="hidden" name="action"    id="h_action" value="save">

  <!-- REF NUMBER -->
  <div class="ref-row" id="refRow">
    <span class="ref-label">Ref No.</span>
    <span class="ref-value" id="refDisplay">SC0001</span>
    <button type="button" class="ref-edit-btn" id="refEditBtn" onclick="openRefEdit()">✏ Edit</button>
    <span class="ref-locked" id="refAutoTag">auto-generated</span>
  </div>

  <!-- ── STEP 1: AGENT INFO (read-only from login) ── -->
  <div class="step-card">
    <div class="step-title"><span class="step-num">1</span> Agent Information</div>
    <div class="form-row">
      <div class="form-group">
        <label>Agent Name</label>
        <input type="text" class="readonly-field" value="{{ agent_name }}" readonly>
        <input type="hidden" name="agent_name" value="{{ agent_name }}">
        <div class="field-note">Auto-filled from your login</div>
      </div>
      <div class="form-group">
        <label>Agent Rank</label>
        <input type="text" class="readonly-field" value="{{ agent_rank }}" readonly>
        <input type="hidden" name="agent_rank" value="{{ agent_rank }}">
      </div>
    </div>
  </div>

  <!-- ── STEP 2: PROPERTY ── -->
  <div class="step-card">
    <div class="step-title"><span class="step-num">2</span> Property Information</div>
    <div class="form-group span2">
      <label>Property Address *</label>
      <input type="text" name="prop_address" id="f_prop_address" placeholder="Full property address" required value="{{ form.prop_address or '' }}">
    </div>
    <div class="form-row" style="margin-top:0">
      <div class="form-group">
        <label>Property Type</label>
        <select name="prop_type" id="f_prop_type">
          <option value="">— Select —</option>
          {% for pt in ['Condominium / Apartment','Terrace House','Semi-Detached','Bungalow','SOHO / Studio','Shop Office','Industrial','Land'] %}
          <option value="{{ pt }}" {{ 'selected' if form.prop_type==pt else '' }}>{{ pt }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="form-group">
        <label>State</label>
        <select name="prop_state" id="f_prop_state">
          <option value="">— Select —</option>
          {% for st in ['Selangor','Kuala Lumpur','Putrajaya','Johor','Penang','Perak','Sabah','Sarawak','Negeri Sembilan','Melaka','Pahang','Terengganu','Kelantan','Kedah','Perlis','Labuan'] %}
          <option value="{{ st }}" {{ 'selected' if form.prop_state==st else '' }}>{{ st }}</option>
          {% endfor %}
        </select>
      </div>
    </div>
  </div>

  <!-- ── STEP 3: PARTY A ── -->
  <div class="step-card">
    <div class="step-title"><span class="step-num">3</span> <span id="partyATitle">Vendor / Seller</span></div>
    <div class="form-row three">
      <div class="form-group">
        <label><span id="partyALbl">Vendor</span> Full Name *</label>
        <input type="text" name="partyA_name" placeholder="Full legal name" required value="{{ form.partyA_name or '' }}">
      </div>
      <div class="form-group">
        <label>NRIC / Passport No.</label>
        <input type="text" name="partyA_ic" placeholder="XXXXXX-XX-XXXX" value="{{ form.partyA_ic or '' }}">
      </div>
      <div class="form-group">
        <label>Phone Number</label>
        <input type="tel" name="partyA_phone" placeholder="e.g. 60112345678" value="{{ form.partyA_phone or '' }}">
      </div>
      <div class="form-group span3">
        <label>Address</label>
        <input type="text" name="partyA_addr" placeholder="Full correspondence address" value="{{ form.partyA_addr or '' }}">
      </div>
    </div>
  </div>

  <!-- ── STEP 4: PARTY B ── -->
  <div class="step-card">
    <div class="step-title"><span class="step-num">4</span> <span id="partyBTitle">Purchaser / Buyer</span></div>
    <div class="form-row three">
      <div class="form-group">
        <label><span id="partyBLbl">Purchaser</span> Full Name *</label>
        <input type="text" name="partyB_name" placeholder="Full legal name" required value="{{ form.partyB_name or '' }}">
      </div>
      <div class="form-group">
        <label>NRIC / Passport No.</label>
        <input type="text" name="partyB_ic" placeholder="XXXXXX-XX-XXXX" value="{{ form.partyB_ic or '' }}">
      </div>
      <div class="form-group">
        <label>Phone Number</label>
        <input type="tel" name="partyB_phone" placeholder="e.g. 60129876543" value="{{ form.partyB_phone or '' }}">
      </div>
      <div class="form-group span3">
        <label>Address</label>
        <input type="text" name="partyB_addr" placeholder="Full correspondence address" value="{{ form.partyB_addr or '' }}">
      </div>
    </div>
  </div>

  <!-- ── STEP 5: TYPE-SPECIFIC ── -->

  <!-- NEW PROJECT -->
  <div class="step-card sec-np smart-sec" id="sec-np">
    <div class="step-title"><span class="step-num">5</span> 🏢 New Project Details</div>
    <div class="form-row three">
      <div class="form-group">
        <label>Project Name</label>
        <input type="text" name="np_project" placeholder="e.g. Nadi Bangsar" value="{{ form.np_project or '' }}">
      </div>
      <div class="form-group">
        <label>Developer Name</label>
        <input type="text" name="np_developer" placeholder="Developer company" value="{{ form.np_developer or '' }}">
      </div>
      <div class="form-group">
        <label>Unit / Lot No.</label>
        <input type="text" name="np_unit" placeholder="e.g. B-12-3A" value="{{ form.np_unit or '' }}">
      </div>
      <div class="form-group">
        <label>Selling Price (RM)</label>
        <input type="text" name="np_price" placeholder="e.g. 680,000.00" value="{{ form.np_price or '' }}">
      </div>
      <div class="form-group">
        <label>Booking Fee (RM)</label>
        <input type="text" name="np_booking" placeholder="e.g. 5,000.00" value="{{ form.np_booking or '' }}">
      </div>
      <div class="form-group">
        <label>Bumiputera Lot</label>
        <select name="np_bumi">
          <option value="">— Select —</option>
          <option {{ 'selected' if form.np_bumi=='Yes' else '' }}>Yes – Bumi Lot</option>
          <option {{ 'selected' if form.np_bumi=='No' else '' }}>No – Open Market</option>
        </select>
      </div>
      <div class="form-group">
        <label>Expected Handover</label>
        <input type="month" name="np_handover" value="{{ form.np_handover or '' }}">
      </div>
      <div class="form-group span2">
        <label>Special Packages / Remarks</label>
        <input type="text" name="np_remarks" placeholder="Furniture package, rebate, etc." value="{{ form.np_remarks or '' }}">
      </div>
    </div>
  </div>

  <!-- SUB SALES -->
  <div class="step-card sec-ss smart-sec" id="sec-ss">
    <div class="step-title"><span class="step-num">5</span> 🏠 Sub Sales – OTP Details</div>
    <div class="form-row three">
      <div class="form-group">
        <label>Purchase Price (RM) *</label>
        <input type="text" name="ss_price" placeholder="e.g. 550,000.00" value="{{ form.ss_price or '' }}">
      </div>
      <div class="form-group span2">
        <label>Price in Words</label>
        <input type="text" name="ss_price_words" placeholder="e.g. Five Hundred Fifty Thousand Only" value="{{ form.ss_price_words or '' }}">
      </div>
      <div class="form-group">
        <label>Earnest Deposit (%)</label>
        <input type="text" name="ss_dep_pct" placeholder="e.g. 1" value="{{ form.ss_dep_pct or '' }}">
      </div>
      <div class="form-group">
        <label>Earnest Deposit (RM)</label>
        <input type="text" name="ss_dep_amt" placeholder="e.g. 5,500.00" value="{{ form.ss_dep_amt or '' }}">
      </div>
      <div class="form-group">
        <label>Cheque / Transfer Ref</label>
        <input type="text" name="ss_cheque" placeholder="e.g. TXN-2024-001" value="{{ form.ss_cheque or '' }}">
      </div>
      <div class="form-group">
        <label>SPA Target Date</label>
        <input type="date" name="ss_spa_date" value="{{ form.ss_spa_date or '' }}">
      </div>
      <div class="form-group span2">
        <label>Special Conditions (if any)</label>
        <input type="text" name="ss_special" placeholder="Leave blank if none" value="{{ form.ss_special or '' }}">
      </div>
    </div>
  </div>

  <!-- RENTAL -->
  <div class="step-card sec-rn smart-sec" id="sec-rn">
    <div class="step-title"><span class="step-num">5</span> 🔑 Tenancy Agreement Details</div>
    <div class="form-row three">
      <div class="form-group">
        <label>Monthly Rent (RM) *</label>
        <input type="text" name="rn_rent" placeholder="e.g. 2,500.00" value="{{ form.rn_rent or '' }}">
      </div>
      <div class="form-group">
        <label>Security Deposit (RM)</label>
        <input type="text" name="rn_sec_dep" placeholder="e.g. 5,000.00 (2 mths)" value="{{ form.rn_sec_dep or '' }}">
      </div>
      <div class="form-group">
        <label>Utility Deposit (RM)</label>
        <input type="text" name="rn_util_dep" placeholder="e.g. 500.00" value="{{ form.rn_util_dep or '' }}">
      </div>
      <div class="form-group">
        <label>Tenancy Period</label>
        <select name="rn_period">
          <option value="">— Select —</option>
          {% for p in ['1 Year','2 Years','3 Years'] %}
          <option {{ 'selected' if form.rn_period==p else '' }}>{{ p }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="form-group">
        <label>Commencement Date</label>
        <input type="date" name="rn_start" value="{{ form.rn_start or '' }}">
      </div>
      <div class="form-group">
        <label>Expiry Date</label>
        <input type="date" name="rn_end" value="{{ form.rn_end or '' }}">
      </div>
      <div class="form-group">
        <label>Furnished Status</label>
        <select name="rn_furnished">
          <option value="">— Select —</option>
          {% for f in ['Fully Furnished','Partially Furnished','Unfurnished'] %}
          <option {{ 'selected' if form.rn_furnished==f else '' }}>{{ f }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="form-group">
        <label>Stamping Duty By</label>
        <select name="rn_stamp">
          <option value="">— Select —</option>
          {% for s in ['Tenant','Landlord','Split equally'] %}
          <option {{ 'selected' if form.rn_stamp==s else '' }}>{{ s }}</option>
          {% endfor %}
        </select>
      </div>
      <div class="form-group">
        <label>Access Card / Key Deposit</label>
        <input type="text" name="rn_key_dep" placeholder="e.g. 200.00" value="{{ form.rn_key_dep or '' }}">
      </div>
      <div class="form-group span3">
        <label>Special Conditions / Inclusions</label>
        <input type="text" name="rn_special" placeholder="e.g. Air-con servicing by landlord, no pets" value="{{ form.rn_special or '' }}">
      </div>
    </div>
  </div>

  <!-- ── STEP 6: SIGNATURES ── -->
  <div class="step-card">
    <div class="step-title"><span class="step-num">6</span> ✍ Signatures</div>
    <div class="sig-grid" id="sigGrid"></div>
  </div>

  <!-- ACTION BAR -->
  <div class="act-bar" id="actBar">
    <button type="button" class="btn-draft" id="defaultDraftBtn">💾 Save Draft</button>
    <button type="button" class="btn-send" id="defaultSendBtn">📤 Send for Signing →</button>
  </div>

</form>
</div><!-- /wrap -->

<!-- ══ MODAL: SEND ══ -->
<div class="mo" id="moSend">
  <div class="mb">
    <h3 id="sendTitle">Send for Signing</h3>
    <p id="sendSub">Choose delivery method</p>
    <div id="sendBody"></div>
    <div class="mo-acts"><button class="btn-sm btn-sm-gh" onclick="closeMo('moSend')">Cancel</button></div>
  </div>
</div>

<!-- ══ MODAL: SIG ══ -->
<div class="mo" id="moSig">
  <div class="mb" style="max-width:460px">
    <h3 id="sigMoTitle">Signature</h3>
    <p>Sign within the box using your finger or mouse</p>
    <canvas class="sig-mo-cv" id="sigMoCv"></canvas>
    <button class="sig-mo-clr" onclick="clearSigMo()">✕ Clear</button>
    <div class="sig-mo-hint">Draw your signature above</div>
    <div class="two-col">
      <div class="m-grp"><label class="m-lbl">Full Name *</label><input class="m-inp" id="sigMoName" type="text" placeholder="Legal name"></div>
      <div class="m-grp"><label class="m-lbl">NRIC No.</label><input class="m-inp" id="sigMoIc" type="text" placeholder="XXXXXX-XX-XXXX"></div>
    </div>
    <div class="m-grp"><label class="m-lbl">Date</label><input class="m-inp" id="sigMoDate" type="date"></div>
    <div class="mo-acts">
      <button class="btn-sm btn-sm-gh" onclick="closeMo('moSig')">Cancel</button>
      <button class="btn-sm" style="background:#16a34a;color:white" onclick="confirmSig()">✓ Confirm</button>
    </div>
  </div>
</div>

<!-- ══ MODAL: REF EDIT ══ -->
<div class="mo" id="moRef">
  <div class="mb">
    <h3>Edit Reference Number</h3>
    <p>System auto-generated this. Change to match your physical file if needed.</p>
    <div class="m-grp"><label class="m-lbl">Reference Number</label><input class="m-inp" id="refInput" type="text" placeholder="e.g. SC0249"></div>
    <div class="mo-acts">
      <button class="btn-sm btn-sm-gh" onclick="closeMo('moRef')">Cancel</button>
      <button class="btn-sm" style="background:#1a3a2a;color:white" onclick="confirmRef()">✓ Confirm</button>
    </div>
  </div>
</div>

<!-- ══ MODAL: RETURN LINK ══ -->
<div class="mo" id="moReturn">
  <div class="mb">
    <h3>✅ Signature Complete!</h3>
    <p id="retSub">Send the return link back to your agent.</p>
    <div class="ret-lnk-box" id="retLnk"></div>
    <button class="ret-btn" onclick="copyRet()" style="background:#1a3a2a;color:white">📋 Copy Return Link</button>
    <a class="ret-btn" id="retWa" href="#" target="_blank" style="background:#25D366;color:white">💬 Send Back via WhatsApp</a>
    <a class="ret-btn" id="retTg" href="#" target="_blank" style="background:#2AABEE;color:white">✈️ Send Back via Telegram</a>
  </div>
</div>

<script>
// ════════════════════════════════════════
// CONFIG
// ════════════════════════════════════════
const TYPE_CFG = {
  np: { label:'New Project', docTitle:'Booking Form', refPfx:'NP',
        partyA:'Developer', partyB:'Purchaser / Buyer',
        ws1:'Developer Confirms', ws2:'Buyer Signs' },
  ss: { label:'Sub Sales', docTitle:'Offer to Purchase', refPfx:'SC',
        partyA:'Vendor / Seller', partyB:'Purchaser / Buyer',
        ws1:'Vendor Signs', ws2:'Purchaser Signs' },
  rn: { label:'Rental', docTitle:'Tenancy Agreement', refPfx:'RN',
        partyA:'Landlord / Owner', partyB:'Tenant',
        ws1:'Landlord Signs', ws2:'Tenant Signs' }
};
const ALLOWED = { 0:[], 1:['pA','pAw'], 2:[], 3:['pB','pBw'], 4:[] };
const STAGE_TAGS = ['Draft','Awaiting Party A','Party A Signed','Awaiting Party B','✓ Complete'];

// ── Server data injected safely as one JSON block ──
var WTP_DATA = {
  stage:    {{ sub_stage | int }},
  type:     {{ sub_type  | tojson }},
  ref:      {{ sub_ref   | tojson }},
  sigs:     {{ sig_data  | safe }},
  nextRefs: {{ next_refs | tojson | safe }}
};

let curType   = 'ss';
let curStage  = 0;
let curRef    = '';
let refLocked = false;
let sigStore  = {};
let sendCh    = '';
let sendTgt   = '';

// ════════════════════════════════════════
// INIT — runs after all functions defined
// ════════════════════════════════════════
window.addEventListener('load', function(){
  // Pull from safe server block
  curStage  = WTP_DATA.stage || 0;
  refLocked = curStage > 0;
  curType   = (TYPE_CFG[WTP_DATA.type] ? WTP_DATA.type : 'ss');
  curRef    = WTP_DATA.ref || autoRef(curType);

  try {
    var s = WTP_DATA.sigs;
    sigStore = (s && typeof s === 'object') ? s : JSON.parse(s || '{}');
  } catch(e){ sigStore = {}; }

  // Init canvases
  document.querySelectorAll('.sig-canvas').forEach(function(c){
    c.width = c.offsetWidth || 240;
    c.height = 68;
  });

  setType(curType, true);
  setRef(curRef);
  if (refLocked) lockRef();
  applyStage();
  restoreAllSigs();

  // Wire default static buttons (avoids quote issues in HTML onclick)
  var db = document.getElementById('defaultDraftBtn');
  var sb = document.getElementById('defaultSendBtn');
  if (db) db.addEventListener('click', function(){ doSave('draft'); });
  if (sb) sb.addEventListener('click', function(){ openSendModal('pA'); });
});

// ════════════════════════════════════════
// TYPE SWITCHING
// ════════════════════════════════════════
function setType(t, init){
  curType = t;
  ['np','ss','rn'].forEach(function(x){
    document.getElementById('tc-'+x).classList.toggle('selected', x===t);
    var s = document.getElementById('sec-'+x);
    if(s) s.style.display = x===t ? 'block' : 'none';
  });
  var cfg = TYPE_CFG[t];
  document.getElementById('partyATitle').textContent = cfg.partyA;
  document.getElementById('partyALbl').textContent   = cfg.partyA.split('/')[0].trim();
  document.getElementById('partyBTitle').textContent = cfg.partyB;
  document.getElementById('partyBLbl').textContent   = cfg.partyB.split('/')[0].trim();
  document.getElementById('ws1lbl').textContent = cfg.ws1;
  document.getElementById('ws2lbl').textContent = cfg.ws2;
  document.getElementById('h_type').value = t;
  if(!init){ setRef(autoRef(t)); }
  renderSigGrid();
  updateActBar();
  updateBanner();
  updateWF();
}

function autoRef(t){
  var pfx = TYPE_CFG[t] ? TYPE_CFG[t].refPfx : 'SC';
  var nextRefsMap = (WTP_DATA && WTP_DATA.nextRefs) ? WTP_DATA.nextRefs : {};
  var next = parseInt(nextRefsMap[t] || 1);
  return pfx + String(next).padStart(4,'0');
}

// ════════════════════════════════════════
// REF NUMBER
// ════════════════════════════════════════
function setRef(r){
  curRef = r;
  document.getElementById('refDisplay').textContent = r;
  document.getElementById('h_ref').value = r;
}
function openRefEdit(){ if(refLocked)return; document.getElementById('refInput').value=curRef; openMo('moRef'); setTimeout(function(){document.getElementById('refInput').select();},80); }
function confirmRef(){
  var v = document.getElementById('refInput').value.trim().toUpperCase();
  if(!v){ alert('Please enter a reference number.'); return; }
  setRef(v);
  document.getElementById('refAutoTag').textContent='edited';
  closeMo('moRef');
}
function lockRef(){
  refLocked=true;
  document.getElementById('refEditBtn').style.display='none';
  document.getElementById('refAutoTag').textContent='locked';
}

// ════════════════════════════════════════
// STAGE ENGINE
// ════════════════════════════════════════
function applyStage(){
  updateWF(); updateBanner(); updateActBar();
  lockFormFields(curStage > 0);
  renderSigGrid();
  if(curStage>0) lockRef();
}

function updateWF(){
  [0,1,2,3].forEach(function(i){
    var el=document.getElementById('ws'+i);
    el.classList.remove('wf-done','wf-now');
    if(curStage>i) el.classList.add('wf-done');
    else if(curStage===i) el.classList.add('wf-now');
  });
  if(curStage===2){ document.getElementById('ws1').classList.remove('wf-now'); document.getElementById('ws1').classList.add('wf-done'); }
  document.getElementById('wfTag').textContent = STAGE_TAGS[curStage]||'';
}

function updateBanner(){
  var cfg=TYPE_CFG[curType];
  var a=cfg.partyA.split('/')[0].trim(), b=cfg.partyB.split('/')[0].trim();
  var msgs = {
    0:'<div class="nb nb-info">ℹ️ &nbsp;<strong>Agent Mode.</strong> Fill in all details above, then tap <em>"Send to '+a+' for Signing →"</em>.</div>',
    1:'<div class="nb nb-warn">✏️ &nbsp;<strong>'+a+' Signing Mode.</strong> Form is locked. Please sign in your boxes below, then tap <em>"Done – Return to Agent"</em>.</div>',
    2:'<div class="nb nb-ok">✅ &nbsp;<strong>'+a+' has signed.</strong> Sections locked permanently. Review then send to '+b+'.</div>',
    3:'<div class="nb nb-warn">✏️ &nbsp;<strong>'+b+' Signing Mode.</strong> Sign in your boxes below, then tap <em>"Done – Return to Agent"</em>.</div>',
    4:'<div class="nb nb-ok">🎉 &nbsp;<strong>All parties have signed!</strong> Submission fully executed. Save or print.</div>'
  };
  document.getElementById('banner').innerHTML = msgs[curStage]||'';
}

function updateActBar(){
  var cfg=TYPE_CFG[curType];
  var a=cfg.partyA.split('/')[0].trim(), b=cfg.partyB.split('/')[0].trim();
  var h = {
    0: '<button type="button" class="btn-draft" onclick="doSave(\'draft\')">💾 Save Draft</button>'
      +'<button type="button" class="btn-send" onclick="openSendModal(\'pA\')">📤 Send to '+a+' →</button>',
    1: '<button type="button" class="btn-done" onclick="partyDone(\'pA\')">✓ Done – Return to Agent</button>',
    2: '<button type="button" class="btn-draft" onclick="doSave(\'save\')">💾 Save</button>'
      +'<button type="button" class="btn-send" onclick="openSendModal(\'pB\')">📤 Send to '+b+' →</button>',
    3: '<button type="button" class="btn-done" onclick="partyDone(\'pB\')">✓ Done – Return to Agent</button>',
    4: '<button type="button" class="btn-draft" onclick="window.print()">🖨 Print</button>'
      +'<button type="button" class="btn-submit" onclick="doSave(\'complete\')">⬇ Save Complete</button>'
  };
  document.getElementById('actBar').innerHTML = h[curStage]||'';
}

function lockFormFields(locked){
  document.querySelectorAll('#uForm input:not([type=hidden]), #uForm select, #uForm textarea').forEach(function(el){
    if(el.classList.contains('readonly-field')) return;
    el.disabled = locked;
    el.style.background = locked ? '#f5f5f5' : '';
  });
  document.querySelectorAll('.tc').forEach(function(el){ el.style.pointerEvents = locked ? 'none' : ''; });
}

// ════════════════════════════════════════
// SIG GRID
// ════════════════════════════════════════
function renderSigGrid(){
  var cfg = TYPE_CFG[curType];
  var sigs = [
    {id:'pA',  role: cfg.partyA},
    {id:'pAw', role:'Witnessed by ('+cfg.partyA.split('/')[0].trim()+')'},
    {id:'pB',  role: cfg.partyB},
    {id:'pBw', role:'Witnessed by ('+cfg.partyB.split('/')[0].trim()+')'},
  ];
  var allowed = ALLOWED[curStage]||[];
  var html = sigs.map(function(s){
    var signed  = !!sigStore[s.id];
    var active  = !signed && allowed.indexOf(s.id)>=0;
    var locked2 = !signed && !active;
    var cls = 'sig-block'+(signed?' sig-signed':active?' sig-active':' sig-locked');
    var icon = signed?'✅':active?'✍':'🔒';
    var hint = active ? '<div class="sig-hint">👆 Tap here to sign</div>' : '';
    var sd = sigStore[s.id]||{};
    return '<div class="'+cls+'" id="sb-'+s.id+'">'
      +'<div class="sig-stamp">✓ Signed</div>'
      +'<div class="sig-role"><span>'+s.role+'</span><span>'+icon+'</span></div>'
      +hint
      +'<canvas class="sig-canvas" id="cv-'+s.id+'" onclick="sigClick(\''+s.id+'\',\''+s.role+'\')"></canvas>'
      +'<button type="button" class="sig-clr" onclick="clearSig(\''+s.id+'\')">Clear</button>'
      +'<div class="sig-flds">'
      +'<div class="sig-fr"><span class="sig-fl">Name</span><span class="sig-fv" data-ph="Full Name">'+esc(sd.name||'')+'</span></div>'
      +'<div class="sig-fr"><span class="sig-fl">NRIC</span><span class="sig-fv" data-ph="XXXXXX-XX-XXXX">'+esc(sd.ic||'')+'</span></div>'
      +'<div class="sig-fr"><span class="sig-fl">Date</span><span class="sig-fv" data-ph="DD/MM/YYYY">'+esc(sd.date||'')+'</span></div>'
      +'</div></div>';
  }).join('');
  document.getElementById('sigGrid').innerHTML = html;
  setTimeout(function(){
    document.querySelectorAll('.sig-canvas').forEach(function(c){ c.width=c.offsetWidth||240; c.height=68; });
    restoreAllSigs();
  }, 60);
}

function restoreAllSigs(){
  Object.keys(sigStore).forEach(function(id){
    var s=sigStore[id];
    if(!s||!s.img) return;
    var fc=document.getElementById('cv-'+id);
    if(!fc) return;
    fc.width=fc.offsetWidth||240; fc.height=68;
    var img=new Image();
    img.onload=function(){ fc.getContext('2d').drawImage(img,0,0,fc.width,fc.height); };
    img.src=s.img;
  });
}

function esc(s){ var d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }

// ════════════════════════════════════════
// SIGNATURE MODAL
// ════════════════════════════════════════
var smCtx=null, smDrw=false, curSigId=null;
function gpos(cv,e){ var r=cv.getBoundingClientRect(),sx=cv.width/r.width,sy=cv.height/r.height,src=e.touches?e.touches[0]:e; return{x:(src.clientX-r.left)*sx,y:(src.clientY-r.top)*sy}; }

function sigClick(id,title){
  var allowed=ALLOWED[curStage]||[];
  if(allowed.indexOf(id)<0||sigStore[id]) return;
  curSigId=id;
  document.getElementById('sigMoTitle').textContent=title+' – Signature';
  var s=sigStore[id]||{};
  document.getElementById('sigMoName').value=s.name||'';
  document.getElementById('sigMoIc').value=s.ic||'';
  document.getElementById('sigMoDate').value=s.date||new Date().toISOString().split('T')[0];
  var cv=document.getElementById('sigMoCv');
  cv.width=cv.offsetWidth||420; cv.height=110;
  smCtx=cv.getContext('2d'); smCtx.clearRect(0,0,cv.width,cv.height);
  smCtx.strokeStyle='#1a3a2a'; smCtx.lineWidth=2.2; smCtx.lineCap='round'; smCtx.lineJoin='round';
  if(s.img){ var img=new Image(); img.onload=function(){smCtx.drawImage(img,0,0);}; img.src=s.img; }
  cv.onmousedown=function(e){smDrw=true;var p=gpos(cv,e);smCtx.beginPath();smCtx.moveTo(p.x,p.y);};
  cv.onmousemove=function(e){if(!smDrw)return;var p=gpos(cv,e);smCtx.lineTo(p.x,p.y);smCtx.stroke();};
  cv.onmouseup=cv.onmouseleave=function(){smDrw=false;};
  cv.ontouchstart=function(e){e.preventDefault();smDrw=true;var p=gpos(cv,e);smCtx.beginPath();smCtx.moveTo(p.x,p.y);};
  cv.ontouchmove=function(e){e.preventDefault();if(!smDrw)return;var p=gpos(cv,e);smCtx.lineTo(p.x,p.y);smCtx.stroke();};
  cv.ontouchend=function(){smDrw=false;};
  openMo('moSig');
}

function clearSigMo(){ smCtx&&smCtx.clearRect(0,0,document.getElementById('sigMoCv').width,110); }

function confirmSig(){
  var name=document.getElementById('sigMoName').value.trim();
  if(!name){ alert('Please enter your full name.'); return; }
  var cv=document.getElementById('sigMoCv');
  var img=cv.toDataURL('image/png');
  var ic=document.getElementById('sigMoIc').value.trim();
  var dt=document.getElementById('sigMoDate').value;
  var dFmt=dt?new Date(dt).toLocaleDateString('en-MY'):'';
  sigStore[curSigId]={name:name,ic:ic,date:dFmt,img:img};
  // draw onto mini canvas
  var fc=document.getElementById('cv-'+curSigId);
  if(fc){ fc.width=fc.offsetWidth||240; fc.height=68; var im2=new Image(); im2.onload=function(){fc.getContext('2d').drawImage(im2,0,0,fc.width,fc.height);}; im2.src=img; }
  // update sub-fields
  var bl=document.getElementById('sb-'+curSigId);
  if(bl){ var fvs=bl.querySelectorAll('.sig-fv'); if(fvs[0])fvs[0].textContent=name; if(fvs[1])fvs[1].textContent=ic; if(fvs[2])fvs[2].textContent=dFmt; }
  document.getElementById('h_sigs').value=JSON.stringify(sigStore);
  closeMo('moSig');
  renderSigGrid();
}

function clearSig(id){
  var c=document.getElementById('cv-'+id); if(c) c.getContext('2d').clearRect(0,0,c.width,c.height);
  delete sigStore[id];
  document.getElementById('h_sigs').value=JSON.stringify(sigStore);
  renderSigGrid();
}

// ════════════════════════════════════════
// SEND MODAL
// ════════════════════════════════════════
function openSendModal(tgt){
  sendTgt=tgt;
  if(!TYPE_CFG[curType]){ alert('Please select a submission type first (New Project, Sub Sales or Rental).'); return; }
  var cfg=TYPE_CFG[curType];
  var who=tgt==='pA'?cfg.partyA.split('/')[0].trim():cfg.partyB.split('/')[0].trim();
  document.getElementById('sendTitle').textContent='📤 Send to '+who+' for Signing';
  document.getElementById('sendSub').textContent=who+' will receive a link to open and sign.';
  document.getElementById('sendBody').innerHTML=
    '<div onclick="showPhone(\'wa\')" class="ch-card"><div class="ch-ico ch-wa-bg">💬</div><div><div class="ch-name">WhatsApp</div><div class="ch-desc">Pre-filled message + signing link</div></div><span>›</span></div>'
   +'<div onclick="showPhone(\'tg\')" class="ch-card"><div class="ch-ico ch-tg-bg">✈️</div><div><div class="ch-name">Telegram</div><div class="ch-desc">Pre-filled message + signing link</div></div><span>›</span></div>'
   +'<div id="phStep" class="ph-step">'
   +'<span class="back-lk" onclick="backCh()">← Back</span>'
   +'<div class="m-grp"><label class="m-lbl" id="phLbl">Phone Number</label><input class="m-inp" id="phNum" type="tel" placeholder="e.g. 601112345678"></div>'
   +'<div class="msg-prev" id="msgPrev"></div>'
   +'<button id="phGo" class="btn-sm btn-sm-wa" onclick="doSend()" style="width:100%;padding:9px;margin-top:2px">Send Now ›</button>'
   +'</div>';
  openMo('moSend');
}

function showPhone(ch){
  sendCh=ch;
  var cfg=TYPE_CFG[curType];
  var who=sendTgt==='pA'?cfg.partyA.split('/')[0].trim():cfg.partyB.split('/')[0].trim();
  var toStage=sendTgt==='pA'?1:3;
  var link=buildLink(toStage);
  var msg='Hi '+who+', please open the link to review and sign the '+cfg.docTitle+' (Ref: '+curRef+'):\n\n'+link;
  document.getElementById('phStep').classList.add('open');
  document.getElementById('sendBody').querySelectorAll('.ch-card').forEach(function(c){c.style.display='none';});
  document.getElementById('phLbl').textContent=ch==='wa'?'WhatsApp Number':'Telegram Phone / Username';
  document.getElementById('msgPrev').textContent=msg;
  var go=document.getElementById('phGo');
  go.className='btn-sm '+(ch==='wa'?'btn-sm-wa':'btn-sm-tg');
  go.textContent=ch==='wa'?'💬 Open WhatsApp':'✈️ Open Telegram';
}

function backCh(){
  document.getElementById('phStep').classList.remove('open');
  document.getElementById('sendBody').querySelectorAll('.ch-card').forEach(function(c){c.style.display='flex';});
}

function doSend(){
  var cfg=TYPE_CFG[curType];
  var who=sendTgt==='pA'?cfg.partyA.split('/')[0].trim():cfg.partyB.split('/')[0].trim();
  var toStage=sendTgt==='pA'?1:3;
  var link=buildLink(toStage);
  var msg=encodeURIComponent('Hi '+who+', please open the link to review and sign the '+cfg.docTitle+' (Ref: '+curRef+'):\n\n'+link);
  var ph=document.getElementById('phNum').value.replace(/\\D/g,'');
  var url=sendCh==='wa'?(ph?'https://wa.me/'+ph+'?text='+msg:'https://wa.me/?text='+msg)
                       :(ph?'https://t.me/'+ph+'?text='+msg:'https://t.me/share/url?url='+encodeURIComponent(link)+'&text='+msg);
  window.open(url,'_blank');
  if(sendTgt==='pA' && curStage===0){
    curStage=1; refLocked=true;
    document.getElementById('h_stage').value=1;
    lockRef();
    doSave('stage');
  }
  closeMo('moSend');
}

// ════════════════════════════════════════
// PARTY DONE
// ════════════════════════════════════════
var retLinkVal='';
function partyDone(party){
  var mainSig=party==='pA'?'pA':'pB';
  if(!sigStore[mainSig]){ var cfg=TYPE_CFG[curType]; alert('Please sign the '+(party==='pA'?cfg.partyA:cfg.partyB).split('/')[0].trim()+' signature box first.'); return; }
  curStage=party==='pA'?2:4;
  document.getElementById('h_stage').value=curStage;
  doSave('stage');
  retLinkVal=buildLink(curStage);
  var retMsg=encodeURIComponent('Hi Agent, I have signed (Ref: '+curRef+'). Updated form:\n\n'+retLinkVal);
  document.getElementById('retSub').textContent=party==='pA'?'Send back to agent to forward to the other party.':'Send back to agent to complete the process.';
  document.getElementById('retLnk').textContent=retLinkVal;
  document.getElementById('retWa').href='https://wa.me/?text='+retMsg;
  document.getElementById('retTg').href='https://t.me/share/url?url='+encodeURIComponent(retLinkVal)+'&text='+retMsg;
  openMo('moReturn');
  applyStage();
}

function copyRet(){
  navigator.clipboard.writeText(retLinkVal).catch(function(){
    var t=document.createElement('textarea');t.value=retLinkVal;document.body.appendChild(t);t.select();document.execCommand('copy');document.body.removeChild(t);
  });
  var btn=document.querySelector('#moReturn .ret-btn');
  if(btn){btn.textContent='✓ Copied!';setTimeout(function(){btn.textContent='📋 Copy Return Link';},2000);}
}

// ════════════════════════════════════════
// URL STATE ENCODING
// ════════════════════════════════════════
function buildLink(toStage){
  var fd={};
  document.querySelectorAll('#uForm input:not([type=hidden]), #uForm select, #uForm textarea').forEach(function(el){ if(el.name && el.value) fd[el.name]=el.value; });
  var payload={stage:toStage, type:curType, ref:curRef, fd:fd, sigs:sigStore};
  try{
    var b64=btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
    return window.location.origin+'/agent/unified-submit?wtp='+b64;
  }catch(e){ return window.location.origin+'/agent/unified-submit'; }
}

// ════════════════════════════════════════
// SAVE (AJAX POST)
// ════════════════════════════════════════
function doSave(action){
  document.getElementById('h_action').value=action;
  document.getElementById('h_sigs').value=JSON.stringify(sigStore);
  document.getElementById('h_type').value=curType;
  document.getElementById('h_ref').value=curRef;
  document.getElementById('h_stage').value=curStage;
  document.getElementById('uForm').submit();
}

// ════════════════════════════════════════
// MODALS
// ════════════════════════════════════════
function openMo(id){ var el=document.getElementById(id); if(el) el.classList.add('open'); else console.error('Modal not found:',id); }
function closeMo(id){ document.getElementById(id).classList.remove('open'); }
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.mo').forEach(function(mo){
    mo.addEventListener('click', function(e){ if(e.target===mo) mo.classList.remove('open'); });
  });
});

// ════════════════════════════════════════
// NAV
// ════════════════════════════════════════
function toggleNav(){ document.getElementById('mainNav').classList.toggle('open'); }
document.addEventListener('DOMContentLoaded',function(){
  document.querySelectorAll('#mainNav a').forEach(function(a){
    a.addEventListener('click',function(){ document.getElementById('mainNav').classList.remove('open'); });
  });
});
</script>
</body>
</html>"""


# ── Agent Unified Submissions List ─────────────────────────
UNIFIED_SUBMISSIONS_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>My Unified Submissions – WTP</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:Arial,sans-serif;margin:0;background:#f0f2f5;color:#1a2a3a}
.topbar{background:#1a3a2a;color:white;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:8px;position:sticky;top:0;z-index:100}
.topbar-title{font-size:1rem;font-weight:700}
.topbar-right a{color:#86efac;text-decoration:none;font-size:13px}
.hamburger{display:none;background:none;border:none;color:white;font-size:22px;cursor:pointer;padding:2px 6px}
.nav-bar{background:white;padding:10px 16px;display:flex;flex-wrap:wrap;gap:4px;align-items:center;box-shadow:0 2px 6px rgba(0,0,0,.08)}
.nav-bar a{color:#16a34a;text-decoration:none;font-weight:600;font-size:13px;padding:5px 10px;border-radius:6px;white-space:nowrap;transition:background .15s}
.nav-bar a:hover{background:#f0fdf4}
.nav-bar a.nav-btn{background:#16a34a;color:white}
.nav-bar a.nav-active{background:#f0fdf4;color:#15803d}
.nav-bar a.nav-logout{color:#dc3545}
@media(max-width:640px){.hamburger{display:block}.nav-bar{display:none;flex-direction:column;align-items:stretch;padding:8px 12px;gap:2px}.nav-bar.open{display:flex}.nav-bar a{padding:10px 12px;font-size:14px;border-bottom:1px solid #f0f0f0}}
.wrap{max-width:1200px;margin:0 auto;padding:16px}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13px;font-weight:600}
.flash-ok{background:#dcfce7;color:#166534;border:1px solid #86efac}
.flash-err{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:14px}
.scard{background:white;border-radius:10px;padding:13px 15px;box-shadow:0 1px 4px rgba(0,0,0,.08);border-top:3px solid #ddd}
.scard h3{margin:0 0 5px;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.scard-val{font-size:1.35rem;font-weight:800}
.filter-wrap{background:white;border-radius:10px;padding:13px 15px;margin-bottom:13px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.filter-wrap h3{margin:0 0 9px;font-size:13px;color:#444;font-weight:700}
.filter-row{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.filter-row select,.filter-row input{padding:7px 10px;border:1px solid #ddd;border-radius:6px;font-size:13px;flex:1;min-width:110px}
.btn-go{padding:7px 14px;background:#16a34a;color:white;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600}
.btn-clr{padding:7px 12px;background:#6c757d;color:white;border:none;border-radius:6px;font-size:13px;text-decoration:none}
.sec-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px}
.sec-hdr h2{margin:0;font-size:15px}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:14px}
table{width:100%;border-collapse:collapse;background:white;min-width:540px}
th{background:#1a3a2a;color:white;padding:10px 12px;text-align:left;font-size:12px;white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f9fafb}
.badge{padding:3px 8px;border-radius:10px;font-size:11px;font-weight:700;white-space:nowrap}
.t-np{background:#dbeafe;color:#1e40af}
.t-ss{background:#f3e8ff;color:#6b21a8}
.t-rn{background:#ffedd5;color:#9a3412}
.s-draft{background:#fef9c3;color:#854d0e}
.s-1{background:#dbeafe;color:#1e40af}
.s-2{background:#dcfce7;color:#166534}
.s-3{background:#dbeafe;color:#1e40af}
.s-4{background:#dcfce7;color:#166534}
.act-btn{display:inline-block;padding:5px 10px;border:none;border-radius:5px;font-size:12px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap;margin:2px 0}
.act-green{background:#16a34a;color:white}
.act-teal{background:#0891b2;color:white}
.act-red{background:#dc3545;color:white}
.empty{padding:36px;text-align:center;background:white;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.empty h3{color:#666;margin-bottom:7px}.empty p{color:#888;margin:0 0 14px}
@media(max-width:640px){.wrap{padding:10px}.filter-row{flex-direction:column;align-items:stretch}.filter-row select,.filter-row input,.btn-go,.btn-clr{width:100%}td,th{padding:8px 10px!important;font-size:12px}}
</style>
</head>
<body>
<div class="topbar">
  <div class="topbar-title">&#128203; Submissions</div>
  <div class="topbar-right"><button class="hamburger" onclick="toggleNav()" aria-label="Menu">☰</button></div>
</div>
<div class="nav-bar" id="mainNav">
  <a href="/admin/dashboard">&#128202; Dashboard</a>
  <a href="/admin/projects">&#127962; Projects</a>
  <a href="/admin/create-project" class="nav-btn">&#10010; New Project</a>
  <a href="/admin/agents">&#128101; Agents</a>
  <a href="/admin/agent-hierarchy">&#128279; Hierarchy</a>
  <a href="/admin/payments">&#9993; Payments</a>
  <a href="/admin/commissions">&#128176; Commissions</a>
  <a href="/admin/unified-submissions" class="nav-active">&#128203; Unified Submissions</a>
  <a href="/admin/agent-performance">&#128200; Performance</a>
  <a href="/admin/commission-calculator">&#9889; Calc</a>
  <a href="/admin/settings">&#9881; Settings</a>
  <a href="/admin/export-data">&#128228; Export</a>
</div>
<div class="wrap">

{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat,msg in messages %}
<div class="flash flash-{{ 'ok' if cat=='success' else 'err' }}">{{ msg }}</div>
{% endfor %}{% endif %}{% endwith %}

<div class="stats-grid">
  <div class="scard" style="border-top-color:#16a34a"><h3>Total</h3><div class="scard-val" style="color:#16a34a">{{ stats.total }}</div></div>
  <div class="scard" style="border-top-color:#1e40af"><h3>New Project</h3><div class="scard-val" style="color:#1e40af">{{ stats.np }}</div></div>
  <div class="scard" style="border-top-color:#6b21a8"><h3>Sub Sales</h3><div class="scard-val" style="color:#6b21a8">{{ stats.ss }}</div></div>
  <div class="scard" style="border-top-color:#9a3412"><h3>Rental</h3><div class="scard-val" style="color:#9a3412">{{ stats.rn }}</div></div>
  <div class="scard" style="border-top-color:#16a34a"><h3>Complete</h3><div class="scard-val" style="color:#16a34a">{{ stats.done }}</div></div>
  <div class="scard" style="border-top-color:#854d0e"><h3>In Progress</h3><div class="scard-val" style="color:#854d0e">{{ stats.prog }}</div></div>
</div>

<div class="filter-wrap">
  <h3>🔍 Filter</h3>
  <form method="GET" class="filter-row">
    <select name="type">
      <option value="all" {{ 'selected' if type_filter=='all' }}>All Types</option>
      <option value="np"  {{ 'selected' if type_filter=='np' }}>🏢 New Project</option>
      <option value="ss"  {{ 'selected' if type_filter=='ss' }}>🏠 Sub Sales</option>
      <option value="rn"  {{ 'selected' if type_filter=='rn' }}>🔑 Rental</option>
    </select>
    <select name="stage">
      <option value="all" {{ 'selected' if stage_filter=='all' }}>All Stages</option>
      <option value="0"   {{ 'selected' if stage_filter=='0' }}>Draft</option>
      <option value="1"   {{ 'selected' if stage_filter=='1' }}>Awaiting Party A</option>
      <option value="2"   {{ 'selected' if stage_filter=='2' }}>Party A Signed</option>
      <option value="3"   {{ 'selected' if stage_filter=='3' }}>Awaiting Party B</option>
      <option value="4"   {{ 'selected' if stage_filter=='4' }}>Complete</option>
    </select>
    <input type="text" name="search" placeholder="Search ref, address, name..." value="{{ search }}">
    <button type="submit" class="btn-go">🔍 Filter</button>
    <a href="/agent/unified-submissions" class="btn-clr">Clear</a>
  </form>
</div>

<div class="sec-hdr">
  <h2>📋 Submissions ({{ submissions|length }})</h2>
  <a href="/agent/unified-submit" class="act-btn act-green">&#10010; New Sale</a>
</div>

{% if submissions %}
<div class="tbl-wrap"><table>
  <thead><tr><th>Ref</th><th>Type</th><th>Property</th><th>Party A</th><th>Party B</th><th>Stage</th><th>Updated</th><th>Actions</th></tr></thead>
  <tbody>
  {% for s in submissions %}
  {% set type_labels = {'np':'🏢 New Project','ss':'🏠 Sub Sales','rn':'🔑 Rental'} %}
  {% set stage_labels = {0:'Draft',1:'Awaiting Party A',2:'Party A Signed',3:'Awaiting Party B',4:'✓ Complete'} %}
  <tr>
    <td><strong>{{ s.ref or '—' }}</strong></td>
    <td><span class="badge t-{{ s.sub_type }}">{{ type_labels.get(s.sub_type,'—') }}</span></td>
    <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ s.prop_address or '—' }}</td>
    <td>{{ s.partyA_name or '—' }}</td>
    <td>{{ s.partyB_name or '—' }}</td>
    <td><span class="badge s-{{ s.stage }}">{{ stage_labels.get(s.stage,'—') }}</span></td>
    <td>{{ s.updated_at[:10] if s.updated_at else '—' }}</td>
    <td>
      <a href="/agent/unified-submit/{{ s.id }}" class="act-btn act-teal">📂 Open</a>
      {% if s.stage == 0 %}
      <a href="/agent/unified-submit/{{ s.id }}/delete" class="act-btn act-red" onclick="return confirm('Delete this submission?')">🗑</a>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table></div>
{% else %}
<div class="empty">
  <h3>No submissions found</h3>
  <p>{% if type_filter!='all' or stage_filter!='all' or search %}Try clearing the filters.{% else %}Start your first unified submission.{% endif %}</p>
  <a href="/agent/unified-submit" class="act-btn act-green" style="padding:9px 18px;font-size:13px">&#10010; New Sale</a>
</div>
{% endif %}
</div>
<script>
function toggleNav(){document.getElementById('mainNav').classList.toggle('open');}
document.addEventListener('DOMContentLoaded',function(){document.querySelectorAll('#mainNav a').forEach(function(a){a.addEventListener('click',function(){document.getElementById('mainNav').classList.remove('open');});});});
</script>
</body>
</html>"""


# ── Admin Unified Submissions View ──────────────────────────
ADMIN_UNIFIED_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Unified Submissions – Admin</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:Arial,sans-serif;margin:0;background:#f0f2f5;color:#1a2a3a}
.topbar{background:#2c3e50;color:white;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:8px;position:sticky;top:0;z-index:100}
.topbar-title{font-size:1rem;font-weight:700}
.topbar-right{display:flex;align-items:center;gap:10px}
.topbar-right a{color:#a8c8ff;text-decoration:none;font-size:13px}
.hamburger{display:none;background:none;border:none;color:white;font-size:22px;cursor:pointer;padding:2px 6px}
.nav-bar{background:white;padding:10px 16px;display:flex;flex-wrap:wrap;gap:4px;align-items:center;box-shadow:0 2px 6px rgba(0,0,0,.08)}
.nav-bar a{color:#007bff;text-decoration:none;font-weight:600;font-size:13px;padding:5px 10px;border-radius:6px;white-space:nowrap;transition:background .15s}
.nav-bar a:hover{background:#f0f7ff}
.nav-bar a.nav-btn{background:#2563eb;color:white}
.nav-bar a.nav-active{background:#eff6ff;color:#1d4ed8}
.nav-bar a.nav-logout{color:#dc3545}
@media(max-width:640px){.hamburger{display:block}.nav-bar{display:none;flex-direction:column;align-items:stretch;padding:8px 12px;gap:2px}.nav-bar.open{display:flex}.nav-bar a{padding:10px 12px;font-size:14px;border-bottom:1px solid #f0f0f0}}
.wrap{max-width:1400px;margin:0 auto;padding:16px}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13px;font-weight:600}
.flash-ok{background:#d4edda;color:#155724;border:1px solid #c3e6cb}
.flash-err{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px}
.scard{background:white;border-radius:10px;padding:13px 15px;box-shadow:0 1px 4px rgba(0,0,0,.08);border-top:3px solid #ddd}
.scard h3{margin:0 0 5px;font-size:10px;color:#888;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.scard-val{font-size:1.35rem;font-weight:800}
.filter-wrap{background:white;border-radius:10px;padding:13px 15px;margin-bottom:13px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.filter-wrap h3{margin:0 0 9px;font-size:13px;color:#444;font-weight:700}
.filter-row{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.filter-row select,.filter-row input{padding:7px 10px;border:1px solid #ddd;border-radius:6px;font-size:13px;flex:1;min-width:110px}
.btn-filter{padding:7px 14px;background:#007bff;color:white;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600}
.btn-clear{padding:7px 12px;background:#6c757d;color:white;border:none;border-radius:6px;font-size:13px;text-decoration:none}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.08);margin-bottom:14px}
table{width:100%;border-collapse:collapse;background:white;min-width:700px}
th{background:#2c3e50;color:white;padding:10px 12px;text-align:left;font-size:12px;white-space:nowrap}
td{padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#f9fafb}
.badge{padding:3px 8px;border-radius:10px;font-size:11px;font-weight:700;white-space:nowrap}
.t-np{background:#dbeafe;color:#1e40af}
.t-ss{background:#f3e8ff;color:#6b21a8}
.t-rn{background:#ffedd5;color:#9a3412}
.s-0{background:#e2e3e5;color:#383d41}
.s-1{background:#cce5ff;color:#004085}
.s-2{background:#d4edda;color:#155724}
.s-3{background:#cce5ff;color:#004085}
.s-4{background:#d4edda;color:#155724}
.act-btn{display:inline-block;padding:4px 9px;border:none;border-radius:4px;font-size:11px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap;margin:2px 0}
.act-blue{background:#007bff;color:white}
.empty{padding:36px;text-align:center;background:white;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.empty h3{color:#666;margin-bottom:7px}.empty p{color:#888;margin:0}
@media(max-width:640px){.wrap{padding:10px}.filter-row{flex-direction:column;align-items:stretch}.filter-row select,.filter-row input,.btn-filter,.btn-clear{width:100%}td,th{padding:8px 10px!important;font-size:12px}}
</style>
</head>
<body>
<div class="topbar">
  <div class="topbar-title">📋 Unified Submissions – Admin</div>
  <div class="topbar-right">
    <span style="font-size:13px">{{ admin_name }}</span>
    <a href="/logout">🔒 Logout</a>
    <button class="hamburger" onclick="toggleNav()" aria-label="Menu">☰</button>
  </div>
</div>
<div class="nav-bar" id="mainNav">
  <a href="/admin/dashboard">&#128202; Dashboard</a>
  <a href="/admin/projects">&#127962; Projects</a>
  <a href="/admin/create-project" class="nav-btn">&#10010; New Project</a>
  <a href="/admin/agents">&#128101; Agents</a>
  <a href="/admin/agent-hierarchy">&#128279; Hierarchy</a>
  <a href="/admin/payments">&#9993; Payments</a>
  <a href="/admin/commissions">&#128176; Commissions</a>
  <a href="/admin/unified-submissions" class="nav-active">&#128203; Unified Submissions</a>
  <a href="/admin/agent-performance">&#128200; Performance</a>
  <a href="/admin/commission-calculator">&#9889; Calc</a>
  <a href="/admin/settings">&#9881; Settings</a>
  <a href="/admin/export-data">&#128228; Export</a>
</div>
<div class="wrap">

{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat,msg in messages %}
<div class="flash flash-{{ 'ok' if cat=='success' else 'err' }}">{{ msg }}</div>
{% endfor %}{% endif %}{% endwith %}

<div class="stats-grid">
  <div class="scard" style="border-top-color:#007bff"><h3>Total</h3><div class="scard-val" style="color:#007bff">{{ stats.total }}</div></div>
  <div class="scard" style="border-top-color:#1e40af"><h3>New Project</h3><div class="scard-val" style="color:#1e40af">{{ stats.np }}</div></div>
  <div class="scard" style="border-top-color:#6b21a8"><h3>Sub Sales</h3><div class="scard-val" style="color:#6b21a8">{{ stats.ss }}</div></div>
  <div class="scard" style="border-top-color:#9a3412"><h3>Rental</h3><div class="scard-val" style="color:#9a3412">{{ stats.rn }}</div></div>
  <div class="scard" style="border-top-color:#28a745"><h3>Complete</h3><div class="scard-val" style="color:#28a745">{{ stats.done }}</div></div>
  <div class="scard" style="border-top-color:#ffc107"><h3>In Progress</h3><div class="scard-val" style="color:#ffc107">{{ stats.prog }}</div></div>
</div>

<div class="filter-wrap">
  <h3>🔍 Filter Submissions</h3>
  <form method="GET" class="filter-row">
    <select name="type">
      <option value="all" {{ 'selected' if type_filter=='all' }}>All Types</option>
      <option value="np"  {{ 'selected' if type_filter=='np' }}>🏢 New Project</option>
      <option value="ss"  {{ 'selected' if type_filter=='ss' }}>🏠 Sub Sales</option>
      <option value="rn"  {{ 'selected' if type_filter=='rn' }}>🔑 Rental</option>
    </select>
    <select name="stage">
      <option value="all" {{ 'selected' if stage_filter=='all' }}>All Stages</option>
      <option value="0"   {{ 'selected' if stage_filter=='0' }}>Draft</option>
      <option value="4"   {{ 'selected' if stage_filter=='4' }}>Complete</option>
    </select>
    <input type="text" name="agent" placeholder="Filter by agent name..." value="{{ agent_filter }}">
    <input type="text" name="search" placeholder="Search ref, property, party..." value="{{ search }}">
    <button type="submit" class="btn-filter">🔍 Filter</button>
    <a href="/admin/unified-submissions" class="btn-clear">Clear</a>
  </form>
</div>

{% if submissions %}
<div class="tbl-wrap"><table>
  <thead><tr><th>Ref</th><th>Type</th><th>Agent</th><th>Rank</th><th>Property</th><th>Party A</th><th>Party B</th><th>Stage</th><th>Updated</th><th>Action</th></tr></thead>
  <tbody>
  {% for s in submissions %}
  {% set type_labels = {'np':'🏢 New Project','ss':'🏠 Sub Sales','rn':'🔑 Rental'} %}
  {% set stage_labels = {0:'Draft',1:'Awaiting Party A',2:'Party A Signed',3:'Awaiting Party B',4:'✓ Complete'} %}
  <tr>
    <td><strong>{{ s.ref or '—' }}</strong></td>
    <td><span class="badge t-{{ s.sub_type }}">{{ type_labels.get(s.sub_type,'—') }}</span></td>
    <td>{{ s.agent_name or '—' }}</td>
    <td><small style="color:#888">{{ s.agent_rank or '—' }}</small></td>
    <td style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ s.prop_address or '—' }}</td>
    <td>{{ s.partyA_name or '—' }}</td>
    <td>{{ s.partyB_name or '—' }}</td>
    <td><span class="badge s-{{ s.stage }}">{{ stage_labels.get(s.stage,'—') }}</span></td>
    <td>{{ s.updated_at[:10] if s.updated_at else '—' }}</td>
    <td><a href="/admin/unified-submission/{{ s.id }}" class="act-btn act-blue">👁 View</a></td>
  </tr>
  {% endfor %}
  </tbody>
</table></div>
{% else %}
<div class="empty"><h3>No submissions found</h3><p>Try adjusting your filters.</p></div>
{% endif %}
</div>
<script>
function toggleNav(){document.getElementById('mainNav').classList.toggle('open');}
document.addEventListener('DOMContentLoaded',function(){document.querySelectorAll('#mainNav a').forEach(function(a){a.addEventListener('click',function(){document.getElementById('mainNav').classList.remove('open');});});});
</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════
# DATABASE — add this call inside your init_database() function
# at the end, just before conn.commit()
# ════════════════════════════════════════════════════════════

def init_submissions_table():
    """
    Call this inside init_database() to create the unified submissions table.
    Add this line near the end of init_database(), before conn.commit():

        init_submissions_table()
    """
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS unified_documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_id      TEXT NOT NULL REFERENCES unified_submissions(id) ON DELETE CASCADE,
            filename    TEXT NOT NULL,
            filepath    TEXT NOT NULL,
            file_type   TEXT,
            file_size   INTEGER,
            doc_label   TEXT DEFAULT 'Supporting Document',
            uploaded_by INTEGER,
            uploaded_at TIMESTAMP DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS unified_submissions (
            id           TEXT PRIMARY KEY,
            ref          TEXT,
            sub_type     TEXT NOT NULL DEFAULT 'ss',
            stage        INTEGER DEFAULT 0,
            agent_id     INTEGER REFERENCES users(id),
            agent_name   TEXT,
            agent_rank   TEXT,
            prop_address TEXT,
            prop_type    TEXT,
            prop_state   TEXT,
            partyA_name  TEXT,
            partyA_ic    TEXT,
            partyA_phone TEXT,
            partyA_addr  TEXT,
            partyB_name  TEXT,
            partyB_ic    TEXT,
            partyB_phone TEXT,
            partyB_addr  TEXT,
            price        TEXT,
            extra_data   TEXT DEFAULT '{}',
            signatures   TEXT DEFAULT '{}',
            created_at   DATETIME DEFAULT (datetime('now')),
            updated_at   DATETIME DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def get_next_ref(sub_type):
    """Auto-generate next reference number for a given type."""
    prefixes = {'np': 'NP', 'ss': 'SC', 'rn': 'RN'}
    pfx = prefixes.get(sub_type, 'SC')
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT ref FROM unified_submissions WHERE sub_type=? AND ref IS NOT NULL",
        (sub_type,)
    ).fetchall()
    conn.close()
    max_n = 0
    for row in rows:
        try:
            n = int(row[0].replace(pfx, ''))
            max_n = max(max_n, n)
        except Exception:
            pass
    return "{}{}".format(pfx, str(max_n + 1).zfill(4))


def get_submissions_for_user(user_id, agent_rank, role,
                              sub_type='all', stage_filter='all',
                              search='', agent_filter=''):
    """
    Fetch unified submissions filtered by the user's rank/role.
    Returns list of Row objects.
    """
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    base = "SELECT * FROM unified_submissions"
    conditions = []
    params = []

    # ── Visibility by role/rank ──
    if role == 'admin':
        pass  # sees everything
    elif agent_rank == 'ATL':
        # Full downline (recursive)
        downline = conn.execute("""
            WITH RECURSIVE dl(id) AS (
                SELECT id FROM users WHERE upline_id=?
                UNION ALL
                SELECT u.id FROM users u JOIN dl ON u.upline_id=dl.id
            )
            SELECT id FROM dl
        """, (user_id,)).fetchall()
        ids = [user_id] + [r[0] for r in downline]
        conditions.append("agent_id IN ({})".format(','.join('?' * len(ids))))
        params.extend(ids)
    elif agent_rank in ('TL', 'Elite REN'):
        # Direct team + self
        direct = conn.execute(
            "SELECT id FROM users WHERE upline_id=?", (user_id,)
        ).fetchall()
        ids = [user_id] + [r[0] for r in direct]
        conditions.append("agent_id IN ({})".format(','.join('?' * len(ids))))
        params.extend(ids)
    else:
        conditions.append("agent_id=?")
        params.append(user_id)

    # ── Type filter ──
    if sub_type and sub_type != 'all':
        conditions.append("sub_type=?")
        params.append(sub_type)

    # ── Stage filter ──
    if stage_filter and stage_filter != 'all':
        conditions.append("stage=?")
        params.append(int(stage_filter))

    # ── Search ──
    if search:
        conditions.append("(ref LIKE ? OR prop_address LIKE ? OR partyA_name LIKE ? OR partyB_name LIKE ?)")
        like = '%' + search + '%'
        params.extend([like, like, like, like])

    # ── Agent name filter (admin only) ──
    if agent_filter and role == 'admin':
        conditions.append("agent_name LIKE ?")
        params.append('%' + agent_filter + '%')

    if conditions:
        base += " WHERE " + " AND ".join(conditions)
    base += " ORDER BY updated_at DESC"

    rows = conn.execute(base, params).fetchall()
    conn.close()
    return rows


def get_submission_stats(user_id, agent_rank, role):
    rows = get_submissions_for_user(user_id, agent_rank, role)
    return {
        'total': len(rows),
        'np':    sum(1 for r in rows if r['sub_type'] == 'np'),
        'ss':    sum(1 for r in rows if r['sub_type'] == 'ss'),
        'rn':    sum(1 for r in rows if r['sub_type'] == 'rn'),
        'done':  sum(1 for r in rows if r['stage'] == 4),
        'prog':  sum(1 for r in rows if 0 < r['stage'] < 4),
    }


# ════════════════════════════════════════════════════════════
# FLASK ROUTES
# ════════════════════════════════════════════════════════════

@app.route("/agent/unified-submit", methods=["GET", "POST"])
@app.route("/agent/unified-submit/<sub_id>", methods=["GET", "POST"])
def unified_submit(sub_id=None):
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    user = conn.execute(
        "SELECT id, name, agent_rank FROM users WHERE id=?",
        (session["user_id"],)
    ).fetchone()
    conn.close()

    agent_name = user["name"] if user else session.get("user_name", "")
    agent_rank = user["agent_rank"] if user else "REN"

    # ── Build next_refs for JS auto-ref (pass next number per type) ──
    def _next_num(t):
        ref_str = get_next_ref(t)
        pfx = {'np':'NP','ss':'SC','rn':'RN'}.get(t,'SC')
        try: return int(ref_str.replace(pfx,''))
        except: return 1
    next_refs = {t: _next_num(t) for t in ['np', 'ss', 'rn']}

    # ── Default empty form values ──
    empty_form  = {}
    sub_id_val  = None
    sub_stage   = 0
    sub_ref     = get_next_ref('ss')
    sub_type    = 'ss'
    sig_data    = {}   # pass as dict, Jinja tojson renders it as {}

    # ── Load from URL payload (shared signing link) ──
    wtp_payload = request.args.get("wtp")
    if wtp_payload:
        try:
            import base64
            raw = base64.b64decode(wtp_payload).decode("utf-8")
            payload = json.loads(raw)
            sub_stage  = payload.get("stage", 0)
            sub_type   = payload.get("type", "ss")
            sub_ref    = payload.get("ref", next_refs.get(sub_type, "SC0001"))
            empty_form = payload.get("fd", {})
            sig_data   = payload.get("sigs", {})

            # If there is also a sub_id, load the DB record for the id/ref
            # but KEEP the wtp payload's stage and signatures (they are more up to date)
            if sub_id:
                conn = get_db_connection()
                conn.row_factory = sqlite3.Row
                rec = conn.execute(
                    "SELECT * FROM unified_submissions WHERE id=?", (sub_id,)
                ).fetchone()
                conn.close()
                if rec:
                    sub_id_val = rec["id"]
                    # Update DB with the returned stage + signatures
                    conn2 = get_db_connection()
                    conn2.execute(
                        "UPDATE unified_submissions SET stage=?, signatures=?, updated_at=datetime('now') WHERE id=?",
                        (sub_stage, json.dumps(sig_data), sub_id)
                    )
                    conn2.commit()
                    conn2.close()
                    # Redirect to clean URL — loads full DB record with all fields
                    from flask import redirect as _redir
                    return _redir("/agent/unified-submit/" + sub_id)
        except Exception as e:
            flash("Could not load form from link. Please try again.", "error")

    # ── Load existing record by ID (no wtp payload) ──
    elif sub_id:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        rec = conn.execute(
            "SELECT * FROM unified_submissions WHERE id=? AND agent_id=?",
            (sub_id, session["user_id"])
        ).fetchone()
        conn.close()
        if rec:
            sub_id_val  = rec["id"]
            sub_stage   = rec["stage"]
            sub_ref     = rec["ref"] or next_refs.get(rec["sub_type"], "SC0001")
            sub_type    = rec["sub_type"]
            try:
                sig_data = json.loads(rec["signatures"] or "{}")
            except Exception:
                sig_data = {}
            extra       = json.loads(rec["extra_data"] or "{}")
            empty_form  = {
                "prop_address": rec["prop_address"] or "",
                "prop_type":    rec["prop_type"]    or "",
                "prop_state":   rec["prop_state"]   or "",
                "partyA_name":  rec["partyA_name"]  or "",
                "partyA_ic":    rec["partyA_ic"]    or "",
                "partyA_phone": rec["partyA_phone"] or "",
                "partyA_addr":  rec["partyA_addr"]  or "",
                "partyB_name":  rec["partyB_name"]  or "",
                "partyB_ic":    rec["partyB_ic"]    or "",
                "partyB_phone": rec["partyB_phone"] or "",
                "partyB_addr":  rec["partyB_addr"]  or "",
            }
            empty_form.update(extra)
        else:
            flash("Submission not found.", "error")
            return redirect("/agent/unified-submissions")

    # ── Handle POST (save) ──
    if request.method == "POST":
        action    = request.form.get("action", "save")
        f_type    = request.form.get("sub_type", "ss")
        f_ref     = request.form.get("sub_ref", "").strip().upper() or get_next_ref(f_type)
        f_stage   = int(request.form.get("sub_stage", 0))
        f_id      = request.form.get("sub_id", "").strip() or str(uuid.uuid4())
        f_sigs    = request.form.get("sig_data", "{}")

        # Common fields
        prop_address = request.form.get("prop_address", "")
        prop_type    = request.form.get("prop_type", "")
        prop_state   = request.form.get("prop_state", "")
        partyA_name  = request.form.get("partyA_name", "")
        partyA_ic    = request.form.get("partyA_ic", "")
        partyA_phone = request.form.get("partyA_phone", "")
        partyA_addr  = request.form.get("partyA_addr", "")
        partyB_name  = request.form.get("partyB_name", "")
        partyB_ic    = request.form.get("partyB_ic", "")
        partyB_phone = request.form.get("partyB_phone", "")
        partyB_addr  = request.form.get("partyB_addr", "")

        # Type-specific fields → extra_data
        extra_keys = [
            "np_project","np_developer","np_unit","np_price","np_booking",
            "np_bumi","np_handover","np_remarks",
            "ss_price","ss_price_words","ss_dep_pct","ss_dep_amt",
            "ss_cheque","ss_spa_date","ss_special",
            "rn_rent","rn_sec_dep","rn_util_dep","rn_period",
            "rn_start","rn_end","rn_furnished","rn_stamp","rn_key_dep","rn_special",
        ]
        extra_data = {k: request.form.get(k, "") for k in extra_keys if request.form.get(k)}

        # Derive display price
        price = (extra_data.get("ss_price")
                 or extra_data.get("np_price")
                 or extra_data.get("rn_rent") or "")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db_connection()
        existing = conn.execute(
            "SELECT id FROM unified_submissions WHERE id=?", (f_id,)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE unified_submissions SET
                  ref=?, sub_type=?, stage=?,
                  prop_address=?, prop_type=?, prop_state=?,
                  partyA_name=?, partyA_ic=?, partyA_phone=?, partyA_addr=?,
                  partyB_name=?, partyB_ic=?, partyB_phone=?, partyB_addr=?,
                  price=?, extra_data=?, signatures=?, updated_at=?
                WHERE id=?
            """, (
                f_ref, f_type, f_stage,
                prop_address, prop_type, prop_state,
                partyA_name, partyA_ic, partyA_phone, partyA_addr,
                partyB_name, partyB_ic, partyB_phone, partyB_addr,
                price, json.dumps(extra_data), f_sigs, now,
                f_id
            ))
        else:
            conn.execute("""
                INSERT INTO unified_submissions
                  (id, ref, sub_type, stage, agent_id, agent_name, agent_rank,
                   prop_address, prop_type, prop_state,
                   partyA_name, partyA_ic, partyA_phone, partyA_addr,
                   partyB_name, partyB_ic, partyB_phone, partyB_addr,
                   price, extra_data, signatures, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                f_id, f_ref, f_type, f_stage,
                session["user_id"], agent_name, agent_rank,
                prop_address, prop_type, prop_state,
                partyA_name, partyA_ic, partyA_phone, partyA_addr,
                partyB_name, partyB_ic, partyB_phone, partyB_addr,
                price, json.dumps(extra_data), f_sigs, now, now
            ))
        conn.commit()
        conn.close()

        if action == "draft":
            flash("Draft saved successfully.", "success")
        elif action == "complete":
            flash("Submission saved as complete! 🎉", "success")
        elif action == "stage":
            pass  # silent save during stage transitions

        return redirect("/agent/unified-submit/" + f_id)

    # ── Load active projects for selector ──
    try:
        conn_p = get_db_connection()
        conn_p.row_factory = sqlite3.Row
        projects_list = conn_p.execute(            "SELECT p.id, p.project_name, p.category, p.project_type, p.project_sale_type, p.location, p.commission_rate, pu.unit_type, pu.base_price, pu.rental_price, pu.square_feet FROM projects p LEFT JOIN project_units pu ON pu.project_id = p.id AND pu.status = 'available' WHERE p.status = 'active' ORDER BY p.project_name, pu.unit_type"
        ).fetchall()
        conn_p.close()
        # Group units under each project
        projects_map = {}
        for row in projects_list:
            pid = row['id']
            if pid not in projects_map:
                projects_map[pid] = {
                    'id':            row['id'],
                    'project_name':  row['project_name'],
                    'category':      row['category'],
                    'project_type':  row['project_type'],
                    'sale_type':     row['project_sale_type'] or 'sales',
                    'location':      row['location'] or '',
                    'commission_rate': row['commission_rate'] or '',
                    'units': []
                }
            if row['unit_type']:
                projects_map[pid]['units'].append({
                    'unit_type':   row['unit_type'],
                    'base_price':  row['base_price'] or '',
                    'rental_price':row['rental_price'] or '',
                    'sqft':        row['square_feet'] or '',
                })
        projects_for_template = list(projects_map.values())
    except Exception:
        projects_for_template = []

    # Load existing documents for this submission
    existing_docs = []
    if sub_id_val:
        try:
            conn_d = get_db_connection()
            conn_d.row_factory = sqlite3.Row
            existing_docs = conn_d.execute(
                "SELECT id, filename, file_type, file_size, doc_label, uploaded_at FROM unified_documents WHERE sub_id=? ORDER BY uploaded_at",
                (sub_id_val,)
            ).fetchall()
            conn_d.close()
        except Exception:
            existing_docs = []

    return render_template(
        'agent/unified_submit.html',
        agent_name  = agent_name,
        agent_rank  = agent_rank,
        next_refs   = next_refs,
        sub_id      = sub_id_val,
        sub_stage   = sub_stage,
        sub_ref     = sub_ref,
        sub_type    = sub_type,
        sig_data    = sig_data,
        form        = empty_form,
        projects    = projects_for_template,
        docs        = existing_docs,
    )




@app.route("/agent/unified-submit/<sub_id>/submit-approval", methods=["POST"])
def unified_submit_for_approval(sub_id):
    """Convert a completed unified submission into a property_listings record for admin approval."""
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rec = conn.execute(
        "SELECT * FROM unified_submissions WHERE id=? AND agent_id=?",
        (sub_id, session["user_id"])
    ).fetchone()
    conn.close()

    if not rec:
        flash("Submission not found.", "error")
        return redirect("/agent/unified-submissions")

    if rec["stage"] < 2:
        flash("Submission must be at least at stage 2 (Party A signed) before submitting for approval.", "error")
        return redirect("/agent/unified-submit/" + sub_id)

    # Check if already submitted
    conn = get_db_connection()
    existing = conn.execute(
        "SELECT id FROM property_listings WHERE notes LIKE ?",
        ('%unified:' + sub_id + '%',)
    ).fetchone()
    conn.close()

    if existing:
        flash("This submission has already been submitted for approval (Listing #{}).".format(existing[0]), "warning")
        return redirect("/agent/unified-submit/" + sub_id)

    # Parse extra data
    try:
        extra = json.loads(rec["extra_data"] or "{}")
    except Exception:
        extra = {}

    # Determine sale type and price
    sub_type   = rec["sub_type"] or "ss"
    sale_type  = "rental" if sub_type == "rn" else "sales"
    price_raw  = (extra.get("ss_price") or extra.get("np_price") or
                  extra.get("rn_rent")  or rec["price"] or "0")
    try:
        sale_price = float(str(price_raw).replace(",", "").replace("RM", "").strip())
    except Exception:
        sale_price = 0.0

    # Commission rate — default 2%
    commission_rate = 0.02
    commission_amount = max(1000.0, min(sale_price * commission_rate, 50000.0)) if sale_price else 0.0

    # Customer = Party B (Purchaser/Tenant/Buyer)
    customer_name  = rec["partyB_name"]  or ""
    customer_phone = rec["partyB_phone"] or ""

    # Build notes with unified submission ref so we can link back
    notes = "Ref: {} | Type: {} | unified:{}".format(
        rec["ref"] or "", sub_type.upper(), sub_id
    )
    if extra.get("ss_special"):  notes += " | " + extra["ss_special"]
    if extra.get("rn_special"):  notes += " | " + extra["rn_special"]
    if extra.get("np_remarks"):  notes += " | " + extra["np_remarks"]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db_connection()
    try:
        # Only use columns guaranteed to exist in original schema
        conn.execute("""
            INSERT INTO property_listings
              (agent_id, customer_name, customer_email, customer_phone,
               property_address, sale_price,
               commission_amount, status, submitted_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session["user_id"],
            customer_name,
            "",
            customer_phone,
            rec["prop_address"] or "",
            sale_price,
            round(commission_amount, 2),
            "submitted",
            now,
            notes,
        ))
        listing_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        # Also mark unified submission as submitted
        conn.execute(
            "UPDATE unified_submissions SET stage=4, updated_at=? WHERE id=?",
            (now, sub_id)
        )
        conn.commit()

        # Notify agent
        try:
            conn.execute("""
                INSERT INTO notifications (user_id, type, title, message, is_read, created_at)
                VALUES (?, 'submission_success', '📋 Submission Sent for Approval',
                        ?, 0, ?)
            """, (
                session["user_id"],
                "Your submission {} (Ref: {}) has been sent to admin for approval as Listing #{}.".format(
                    sub_type.upper(), rec["ref"] or "", listing_id),
                now
            ))
            conn.commit()
        except Exception:
            pass

        flash("✅ Submission sent for admin approval! Listing #{} created.".format(listing_id), "success")
    except Exception as e:
        conn.rollback()
        flash("Error submitting for approval: {}".format(str(e)), "error")
    finally:
        conn.close()

    return redirect("/agent/unified-submit/" + sub_id)



# ── UNIFIED SUBMISSION DOCUMENT UPLOAD ──────────────────────

@app.route("/agent/unified-submit/<sub_id>/upload", methods=["POST"])
def unified_upload_doc(sub_id):
    """Upload a document to a unified submission."""
    if "user_id" not in session or session["user_role"] != "agent":
        return jsonify({"ok": False, "error": "Unauthorised"}), 403

    conn = get_db_connection()
    rec = conn.execute(
        "SELECT id FROM unified_submissions WHERE id=? AND agent_id=?",
        (sub_id, session["user_id"])
    ).fetchone()
    conn.close()

    if not rec:
        return jsonify({"ok": False, "error": "Submission not found"}), 404

    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file"}), 400

    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"ok": False, "error": "Empty file"}), 400

    if not allowed_file(f.filename):
        return jsonify({"ok": False, "error": "File type not allowed. Use PDF, JPG, PNG, DOC."}), 400

    label    = request.form.get("label", "Supporting Document")
    filename = secure_filename(f.filename)
    ext      = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"

    # Save with unique name
    import uuid as _uuid
    unique_name = "{}_{}_{}.{}".format(sub_id[:8], session["user_id"], _uuid.uuid4().hex[:6], ext)
    upload_dir  = os.path.join(app.config["UPLOAD_FOLDER"], "unified")
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, unique_name)
    f.save(filepath)

    conn = get_db_connection()
    conn.execute("""
        INSERT INTO unified_documents
          (sub_id, filename, filepath, file_type, file_size, doc_label, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        sub_id, f.filename, filepath, ext,
        os.path.getsize(filepath), label, session["user_id"]
    ))
    conn.commit()
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    return jsonify({"ok": True, "doc_id": doc_id, "filename": f.filename, "label": label})


@app.route("/agent/unified-submit/<sub_id>/delete-doc/<int:doc_id>", methods=["POST"])
def unified_delete_doc(sub_id, doc_id):
    """Delete a document from a unified submission."""
    if "user_id" not in session or session["user_role"] != "agent":
        return jsonify({"ok": False, "error": "Unauthorised"}), 403

    conn = get_db_connection()
    doc = conn.execute(
        """SELECT d.filepath FROM unified_documents d
           JOIN unified_submissions s ON s.id = d.sub_id
           WHERE d.id=? AND s.id=? AND s.agent_id=?""",
        (doc_id, sub_id, session["user_id"])
    ).fetchone()

    if not doc:
        conn.close()
        return jsonify({"ok": False, "error": "Not found"}), 404

    # Delete file
    try:
        if os.path.exists(doc[0]):
            os.remove(doc[0])
    except Exception:
        pass

    conn.execute("DELETE FROM unified_documents WHERE id=?", (doc_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/agent/unified-submit/<sub_id>/docs")
def unified_get_docs(sub_id):
    """Get list of documents for a unified submission (JSON)."""
    if "user_id" not in session:
        return jsonify({"ok": False}), 403

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    docs = conn.execute(
        """SELECT d.id, d.filename, d.file_type, d.file_size, d.doc_label, d.uploaded_at
           FROM unified_documents d
           JOIN unified_submissions s ON s.id = d.sub_id
           WHERE d.sub_id=?
           ORDER BY d.uploaded_at""",
        (sub_id,)
    ).fetchall()
    conn.close()

    return jsonify({"ok": True, "docs": [dict(d) for d in docs]})

@app.route("/agent/unified-submissions")
def unified_submissions():
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    user = conn.execute(
        "SELECT agent_rank FROM users WHERE id=?", (session["user_id"],)
    ).fetchone()
    conn.close()

    agent_rank  = user["agent_rank"] if user else "REN"
    type_filter  = request.args.get("type",  "all")
    stage_filter = request.args.get("stage", "all")
    search       = request.args.get("search","").strip()

    submissions = get_submissions_for_user(
        session["user_id"], agent_rank, "agent",
        type_filter, stage_filter, search
    )
    stats = get_submission_stats(session["user_id"], agent_rank, "agent")

    return render_template_string(
        UNIFIED_SUBMISSIONS_TEMPLATE,
        submissions  = submissions,
        stats        = stats,
        type_filter  = type_filter,
        stage_filter = stage_filter,
        search       = search,
    )


@app.route("/agent/unified-submit/<sub_id>/delete")
def unified_submit_delete(sub_id):
    if "user_id" not in session or session["user_role"] != "agent":
        return redirect("/login")
    conn = get_db_connection()
    rec = conn.execute(
        "SELECT stage FROM unified_submissions WHERE id=? AND agent_id=?",
        (sub_id, session["user_id"])
    ).fetchone()
    if rec and rec[0] == 0:
        conn.execute("DELETE FROM unified_submissions WHERE id=?", (sub_id,))
        conn.commit()
        flash("Draft deleted.", "success")
    else:
        flash("Only draft submissions can be deleted.", "error")
    conn.close()
    return redirect("/agent/unified-submissions")


@app.route("/admin/unified-submissions")
def admin_unified_submissions():
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")

    type_filter  = request.args.get("type",  "all")
    stage_filter = request.args.get("stage", "all")
    search       = request.args.get("search","").strip()
    agent_filter = request.args.get("agent", "").strip()

    submissions = get_submissions_for_user(
        session["user_id"], "ATL", "admin",
        type_filter, stage_filter, search, agent_filter
    )
    stats = get_submission_stats(session["user_id"], "ATL", "admin")

    return render_template_string(
        ADMIN_UNIFIED_TEMPLATE,
        admin_name   = session.get("user_name", "Admin"),
        submissions  = submissions,
        stats        = stats,
        type_filter  = type_filter,
        stage_filter = stage_filter,
        search       = search,
        agent_filter = agent_filter,
    )


@app.route("/admin/unified-submission/<sub_id>")
def admin_unified_submission_view(sub_id):
    """Admin view of a unified submission with approve/reject actions."""
    if "user_id" not in session or session["user_role"] != "admin":
        return redirect("/login")
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rec = conn.execute(
        "SELECT * FROM unified_submissions WHERE id=?", (sub_id,)
    ).fetchone()
    conn.close()
    if not rec:
        flash("Submission not found.", "error")
        return redirect("/admin/unified-submissions")

    extra = json.loads(rec["extra_data"] or "{}")
    sigs  = {}
    try: sigs = json.loads(rec["signatures"] or "{}")
    except Exception: pass

    # Find linked property_listing if submitted for approval
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    linked = conn.execute(
        "SELECT id, status, sale_price, commission_amount FROM property_listings WHERE notes LIKE ?",
        ('%unified:' + sub_id + '%',)
    ).fetchone()

    # Load docs
    docs = conn.execute(
        "SELECT id, filename, file_type, file_size, doc_label, uploaded_at FROM unified_documents WHERE sub_id=? ORDER BY uploaded_at",
        (sub_id,)
    ).fetchall()
    conn.close()

    form_data = {
        "prop_address": rec["prop_address"] or "",
        "prop_type":    rec["prop_type"]    or "",
        "prop_state":   rec["prop_state"]   or "",
        "partyA_name":  rec["partyA_name"]  or "",
        "partyA_ic":    rec["partyA_ic"]    or "",
        "partyA_phone": rec["partyA_phone"] or "",
        "partyA_addr":  rec["partyA_addr"]  or "",
        "partyB_name":  rec["partyB_name"]  or "",
        "partyB_ic":    rec["partyB_ic"]    or "",
        "partyB_phone": rec["partyB_phone"] or "",
        "partyB_addr":  rec["partyB_addr"]  or "",
    }
    form_data.update(extra)

    return render_template(
        'admin/unified_view.html',
        agent_name = rec["agent_name"] or "",
        agent_rank = rec["agent_rank"] or "REN",
        sub_id     = rec["id"],
        sub_stage  = rec["stage"],
        sub_ref    = rec["ref"] or "",
        sub_type   = rec["sub_type"] or "ss",
        sig_data   = sigs,
        form       = form_data,
        docs       = docs,
        linked     = linked,
    )

# ════════════════════════════════════════════════════════════
# END OF WTP UNIFIED SUBMISSION SYSTEM
# ════════════════════════════════════════════════════════════

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