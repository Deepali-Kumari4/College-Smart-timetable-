from flask import Flask, render_template, request, redirect, session, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import timetable
import os
import time
import base64
from datetime import datetime
import json
import uuid
import re
import secrets
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_env_file(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "very-secret-key")
print("SMART TIMETABLE SERVER STARTED")
USERS_FILE = os.path.join(BASE_DIR, "users.txt")
PENDING_FILE = os.path.join(BASE_DIR, "users_pending.txt")
DATA_FILE = os.path.join(BASE_DIR, "data.txt")
TIMETABLE_FILE = os.path.join(BASE_DIR, "timetable_output.txt")
HISTORY_FILE = os.path.join(BASE_DIR, "approval_history.txt")
PREFERENCE_REQUESTS_FILE = os.path.join(BASE_DIR, "preference_requests.txt")
PREFERENCE_HISTORY_FILE = os.path.join(BASE_DIR, "preference_history.txt")
MAX_ROOM_CAPACITY = 120
PROFILE_UPLOAD_DIR = os.path.join(BASE_DIR, "static", "profile_pics")
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
EVENTS_FILE = os.path.join(BASE_DIR, "events.txt")
TIMETABLE_HISTORY_FILE = os.path.join(BASE_DIR, "timetable_history.txt")
SEMESTER_OPTIONS = [
    ("jan_apr", "Jan-Apr Semester"),
    ("aug_nov", "Aug-Nov Semester"),
    ("dec_vacation", "December Vacation"),
    ("jan_may", "Jan-May Semester")
]
ALLOWED_DEPARTMENTS = {"ALL", "CSE", "ECE", "IT", "ME"}
GOVERNMENT_HOLIDAYS = [
    ("01-26", "Republic Day"),
    ("08-15", "Independence Day"),
    ("10-02", "Gandhi Jayanti"),
    ("12-25", "Christmas")
]
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "http://127.0.0.1:5000/auth/google/callback"
).strip()
GOOGLE_ALLOWED_DOMAIN = os.environ.get("GOOGLE_ALLOWED_DOMAIN", "").strip().lower()
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_VERIFY_SERVICE_SID = os.environ.get("TWILIO_VERIFY_SERVICE_SID", "").strip()
OTP_EXPIRY_SECONDS = int(os.environ.get("OTP_EXPIRY_SECONDS", "300"))
OTP_MAX_ATTEMPTS = int(os.environ.get("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN_SECONDS = int(os.environ.get("OTP_RESEND_COOLDOWN_SECONDS", "30"))
OTP_MAX_REQUESTS_PER_HOUR = int(os.environ.get("OTP_MAX_REQUESTS_PER_HOUR", "5"))
MAX_EVENTS_PER_DAY = 3
_OTP_SESSION_STATE = {}
_OTP_PHONE_SEND_LOGS = {}


def infer_default_semester_key(month):
    if month in (1, 2, 3, 4):
        return "jan_apr"
    if month in (8, 9, 10, 11):
        return "aug_nov"
    if month == 12:
        return "dec_vacation"
    return "jan_may"


def build_semester_label(key, year):
    year_str = str(year).strip()
    labels = {
        "jan_apr": f"{year_str} Jan-Apr Semester",
        "aug_nov": f"{year_str} Aug-Nov Semester",
        "dec_vacation": f"{year_str} December Vacation",
        "jan_may": f"{year_str} Jan-May Semester"
    }
    return labels.get(key, f"{year_str} Jan-Apr Semester")


def append_line_safe(file_path, line):
    # Ensure appended records always start on a new line.
    needs_newline = False
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b"\n", b"\r")

    with open(file_path, "a") as f:
        if needs_newline:
            f.write("\n")
        f.write(line.rstrip("\n") + "\n")


def parse_user_line(line):
    parts = line.strip().split(",")
    if len(parts) < 4:
        return None
    profile_pic = parts[5] if len(parts) >= 6 else ""
    phone = parts[6] if len(parts) >= 7 else ""
    return {
        "email": parts[0],
        "hash": parts[1],
        "role": parts[2],
        "name": parts[3],
        "department": parts[4] if len(parts) >= 5 else "ALL",
        "profile_pic": profile_pic,
        "phone": phone
    }


def parse_pending_line(line):
    parts = line.strip().split(",")
    if len(parts) < 5:
        return None
    return {
        "email": parts[0],
        "name": parts[1],
        "department": parts[2],
        "role": parts[3],
        "hash": parts[4]
    }


def parse_course_line(line):
    parts = line.strip().split(",")
    if len(parts) < 6:
        return None

    subject = parts[0]
    teacher = parts[1]
    students = parts[2]

    # Backward-compatible: old file has no target department.
    if ":" in parts[3]:
        target = "ALL"
        prefs = parts[3:]
    else:
        target = parts[3] if parts[3] else "ALL"
        prefs = parts[4:]

    prefs = (prefs + ["-:-", "-:-", "-:-"])[:3]

    return {
        "subject": subject,
        "teacher": teacher,
        "students": students,
        "target": target,
        "prefs": prefs
    }


def serialize_course(course):
    return ",".join([
        course["subject"],
        course["teacher"],
        str(course["students"]),
        course.get("target", "ALL"),
        course["prefs"][0],
        course["prefs"][1],
        course["prefs"][2]
    ])


def parse_preference_request_line(line):
    parts = line.strip().split(",")
    if len(parts) < 8:
        return None
    return {
        "id": parts[0],
        "subject": parts[1],
        "teacher": parts[2],
        "students": parts[3],
        "target": parts[4],
        "prefs": [parts[5], parts[6], parts[7]]
    }


def serialize_preference_request(req):
    return ",".join([
        req["id"],
        req["subject"],
        req["teacher"],
        str(req["students"]),
        req.get("target", "ALL"),
        req["prefs"][0],
        req["prefs"][1],
        req["prefs"][2]
    ])


def load_preference_requests():
    requests = []
    if os.path.exists(PREFERENCE_REQUESTS_FILE):
        with open(PREFERENCE_REQUESTS_FILE) as f:
            for line in f:
                parsed = parse_preference_request_line(line)
                if parsed:
                    requests.append(parsed)
    return requests


def save_preference_requests(requests):
    with open(PREFERENCE_REQUESTS_FILE, "w") as f:
        for req in requests:
            f.write(serialize_preference_request(req) + "\n")


def load_courses():
    courses = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            for line in f:
                parsed = parse_course_line(line)
                if parsed:
                    courses.append(parsed)
    return courses


def save_courses(courses):
    with open(DATA_FILE, "w") as f:
        for course in courses:
            f.write(serialize_course(course) + "\n")


def normalize_prefs(pref_list):
    cleaned = []
    for p in pref_list:
        p = (p or "").strip()
        if not p or p == "-:-":
            continue
        if p not in cleaned:
            cleaned.append(p)
    return (cleaned + ["-:-", "-:-", "-:-"])[:3]


def find_course(courses, subject, teacher, target):
    for c in courses:
        if (
            c.get("subject", "") == subject
            and c.get("teacher", "") == teacher
            and c.get("target", "ALL") == target
        ):
            return c
    return None


def apply_timetable_delete(day, slot, subject, room, teacher, target):
    rows = load_timetable_rows()
    filtered = []
    deleted = False

    for row in rows:
        match = (
            row["day"] == day
            and row["slot"] == slot
            and row["subject"] == subject
            and row["room"] == room
            and row.get("teacher", "") == teacher
            and row.get("target", "ALL") == target
        )
        if match and not deleted:
            deleted = True
            continue
        filtered.append(row)

    if deleted:
        save_timetable_rows(filtered)

        # Keep generation source in sync.
        courses = load_courses()
        course = find_course(courses, subject, teacher, target or "ALL")
        if course:
            old_pref = f"{day}:{slot}"
            prefs = [p for p in course.get("prefs", []) if p != "-:-"]
            if old_pref in prefs:
                prefs.remove(old_pref)
                course["prefs"] = normalize_prefs(prefs)
                save_courses(courses)

    return deleted


def apply_timetable_update(old_row, new_row):
    rows = load_timetable_rows()
    target_index = -1
    for i, row in enumerate(rows):
        if (
            row.get("day", "") == old_row["day"]
            and row.get("slot", "") == old_row["slot"]
            and row.get("subject", "") == old_row["subject"]
            and row.get("room", "") == old_row["room"]
            and row.get("teacher", "") == old_row["teacher"]
            and row.get("target", "ALL") == old_row["target"]
        ):
            target_index = i
            break

    if target_index == -1:
        return False

    rows[target_index] = new_row
    save_timetable_rows(rows)

    # Keep generation source in sync for next "Generate".
    courses = load_courses()
    old_course = find_course(courses, old_row["subject"], old_row["teacher"], old_row["target"] or "ALL")
    new_course = find_course(courses, new_row["subject"], new_row["teacher"], new_row["target"])
    old_pref = f"{old_row['day']}:{old_row['slot']}"
    new_pref = f"{new_row['day']}:{new_row['slot']}"
    changed_courses = False

    if old_course:
        prefs = [p for p in old_course.get("prefs", []) if p != "-:-"]
        if old_pref in prefs:
            prefs.remove(old_pref)
            old_course["prefs"] = normalize_prefs(prefs)
            changed_courses = True

    if new_course:
        prefs = [p for p in new_course.get("prefs", []) if p != "-:-"]
        if new_pref not in prefs:
            if len(prefs) < 3:
                prefs.append(new_pref)
            else:
                prefs[-1] = new_pref
            new_course["prefs"] = normalize_prefs(prefs)
            changed_courses = True

    if changed_courses:
        save_courses(courses)

    return True


def parse_history_line(line):
    parts = line.strip().split(",")
    if len(parts) < 7:
        return None
    return {
        "timestamp": parts[0],
        "action": parts[1],
        "email": parts[2],
        "name": parts[3],
        "department": parts[4],
        "role": parts[5],
        "admin": parts[6]
    }


def log_admin_action(action, pending_user):
    admin_email = session.get("email", "admin")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_line_safe(
        HISTORY_FILE,
        (
            f"{timestamp},{action},{pending_user['email']},"
            f"{pending_user['name']},{pending_user['department']},"
            f"{pending_user['role']},{admin_email}"
        )
    )


def parse_preference_history_line(line):
    parts = line.strip().split(",")
    if len(parts) < 6:
        return None
    return {
        "timestamp": parts[0],
        "action": parts[1],
        "subject": parts[2],
        "teacher": parts[3],
        "target": parts[4],
        "admin": parts[5]
    }


def log_preference_action(action, request_data):
    admin_email = session.get("email", "admin")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_line_safe(
        PREFERENCE_HISTORY_FILE,
        (
            f"{timestamp},{action},{request_data['subject']},"
            f"{request_data['teacher']},{request_data['target']},{admin_email}"
        )
    )


def load_timetable_rows():
    rows = []
    if os.path.exists(TIMETABLE_FILE):
        with open(TIMETABLE_FILE) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 4:
                    continue
                rows.append({
                    "day": parts[0],
                    "slot": parts[1],
                    "subject": parts[2],
                    "room": parts[3],
                    "teacher": parts[4] if len(parts) >= 5 else "",
                    "target": parts[5] if len(parts) >= 6 else "ALL",
                    "label": parts[6] if len(parts) >= 7 else ""
                })
    return rows


def save_timetable_rows(rows):
    with open(TIMETABLE_FILE, "w") as f:
        for row in rows:
            base = [
                row.get("day", ""),
                row.get("slot", ""),
                row.get("subject", ""),
                row.get("room", ""),
                row.get("teacher", ""),
                row.get("target", "ALL")
            ]
            label = row.get("label", "").strip()
            if label:
                base.append(label)
            f.write(",".join(base) + "\n")


def load_users():
    users = []
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            for line in f:
                parsed = parse_user_line(line)
                if parsed:
                    users.append(parsed)
    return users


def save_users(users):
    with open(USERS_FILE, "w") as f:
        for u in users:
            department = u.get("department", "ALL") or "ALL"
            profile_pic = u.get("profile_pic", "")
            phone = u.get("phone", "")
            f.write(
                f"{u['email']},{u['hash']},{u['role']},"
                f"{u['name']},{department},{profile_pic},{phone}\n"
            )


def load_events():
    events = []
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if "id" in e and "title" in e and "date" in e:
                        events.append(e)
                except json.JSONDecodeError:
                    continue
    return events


def save_events(events):
    with open(EVENTS_FILE, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def build_government_holidays(year):
    holidays = []
    year_str = str(year).strip()
    for mm_dd, title in GOVERNMENT_HOLIDAYS:
        date_str = f"{year_str}-{mm_dd}"
        slug = title.lower().replace(" ", "-")
        holidays.append({
            "id": f"gov-{date_str}-{slug}",
            "title": title,
            "subject": title,
            "date": date_str,
            "type": "government_holiday",
            "important": True,
            "target": "ALL",
            "creator_name": "System",
            "creator_email": "system@smarttimetable.local",
            "creator_role": "system"
        })
    return holidays


def load_calendar_events():
    all_events = load_events()
    now_year = datetime.now().year
    # Keep holiday generation practical for current academic usage.
    year_range = range(now_year - 1, now_year + 3)
    generated = []
    for year in year_range:
        generated.extend(build_government_holidays(year))

    seen = set()
    merged = []
    for event in all_events + generated:
        key = (
            event.get("title", "").strip().lower(),
            event.get("date", "").strip()
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(event)

    merged.sort(key=lambda e: (e.get("date", ""), e.get("title", "")))
    return merged


def load_timetable_history():
    history = []
    if os.path.exists(TIMETABLE_HISTORY_FILE):
        with open(TIMETABLE_HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if "semester" in row and "generated_at" in row:
                        history.append(row)
                except json.JSONDecodeError:
                    continue
    history.sort(key=lambda x: x.get("generated_at", ""), reverse=True)
    return history


def group_timetable_history_by_semester(history_rows):
    grouped_map = {}
    order = []
    for h in history_rows:
        sem = h.get("semester", "Unknown Semester")
        if sem not in grouped_map:
            grouped_map[sem] = []
            order.append(sem)
        grouped_map[sem].append(h)

    grouped = []
    for sem in order:
        runs = grouped_map[sem]
        latest = runs[0] if runs else {}
        grouped.append({
            "semester": sem,
            "latest_generated_at": latest.get("generated_at", ""),
            "latest_rows": latest.get("total_rows", 0),
            "latest_subjects": latest.get("subjects", []),
            "generated_by": latest.get("generated_by", ""),
            "total_runs": len(runs),
            "runs": runs
        })
    return grouped


def log_timetable_history(semester, generated_by, rows):
    record = {
        "id": str(uuid.uuid4()),
        "semester": semester,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generated_by": generated_by,
        "total_rows": len(rows),
        "subjects": sorted(list({r.get("subject", "") for r in rows if r.get("subject", "")})),
        "rows": rows
    }
    append_line_safe(TIMETABLE_HISTORY_FILE, json.dumps(record))


def event_color(event_type):
    palette = {
        "exam": "#dc2626",
        "test": "#f59e0b",
        "vacation": "#16a34a",
        "government_holiday": "#0d9488",
        "general": "#2563eb"
    }
    return palette.get(event_type, "#2563eb")


def sanitize_label_color(color_value):
    value = (color_value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value.lower()
    return ""


def sanitize_target_department(value, default="ALL"):
    dept = (value or default).strip().upper()
    if dept in ALLOWED_DEPARTMENTS:
        return dept
    return default


def event_type_label(event_type):
    labels = {
        "general": "General",
        "test": "Test",
        "exam": "Exam",
        "vacation": "Official Holiday / Leave Day",
        "government_holiday": "National Celebration Day"
    }
    return labels.get(event_type, "General")


def to_calendar_event(event, viewer_email):
    custom_color = sanitize_label_color(event.get("custom_color", ""))
    creator_role = event.get("creator_role", "")
    if creator_role == "teacher":
        assigned_by_label = "Assigned by Faculty"
    elif creator_role == "admin":
        assigned_by_label = "Published by Administration"
    elif creator_role == "student":
        assigned_by_label = "Created by Student"
    else:
        assigned_by_label = "Published by System"
    return {
        "id": event["id"],
        "title": event["title"],
        "start": event["date"],
        "allDay": True,
        "color": custom_color if custom_color else event_color(event.get("type", "general")),
        "extendedProps": {
            "type": event.get("type", "general"),
            "type_label": event_type_label(event.get("type", "general")),
            "important": event.get("important", False),
            "custom_color": custom_color,
            "subject": event.get("subject", event.get("title", "")),
            "creator_name": event.get("creator_name", ""),
            "creator_email": event.get("creator_email", ""),
            "creator_role": creator_role,
            "target": sanitize_target_department(event.get("target", "ALL")),
            "assigned_by_label": assigned_by_label,
            "is_owner": event.get("creator_email", "") == viewer_email
        }
    }


def can_add_event_for_date(events, date_value, creator_role, creator_email, exclude_id=""):
    date_str = (date_value or "").strip()
    role = (creator_role or "").strip().lower()
    owner = (creator_email or "").strip().lower()
    exclude = (exclude_id or "").strip()
    if not date_str:
        return False

    count = 0
    for e in events:
        if (e.get("date", "") or "").strip() != date_str:
            continue
        if exclude and (e.get("id", "") or "").strip() == exclude:
            continue

        e_role = (e.get("creator_role", "") or "").strip().lower()
        e_owner = (e.get("creator_email", "") or "").strip().lower()

        # Shared stream (teacher/admin) capped together.
        if role in ("teacher", "admin"):
            if e_role in ("teacher", "admin"):
                count += 1
            continue

        # Student stream is private and capped per-student.
        if role == "student":
            if e_role == "student" and e_owner == owner:
                count += 1
            continue

    return count < MAX_EVENTS_PER_DAY


def get_upcoming_vacations(limit=10):
    today = datetime.now().strftime("%Y-%m-%d")
    vacations = []
    for e in load_calendar_events():
        if e.get("type", "") in ("vacation", "government_holiday") and e.get("date", "") >= today:
            vacations.append(e)
    vacations.sort(key=lambda x: x.get("date", ""))
    return vacations[:limit]


def infer_department_from_email(email):
    prefix = email.split("@")[0].lower()
    if prefix.startswith("cs"):
        return "CSE"
    if prefix.startswith("ec") or prefix.startswith("ece"):
        return "ECE"
    if prefix.startswith("it"):
        return "IT"
    if prefix.startswith("me"):
        return "ME"
    return "ALL"


def find_approved_user(email, role):
    email_norm = (email or "").strip().lower()
    role_norm = (role or "").strip().lower()
    for user in load_users():
        if user.get("role", "").strip().lower() != role_norm:
            continue
        if user.get("email", "").strip().lower() == email_norm:
            return user
    return None


def find_pending_user(email, role):
    email_norm = (email or "").strip().lower()
    role_norm = (role or "").strip().lower()
    if not os.path.exists(PENDING_FILE):
        return None
    with open(PENDING_FILE) as f:
        for line in f:
            pending = parse_pending_line(line)
            if not pending:
                continue
            if pending.get("role", "").strip().lower() != role_norm:
                continue
            if pending.get("email", "").strip().lower() == email_norm:
                return pending
    return None


def set_user_session(user):
    role = user.get("role", "").strip().lower()
    session["email"] = user.get("email", "")
    session["role"] = role
    session["name"] = user.get("name", "")
    dept = user.get("department", "ALL")
    if role == "student" and (not dept or dept.upper() == "ALL"):
        dept = infer_department_from_email(user.get("email", ""))
    session["department"] = dept
    session["profile_pic"] = user.get("profile_pic", "")


def role_dashboard_path(role):
    role_value = (role or "").strip().lower()
    if role_value == "admin":
        return "/admin/dashboard"
    if role_value == "teacher":
        return "/teacher/dashboard"
    if role_value == "student":
        return "/student/dashboard"
    return "/login"


def http_get_json(url, headers=None, timeout=12):
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def http_post_form_json(url, form_data, timeout=12):
    payload = urlencode(form_data).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = Request(url, data=payload, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def build_login_error_redirect(message):
    msg = (message or "Authentication failed.").strip()
    return redirect("/login?" + urlencode({"error": msg}))


def normalize_phone_e164_india(raw_phone):
    phone = re.sub(r"\D", "", (raw_phone or "").strip())
    if phone.startswith("0") and len(phone) == 11:
        phone = phone[1:]
    if len(phone) == 10:
        return f"+91{phone}"
    if len(phone) == 12 and phone.startswith("91"):
        return f"+{phone}"
    if len(phone) == 13 and phone.startswith("091"):
        return f"+91{phone[3:]}"
    if raw_phone and raw_phone.strip().startswith("+") and len(phone) == 12 and phone.startswith("91"):
        return raw_phone.strip()
    return ""


def twilio_post_form_json(url, form_data, timeout=12):
    payload = urlencode(form_data).encode("utf-8")
    token = f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")
    auth_header = "Basic " + base64.b64encode(token).decode("ascii")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": auth_header
    }
    req = Request(url, data=payload, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def otp_session_key(email, role):
    return f"{(email or '').strip().lower()}|{(role or '').strip().lower()}"


def prune_phone_send_log(phone):
    now = int(time.time())
    entries = _OTP_PHONE_SEND_LOGS.get(phone, [])
    entries = [ts for ts in entries if now - ts <= 3600]
    _OTP_PHONE_SEND_LOGS[phone] = entries
    return entries


def save_otp_session(email, role, phone):
    key = otp_session_key(email, role)
    _OTP_SESSION_STATE[key] = {
        "email": (email or "").strip().lower(),
        "role": (role or "").strip().lower(),
        "phone": phone,
        "sent_at": int(time.time()),
        "attempts": 0
    }


def get_otp_session(email, role):
    key = otp_session_key(email, role)
    return _OTP_SESSION_STATE.get(key)


def mask_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "******"
    return f"+{digits[:2]}******{digits[-2:]}"


def get_google_missing_keys():
    missing = []
    if not GOOGLE_CLIENT_ID:
        missing.append("GOOGLE_CLIENT_ID")
    if not GOOGLE_CLIENT_SECRET:
        missing.append("GOOGLE_CLIENT_SECRET")
    return missing


# =====================================================
# HOME → REDIRECT TO LOGIN
# =====================================================
@app.route("/")
def home():
    return redirect("/login")


# =====================================================
# LOGIN (ADMIN / TEACHER / STUDENT)
# =====================================================
@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "GET":
        google_missing = get_google_missing_keys()
        return render_template(
            "login.html",
            message=request.args.get("message", ""),
            error=request.args.get("error", ""),
            google_enabled=len(google_missing) == 0,
            google_missing=", ".join(google_missing)
        )

    # ---------------- ADMIN / TEACHER ----------------
    role = request.form.get("role", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "").strip()

    if not os.path.exists(USERS_FILE):
        return redirect("/login?error=No+users+found.+Admin+must+create+accounts.")

    with open(USERS_FILE) as f:
        for line in f:
            user = parse_user_line(line)
            if not user:
                continue

            if (
                user["email"] == email
                and user["role"] == role
                and check_password_hash(user["hash"], password)
            ):
                set_user_session(user)

                if role == "admin":
                    return redirect("/admin/dashboard")
                if role == "teacher":
                    return redirect("/teacher/dashboard")
                if role == "student":
                    return redirect("/student/dashboard")

    return redirect("/login?error=Invalid+credentials+or+not+approved+yet.")


@app.route("/auth/google")
def auth_google():
    mode = request.args.get("mode", "login").strip().lower()
    role = request.args.get("role", "").strip().lower()
    login_hint = request.args.get("login_hint", "").strip().lower()
    if mode not in ("login", "signup"):
        return redirect("/login?error=Invalid+Google+auth+request.")
    if role not in ("teacher", "student"):
        return redirect("/login?error=Select+Teacher+or+Student+before+Google+auth.")

    department = request.args.get("department", "").strip().upper()
    if mode == "signup":
        if not department:
            return redirect("/login?error=Department+is+required+for+Google+signup.")
        if department not in ALLOWED_DEPARTMENTS:
            return redirect("/login?error=Invalid+department+for+Google+signup.")

    missing = get_google_missing_keys()
    if missing:
        missing_text = ",".join(missing)
        return redirect("/login?error=Google+Sign-In+not+configured:+missing+" + missing_text)

    state = secrets.token_urlsafe(24)
    session["google_oauth_state"] = state
    session["google_oauth_mode"] = mode
    session["google_login_role"] = role
    session["google_signup_department"] = department if mode == "signup" else ""
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state
    }
    if login_hint and "@" in login_hint:
        params["login_hint"] = login_hint
    if GOOGLE_ALLOWED_DOMAIN:
        params["hd"] = GOOGLE_ALLOWED_DOMAIN
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return redirect(auth_url)


@app.route("/auth/google/callback")
def auth_google_callback():
    oauth_error = request.args.get("error", "").strip()
    if oauth_error:
        return redirect("/login?error=Google+login+was+cancelled+or+failed.")

    state = request.args.get("state", "").strip()
    expected_state = session.pop("google_oauth_state", "")
    mode = session.pop("google_oauth_mode", "login").strip().lower()
    role = session.pop("google_login_role", "").strip().lower()
    signup_department = session.pop("google_signup_department", "").strip().upper()
    code = request.args.get("code", "").strip()

    if not state or not expected_state or state != expected_state:
        return redirect("/login?error=Google+login+state+mismatch.+Try+again.")
    if mode not in ("login", "signup"):
        return redirect("/login?error=Invalid+Google+auth+mode.")
    if role not in ("teacher", "student"):
        return redirect("/login?error=Invalid+role+for+Google+login.")
    if not code:
        return redirect("/login?error=Google+authorization+code+missing.")

    try:
        token_data = http_post_form_json(
            "https://oauth2.googleapis.com/token",
            {
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code"
            }
        )
    except HTTPError as e:
        message = "Google token exchange failed."
        try:
            payload = json.loads(e.read().decode("utf-8"))
            message = payload.get("error_description") or payload.get("error") or message
        except Exception:
            pass
        return build_login_error_redirect(f"Google login failed: {message}")
    except (URLError, TimeoutError, ValueError):
        return build_login_error_redirect("Google login failed due to network/timeout. Please retry.")

    id_token = token_data.get("id_token", "")
    access_token = token_data.get("access_token", "")
    if not id_token and not access_token:
        return build_login_error_redirect("Google token exchange succeeded but no usable token was returned.")

    token_info = {}
    if id_token:
        try:
            token_info = http_get_json(
                "https://oauth2.googleapis.com/tokeninfo?" + urlencode({"id_token": id_token})
            )
        except Exception:
            token_info = {}

    if not token_info and access_token:
        try:
            token_info = http_get_json(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"}
            )
        except HTTPError as e:
            message = "Unable to read Google user profile."
            try:
                payload = json.loads(e.read().decode("utf-8"))
                message = payload.get("error_description") or payload.get("error") or message
            except Exception:
                pass
            return build_login_error_redirect(f"Google login verification failed: {message}")
        except (URLError, TimeoutError, ValueError):
            return build_login_error_redirect("Unable to verify Google account details right now. Please retry.")

    if not token_info:
        return build_login_error_redirect("Google login verification failed. Please retry in a minute.")

    issuer = token_info.get("iss", "")
    audience = token_info.get("aud", "")
    email_verified = token_info.get("email_verified", "")
    email = token_info.get("email", "").strip()
    name = token_info.get("name", "").strip()

    if issuer and issuer not in ("https://accounts.google.com", "accounts.google.com"):
        return build_login_error_redirect("Invalid Google token issuer.")
    if audience and audience != GOOGLE_CLIENT_ID:
        return build_login_error_redirect("Invalid Google token audience.")
    if str(email_verified).lower() not in ("true", "1"):
        return build_login_error_redirect("Google email must be verified.")
    if not email:
        return build_login_error_redirect("Unable to read email from Google account.")
    if GOOGLE_ALLOWED_DOMAIN:
        email_domain = email.split("@")[-1].lower() if "@" in email else ""
        if email_domain != GOOGLE_ALLOWED_DOMAIN:
            return build_login_error_redirect("Use your institutional Google account only.")

    if mode == "signup":
        if not name:
            name = email.split("@")[0]
        department = signup_department or infer_department_from_email(email)
        if department not in ALLOWED_DEPARTMENTS:
            department = "ALL"

        if find_approved_user(email, role):
            return redirect("/login?message=Account+already+approved.+Use+Google+login+or+password+login.")
        if find_pending_user(email, role):
            return redirect("/login?message=Google+signup+already+pending+admin+approval.")

        random_password_hash = generate_password_hash(secrets.token_urlsafe(24))
        append_line_safe(PENDING_FILE, f"{email},{name},{department},{role},{random_password_hash}")
        return redirect("/login?message=Google+signup+submitted.+Wait+for+admin+approval.")

    user = find_approved_user(email, role)
    if not user:
        return redirect("/login?error=No+approved+account+for+this+email+and+role.")

    set_user_session(user)

    if role == "teacher":
        return redirect("/teacher/dashboard")
    return redirect("/student/dashboard")


# =====================================================
# PHONE OTP LOGIN (TEACHER / STUDENT)
# =====================================================
@app.route("/auth/phone/send-otp", methods=["POST"])
def auth_phone_send_otp():
    role = (request.form.get("role") or "").strip().lower()
    email = (request.form.get("email") or "").strip().lower()
    phone = normalize_phone_e164_india(request.form.get("phone", ""))

    if role not in ("teacher", "student"):
        return jsonify({"ok": False, "error": "Select Teacher or Student role."}), 400
    if not email or not phone:
        return jsonify({"ok": False, "error": "Email and valid Indian phone number are required."}), 400
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_VERIFY_SERVICE_SID:
        return jsonify({"ok": False, "error": "Phone OTP is not configured on server."}), 500

    user = find_approved_user(email, role)
    if not user:
        return jsonify({"ok": False, "error": "No approved account for this email and role."}), 404

    existing_phone = normalize_phone_e164_india(user.get("phone", ""))
    if existing_phone and existing_phone != phone:
        return jsonify({"ok": False, "error": "This account is already linked to a different phone."}), 409

    otp_state = get_otp_session(email, role)
    now = int(time.time())
    if otp_state and now - otp_state.get("sent_at", 0) < OTP_RESEND_COOLDOWN_SECONDS:
        return jsonify({"ok": False, "error": f"Please wait {OTP_RESEND_COOLDOWN_SECONDS} seconds before resending OTP."}), 429

    recent_sends = prune_phone_send_log(phone)
    if len(recent_sends) >= OTP_MAX_REQUESTS_PER_HOUR:
        return jsonify({"ok": False, "error": "OTP limit reached for this phone. Try again in an hour."}), 429

    verify_url = f"https://verify.twilio.com/v2/Services/{TWILIO_VERIFY_SERVICE_SID}/Verifications"
    try:
        twilio_post_form_json(verify_url, {"To": phone, "Channel": "sms"})
    except HTTPError as e:
        details = "Unable to send OTP right now."
        try:
            body = e.read().decode("utf-8")
            payload = json.loads(body)
            details = payload.get("message", details)
        except Exception:
            pass
        return jsonify({"ok": False, "error": details}), 502
    except (URLError, TimeoutError, ValueError):
        return jsonify({"ok": False, "error": "Unable to send OTP right now."}), 502

    if not existing_phone:
        users = load_users()
        for u in users:
            if (
                (u.get("email", "").strip().lower() == email)
                and (u.get("role", "").strip().lower() == role)
            ):
                u["phone"] = phone
                break
        save_users(users)

    recent_sends.append(now)
    _OTP_PHONE_SEND_LOGS[phone] = recent_sends
    save_otp_session(email, role, phone)

    return jsonify({
        "ok": True,
        "message": f"OTP sent to {mask_phone(phone)}.",
        "cooldown_seconds": OTP_RESEND_COOLDOWN_SECONDS,
        "expires_in_seconds": OTP_EXPIRY_SECONDS
    })


@app.route("/auth/phone/verify-otp", methods=["POST"])
def auth_phone_verify_otp():
    role = (request.form.get("role") or "").strip().lower()
    email = (request.form.get("email") or "").strip().lower()
    phone = normalize_phone_e164_india(request.form.get("phone", ""))
    code = (request.form.get("otp") or "").strip()

    if role not in ("teacher", "student"):
        return jsonify({"ok": False, "error": "Select Teacher or Student role."}), 400
    if not email or not phone or not code:
        return jsonify({"ok": False, "error": "Role, email, phone and OTP are required."}), 400
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_VERIFY_SERVICE_SID:
        return jsonify({"ok": False, "error": "Phone OTP is not configured on server."}), 500

    otp_state = get_otp_session(email, role)
    if not otp_state:
        return jsonify({"ok": False, "error": "Send OTP first."}), 400
    if otp_state.get("phone", "") != phone:
        return jsonify({"ok": False, "error": "Phone number does not match the OTP request."}), 400

    now = int(time.time())
    sent_at = int(otp_state.get("sent_at", 0))
    if now - sent_at > OTP_EXPIRY_SECONDS:
        _OTP_SESSION_STATE.pop(otp_session_key(email, role), None)
        return jsonify({"ok": False, "error": "OTP expired. Send a new OTP."}), 400

    attempts = int(otp_state.get("attempts", 0))
    if attempts >= OTP_MAX_ATTEMPTS:
        _OTP_SESSION_STATE.pop(otp_session_key(email, role), None)
        return jsonify({"ok": False, "error": "Maximum OTP attempts reached. Send new OTP."}), 429

    verify_check_url = f"https://verify.twilio.com/v2/Services/{TWILIO_VERIFY_SERVICE_SID}/VerificationCheck"
    try:
        payload = twilio_post_form_json(verify_check_url, {"To": phone, "Code": code})
    except HTTPError as e:
        details = "Unable to verify OTP right now."
        try:
            body = e.read().decode("utf-8")
            parsed = json.loads(body)
            details = parsed.get("message", details)
        except Exception:
            pass
        return jsonify({"ok": False, "error": details}), 502
    except (URLError, TimeoutError, ValueError):
        return jsonify({"ok": False, "error": "Unable to verify OTP right now."}), 502

    if payload.get("status", "").strip().lower() != "approved":
        otp_state["attempts"] = attempts + 1
        remaining = max(OTP_MAX_ATTEMPTS - otp_state["attempts"], 0)
        if remaining == 0:
            _OTP_SESSION_STATE.pop(otp_session_key(email, role), None)
            return jsonify({"ok": False, "error": "Invalid OTP. Maximum attempts reached."}), 429
        return jsonify({"ok": False, "error": f"Invalid OTP. {remaining} attempt(s) left."}), 400

    user = find_approved_user(email, role)
    if not user:
        _OTP_SESSION_STATE.pop(otp_session_key(email, role), None)
        return jsonify({"ok": False, "error": "No approved account for this email and role."}), 404

    linked_phone = normalize_phone_e164_india(user.get("phone", ""))
    if linked_phone and linked_phone != phone:
        _OTP_SESSION_STATE.pop(otp_session_key(email, role), None)
        return jsonify({"ok": False, "error": "This account is linked to a different phone."}), 409

    set_user_session(user)
    _OTP_SESSION_STATE.pop(otp_session_key(email, role), None)
    return jsonify({"ok": True, "redirect": role_dashboard_path(role), "message": "Phone OTP login successful."})


# =====================================================
# SIGNUP REQUEST (TEACHER / STUDENT)
# =====================================================
@app.route("/signup", methods=["POST"])
def signup():

    role = request.form.get("role", "").strip()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    department = request.form.get("department", "").strip()
    password = request.form.get("password", "").strip()

    if role not in ("teacher", "student"):
        return redirect("/login?error=Please+select+Teacher+or+Student+for+signup.")

    if not name or not email or not department or not password:
        return redirect("/login?error=All+signup+fields+are+required.")

    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            for line in f:
                user = parse_user_line(line)
                if user and user["email"] == email and user["role"] == role:
                    return redirect("/login?error=Account+already+exists.+Please+login.")

    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            for line in f:
                pending = parse_pending_line(line)
                if pending and pending["email"] == email and pending["role"] == role:
                    return redirect("/login?error=Signup+request+already+pending+admin+approval.")

    hashed = generate_password_hash(password)
    append_line_safe(PENDING_FILE, f"{email},{name},{department},{role},{hashed}")

    return redirect("/login?message=Signup+request+submitted.+Wait+for+admin+approval.")


# =====================================================
# FORGOT PASSWORD (APPROVED USERS ONLY)
# =====================================================
@app.route("/forgot-password", methods=["POST"])
def forgot_password():
    role = request.form.get("role", "").strip()
    email = request.form.get("email", "").strip()
    new_password = request.form.get("new_password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()

    if role not in ("teacher", "student"):
        return redirect("/login?error=Password+reset+is+available+only+for+Teacher+and+Student.")
    if not email or not new_password or not confirm_password:
        return redirect("/login?error=All+forgot+password+fields+are+required.")
    if new_password != confirm_password:
        return redirect("/login?error=New+password+and+confirm+password+must+match.")
    if len(new_password) < 8:
        return redirect("/login?error=Password+must+be+at+least+8+characters.")
    if not re.search(r"[A-Z]", new_password):
        return redirect("/login?error=Password+must+include+at+least+one+uppercase+letter.")
    if not re.search(r"[a-z]", new_password):
        return redirect("/login?error=Password+must+include+at+least+one+lowercase+letter.")
    if not re.search(r"\d", new_password):
        return redirect("/login?error=Password+must+include+at+least+one+number.")

    users = load_users()
    target = None
    for user in users:
        if user.get("email", "") == email and user.get("role", "") == role:
            target = user
            break

    if not target:
        return redirect("/login?error=Password+reset+allowed+only+for+registered+approved+users.")

    target["hash"] = generate_password_hash(new_password)
    save_users(users)
    return redirect("/login?message=Password+updated+successfully.+Please+login.")


# =====================================================
# LOGOUT
# =====================================================
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# =====================================================
# ADMIN DASHBOARD
# =====================================================
@app.route("/admin/dashboard")
def admin_dashboard():

    if session.get("role") != "admin":
        return redirect("/login")

    pending = []
    history = []
    preference_requests = load_preference_requests()
    preference_history = []
    timetable_rows = load_timetable_rows()
    timetable_history = load_timetable_history()
    timetable_history_grouped = group_timetable_history_by_semester(timetable_history)
    now = datetime.now()
    default_semester_key = infer_default_semester_key(now.month)
    default_semester_year = now.year
    admin_events = load_events()
    admin_events.sort(key=lambda x: x.get("date", ""))
    vacations = get_upcoming_vacations()
    users = load_users()
    courses = load_courses()
    teacher_cards = []

    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            for line in f:
                parsed = parse_pending_line(line)
                if parsed:
                    pending.append(parsed)

    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            for line in f:
                parsed = parse_history_line(line)
                if parsed:
                    history.append(parsed)

    if os.path.exists(PREFERENCE_HISTORY_FILE):
        with open(PREFERENCE_HISTORY_FILE) as f:
            for line in f:
                parsed = parse_preference_history_line(line)
                if parsed:
                    preference_history.append(parsed)

    history.reverse()
    preference_history.reverse()

    for user in users:
        if user["role"] != "teacher":
            continue

        teacher_name = user["name"]
        teacher_courses = [c for c in courses if c["teacher"] == teacher_name]
        teacher_rows = [r for r in timetable_rows if r.get("teacher", "") == teacher_name]
        absent_count = len([r for r in teacher_rows if r.get("label", "") == "Teacher Absent"])

        teacher_cards.append({
            "name": teacher_name,
            "email": user["email"],
            "department": user.get("department", "ALL"),
            "profile_pic": user.get("profile_pic", ""),
            "courses": teacher_courses,
            "timetable_count": len(teacher_rows),
            "absent_count": absent_count,
            "is_all_absent": len(teacher_rows) > 0 and absent_count == len(teacher_rows)
        })

    return render_template(
        "admin.html",
        pending=pending,
        history=history,
        preference_requests=preference_requests,
        preference_history=preference_history,
        timetable_rows=timetable_rows,
        timetable_history=timetable_history,
        timetable_history_grouped=timetable_history_grouped,
        approved_courses_count=len(courses),
        approved_course_stack=sorted(courses, key=lambda c: (c.get("teacher", ""), c.get("subject", ""))),
        semester_options=SEMESTER_OPTIONS,
        default_semester_key=default_semester_key,
        default_semester_year=default_semester_year,
        teacher_cards=teacher_cards,
        admin_events=admin_events,
        vacations=vacations,
        admin_name=session.get("name", "Admin"),
        admin_email=session.get("email", ""),
        admin_profile_pic=session.get("profile_pic", ""),
        message=request.args.get("message", ""),
        error=request.args.get("error", "")
    )


# =====================================================
# ADMIN APPROVE TEACHER
# =====================================================
@app.route("/admin/approve")
def approve_teacher():

    if session.get("role") != "admin":
        return "Unauthorized"

    email = request.args.get("email")
    if not email:
        return redirect("/admin/dashboard")

    approved = None
    remaining = []

    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            for line in f:
                pending = parse_pending_line(line)
                if not pending:
                    continue
                if approved is None and pending["email"] == email:
                    approved = pending
                else:
                    remaining.append(line)

        with open(PENDING_FILE, "w") as f:
            f.writelines(remaining)

    if not approved:
        return redirect("/admin/dashboard")

    append_line_safe(
        USERS_FILE,
        (
            f"{approved['email']},{approved['hash']},{approved['role']},"
            f"{approved['name']},{approved['department']}"
        )
    )
    log_admin_action("approved", approved)
    return redirect("/admin/dashboard")


# =====================================================
# ADMIN REJECT
# =====================================================
@app.route("/admin/reject")
def reject_teacher():

    if session.get("role") != "admin":
        return "Unauthorized"

    email = request.args.get("email")
    role = request.args.get("role")
    rejected = None

    if os.path.exists(PENDING_FILE):
        remaining = []
        with open(PENDING_FILE) as f:
            for line in f:
                pending = parse_pending_line(line)
                if not pending:
                    continue
                if (
                    pending["email"] == email
                    and (role is None or pending["role"] == role)
                ):
                    if rejected is None:
                        rejected = pending
                    continue
                else:
                    remaining.append(line)

        with open(PENDING_FILE, "w") as f:
            f.writelines(remaining)

    if rejected:
        log_admin_action("rejected", rejected)

    return redirect("/admin/dashboard")


# =====================================================
# ADMIN PREFERENCE APPROVAL
# =====================================================
@app.route("/admin/preferences/approve")
def approve_preference():

    if session.get("role") != "admin":
        return "Unauthorized"

    request_id = request.args.get("id", "")
    requests = load_preference_requests()
    approved = None
    remaining = []

    for req in requests:
        if approved is None and req["id"] == request_id:
            approved = req
        else:
            remaining.append(req)

    if not approved:
        return redirect("/admin/dashboard?section=preferences-section")

    try:
        if int(approved["students"]) > MAX_ROOM_CAPACITY:
            return redirect(
                f"/admin/dashboard?error=Cannot+approve:+students+exceed+max+room+capacity+({MAX_ROOM_CAPACITY}).+Please+edit+request.&section=preferences-section"
            )
    except ValueError:
        return redirect("/admin/dashboard?error=Invalid+students+count+in+request.&section=preferences-section")

    courses = load_courses()
    upserted = False
    for i, course in enumerate(courses):
        if (
            course["subject"] == approved["subject"]
            and course["teacher"] == approved["teacher"]
        ):
            courses[i] = {
                "subject": approved["subject"],
                "teacher": approved["teacher"],
                "students": approved["students"],
                "target": approved["target"],
                "prefs": approved["prefs"]
            }
            upserted = True
            break

    if not upserted:
        courses.append({
            "subject": approved["subject"],
            "teacher": approved["teacher"],
            "students": approved["students"],
            "target": approved["target"],
            "prefs": approved["prefs"]
        })

    save_courses(courses)
    save_preference_requests(remaining)
    log_preference_action("approved", approved)
    return redirect("/admin/dashboard?message=Preference+approved.+Review+in+Generate+Timetable.&section=generate-section")


@app.route("/admin/preferences/reject")
def reject_preference():

    if session.get("role") != "admin":
        return "Unauthorized"

    request_id = request.args.get("id", "")
    requests = load_preference_requests()
    rejected = None
    remaining = []

    for req in requests:
        if rejected is None and req["id"] == request_id:
            rejected = req
        else:
            remaining.append(req)

    save_preference_requests(remaining)
    if rejected:
        log_preference_action("rejected", rejected)
    return redirect("/admin/dashboard")


@app.route("/admin/preferences/edit", methods=["GET", "POST"])
def edit_preference():

    if session.get("role") != "admin":
        return "Unauthorized"

    request_id = request.args.get("id", "").strip()
    requests = load_preference_requests()
    target_req = None
    idx = -1

    for i, req in enumerate(requests):
        if req["id"] == request_id:
            target_req = req
            idx = i
            break

    if target_req is None:
        return redirect("/admin/dashboard")

    if request.method == "POST":
        target_req["subject"] = request.form.get("subject", "").strip()
        target_req["students"] = request.form.get("students", "").strip()
        target_req["target"] = request.form.get("target", "ALL").strip() or "ALL"
        try:
            if int(target_req["students"]) > MAX_ROOM_CAPACITY:
                return redirect(
                    f"/admin/preferences/edit?id={request_id}&error=Students+exceed+max+room+capacity+({MAX_ROOM_CAPACITY})."
                )
        except ValueError:
            return redirect(f"/admin/preferences/edit?id={request_id}&error=Invalid+students+count.")

        target_req["prefs"] = [
            f"{request.form.get('day1', '-').strip()}:{request.form.get('slot1', '-').strip()}",
            f"{request.form.get('day2', '-').strip()}:{request.form.get('slot2', '-').strip()}",
            f"{request.form.get('day3', '-').strip()}:{request.form.get('slot3', '-').strip()}"
        ]

        # Keep id consistent with teacher+subject.
        target_req["id"] = f"{target_req['teacher']}|{target_req['subject']}".lower()
        requests[idx] = target_req
        save_preference_requests(requests)
        log_preference_action("edited", target_req)
        return redirect("/admin/dashboard")

    day_slot = []
    for pref in target_req["prefs"]:
        if ":" in pref:
            day_slot.append(pref.split(":", 1))
        else:
            day_slot.append(["-", "-"])
    while len(day_slot) < 3:
        day_slot.append(["-", "-"])

    return render_template(
        "admin_edit_preference.html",
        req=target_req,
        day_slot=day_slot,
        error=request.args.get("error", "")
    )


# =====================================================
# GENERATE TIMETABLE
# =====================================================
@app.route("/generate", methods=["POST"])
def generate():

    if session.get("role") != "admin":
        return "Unauthorized"

    semester = request.form.get("semester", "").strip()
    semester_key = request.form.get("semester_key", "").strip()
    semester_year = request.form.get("semester_year", "").strip() or str(datetime.now().year)
    if not semester:
        semester = build_semester_label(semester_key, semester_year)
    ok, msg = timetable.run()
    if ok:
        rows = load_timetable_rows()
        log_timetable_history(
            semester=semester,
            generated_by=session.get("email", "admin"),
            rows=rows
        )
        return redirect("/admin/dashboard?message=Timetable+generated+successfully.")
    return redirect("/admin/dashboard?error=" + msg.replace(" ", "+"))


@app.route("/admin/timetable/delete")
def delete_timetable_entry():

    if session.get("role") != "admin":
        return "Unauthorized"

    day = request.args.get("day", "")
    slot = request.args.get("slot", "")
    subject = request.args.get("subject", "")
    room = request.args.get("room", "")
    teacher = request.args.get("teacher", "")
    target = request.args.get("target", "")
    section = request.args.get("section", "").strip()

    apply_timetable_delete(day, slot, subject, room, teacher, target)

    redirect_url = "/admin/dashboard?message=Timetable+entry+deleted."
    if section:
        redirect_url += "&section=" + section
    return redirect(redirect_url)


@app.route("/admin/timetable/delete_api", methods=["POST"])
def delete_timetable_entry_api():
    if session.get("role") != "admin":
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    day = request.form.get("day", "").strip()
    slot = request.form.get("slot", "").strip()
    subject = request.form.get("subject", "").strip()
    room = request.form.get("room", "").strip()
    teacher = request.form.get("teacher", "").strip()
    target = request.form.get("target", "").strip()

    deleted = apply_timetable_delete(day, slot, subject, room, teacher, target)
    if not deleted:
        return jsonify({"ok": False, "error": "Entry not found"}), 404
    return jsonify({"ok": True})


@app.route("/admin/timetable/label_absent")
def label_timetable_absent():

    if session.get("role") != "admin":
        return "Unauthorized"

    day = request.args.get("day", "")
    slot = request.args.get("slot", "")
    subject = request.args.get("subject", "")
    room = request.args.get("room", "")
    teacher = request.args.get("teacher", "")
    target = request.args.get("target", "")
    clear = request.args.get("clear", "0") == "1"

    rows = load_timetable_rows()
    updated = False
    for row in rows:
        match = (
            row["day"] == day
            and row["slot"] == slot
            and row["subject"] == subject
            and row["room"] == room
            and row.get("teacher", "") == teacher
            and row.get("target", "ALL") == target
        )
        if match and not updated:
            row["label"] = "" if clear else "Teacher Absent"
            updated = True

    save_timetable_rows(rows)
    if clear:
        return redirect("/admin/dashboard?message=Timetable+label+cleared.")
    return redirect("/admin/dashboard?message=Absent+label+added.")


@app.route("/admin/timetable/edit", methods=["GET", "POST"])
def edit_timetable_entry():

    if session.get("role") != "admin":
        return "Unauthorized"

    old_day = request.values.get("old_day", "").strip()
    old_slot = request.values.get("old_slot", "").strip()
    old_subject = request.values.get("old_subject", "").strip()
    old_room = request.values.get("old_room", "").strip()
    old_teacher = request.values.get("old_teacher", "").strip()
    old_target = request.values.get("old_target", "").strip()
    source_section = request.values.get("source_section", "").strip() or "generate-section"

    rows = load_timetable_rows()
    target_row = None
    for i, row in enumerate(rows):
        if (
            row.get("day", "") == old_day
            and row.get("slot", "") == old_slot
            and row.get("subject", "") == old_subject
            and row.get("room", "") == old_room
            and row.get("teacher", "") == old_teacher
            and row.get("target", "ALL") == old_target
        ):
            target_row = row
            break

    if target_row is None:
        return redirect("/admin/dashboard?error=Timetable+entry+not+found.&section=" + source_section)

    if request.method == "POST":
        new_day = request.form.get("day", target_row.get("day", "")).strip()
        new_slot = request.form.get("slot", target_row.get("slot", "")).strip()
        new_subject = request.form.get("subject", target_row.get("subject", "")).strip()
        new_teacher = request.form.get("teacher", target_row.get("teacher", "")).strip()
        new_room = request.form.get("room", target_row.get("room", "")).strip()
        new_target = request.form.get("target", target_row.get("target", "ALL")).strip() or "ALL"
        new_label = request.form.get("label", target_row.get("label", "")).strip()

        new_row = {
            "day": new_day,
            "slot": new_slot,
            "subject": new_subject,
            "teacher": new_teacher,
            "room": new_room,
            "target": new_target,
            "label": new_label
        }
        updated = apply_timetable_update(
            {
                "day": old_day,
                "slot": old_slot,
                "subject": old_subject,
                "room": old_room,
                "teacher": old_teacher,
                "target": old_target or "ALL"
            },
            new_row
        )
        if not updated:
            return redirect("/admin/dashboard?error=Timetable+entry+not+found.&section=" + source_section)

        return redirect("/admin/dashboard?message=Timetable+entry+updated.&section=" + source_section)

    return render_template(
        "admin_edit_timetable.html",
        row=target_row,
        old_day=old_day,
        old_slot=old_slot,
        old_subject=old_subject,
        old_room=old_room,
        old_teacher=old_teacher,
        old_target=old_target,
        source_section=source_section
    )


@app.route("/admin/timetable/update_api", methods=["POST"])
def update_timetable_entry_api():
    if session.get("role") != "admin":
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    old_row = {
        "day": request.form.get("old_day", "").strip(),
        "slot": request.form.get("old_slot", "").strip(),
        "subject": request.form.get("old_subject", "").strip(),
        "room": request.form.get("old_room", "").strip(),
        "teacher": request.form.get("old_teacher", "").strip(),
        "target": request.form.get("old_target", "").strip() or "ALL"
    }

    new_row = {
        "day": request.form.get("day", old_row["day"]).strip(),
        "slot": request.form.get("slot", old_row["slot"]).strip(),
        "subject": request.form.get("subject", old_row["subject"]).strip(),
        "teacher": request.form.get("teacher", old_row["teacher"]).strip(),
        "room": request.form.get("room", old_row["room"]).strip(),
        "target": request.form.get("target", old_row["target"]).strip() or "ALL",
        "label": request.form.get("label", "").strip()
    }

    updated = apply_timetable_update(old_row, new_row)
    if not updated:
        return jsonify({"ok": False, "error": "Entry not found"}), 404
    return jsonify({"ok": True, "row": new_row})


@app.route("/admin/timetable/teacher_absent")
def mark_teacher_all_absent():

    if session.get("role") != "admin":
        return "Unauthorized"

    teacher = request.args.get("teacher", "")
    clear = request.args.get("clear", "0") == "1"
    rows = load_timetable_rows()
    updated = 0

    for row in rows:
        if row.get("teacher", "") == teacher:
            if clear:
                if row.get("label", "") == "Teacher Absent":
                    row["label"] = ""
                    updated += 1
            else:
                if row.get("label", "") != "Teacher Absent":
                    row["label"] = "Teacher Absent"
                    updated += 1

    save_timetable_rows(rows)
    if clear:
        return redirect("/admin/dashboard?message=Teacher+absence+cleared+for+all+classes.")
    return redirect("/admin/dashboard?message=Teacher+marked+absent+for+all+classes.")


@app.route("/events")
def events():
    role = session.get("role")
    email = session.get("email", "")
    if role not in ("admin", "teacher", "student"):
        return jsonify([])

    all_events = load_calendar_events()
    if role in ("teacher", "student"):
        viewer_dept = sanitize_target_department(
            session.get("department", "") or infer_department_from_email(email),
            default="ALL"
        )
        filtered = []
        for e in all_events:
            target = sanitize_target_department(e.get("target", "ALL"), default="ALL")
            is_owner = e.get("creator_email", "") == email
            creator_role = (e.get("creator_role", "") or "").strip().lower()

            # Student-created events are private to the same student only.
            if creator_role == "student" and not is_owner:
                continue

            # Teacher/Admin/System events are visible to all users.
            if creator_role in ("teacher", "admin", "system"):
                filtered.append(e)
                continue

            if target == "ALL" or target == viewer_dept or is_owner:
                filtered.append(e)
        all_events = filtered
    payload = [to_calendar_event(e, email) for e in all_events]
    return jsonify(payload)


@app.route("/teacher/add_event", methods=["POST"])
def add_teacher_event():
    if session.get("role") != "teacher":
        return "Unauthorized", 401

    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    date = request.form.get("date", "").strip()
    event_type = request.form.get("event_type", "general").strip().lower()
    important = request.form.get("important", "no").strip().lower() == "yes"
    custom_color = sanitize_label_color(request.form.get("label_color", ""))
    target = sanitize_target_department(request.form.get("target", "ALL"), default="ALL")

    if not title or not date:
        return "Missing title/date", 400
    if event_type not in ("general", "test", "exam"):
        event_type = "general"

    all_events = load_events()
    if not can_add_event_for_date(
        all_events,
        date_value=date,
        creator_role="teacher",
        creator_email=session.get("email", "")
    ):
        return f"No more than {MAX_EVENTS_PER_DAY} shared events are allowed on this date.", 400
    all_events.append({
        "id": str(uuid.uuid4()),
        "title": title,
        "subject": subject if subject else title,
        "date": date,
        "type": event_type,
        "important": important,
        "custom_color": custom_color,
        "target": target,
        "creator_name": session.get("name", ""),
        "creator_email": session.get("email", ""),
        "creator_role": "teacher"
    })
    save_events(all_events)
    return "OK", 200


@app.route("/teacher/update_event", methods=["POST"])
def update_teacher_event():
    if session.get("role") != "teacher":
        return "Unauthorized", 401

    event_id = request.form.get("id", "").strip()
    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    date = request.form.get("date", "").strip()
    event_type = request.form.get("event_type", "general").strip().lower()
    important = request.form.get("important", "no").strip().lower() == "yes"
    custom_color = sanitize_label_color(request.form.get("label_color", ""))
    target = sanitize_target_department(request.form.get("target", "ALL"), default="ALL")

    all_events = load_events()
    updated = False
    for e in all_events:
        if e.get("id") == event_id:
            if e.get("creator_email", "") != session.get("email", ""):
                return "Forbidden", 403
            candidate_date = date if date else e.get("date", "")
            if not can_add_event_for_date(
                all_events,
                date_value=candidate_date,
                creator_role="teacher",
                creator_email=session.get("email", ""),
                exclude_id=event_id
            ):
                return f"No more than {MAX_EVENTS_PER_DAY} shared events are allowed on this date.", 400
            if title:
                e["title"] = title
            if subject:
                e["subject"] = subject
            if date:
                e["date"] = date
            if event_type in ("general", "test", "exam"):
                e["type"] = event_type
            e["important"] = important
            e["custom_color"] = custom_color
            e["target"] = target
            updated = True
            break

    if not updated:
        return "Event not found", 404

    save_events(all_events)
    return "OK", 200


@app.route("/teacher/delete_event", methods=["POST"])
def delete_teacher_event():
    if session.get("role") != "teacher":
        return "Unauthorized", 401

    event_id = request.form.get("id", "").strip()
    all_events = load_events()
    kept = []
    deleted = False
    for e in all_events:
        if e.get("id") == event_id:
            if e.get("creator_email", "") != session.get("email", ""):
                return "Forbidden", 403
            deleted = True
            continue
        kept.append(e)

    if not deleted:
        return "Event not found", 404

    save_events(kept)
    return "OK", 200


@app.route("/student/add_event", methods=["POST"])
def add_student_event():
    if session.get("role") != "student":
        return "Unauthorized", 401

    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    date = request.form.get("date", "").strip()
    event_type = request.form.get("event_type", "general").strip().lower()
    important = request.form.get("important", "no").strip().lower() == "yes"
    custom_color = sanitize_label_color(request.form.get("label_color", ""))
    target = sanitize_target_department(request.form.get("target", "ALL"), default="ALL")

    if not title or not date:
        return "Missing title/date", 400
    if event_type not in ("general", "test", "exam"):
        event_type = "general"

    all_events = load_events()
    if not can_add_event_for_date(
        all_events,
        date_value=date,
        creator_role="student",
        creator_email=session.get("email", "")
    ):
        return f"No more than {MAX_EVENTS_PER_DAY} personal events are allowed on this date.", 400
    all_events.append({
        "id": str(uuid.uuid4()),
        "title": title,
        "subject": subject if subject else title,
        "date": date,
        "type": event_type,
        "important": important,
        "custom_color": custom_color,
        "target": target,
        "creator_name": session.get("name", ""),
        "creator_email": session.get("email", ""),
        "creator_role": "student"
    })
    save_events(all_events)
    return "OK", 200


@app.route("/student/update_event", methods=["POST"])
def update_student_event():
    if session.get("role") != "student":
        return "Unauthorized", 401

    event_id = request.form.get("id", "").strip()
    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    date = request.form.get("date", "").strip()
    event_type = request.form.get("event_type", "general").strip().lower()
    important = request.form.get("important", "no").strip().lower() == "yes"
    custom_color = sanitize_label_color(request.form.get("label_color", ""))
    target = sanitize_target_department(request.form.get("target", "ALL"), default="ALL")

    all_events = load_events()
    updated = False
    for e in all_events:
        if e.get("id") == event_id:
            if e.get("creator_email", "") != session.get("email", ""):
                return "Forbidden", 403
            candidate_date = date if date else e.get("date", "")
            if not can_add_event_for_date(
                all_events,
                date_value=candidate_date,
                creator_role="student",
                creator_email=session.get("email", ""),
                exclude_id=event_id
            ):
                return f"No more than {MAX_EVENTS_PER_DAY} personal events are allowed on this date.", 400
            if title:
                e["title"] = title
            if subject:
                e["subject"] = subject
            if date:
                e["date"] = date
            if event_type in ("general", "test", "exam"):
                e["type"] = event_type
            e["important"] = important
            e["custom_color"] = custom_color
            e["target"] = target
            updated = True
            break

    if not updated:
        return "Event not found", 404

    save_events(all_events)
    return "OK", 200


@app.route("/student/delete_event", methods=["POST"])
def delete_student_event():
    if session.get("role") != "student":
        return "Unauthorized", 401

    event_id = request.form.get("id", "").strip()
    all_events = load_events()
    kept = []
    deleted = False
    for e in all_events:
        if e.get("id") == event_id:
            if e.get("creator_email", "") != session.get("email", ""):
                return "Forbidden", 403
            deleted = True
            continue
        kept.append(e)

    if not deleted:
        return "Event not found", 404

    save_events(kept)
    return "OK", 200


@app.route("/admin/events/add", methods=["POST"])
def admin_add_event():
    if session.get("role") != "admin":
        return "Unauthorized"

    title = request.form.get("title", "").strip()
    subject = request.form.get("subject", "").strip()
    date = request.form.get("date", "").strip()
    event_type = request.form.get("event_type", "general").strip().lower()
    important = request.form.get("important", "no").strip().lower() == "yes"
    if not title or not date:
        return redirect("/admin/dashboard?error=Event+title+and+date+are+required.")
    if event_type not in ("general", "test", "exam", "vacation"):
        event_type = "general"

    all_events = load_events()
    if not can_add_event_for_date(
        all_events,
        date_value=date,
        creator_role="admin",
        creator_email=session.get("email", "")
    ):
        return redirect("/admin/dashboard?error=No+more+than+3+shared+events+are+allowed+on+this+date.")
    all_events.append({
        "id": str(uuid.uuid4()),
        "title": title,
        "subject": subject if subject else title,
        "date": date,
        "type": event_type,
        "important": important,
        "creator_name": session.get("name", ""),
        "creator_email": session.get("email", ""),
        "creator_role": "admin"
    })
    save_events(all_events)
    return redirect("/admin/dashboard?message=Event+added+successfully.")


@app.route("/admin/events/delete")
def admin_delete_event():
    if session.get("role") != "admin":
        return "Unauthorized"

    event_id = request.args.get("id", "")
    all_events = load_events()
    kept = [e for e in all_events if e.get("id") != event_id]
    save_events(kept)
    return redirect("/admin/dashboard?message=Event+deleted.")


@app.route("/admin/events/edit", methods=["GET", "POST"])
def admin_edit_event():
    if session.get("role") != "admin":
        return "Unauthorized"

    event_id = request.args.get("id", "")
    all_events = load_events()
    target = None
    for e in all_events:
        if e.get("id") == event_id:
            target = e
            break

    if target is None:
        return redirect("/admin/dashboard?error=Event+not+found.")

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        subject = request.form.get("subject", "").strip()
        date = request.form.get("date", "").strip()
        event_type = request.form.get("event_type", "general").strip().lower()
        important = request.form.get("important", "no").strip().lower() == "yes"
        candidate_date = date if date else target.get("date", "")
        if not can_add_event_for_date(
            all_events,
            date_value=candidate_date,
            creator_role="admin",
            creator_email=session.get("email", ""),
            exclude_id=event_id
        ):
            return redirect("/admin/dashboard?error=No+more+than+3+shared+events+are+allowed+on+this+date.")
        if title:
            target["title"] = title
        if subject:
            target["subject"] = subject
        if date:
            target["date"] = date
        if event_type in ("general", "test", "exam", "vacation"):
            target["type"] = event_type
        target["important"] = important
        save_events(all_events)
        return redirect("/admin/dashboard?message=Event+updated.")

    return render_template("admin_edit_event.html", event=target)


@app.route("/admin/events/update_api", methods=["POST"])
def admin_update_event_api():
    if session.get("role") != "admin":
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    event_id = request.form.get("id", "").strip()
    title = request.form.get("title", "").strip()
    date = request.form.get("date", "").strip()
    event_type = request.form.get("event_type", "general").strip().lower()
    important = request.form.get("important", "no").strip().lower() == "yes"

    if not event_id:
        return jsonify({"ok": False, "error": "Event id is required"}), 400
    if not title or not date:
        return jsonify({"ok": False, "error": "Title and date are required"}), 400
    if event_type not in ("general", "test", "exam", "vacation"):
        event_type = "general"

    all_events = load_events()
    updated = None
    for e in all_events:
        if e.get("id", "") == event_id:
            if not can_add_event_for_date(
                all_events,
                date_value=date,
                creator_role="admin",
                creator_email=session.get("email", ""),
                exclude_id=event_id
            ):
                return jsonify({"ok": False, "error": "No more than 3 shared events are allowed on this date."}), 400
            e["title"] = title
            e["subject"] = e.get("subject", title) or title
            e["date"] = date
            e["type"] = event_type
            e["important"] = important
            updated = e
            break

    if updated is None:
        return jsonify({"ok": False, "error": "Event not found"}), 404

    save_events(all_events)
    return jsonify({
        "ok": True,
        "event": {
            "id": updated.get("id", ""),
            "title": updated.get("title", ""),
            "date": updated.get("date", ""),
            "type": updated.get("type", "general"),
            "creator_name": updated.get("creator_name", ""),
            "important": bool(updated.get("important", False))
        }
    })


# =====================================================
# TEACHER DASHBOARD
# =====================================================
@app.route("/teacher/dashboard")
def teacher_dashboard():

    if session.get("role") != "teacher":
        return redirect("/login")

    teacher = session["name"]
    rows = []
    pending_rows = []
    institute_timetable = load_timetable_rows()
    my_timetable = []
    today_short = datetime.now().strftime("%a")
    today_name = datetime.now().strftime("%A")
    today_classes = []

    for course in load_courses():
        if course["teacher"] == teacher:
            rows.append(course)

    for req in load_preference_requests():
        if req["teacher"] == teacher:
            pending_rows.append(req)

    for row in institute_timetable:
        if row["teacher"] == teacher:
            my_timetable.append(row)
            if row["day"] == today_short:
                today_classes.append(row)

    today_classes.sort(key=lambda r: r["slot"])

    return render_template(
        "teacher_dashboard.html",
        teacher=teacher,
        teacher_email=session.get("email", ""),
        teacher_department=session.get("department", "ALL"),
        teacher_profile_pic=session.get("profile_pic", ""),
        rows=rows,
        pending_rows=pending_rows,
        institute_timetable=institute_timetable,
        my_timetable=my_timetable,
        vacations=get_upcoming_vacations(),
        today_name=today_name,
        today_classes=today_classes
    )


# =====================================================
# TEACHER SUBMIT COURSE
# =====================================================
@app.route("/submit_teacher", methods=["POST"])
def submit_teacher():

    if session.get("role") != "teacher":
        return "Unauthorized"

    subject = request.form["subject"]
    teacher = session["name"]
    students = request.form["students"]
    target = request.form.get("target", "ALL").strip() or "ALL"
    try:
        if int(students) > MAX_ROOM_CAPACITY:
            return f"Students count exceeds max room capacity ({MAX_ROOM_CAPACITY}). Please split into multiple batches."
    except ValueError:
        return "Invalid students count."

    day1 = request.form["day1"]
    slot1 = request.form["slot1"]
    day2 = request.form["day2"]
    slot2 = request.form["slot2"]
    day3 = request.form["day3"]
    slot3 = request.form["slot3"]

    request_id = f"{teacher}|{subject}".lower()
    new_request = {
        "id": request_id,
        "subject": subject,
        "teacher": teacher,
        "students": students,
        "target": target,
        "prefs": [f"{day1}:{slot1}", f"{day2}:{slot2}", f"{day3}:{slot3}"]
    }

    requests = load_preference_requests()
    updated = False
    for i, req in enumerate(requests):
        if req["id"] == request_id:
            requests[i] = new_request
            updated = True
            break

    if not updated:
        requests.append(new_request)

    save_preference_requests(requests)

    return redirect("/teacher/dashboard")


# =====================================================
# STUDENT DASHBOARD
# =====================================================
@app.route("/student/dashboard")
def student_dashboard():

    if session.get("role") != "student":
        return redirect("/login")

    student_name = session.get("name", "Student")
    student_department = session.get("department", "ALL")
    if not student_department or student_department.upper() == "ALL":
        student_department = infer_department_from_email(session.get("email", ""))

    institute_timetable = load_timetable_rows()
    my_timetable = []
    institute_today = []
    my_today = []
    today_short = datetime.now().strftime("%a")
    today_name = datetime.now().strftime("%A")
    day_order = {"Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5}
    slot_order = {"S1": 1, "S2": 2, "S3": 3, "S4": 4}

    for row in institute_timetable:
        if row["day"] == today_short:
            institute_today.append(row)

    for row in institute_timetable:
        target = row["target"].strip().upper()
        dept = student_department.strip().upper()
        if target == "ALL" or dept == "ALL" or target == dept:
            my_timetable.append(row)
            if row["day"] == today_short:
                my_today.append(row)

    my_timetable.sort(
        key=lambda r: (
            day_order.get(r["day"], 99),
            slot_order.get(r["slot"], 99),
            r["subject"]
        )
    )
    my_today.sort(key=lambda r: (slot_order.get(r["slot"], 99), r["subject"]))
    institute_today.sort(key=lambda r: (slot_order.get(r["slot"], 99), r["subject"]))

    return render_template(
        "student_dashboard.html",
        student_name=student_name,
        student_email=session.get("email", ""),
        student_department=student_department,
        student_profile_pic=session.get("profile_pic", ""),
        institute_timetable=institute_timetable,
        my_timetable=my_timetable,
        my_today=my_today,
        institute_today=institute_today,
        vacations=get_upcoming_vacations(),
        today_name=today_name
    )


@app.route("/student/timetable")
def student_timetable():

    if session.get("role") != "student":
        return redirect("/login")

    timetable_data = []

    if os.path.exists(TIMETABLE_FILE):
        with open(TIMETABLE_FILE) as f:
            for line in f:
                parts = line.strip().split(",")
                if len(parts) < 4:
                    continue
                day, slot, subject, room = parts[0], parts[1], parts[2], parts[3]
                timetable_data.append({
                    "day": day,
                    "slot": slot,
                    "subject": subject,
                    "room": room
                })

    return render_template(
        "student_timetable.html",
        timetable=timetable_data
    )


@app.route("/profile")
def profile_page():
    role = session.get("role")
    email = session.get("email")
    if not role or not email:
        return redirect("/login")

    users = load_users()
    current = None
    for u in users:
        if u["email"] == email and u["role"] == role:
            current = u
            break

    if current is None:
        return redirect("/login")

    return render_template(
        "profile.html",
        role=role,
        name=current.get("name", ""),
        email=current.get("email", ""),
        department=current.get("department", "ALL"),
        profile_pic=current.get("profile_pic", ""),
        message=request.args.get("message", ""),
        error=request.args.get("error", "")
    )


@app.route("/profile/update", methods=["POST"])
def update_profile():

    role = session.get("role")
    email = session.get("email")
    if not role or not email:
        return redirect("/login")

    users = load_users()
    target = None
    for u in users:
        if u["email"] == email and u["role"] == role:
            target = u
            break

    if target is None:
        return redirect("/login")

    name = request.form.get("name", "").strip()
    department = request.form.get("department", "").strip() or target.get("department", "ALL")
    if role == "admin":
        department = "ALL"

    if name:
        target["name"] = name
        session["name"] = name

    target["department"] = department
    session["department"] = department

    file = request.files.get("profile_pic")
    if file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext in ALLOWED_IMAGE_EXT:
            os.makedirs(PROFILE_UPLOAD_DIR, exist_ok=True)
            safe_email = secure_filename(email.replace("@", "_at_"))
            filename = f"{role}_{safe_email}_{int(datetime.now().timestamp())}{ext}"
            file_path = os.path.join(PROFILE_UPLOAD_DIR, filename)
            file.save(file_path)
            rel_path = f"profile_pics/{filename}"
            target["profile_pic"] = rel_path
            session["profile_pic"] = rel_path

    save_users(users)
    return redirect("/profile?message=Profile+updated+successfully.")


# =====================================================
# RUN
# =====================================================
if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"Starting SmartTimetable on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)
