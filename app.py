from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from functools import wraps
from datetime import datetime, date, time
from dotenv import load_dotenv
from supabase import create_client
import os
from flask import abort
import re
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
HF_TOKEN      = os.getenv("HUGGINGFACE_API_KEY", "")
SECRET        = os.getenv("FLASK_SECRET", "dev")

sb = create_client(SUPABASE_URL, SERVICE_KEY)

app = Flask(__name__)
app.secret_key = SECRET
N8N_CREATE_URL= (os.getenv("N8N_CREATE_URL") or "").strip()
N8N_WEBHOOK_SECRET = "some random secret"
N8N_DELETE_URL = os.getenv("N8N_DELETE_URL", "")


# ---------- AUTH (same simple demo login) ----------
def login_required(f):
    @wraps(f)
    def _wrap(*a, **kw):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*a, **kw)
    return _wrap

import uuid

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
    student = sb.table("Students").select("*").eq("dashboard_token", token).execute().data
    if not student:
        abort(404)

    student = student[0]

    attendance = sb.table("attendance").select("*").eq("name", student["name"]).execute().data
    payments   = sb.table("payment_records").select("*").eq("name", student["name"]).execute().data
    schedule   = sb.table("class_schedule").select("*").eq("name", student["name"]).execute().data

    return render_template(
        "student_dashboard.html",
        student=student,
        attendance=attendance,
        payments=payments,
        schedule=schedule
    )


@app.route("/", methods=["GET","POST"])
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if request.form["username"] == "admin" and request.form["password"] == "1234":
            session["user"] = "admin"
            return redirect(url_for("dashboard"))
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))

# ---------- HELPERS ----------
def _to_num(x, default=0.0):
    try:
        return float(x)
    except:
        return default

def get_students():
    res = sb.table("Students").select("*").order("id").execute()
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

@app.route("/student/<int:student_id>")
@login_required
def student_profile(student_id):
    # fetch student details from Students table
    student = sb.table("Students").select("*").eq("id", student_id).execute().data
    if not student:
        return "Student not found", 404
    return render_template("student_profile.html", student=student[0])

@app.route("/submit", methods=["POST"])
def submit():
    payload = {
        "name": request.form["name"],
        "grade": int(request.form.get("grade") or 0),
        "course": request.form.get("course"),
        "school": request.form.get("school"),
        "email Id": request.form.get("email Id"),
        "mobile number": request.form.get("mobile number", None), # Use the helper function
        "hourly_rate": _to_num(request.form.get("hourly_rate"), 0.0),
        "dashboard_token": str(uuid.uuid4())  # ✅ add token
    }

    sb.table("Students").insert(payload).execute()

    return render_template(
        "dashboard.html",
        students=get_students(),
        unique_courses=get_unique_courses()
    )


# ---------- DASHBOARD ----------
@app.route("/dashboard")
@login_required
def dashboard():
    students = get_students()
    return render_template("dashboard.html", students=students, unique_courses=get_unique_courses())

# ---------- CRUD: edit/delete student (same routes) ----------
@app.route("/edit_student/<int:sid>", methods=["GET", "POST"])
def edit_student(sid):
    if request.method == "POST":
        updated = {
            "name": request.form.get("name", ""),
            "grade": int(request.form.get("grade") or 0),
            "course": request.form.get("course", ""),
            "school": request.form.get("school", ""),
            "email Id": request.form.get("email Id", ""),
            "mobile number": request.form.get("mobile number"), # Use the helper function
            "hourly_rate": _to_num(request.form.get("hourly_rate")), # Use the helper function
        }
        sb.table("Students").update(updated).eq("id", sid).execute()
        return redirect(url_for("dashboard"))

    # fetch existing student data
    student = sb.table("Students").select("*").eq("id", sid).single().execute().data
    return render_template("edit.html", student=student)


@app.route("/delete_student/<int:sid>")
@login_required
def delete_student(sid):
    # delete directly by id
    sb.table("Students").delete().eq("id", sid).execute()
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
    if not url:
        app.logger.error("n8n url empty; skipping")
        return
    try:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if N8N_WEBHOOK_SECRET:
            sig = hmac.new(N8N_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-LMS-Signature"] = sig
        r = requests.post(url, data=body, headers=headers, timeout=8)
        app.logger.info(f"n8n delete → {r.status_code} {r.text[:200]}")
    except Exception as e:
        app.logger.exception(f"n8n delete failed: {e}")


@app.route("/schedule", methods=["GET", "POST"])
@login_required
def schedule():
    # 1) Load existing schedule rows
    sched = sb.table("class_schedule").select("*").order("id").execute().data or []

    # 2) Fast list of student names for the dropdown
    name_rows = sb.table("Students").select("name").order("name").execute().data or []
    student_names = [r["name"] for r in name_rows]

    # 3) Map name -> email Id  (note the exact column: "email Id")
    stu_rows = sb.table("Students").select('name, "email Id"').execute().data or []
    emails = {r["name"]: (r.get("email Id") or "") for r in stu_rows}

    if request.method == "POST":
        name   = request.form["student"]
        course = request.form.get("course")
        d      = request.form.get("date")        # "YYYY-MM-DD"
        start  = request.form.get("time")        # "HH:MM"
        end_t  = request.form.get("end_time")    # "HH:MM"
        dur    = _to_num(request.form.get("duration"))

        # 4) Insert into Supabase and get the created row (with id)
        insert_payload = {
            "name": name,
            "course": course,
            "date": d,
            "start_time": start,
            "end_time": end_t,
            "duration": dur,
            "email Id": emails.get(name, "")
        }
        res = sb.table("class_schedule").insert(insert_payload).execute()
        created = (res.data or [{}])[0]

        # 5) Kick off n8n in the background (non-blocking)
        payload = {
            "event": "class_scheduled",
            "source": "likhil_lms",
            "row": created   # includes schedule id; n8n will create GCal & update meet_url there
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
    # Load rows in the same order the table shows
    rows = sb.table("class_schedule").select("*").order("id").execute().data or []
    if row_index < 0 or row_index >= len(rows):
        return "Not found", 404

    row = rows[row_index]
    schedule_id = row.get("id")
    event_id    = row.get("google_event_id")  # may be None/empty

    # 1) Tell n8n to delete the calendar event (if we have the ID, great; if not, n8n will look it up)
    payload = {"event":"class_deleted","source":"likhil_lms","row":{"id":schedule_id,"google_event_id":event_id}}
    _post_to_n8n_sync(N8N_DELETE_URL, payload)

    # 2) Delete the row from Supabase
    sb.table("class_schedule").delete().eq("id", schedule_id).execute()

    return redirect(url_for("schedule"))

def _fire_and_forget(url: str, payload: dict):
    """POST JSON to n8n; ignore failures so the UI remains snappy."""
    if not url:
        return
    try:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if N8N_WEBHOOK_SECRET:
            sig = hmac.new(N8N_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            headers["X-LMS-Signature"] = sig
        requests.post(url, data=raw, headers=headers, timeout=4)
    except Exception as e:
        app.logger.warning(f"n8n delete webhook failed: {e}")
# FINISH = move row to attendance then delete from schedule
@app.route("/schedule/finish/<int:index>")
@login_required
def finish_schedule(index):
    # 1) Load list (ordered by id so index matches your UI)
    sched = sb.table("class_schedule").select("*").order("id").execute().data or []
    if index < 0 or index >= len(sched):
        return "Not found", 404
    row = sched[index]

    # 2) Write to attendance
    sb.table("attendance").insert({
        "name":     row.get("name"),
        "course":   row.get("course"),
        "date":     row.get("date"),
        "time":     row.get("start_time"),
        "duration": _to_num(row.get("duration")),
    }).execute()

    # 3) Ask n8n to delete the Google Calendar event (if we have an id)
    geid = (row.get("google_event_id") or "").strip()
    if geid:
        _fire_and_forget(N8N_DELETE_URL, {
            "event": "class_finished",       # same delete flow can use this OR "class_deleted"
            "source": "likhil_lms",
            "row": {
                "id": row.get("id"),
                "google_event_id": geid
            }
        })

    # 4) Delete the schedule row itself
    sb.table("class_schedule").delete().eq("id", row["id"]).execute()

    return redirect(url_for("schedule"))
# ---------- ATTENDANCE (month filter kept) ----------
@app.route("/attendance")
@login_required
def attendance():
    rows = sb.table("attendance").select("*").order("date", desc=True).execute().data or []

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
    if request.method == "POST":
        sb.table("payment_records").insert({
            "name": request.form["name"],
            "course": request.form.get("course"),
            "amount": _to_num(request.form.get("amount")),
            "cleared_date": request.form.get("cleared_date"),
            "advance_hours": _to_num(request.form.get("advance_hours")),
            "advance_amount": _to_num(request.form.get("advance_amount")),
        }).execute()
        return redirect(url_for("payment_records"))

    records = sb.table("payment_records").select("*").order("cleared_date", desc=True).execute().data or []
    students = [s["name"] for s in get_students()]
    courses  = get_unique_courses()
    return render_template("payment_records.html", records=records, students=students, courses=courses)

# dues since last cleared date minus advance hours
@app.route("/payment_status", methods=["GET","POST"])
@login_required
def payment_status():
    # ✅ Get all student names for dropdown
    students = sorted([s["name"] for s in get_students()])
    status, class_details = None, []

    if request.method == "POST":
        selected = request.form.get("student")

        # ✅ Fetch Hourly Rate directly from Students table
        student_data = sb.table("Students").select("Hourly Rate").eq("name", selected).execute().data
        if not student_data:
            flash("No hourly rate found for this student. Please update their record.", "warning")
            return render_template("payment_status.html", students=students, status=None, classes=[])

        rate = _to_num(student_data[0].get("Hourly Rate", 0))

        # ✅ Fetch payment records
        pays = sb.table("payment_records").select("*").eq("name", selected)\
               .order("cleared_date").execute().data or []

        cleared_date = datetime(1970,1,1).date()
        adv_hrs, adv_amount = 0.0, 0.0
        if pays:
            last = pays[-1]
            cleared_date = pd.to_datetime(last["cleared_date"]).date()
            adv_hrs = _to_num(last.get("advance_hours"))
            adv_amount = _to_num(last.get("advance_amount"))

        # ✅ Attendance after cleared date
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
            # ✅ Proper pending amount calculation
            "pending_amount": round(pending_hrs * rate - adv_hrs * rate - adv_amount, 2),
        }

    return render_template("payment_status.html",
                           students=students, status=status, classes=class_details)


# ---------- RESCHEDULE (same route shape) ----------
@app.route("/schedule/reschedule/<int:row_index>", methods=["GET","POST"])
@login_required
def reschedule(row_index):
    rows = sb.table("class_schedule").select("*").order("id").execute().data or []
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
def generate_huggingface_report(student_name, **kwargs):
    # keep your existing implementation if needed; here return a safe stub
    return f"<h1 class='fs-3 fw-bold'>Performance Report for {student_name}</h1><p class='mb-3'>Report content…</p>"

@app.route("/student_report", methods=["GET","POST"])
@login_required
def student_report():
    all_students = get_students()
    student_names = sorted({s["name"] for s in all_students})

    vals = request.values
    selected_student = (vals.get("student") or "").strip()
    tab = vals.get("tab","payment")
    pay_start = vals.get("payment_start")
    pay_end   = vals.get("payment_end")
    att_start = vals.get("att_start")
    att_end   = vals.get("att_end")

    # payments
    q = sb.table("payment_records").select("*")
    if selected_student: q = q.eq("name", selected_student)
    pays = q.execute().data or []
    dfp = pd.DataFrame(pays)
    if not dfp.empty and "cleared_date" in dfp:
        dfp["cleared_date"] = pd.to_datetime(dfp["cleared_date"], errors="coerce")
        if pay_start: dfp = dfp[dfp["cleared_date"] >= pd.to_datetime(pay_start)]
        if pay_end:   dfp = dfp[dfp["cleared_date"] <= pd.to_datetime(pay_end)]
    payment_records = dfp.to_dict("records") if not dfp.empty else []
    total_paid = float(dfp["amount"].sum()) if not dfp.empty and "amount" in dfp else 0.0

    # attendance
    qa = sb.table("attendance").select("*")
    if selected_student: qa = qa.eq("name", selected_student)
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
    report_data = {
        "student_name": selected_student,
        "discipline": request.form.get("discipline"),
        "punctuality": request.form.get("punctuality"),
        "participation": request.form.get("participation"),
        "missed_classes": request.form.get("missed_classes", "0"),
        "absence_reason": request.form.get("absence_reason"),
        "homework_submission": request.form.get("homework_submission"),
        "missed_work_status": request.form.get("missed_work_status"),
        "behavior_comments": request.form.getlist("positive_traits"),
        "comments": request.form.getlist("needs_attention"),
        "free_text": request.form.get("free_text"),
        "start_date": request.form.get("start_date"),
        "end_date": request.form.get("end_date")
    }
    if "student_name" in report_data:
        del report_data["student_name"]
    report_html = generate_huggingface_report(selected_student, **report_data) if selected_student else ""
    report = Markup(report_html)

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
        report=report
    )

@app.route("/attendance/edit/<record_id>", methods=["GET", "POST"])
@login_required
def edit_attendance(record_id):
    # Fetch record by ID
    record = sb.table("attendance").select("*").eq("id", record_id).execute().data
    if not record:
        return redirect(url_for("attendance"))

    if request.method == "POST":
        name = request.form.get("name")
        course = request.form.get("course")
        date = request.form.get("date")
        time = request.form.get("time")
        duration = request.form.get("duration")

        sb.table("attendance").update({
            "name": name,
            "course": course,
            "date": date,
            "time": time,
            "duration": duration
        }).eq("id", record_id).execute()

        return redirect(url_for("attendance"))

    return render_template("edit_attendance.html", record=record[0])


@app.route("/attendance/delete/<record_id>")
@login_required
def delete_attendance(record_id):
    sb.table("attendance").delete().eq("id", record_id).execute()
    return redirect(url_for("attendance"))

# ---------- Student Dashboard Route ----------



# ------------------ MAIN ------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
