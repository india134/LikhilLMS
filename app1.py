from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from functools import wraps
from datetime import datetime, date, time
from dotenv import load_dotenv
from supabase import create_client
import os
from flask import abort
import re
import markdown
import json
import hmac
import hashlib
import pandas as pd
from markupsafe import Markup
import os
import os, json, hmac, hashlib, threading, requests
from flask import request, render_template, redirect, url_for
  # this is the one sending the POST to n8n
from datetime import datetime
import requests
import uuid

# ---------- ENV & CLIENT ----------
load_dotenv()
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SERVICE_KEY   = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

SECRET        = os.getenv("FLASK_SECRET", "dev")

sb = create_client(SUPABASE_URL, SERVICE_KEY)

app = Flask(__name__)
app.secret_key = SECRET
N8N_CREATE_URL= (os.getenv("N8N_CREATE_URL") or "").strip()
N8N_WEBHOOK_SECRET = "some random secret"
N8N_DELETE_URL = os.getenv("N8N_DELETE_URL", "")

from functools import wraps
from flask import session, redirect, url_for
def login_required(role=None):
    def login_decorator(f):
        @wraps(f)
        def login_wrapper(*args, **kwargs):
            # --- Login check ---
            if "user_id" not in session:
                if role == "admin":
                    return redirect(url_for("admin_login"))
                elif role == "god":
                    return redirect(url_for("god_login"))
                else:
                    return redirect(url_for("tutor_login"))

            # --- Role check ---
            if role and session.get("role") != role:
                return "Unauthorized", 403

            return f(*args, **kwargs)
        
        # Ensure the wrapper has a unique name
        login_wrapper.__name__ = f"{f.__name__}_login_required"
        return login_wrapper
    return login_decorator



import uuid

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/fix_tokens")
def fix_tokens():
    students = sb.table("Students").select("*").is_("dashboard_token", None).execute().data
    for student in students:
        sb.table("Students").update({
            "dashboard_token": str(uuid.uuid4())
        }).eq("id", student["id"]).execute()
    return f"Fixed {len(students)} students"

@app.route("/student_dashboard/<token>")
def student_dashboard(token):
    # fetch student by token
    student = sb.table("Students").select("*").eq("dashboard_token", token).eq("tutor_id", session.get("tutor_id")).execute().data

    if not student:
        abort(404)

    student = student[0]
    selected = student["name"] # get student's name

    attendance = sb.table("attendance").select("*").eq("name", selected).execute().data
    payments = sb.table("payment_records").select("*").eq("name", selected).execute().data
    
    # 📝 FIX: Select the 'meet_link' column from the 'class_schedule' table
    schedule = sb.table("class_schedule").select("date, start_time, end_time, course, meet_url").eq("name", selected).execute().data
    
    # ✅ NEW: Calculate pending fee status right in the route
    status, class_details = None, []
    
    student_data = sb.table("Students").select("hourly_rate").eq("name", selected).execute().data
    if student_data:
        rate = _to_num(student_data[0].get("hourly_rate", 0))

        pays = sb.table("payment_records").select("*").eq("name", selected)\
               .order("cleared_date").execute().data or []
        
        cleared_date = datetime(1970,1,1).date()
        adv_hrs, adv_amount = 0.0, 0.0
        if pays:
            last = pays[-1]
            cleared_date = pd.to_datetime(last["cleared_date"]).date()
            adv_hrs = _to_num(last.get("advance_hours"))
            adv_amount = _to_num(last.get("advance_amount"))

        att = sb.table("attendance").select("*").eq("name", selected)\
              .gte("date", str(cleared_date)).order("date").execute().data or []

        total_since = 0.0
        for a in att:
            d = pd.to_datetime(a["date"]).date()
            if d > cleared_date:
                total_since += _to_num(a.get("duration"))
                class_details.append({
                    "Date": a["date"],
                    "Course": a.get("course",""),
                    "Time": a.get("time",""),
                    "Duration": _to_num(a.get("duration"))
                })
        
        pending_hrs = max(total_since - adv_hrs, 0.0)
        
        status = {
            "student": selected,
            "cleared_date": str(cleared_date),
            "advance_hrs": adv_hrs,
            "total_since": total_since,
            "pending_hrs": pending_hrs,
            "rate": rate,
            "pending_amount": round(pending_hrs * rate, 2),
        }

    return render_template(
        "student_dashboard.html",
        student=student,
        attendance=attendance,
        payments=payments,
        schedule=schedule,
        pending_status=status,
        pending_classes=class_details
    )

@app.route("/admin_dashboard")
@login_required(role="admin")
def admin_dashboard():
    if session.get("role") != "admin":
        return "Unauthorized", 403

    org_id = session.get("org_id")
    if not org_id:
        return "No organisation assigned", 400

    # Fetch tutors of this organisation
    tutors = sb.table("profiles").select(
        "user_id, full_name, email, role, is_active"
    ).eq("org_id", org_id).eq("role", "tutor").execute().data or []

    # Split tutors
    pending_tutors = [t for t in tutors if not t["is_active"]]
    active_tutors  = [t for t in tutors if t["is_active"]]

    return render_template(
        "admin_dashboard.html",
        pending_tutors=pending_tutors,
        active_tutors=active_tutors
    )


@app.route("/god_dashboard")
@login_required(role="god")
def god_dashboard():
    if session.get("role") != "god_admin":
        return "Unauthorized", 403

    # Fetch all profiles
    profiles = sb.table("profiles").select(
        "user_id, full_name, email, role, org_id, is_active"
    ).execute().data or []

    # Fetch organisations
    orgs = sb.table("organizations").select("id, name").execute().data or []
    org_map = {o["id"]: o["name"] for o in orgs}

    # Split profiles
    org_admins_active   = [p for p in profiles if p["role"] == "admin" and p["is_active"]]
    org_admins_pending  = [p for p in profiles if p["role"] == "admin" and not p["is_active"]]
    solo_tutors_active  = [p for p in profiles if p["role"] == "tutor" and not p["org_id"] and p["is_active"]]
    solo_tutors_pending = [p for p in profiles if p["role"] == "tutor" and not p["org_id"] and not p["is_active"]]
    org_tutors          = [p for p in profiles if p["role"] == "tutor" and p["org_id"]]

    # Build map: org_id -> tutors list
    tutors_by_org = {}
    for t in org_tutors:
        tutors_by_org.setdefault(t["org_id"], []).append(t)

    return render_template(
        "god_dashboard.html",
        org_admins_active=org_admins_active,
        org_admins_pending=org_admins_pending,
        solo_tutors_active=solo_tutors_active,
        solo_tutors_pending=solo_tutors_pending,
        tutors_by_org=tutors_by_org,
        org_map=org_map
    )





@app.route("/login/tutor", methods=["GET", "POST"])
def tutor_login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        resp = sb.auth.sign_in_with_password({"email": email, "password": password})

        if resp.user:
            prof = sb.table("profiles").select("is_active, role, full_name, org_id").eq("user_id", resp.user.id).execute().data
            if prof and prof[0]["is_active"]:
                session["tutor_id"] = resp.user.id
                session["role"] = prof[0]["role"]
                session["name"] = prof[0]["full_name"]
                session["org_id"] = prof[0].get("org_id")
                return redirect(url_for("dashboard"))
            else:
                flash("Your account is not activated yet. Please wait for approval.", "warning")
                return redirect(url_for("tutor_login"))
        else:
            flash(resp.error.message if resp.error else "Login failed", "danger")

    return render_template("tutor_login.html")


@app.route("/admin_login", methods=["GET","POST"])
def admin_login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        resp = sb.auth.sign_in_with_password({"email": email, "password": password})

        if resp.user:
            prof = sb.table("profiles").select("role, full_name, is_active, org_id") \
                    .eq("user_id", resp.user.id).execute().data

            if prof and prof[0]["role"] == "admin":
                if prof[0]["is_active"]:
                    session["tutor_id"] = resp.user.id
                    session["role"] = "admin"
                    session["name"] = prof[0]["full_name"]
                    session["org_id"] = prof[0]["org_id"]
                    return redirect(url_for("admin_dashboard"))
                else:
                    flash("Your account is pending God Admin approval.", "warning")
            else:
                flash("Unauthorized: only admins can log in here.", "danger")

    return render_template("admin_login.html")

@app.route("/god_login", methods=["GET", "POST"])
def god_login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        # Hardcoded God Admin credentials
        if email == "likhileducation@gmail.com" and password == "supersecret":
            session["user_id"] = "god-admin"
            session["role"] = "god_admin"
            session["name"] = "Super Admin"
            return redirect(url_for("god_dashboard"))
        else:
            flash("Invalid God Admin credentials", "danger")

    return render_template("god_login.html")




@app.route("/admin_tutors")
def admin_tutors():
    role = session.get("role")
    if not role:
        return "Unauthorized", 403

    if role == "god_admin":
        # God admin: solo tutors + org admins who are not active
        tutors = sb.table("profiles").select("*") \
                   .or_("and(is_active.eq.false,org_id.is.null),and(is_active.eq.false,role.eq.admin)") \
                   .execute().data or []
    elif role == "admin":
        # Org admin: tutors in the same org, not yet active
        tutors = sb.table("profiles").select("*") \
                   .eq("org_id", session["org_id"]) \
                   .eq("role", "tutor") \
                   .eq("is_active", False) \
                   .execute().data or []
    else:
        return "Unauthorized", 403

    return render_template("admin_tutors.html", tutors=tutors)



@app.route("/toggle_tutor/<uuid:user_id>")
@login_required
def toggle_tutor(user_id):
    role = session.get("role")
    if not role:
        return "Unauthorized", 403

    # Fetch profile
    prof = sb.table("profiles").select("is_active, role, org_id").eq("user_id", user_id).execute().data
    if not prof:
        return "User not found", 404

    profile = prof[0]
    new_status = not profile["is_active"]

    if role == "god_admin":
        # God Admin: can toggle solo tutors and org admins
        if profile["org_id"] is None or profile["role"] == "admin":
            sb.table("profiles").update({"is_active": new_status}).eq("user_id", user_id).execute()
        else:
            return "Unauthorized", 403
        return redirect(url_for("god_dashboard"))

    elif role == "admin":
        # Org Admin: can toggle tutors in their org only
        if profile["role"] == "tutor" and profile["org_id"] == session.get("org_id"):
            sb.table("profiles").update({"is_active": new_status}).eq("user_id", user_id).execute()
        else:
            return "Unauthorized", 403
        return redirect(url_for("admin_dashboard"))

    else:
        return "Unauthorized", 403



@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- HELPERS ----------
def _to_num(x, default=0.0):
    try:
        return float(x)
    except:
        return default

def get_students():
    tutor_id = session.get("tutor_id")
    res = sb.table("Students").select("*").eq("tutor_id", tutor_id).order("id").execute()
    return res.data or []


def get_unique_courses():
    rows = sb.table("Students").select("course").not_.is_("course", None).execute().data or []
    courses = sorted({(r.get("course") or "").strip() for r in rows if (r.get("course") or "").strip()})
    # Optional: include your custom list
    base = ["AP Statistics","IB Math AA HL","IB Math AA SL","IB Math AI HL","IB Math AI SL",
            "AP Calculus AB","AP Calculus BC","SAT Math","A level","GCSE"]
    return sorted({*courses, *base})

# ---------- REGISTRATION → /submit ----------
@app.route("/register")
def register():
    return render_template("register.html")

@app.route("/register_org_admin", methods=["GET", "POST"])
def register_org_admin():
    if request.method == "POST":
        full_name = request.form["full_name"]
        email = request.form["email"]
        password = request.form["password"]
        org_name = request.form["org_name"]

        # Step 1: Create org_id
        org_id = str(uuid.uuid4())
        sb.table("organizations").insert({
            "id": org_id,
            "name": org_name
        }).execute()

        # Step 2: Create admin in Supabase Auth
        resp = sb.auth.sign_up({"email": email, "password": password})
        if resp.user:
            sb.table("profiles").insert({
                "user_id": resp.user.id,
                "full_name": full_name,
                "role": "admin",
                "is_active": False,   # you (super admin) approve this later
                "org_id": org_id
            }).execute()

            flash("Registration successful! Please wait for Super Admin approval.", "success")
            return redirect(url_for("admin_login"))

        flash("Registration failed!", "danger")

    return render_template("register_org_admin.html")


@app.route("/register_tutor", methods=["GET","POST"])
def register_tutor():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        full_name = request.form["full_name"]
        org_code = request.form.get("org_code") or None

        resp = sb.auth.sign_up({"email": email, "password": password})

        if resp.user:
            org_id = None
            if org_code:
                org = sb.table("organizations").select("id").eq("code", org_code).execute().data
                if not org:
                    flash("Organisation code not found!", "danger")
                    return redirect(url_for("register_tutor"))
                org_id = org[0]["id"]

            sb.table("profiles").insert({
                "user_id": resp.user.id,
                "full_name": full_name,
                "role": "tutor",
                "org_id": org_id,
                "is_active": False
            }).execute()

            flash("Registration successful! Wait for approval.", "success")
            return redirect(url_for("login"))
    return render_template("register_tutor.html")



@app.route("/student/<int:student_id>")
@login_required
def student_profile(student_id):
    tutor_id = session.get("tutor_id")

    # fetch student details belonging only to this tutor
    student = (
        sb.table("Students")
        .select("*")
        .eq("id", student_id)
        .eq("tutor_id", tutor_id)
        .execute()
        .data
    )

    if not student:
        return "Student not found or unauthorized", 404

    return render_template("student_profile.html", student=student[0])


@app.route("/submit", methods=["POST"])
def submit():
    tutor_id = session.get("tutor_id")  # current logged-in tutor

    payload = {
        "name": request.form["name"],
        "grade": int(request.form.get("grade") or 0),
        "course": request.form.get("course"),
        "school": request.form.get("school"),
        "email Id": request.form.get("email Id"),
        "mobile number": request.form.get("mobile number"),
        "hourly_rate": _to_num(request.form.get("hourly_rate"), 0.0),
        "dashboard_token": str(uuid.uuid4()),
        "tutor_id": tutor_id   # link student to tutor
    }

    sb.table("Students").insert(payload).execute()
    return redirect(url_for("dashboard"))



# ---------- DASHBOARD ----------
@app.route("/dashboard")
@login_required
def dashboard():
    students = get_students()
    return render_template("dashboard.html", students=students, unique_courses=get_unique_courses())

# ---------- CRUD: edit/delete student (same routes) ----------
# ... other code ...

@app.route("/edit_student/<int:sid>", methods=["GET", "POST"])
@login_required
def edit_student(sid):
    tutor_id = session.get("tutor_id")

    if request.method == "POST":
        updated = {
            # ... other fields ...
            "hourly_rate": _to_num(request.form.get("hourly_rate")),
            "currency": request.form.get("currency"),
        }

        # only update if this student belongs to the logged-in tutor
        sb.table("Students").update(updated).eq("id", sid).eq("tutor_id", tutor_id).execute()
        return redirect(url_for("dashboard"))

    # fetch existing student data scoped to tutor
    student = (
        sb.table("Students")
        .select("*")
        .eq("id", sid)
        .eq("tutor_id", tutor_id)
        .single()
        .execute()
        .data
    )

    if not student:
        return "Student not found or unauthorized", 404

    return render_template("edit.html", student=student)

# ... other code ...
@app.route("/delete_student/<int:sid>")
@login_required
def delete_student(sid):
    tutor_id = session.get("tutor_id")

    # delete only if it belongs to this tutor
    sb.table("Students").delete().eq("id", sid).eq("tutor_id", tutor_id).execute()
    return redirect(url_for("dashboard"))



# ---------- SCHEDULE ----------
def _post_to_n8n_async(payload: dict):
    """Send payload to n8n without blocking the request thread."""
    if not N8N_CREATE_URL:
        # Nothing to do if URL isn't configured
        return

    try:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        if N8N_WEBHOOK_SECRET:
            sig = hmac.new(N8N_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-LMS-Signature"] = sig

        # Fire-and-forget with a short timeout so UI never stalls
        requests.post(N8N_CREATE_URL, data=body, headers=headers, timeout=4)
    except Exception as e:
        # Don’t break the user flow if this fails
        try:
            app.logger.warning(f"n8n webhook failed: {e}")
        except Exception:
            pass

def _post_to_n8n_sync(url: str, payload: dict):
    print(f"DELETE WEBHOOK CALLED")
    print(f"URL: {url}")
    
    if not url:
        print("ERROR: n8n URL is empty!")
        return
        
    try:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        
        # REMOVE THIS SIGNATURE BLOCK - IT'S BREAKING YOUR WEBHOOK
        # if N8N_WEBHOOK_SECRET:
        #     sig = hmac.new(N8N_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        #     headers["X-LMS-Signature"] = sig
        
        r = requests.post(url, data=body, headers=headers, timeout=10)
        print(f"Response Status: {r.status_code}")
        print(f"Response Text: {r.text}")
            
    except Exception as e:
        print(f"Exception: {e}")


@app.route("/schedule", methods=["GET", "POST"])
@login_required
def schedule():
    tutor_id = session.get("tutor_id")

    # 1) Load existing schedule rows for this tutor only
    sched = (
        sb.table("class_schedule")
        .select("*")
        .eq("tutor_id", tutor_id)
        .order("id")
        .execute()
        .data or []
    )

    # 2) Fast list of student names for this tutor only
    name_rows = (
        sb.table("Students")
        .select("name")
        .eq("tutor_id", tutor_id)
        .order("name")
        .execute()
        .data or []
    )
    student_names = [r["name"] for r in name_rows]

    # 3) Map name -> email (scoped to this tutor’s students)
    stu_rows = (
        sb.table("Students")
        .select('name, "email Id"')
        .eq("tutor_id", tutor_id)
        .execute()
        .data or []
    )
    emails = {r["name"]: (r.get("email Id") or "") for r in stu_rows}

    if request.method == "POST":
        name   = request.form["student"]
        course = request.form.get("course")
        d      = request.form.get("date")
        start  = request.form.get("time")
        end_t  = request.form.get("end_time")
        dur    = _to_num(request.form.get("duration"))

        # 4) Insert into Supabase with tutor_id
        insert_payload = {
            "name": name,
            "course": course,
            "date": d,
            "start_time": start,
            "end_time": end_t,
            "duration": dur,
            "email Id": emails.get(name, ""),
            "tutor_id": tutor_id
        }
        res = sb.table("class_schedule").insert(insert_payload).execute()
        created = (res.data or [{}])[0]

        # 5) Trigger n8n in background
        payload = {
            "event": "class_scheduled",
            "source": "likhil_lms",
            "row": created
        }
        threading.Thread(target=_post_to_n8n_async, args=(payload,), daemon=True).start()

        return redirect(url_for("schedule"))

    custom_courses = get_unique_courses()
    return render_template(
        "schedule.html",
        students=student_names,
        schedule_data=sched,
        custom_courses=custom_courses
    )


# keep delete route shape; use list order to resolve id
@app.route("/schedule/delete/<int:row_index>", methods=["POST"])
@login_required
def delete_schedule(row_index):
    tutor_id = session.get("tutor_id")
    print(f"\n🗑️ DELETE SCHEDULE CALLED - Row Index: {row_index} | Tutor: {tutor_id}")

    # Load schedules only for this tutor
    rows = (
        sb.table("class_schedule")
        .select("*")
        .eq("tutor_id", tutor_id)
        .order("id")
        .execute()
        .data or []
    )
    print(f"📊 Total schedule rows for this tutor: {len(rows)}")

    if row_index < 0 or row_index >= len(rows):
        print(f"❌ Invalid row index: {row_index}")
        return "Not found", 404

    row = rows[row_index]
    schedule_id = row.get("id")
    event_id = row.get("google_event_id", "")

    print(f"🎯 Deleting schedule:")
    print(f"   Schedule ID: {schedule_id}")
    print(f"   Google Event ID: {event_id}")
    print(f"   Student: {row.get('name')}")
    print(f"   Date: {row.get('date')} {row.get('start_time')}")

    # 1) Tell n8n to delete the calendar event
    payload = {
        "event": "class_deleted",
        "source": "likhil_lms",
        "row": {
            "id": schedule_id,
            "google_event_id": event_id,
            "name": row.get("name"),
            "date": row.get("date"),
            "start_time": row.get("start_time")
        }
    }

    print(f"🎯 N8N_DELETE_URL: {N8N_DELETE_URL}")
    _post_to_n8n_sync(N8N_DELETE_URL, payload)

    # 2) Delete from Supabase (scoped by tutor_id for safety)
    print(f"🗄️ Deleting from Supabase...")
    sb.table("class_schedule").delete().eq("id", schedule_id).eq("tutor_id", tutor_id).execute()
    print(f"✅ Deleted from Supabase")

    return redirect(url_for("schedule"))

def _fire_and_forget(url: str, payload: dict):
    """Enhanced fire and forget with debugging"""
    print(f"🚀 FIRE AND FORGET CALLED")
    print(f"URL: {url}")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    
    if not url:
        print("❌ No URL provided to fire_and_forget")
        return
        
    try:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        
        # Remove the signature block entirely
        # if N8N_WEBHOOK_SECRET:
        #    sig = hmac.new(N8N_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        #    headers["X-LMS-Signature"] = sig
            
        print(f"📤 Fire-and-forget request to: {url}")
        response = requests.post(url, data=raw, headers=headers, timeout=4)
        print(f"✅ Fire-and-forget response: {response.status_code}")
        
    except Exception as e:
        print(f"💥 Fire-and-forget failed: {e}")
        app.logger.warning(f"n8n delete webhook failed: {e}")
# FINISH = move row to attendance then delete from schedule
@app.route("/schedule/finish/<int:index>")
@login_required
def finish_schedule(index):
    tutor_id = session.get("tutor_id")
    print(f"\n🏁 FINISH SCHEDULE CALLED - Index: {index}")

    # Load tutor’s own schedules
    sched = (
        sb.table("class_schedule")
        .select("*")
        .eq("tutor_id", tutor_id)
        .order("id")
        .execute()
        .data or []
    )
    if index < 0 or index >= len(sched):
        return "Not found", 404

    row = sched[index]
    print(f"🎯 Finishing schedule: {row.get('id')} for tutor {tutor_id}")

    # Write to attendance (with tutor_id)
    sb.table("attendance").insert({
        "name":     row.get("name"),
        "course":   row.get("course"),
        "date":     row.get("date"),
        "time":     row.get("start_time"),
        "duration": _to_num(row.get("duration")),
        "tutor_id": tutor_id
    }).execute()

    # Tell n8n to delete Google Calendar event
    geid = (row.get("google_event_id") or "").strip()
    if geid:
        payload = {
            "event": "class_finished",
            "source": "likhil_lms",
            "row": {
                "id": row.get("id"),
                "google_event_id": geid,
                "name": row.get("name"),
                "date": row.get("date"),
                "start_time": row.get("start_time")
            }
        }
        _fire_and_forget(N8N_DELETE_URL, payload)

    # Delete schedule row itself
    sb.table("class_schedule").delete().eq("id", row["id"]).eq("tutor_id", tutor_id).execute()

    return redirect(url_for("schedule"))

# ---------- ATTENDANCE (month filter kept) ----------
@app.route("/attendance")
@login_required
def attendance():
    tutor_id = session.get("tutor_id")

    # Fetch only this tutor's attendance records
    rows = (
        sb.table("attendance")
        .select("*")
        .eq("tutor_id", tutor_id)
        .order("date", desc=True)
        .execute()
        .data or []
    )

    start_date = request.args.get("start")
    end_date = request.args.get("end")

    filtered = []
    for r in rows:
        dt = pd.to_datetime(r["date"]).date()
        if start_date and dt < pd.to_datetime(start_date).date():
            continue
        if end_date and dt > pd.to_datetime(end_date).date():
            continue
        filtered.append(r)

    return render_template("attendance.html", records=filtered)


# ---------- PAYMENTS ----------
@app.route("/payment_records", methods=["GET","POST"])
@login_required
def payment_records():
    tutor_id = session.get("tutor_id")

    if request.method == "POST":
        sb.table("payment_records").insert({
            "name": request.form["name"],
            "course": request.form.get("course"),
            "amount": _to_num(request.form.get("amount")),
            "cleared_date": request.form.get("cleared_date"),
            "advance_hours": _to_num(request.form.get("advance_hours")),
            "advance_amount": _to_num(request.form.get("advance_amount")),
            "tutor_id": tutor_id
        }).execute()
        return redirect(url_for("payment_records"))

    # Fetch only this tutor's payment records
    records = (
        sb.table("payment_records")
        .select("*")
        .eq("tutor_id", tutor_id)
        .order("cleared_date", desc=True)
        .execute()
        .data or []
    )

    students = [s["name"] for s in get_students()]  # get_students() already scoped
    courses  = get_unique_courses()
    return render_template("payment_records.html", records=records, students=students, courses=courses)


# dues since last cleared date minus advance hours
@app.route("/payment_status", methods=["GET","POST"])
@login_required
def payment_status():
    tutor_id = session.get("tutor_id")

    # Only this tutor's students
    students = sorted([s["name"] for s in get_students()])
    status, class_details = None, []

    CURRENCY_SYMBOLS = {"INR": "₹","USD": "$","EUR": "€","GBP": "£"}
    currency_symbol = "₹"  # default

    if request.method == "POST":
        selected = request.form.get("student")

        # Fetch student data (scoped by tutor)
        student_data = (
            sb.table("Students")
            .select("hourly_rate, currency")
            .eq("name", selected)
            .eq("tutor_id", tutor_id)
            .execute()
            .data
        )
        if not student_data:
            flash("No hourly rate found for this student. Please update their record.", "warning")
            return render_template("payment_status.html", students=students, status=None, classes=[], currency_symbol=currency_symbol)

        rate = _to_num(student_data[0].get("hourly_rate", 0))
        currency_code = student_data[0].get("currency", "INR")
        currency_symbol = CURRENCY_SYMBOLS.get(currency_code, "₹")

        # Payment records (scoped by tutor)
        pays = (
            sb.table("payment_records")
            .select("*")
            .eq("name", selected)
            .eq("tutor_id", tutor_id)
            .order("cleared_date")
            .execute()
            .data or []
        )

        cleared_date = datetime(1970,1,1).date()
        adv_hrs, adv_amount = 0.0, 0.0
        if pays:
            last = pays[-1]
            cleared_date = pd.to_datetime(last["cleared_date"]).date()
            adv_hrs = _to_num(last.get("advance_hours"))
            adv_amount = _to_num(last.get("advance_amount"))

        # Attendance after cleared date (scoped by tutor)
        att = (
            sb.table("attendance")
            .select("*")
            .eq("name", selected)
            .eq("tutor_id", tutor_id)
            .gte("date", str(cleared_date))
            .order("date")
            .execute()
            .data or []
        )

        total_since = 0.0
        for a in att:
            d = pd.to_datetime(a["date"]).date()
            if d > cleared_date:
                total_since += _to_num(a.get("duration"))
                class_details.append({
                    "Date": a["date"],
                    "Course": a.get("course",""),
                    "Time": a.get("time",""),
                    "Duration": _to_num(a.get("duration"))
                })

        pending_hrs = max(total_since - adv_hrs, 0.0)
        status = {
            "student": selected,
            "cleared_date": str(cleared_date),
            "advance_hrs": adv_hrs,
            "total_since": total_since,
            "pending_hrs": pending_hrs,
            "rate": rate,
            "pending_amount": round(pending_hrs * rate, 2),
        }

    return render_template("payment_status.html",
                           students=students, status=status, classes=class_details, currency_symbol=currency_symbol)

# ---------- RESCHEDULE (same route shape) ----------
@app.route("/schedule/reschedule/<int:row_index>", methods=["GET","POST"])
@login_required
def reschedule(row_index):
    rows = sb.table("class_schedule").select("*").eq("tutor_id", session.get("tutor_id")).order("id").execute().data or []

    if row_index < 0 or row_index >= len(rows):
        return "Not found", 404
    row = rows[row_index]
    sid = row["id"]

    if request.method == "POST":
        sb.table("class_schedule").update({
            "date": request.form["date"],
            "start_time": request.form["time"],
            "duration": _to_num(request.form["duration"])
        }).eq("id", sid).execute()
        return redirect(url_for("schedule"))

    # emulate your old `row_values` for template
    data = [
        row.get("name",""), row.get("course",""), row.get("date",""),
        row.get("start_time",""), row.get("end_time",""), row.get("duration","")
    ]
    return render_template("reschedule.html", row_index=row_index, data=data)

# ---------- STUDENT REPORT (reads from Supabase; keeps your template contract) ----------
# You can plug your HuggingFace function here if you want to keep AI reports.
# ---------- STUDENT REPORT (reads from Supabase; keeps your template contract) ----------
# You can plug your HuggingFace function here if you want to keep AI reports.
# You can plug your HuggingFace function here to generate AI reports.

@app.route("/student_report", methods=["GET","POST"])
@login_required
def student_report():
    tutor_id = session.get("tutor_id")

    # only this tutor's students
    all_students = get_students()  
    student_names = sorted({s["name"] for s in all_students})

    vals = request.values
    selected_student = (vals.get("student") or "").strip()
    tab = vals.get("tab","payment")
    pay_start = vals.get("payment_start")
    pay_end   = vals.get("payment_end")
    att_start = vals.get("att_start")
    att_end   = vals.get("att_end")

    # payments (scoped by tutor)
    q = sb.table("payment_records").select("*").eq("tutor_id", tutor_id)
    if selected_student: 
        q = q.eq("name", selected_student)
    pays = q.execute().data or []

    dfp = pd.DataFrame(pays)
    if not dfp.empty and "cleared_date" in dfp:
        dfp["cleared_date"] = pd.to_datetime(dfp["cleared_date"], errors="coerce")
        if pay_start: dfp = dfp[dfp["cleared_date"] >= pd.to_datetime(pay_start)]
        if pay_end:   dfp = dfp[dfp["cleared_date"] <= pd.to_datetime(pay_end)]
    payment_records = dfp.to_dict("records") if not dfp.empty else []
    total_paid = float(dfp["amount"].sum()) if not dfp.empty and "amount" in dfp else 0.0

    # attendance (scoped by tutor)
    qa = sb.table("attendance").select("*").eq("tutor_id", tutor_id)
    if selected_student: 
        qa = qa.eq("name", selected_student)
    att = qa.execute().data or []

    dfa = pd.DataFrame(att)
    attendance_records, attendance_count, attendance_total_hours = [], 0, 0.0
    if not dfa.empty and "date" in dfa:
        dfa["date"] = pd.to_datetime(dfa["date"], errors="coerce")
        if att_start: dfa = dfa[dfa["date"] >= pd.to_datetime(att_start)]
        if att_end:   dfa = dfa[dfa["date"] <= pd.to_datetime(att_end)]
        dfa["Duration"] = pd.to_numeric(dfa.get("duration", 0), errors="coerce").fillna(0)
        dfa.rename(columns={"name":"Name","course":"Course","time":"Time"}, inplace=True)
        attendance_records = dfa.to_dict("records")
        attendance_count = len(attendance_records)
        attendance_total_hours = float(dfa["Duration"].sum())

    # AI report (optional)
    report_html = ""
    if selected_student and request.method == "POST":
        raw_markdown = generate_openai_report(selected_student, attendance_records, att_start, att_end, **request.form)
        report_html = markdown.markdown(raw_markdown)

    return render_template(
        "student_report.html",
        student_names=student_names,
        selected_student=selected_student,
        tab=tab,
        payment_records=payment_records,
        total_paid=total_paid,
        attendance_records=attendance_records,
        attendance_count=attendance_count,
        attendance_total_hours=attendance_total_hours,
        report=Markup(report_html)
    )


def generate_openai_report(student_name, attendance_records, start_date, end_date, **kwargs):
    # Retrieve the API key from environment variables
    OPENAI_API_KEY = os.getenv("OPEN_API_KEY", "")
    if not OPENAI_API_KEY:
        return "<p class='text-danger'>AI report generation failed: OpenAI API key not found.</p>"

    # Calculate attendance statistics
    total_classes = len(attendance_records)
    total_hours = sum(record.get('Duration', 0) for record in attendance_records) if attendance_records else 0
    avg_duration = total_hours / total_classes if total_classes > 0 else 0
    courses = list(set(record.get('Course', 'N/A') for record in attendance_records)) if attendance_records else []
    courses_str = ", ".join(courses) if courses else "No courses"

    # Build a comprehensive prompt
    prompt = f"""
Generate a detailed student performance report for {student_name} covering the period from {start_date} to {end_date}.

ATTENDANCE DATA:
- Total classes attended: {total_classes}
- Total hours: {total_hours:.1f} hours
- Average class duration: {avg_duration:.1f} hours
- Courses: {courses_str}

PERFORMANCE METRICS:
- Class Participation: {kwargs.get('participation', 'Not specified')}
- Effort & Consistency: {kwargs.get('effort', 'Not specified')}
- Progress Trend: {kwargs.get('progress', 'Not specified')}
- Homework Punctuality: {kwargs.get('punctuality', 'Not specified')}
- Homework Submission: {kwargs.get('homework_submission', 'Not specified')}

TEACHER OBSERVATIONS:
- Positive traits: {', '.join(kwargs.get('positive_traits', [])) if kwargs.get('positive_traits') else 'Not specified'}
- Areas needing attention: {', '.join(kwargs.get('needs_attention', [])) if kwargs.get('needs_attention') else 'Not specified'}
- Additional notes: {kwargs.get('free_text', 'No additional notes')}

Please create a professional, encouraging report in markdown format. Start with a title that includes the date range like:
"## {student_name}'s Performance Report ({start_date} to {end_date})"

Then use these headings with ####:
#### Overall Performance Summary
#### Class Attendance & Participation  
#### Academic Progress & Effort
#### Homework & Assignment Performance
#### Areas of Strength
#### Areas for Improvement
#### Teacher Recommendations

Use ## for the main title and #### for all section headings. Keep the tone professional but encouraging. Focus on specific insights rather than generic statements.
"""

    # OpenAI API call
    try:
        API_URL = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }

        data = {
            "model": "gpt-3.5-turbo",
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 800,
            "temperature": 0.7
        }

        response = requests.post(API_URL, headers=headers, json=data)

        if response.status_code == 200:
            result = response.json()
            generated_text = result['choices'][0]['message']['content']
            return generated_text
        else:
            print(f"OpenAI API Error: {response.text}")
            return f"<p class='text-danger'>AI report generation failed. Status code: {response.status_code}. Error: {response.text}</p>"

    except Exception as e:
        print(f"OpenAI API call failed: {e}")
        return f"<p class='text-danger'>AI report generation failed due to an exception: {e}</p>"
@app.route("/attendance/edit/<record_id>", methods=["GET", "POST"])
@login_required
def edit_attendance(record_id):
    tutor_id = session.get("tutor_id")

    # fetch only tutor's record
    record = (
        sb.table("attendance")
        .select("*")
        .eq("id", record_id)
        .eq("tutor_id", tutor_id)
        .execute()
        .data
    )
    if not record:
        return redirect(url_for("attendance"))

    if request.method == "POST":
        sb.table("attendance").update({
            "name": request.form.get("name"),
            "course": request.form.get("course"),
            "date": request.form.get("date"),
            "time": request.form.get("time"),
            "duration": request.form.get("duration")
        }).eq("id", record_id).eq("tutor_id", tutor_id).execute()
        return redirect(url_for("attendance"))

    return render_template("edit_attendance.html", record=record[0])


@app.route("/attendance/delete/<record_id>")
@login_required
def delete_attendance(record_id):
    tutor_id = session.get("tutor_id")

    sb.table("attendance").delete().eq("id", record_id).eq("tutor_id", tutor_id).execute()
    return redirect(url_for("attendance"))


# ---------- Student Dashboard Route ----------



# ------------------ MAIN ------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
