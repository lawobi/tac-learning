# =========================
# IMPORTS
# =========================
import streamlit as st
import sqlite3
import bcrypt
import os
import stripe
from openai import OpenAI
from dotenv import load_dotenv
from textwrap import dedent
from docx import Document
from docx.shared import Inches
import base64
from pathlib import Path

# =========================
# PEDAGOGY (INLINE – NO EXTERNAL FILE)
# =========================

QA_CHECKLIST = [
    "Learning purpose is explicit",
    "Meaningful context or lived example included",
    "Cognitive load controlled",
    "Developmental readiness respected",
    "SEN/EAL supports embedded",
    "Active or enquiry task present",
    "Competency demonstrated",
    "Retrieval included",
    "Emotional tone is respectful",
    "Layout is accessible",
    "Curiosity and dignity preserved",
]

PEDAGOGY_CORE_POSITION = """
Learning is a relational, developmental, and meaningful process.
Understanding precedes memorisation.
Curiosity, dignity, and emotional safety are essential.
"""

# =========================
# ENV & KEYS
# =========================

load_dotenv()

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY")
STRIPE_PRICE_ANNUAL = os.getenv("STRIPE_PRICE_ANNUAL")

missing = []
if not OPENAI_KEY:
    missing.append("OPENAI_API_KEY")
if not STRIPE_KEY:
    missing.append("STRIPE_SECRET_KEY")
if not STRIPE_PRICE_MONTHLY:
    missing.append("STRIPE_PRICE_MONTHLY")
if not STRIPE_PRICE_ANNUAL:
    missing.append("STRIPE_PRICE_ANNUAL")

if missing:
    st.error(f"Missing environment variables: {', '.join(missing)}")
    st.stop()


stripe.api_key = STRIPE_KEY
client = OpenAI(api_key=OPENAI_KEY)

import smtplib
from email.message import EmailMessage

def send_email(to_email: str, subject: str, body: str):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pw = os.getenv("SMTP_PASS")

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, pw)
        server.send_message(msg)


# =========================
# DATABASE
# =========================

DB = "users.db"

def db():
    return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password BLOB,
            paid INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def upgrade_db():
    conn = db()
    c = conn.cursor()

    # Add subscription columns (ignore if already exists)
    try:
        c.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT 'none'")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def create_user(email, pw):
    email = email.strip().lower()
    hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt())
    try:
        conn = db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (email, password) VALUES (?, ?)",
            (email, hashed)
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

def login_user(email, pw):
    email = email.strip().lower()
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, password, paid FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    if row and bcrypt.checkpw(pw.encode(), row[1]):
        return {"id": row[0], "email": email, "paid": bool(row[2])}
    return None

def mark_paid(uid):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET paid=1 WHERE id=?", (uid,))
    conn.commit()
    conn.close()
def mark_subscribed(uid):
    conn = db()
    c = conn.cursor()
    c.execute(
        "UPDATE users SET paid=1, subscription_status='active' WHERE id=?",
        (uid,)
    )
    conn.commit()
    conn.close()

init_db()
upgrade_db()


# =========================
# SESSION STATE
# =========================

st.session_state.setdefault("user", None)
st.session_state.setdefault("show_login", False)
st.session_state.setdefault("final_lesson", None)
st.session_state.setdefault("lesson_image", None)

# =========================
# LOGIN PAGE
# =========================

def login_page():
    st.title("🔐 Login / Create Account")

    t1, t2, t3 = st.tabs(["Login", "Create account", "Forgot password"])


    with t1:
        e = st.text_input("Email", key="login_e")
        p = st.text_input("Password", type="password", key="login_p")
        if st.button("Login", key="login_btn"):
            u = login_user(e, p)
            if u:
                st.session_state.user = u
                st.session_state.show_login = False
                st.rerun()
            else:
                st.error("Invalid login")

    with t2:
        e = st.text_input("Email", key="reg_e")
        p1 = st.text_input("Password", type="password", key="reg_p1")
        p2 = st.text_input("Confirm password", type="password", key="reg_p2")
        if st.button("Create account", key="reg_btn"):
            if p1 != p2:
                st.error("Passwords do not match")
            elif create_user(e, p1):
                st.success("Account created. Please log in.")
            else:
                st.error("Account already exists")

    with t3:
        reset_email = st.text_input("Enter your email", key="reset_email")
        if st.button("Send reset link", key="send_reset_link_btn"):
            token = create_reset_token(reset_email.strip().lower())
            base_url = os.getenv("APP_BASE_URL", "http://localhost:8501")
            link = f"{base_url}/?reset={token}"
            send_email(
            reset_email,
            "Reset your TAC password",
            f"Click this link to reset your password (valid 1 hour):\n\n{link}"
        )
        st.success("If that email exists, a reset link has been sent.")


# =========================
# PAYMENTS (SUBSCRIPTIONS ONLY)
# =========================

# =========================
# PAYMENTS (SUBSCRIPTIONS ONLY)
# =========================

def start_subscription_checkout(plan: str, email: str):
    """
    plan: 'monthly' or 'annual'
    returns Stripe Checkout URL
    """
    if plan == "monthly":
        price_id = os.getenv("STRIPE_PRICE_MONTHLY")
    elif plan == "annual":
        price_id = os.getenv("STRIPE_PRICE_ANNUAL")
    else:
        raise ValueError("Invalid plan")

    if not price_id:
        st.error("Stripe price ID missing in environment variables.")
        st.stop()

    base_url = os.getenv("APP_BASE_URL", "http://localhost:8501")

    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=email,
        success_url=f"{base_url}/?subscribed=true",
        cancel_url=f"{base_url}/",
    )

    return session.url


def subscription_cta():
    st.warning("🔒 Subscription required to unlock this feature")

    plan = st.radio(
        "Choose a plan",
        ["Monthly (£10/month)", "Annual (£100/year)"],
        index=0,
        key="subscription_plan_radio",
    )

    if st.button("Subscribe now", key="subscribe_now_btn"):
        if not st.session_state.user:
            st.error("Please log in first.")
            return

        plan_key = "monthly" if "Monthly" in plan else "annual"
        url = start_subscription_checkout(plan_key, st.session_state.user["email"])
        st.markdown(f"[➡ Proceed to secure Stripe Checkout]({url})")


# =========================
# AI FUNCTIONS
# =========================

def generate_lesson(topic, year):
    prompt = dedent(f"""
    Create a classroom-ready lesson.

    Topic: {topic}
    Year group: {year}

    Include:
    - Clear learning objectives
    - Explanation with examples
    - Differentiated tasks (Support / Core / Stretch)
    - Retrieval or plenary
    """)
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are an expert teacher."},
            {"role": "user", "content": prompt},
        ],
    )
    return r.choices[0].message.content

def run_pedagogical_qa(content):
    checklist = "\n".join(f"- {c}" for c in QA_CHECKLIST)
    prompt = f"""
You are performing a strict pedagogical QA.

PEDAGOGY:
{PEDAGOGY_CORE_POSITION}

CHECKLIST (ALL REQUIRED):
{checklist}

CONTENT:
{content}

Revise until all standards are met.
Output ONLY the final content.
"""
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Senior education QA reviewer"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    return r.choices[0].message.content

def generate_lesson_image(topic, year):
    prompt = f"Educational illustration for a {year} lesson on {topic}. Simple, no text."
    r = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size="1024x1024"
    )
    img_bytes = base64.b64decode(r.data[0].b64_json)
    path = Path("lesson_image.png")
    path.write_bytes(img_bytes)
    return str(path)

# =========================
# WORD EXPORT
# =========================

def paid_download_block(title, text, image_path=None):
    if not (st.session_state.user and st.session_state.user["paid"]):
        return

    doc = Document()
    doc.add_heading(title, level=1)

    if image_path and os.path.exists(image_path):
        doc.add_picture(image_path, width=Inches(5))

    for line in text.split("\n"):
        doc.add_paragraph(line)

    filename = f"{title.replace(' ', '_').lower()}.docx"
    doc.save(filename)

    with open(filename, "rb") as f:
        st.download_button(
            "⬇ Download as Word (.docx)",
            f,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

# =========================
# UI
# =========================

st.set_page_config("TAC Teaching Suite", layout="wide")
# =========================
# PASSWORD RESET LINK HANDLER
# =========================
if "reset" in st.query_params:
    st.title("🔑 Reset Password")
    token = st.query_params["reset"]

    try:
        email = verify_reset_token(token)

        new_pw = st.text_input("New password", type="password")
        new_pw2 = st.text_input("Confirm new password", type="password")

        if st.button("Update password", key="reset_pw_btn"):
            if new_pw != new_pw2:
                st.error("Passwords do not match")
            elif len(new_pw) < 6:
                st.error("Password must be at least 6 characters")
            else:
                update_password(email, new_pw)
                st.success("✅ Password updated. You can now log in.")
    except Exception:
        st.error("Reset link invalid or expired.")

    st.stop()

st.sidebar.title("TAC Teaching Suite")
from itsdangerous import URLSafeTimedSerializer

def get_serializer():
    return URLSafeTimedSerializer(os.getenv("RESET_SECRET", "dev-secret"))

def create_reset_token(email: str):
    return get_serializer().dumps(email)

def verify_reset_token(token: str, max_age_seconds=3600):
    return get_serializer().loads(token, max_age=max_age_seconds)

def update_password(email: str, new_pw: str):
    hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt())
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET password=? WHERE email=?", (hashed, email.strip().lower()))
    conn.commit()
    conn.close()

if st.session_state.user:
    st.sidebar.success(f"Logged in: {st.session_state.user['email']}")
else:
    if st.sidebar.button("🔐 Login / Create account"):
        st.session_state.show_login = True
        st.rerun()

if st.session_state.show_login and not st.session_state.user:
    login_page()
    st.stop()

if "paid" in st.query_params and st.session_state.user:
    mark_paid(st.session_state.user["id"])
    st.session_state.user["paid"] = True
    st.success("✅ Payment successful")
if "subscribed" in st.query_params and st.session_state.user:
    mark_subscribed(st.session_state.user["id"])
    st.session_state.user["paid"] = True
    st.success("✅ Subscription active. Access unlocked.")

# =========================
# LESSON GENERATOR
# =========================

st.title("📘 Lesson Generator")

topic = st.text_input("Lesson topic")
year = st.text_input("Year group")
include_image = st.checkbox("Include visual", value=True)

if st.button("Generate lesson", key="gen_lesson"):
    draft = generate_lesson(topic, year)

    status = st.empty()
    status.info("🔍 Running pedagogical QA…")

    final = run_pedagogical_qa(draft)
    st.session_state.final_lesson = final

    status.success("✅ Lesson quality verified")

    st.markdown(final)

    if include_image:
        img = generate_lesson_image(topic, year)
        st.session_state.lesson_image = img
        st.image(img, caption="Lesson visual")

# Persistent download
if st.session_state.final_lesson:
    if st.session_state.user and st.session_state.user["paid"]:
        paid_download_block(
            "Lesson Plan",
            st.session_state.final_lesson,
            st.session_state.lesson_image,
        )
    else:
        st.info("🔒 Preview only")
      #payment_cta() #

# ======================
#Email Sender Options
# ======================

# import smtplib
from email.message import EmailMessage

def send_email(to_email: str, subject: str, body: str):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pw = os.getenv("SMTP_PASS")

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, pw)
        server.send_message(msg)