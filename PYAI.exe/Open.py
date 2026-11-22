import datetime
import hashlib
import json
import os
import re
import secrets
import threading
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import colorchooser, messagebox
from google import genai
from google.genai import types
import subprocess
import sys
import os

if getattr(sys, 'frozen', False):
    app_folder = os.path.dirname(sys.executable)
else:
    app_folder = os.path.dirname(os.path.abspath(__file__))

dictionary_path = os.path.join(app_folder, "Dictionary.exe")

if os.path.exists(dictionary_path):
    subprocess.Popen([dictionary_path])
else:
    print("Dictionary.exe not found!")
try:
    from google_auth_oauthlib.flow import InstalledAppFlow
    HAS_GOOGLE_OAUTH = True
except ImportError:
    HAS_GOOGLE_OAUTH = False

# ==================================================
# DICTIONARY-GPT SETTINGS
# ==================================================

MODEL = "gemini-3.5-flash-lite"

# TEMPORARY KEY FOR TESTING — replace with os.getenv("GEMINI_API_KEY", "") before sharing
GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    "AQ.Ab8RN6ID1RUQTcIJs2LPD6-xGbVFBBL7BCx_nRiFP8fZ6KEM0A",
).strip()

STARTING_CREDITS = 5.00
COST_MULTIPLIER = 2.0
MIN_COST = 0.02
MAX_COST = 1.50
CLAIM_WINDOW_SECONDS = 24 * 60 * 60
GRACE_WINDOW_SECONDS = 48 * 60 * 60

if getattr(sys, "frozen", False):
    FOLDER = Path(sys.executable).resolve().parent
else:
    FOLDER = Path(__file__).resolve().parent

DICTIONARY_FILE = FOLDER / "Dictionary.py"
DICTIONARY_FILE = FOLDER / "Dictionary.py"
CREDENTIALS_FILE = FOLDER / "credentials.json"
USERS_DB_FILE = FOLDER / "users_db.json"
SESSION_FILE = FOLDER / "gmail_session.json"


# ==================================================
# HELPERS
# ==================================================

def draw_rounded_rect(canvas, x1, y1, x2, y2, radius=16, **kwargs):
    r = max(2, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return salt.hex(), digest.hex()


def now_ts() -> float:
    return datetime.datetime.now().timestamp()


def reward_for_streak_day(streak_day: int) -> float:
    """
    Day 1 = +0
    Days 8, 15, 22, ... (cycle start after week 1) = +1
    Days 2-6, 9-13, 16-20, ... = +2
    Days 7, 14, 21, ... = +5
    """
    day = max(1, streak_day)
    if day == 1:
        return 0.0
    cycle_day = ((day - 1) % 7) + 1
    if cycle_day == 1:
        return 1.0
    if cycle_day <= 6:
        return 2.0
    return 5.0


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


# ==================================================
# MAIN APPLICATION
# ==================================================

class DictionaryGPT:

    def __init__(self, root):
        self.root = root
        self.root.title("Dictionary-GPT")

        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()

        width = int(screen_width * 0.94)
        height = int(screen_height * 0.88)
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2

        root.geometry(f"{width}x{height}+{x}+{y}")
        root.minsize(1060, 650)

        self.theme = {
            "bg": "#212121",
            "sidebar": "#171717",
            "surface": "#2F2F2F",
            "surface_hover": "#383838",
            "border": "#3A3A3A",
            "user_bubble": "#2F2F2F",
            "user_text": "#ECECEC",
            "ai_bubble": "#212121",
            "ai_text": "#ECECEC",
            "accent": "#10A37F",
            "muted": "#9B9B9B",
            "font_size": 11,
            "ai_name": "Dictionary-GPT",
            "ai_vibe": "Thoughtful & Natural",
        }

        root.configure(bg=self.theme["bg"])

        self.client = None
        self.conversation = []
        self.message_records = []
        self.thinking_frame = None
        self.request_id = 0
        self.credits = STARTING_CREDITS
        self.last_cost = 0.00
        self.streak_day = 0
        self.last_claim_ts = None
        self.fomo_notice = ""
        self._countdown_job = None

        self.gmail = ""
        self.username = "User"
        self.auth_method = "Gmail + Password"

        if not DICTIONARY_FILE.exists():
            messagebox.showerror(
                "Dictionary Missing",
                "Dictionary.py must be in the same folder as Open.py!"
            )
            root.destroy()
            return

        self.dictionary = DICTIONARY_FILE.read_text(encoding="utf-8")
        self.lines = self.dictionary.splitlines()
        self.numbered_dictionary = "\n".join(
            f"Line {i}: {line}"
            for i, line in enumerate(self.lines, 1)
        )

        self.users_db = self.load_users_db()

        if not self.load_saved_session():
            self.show_auth_gate()
        else:
            self.evaluate_streak_state()
            self.create_interface()

    def close_app(self):
        self._cancel_countdown_job()
        if self.gmail:
            self.sync_user_record()
            self.save_session()
        if self.root.winfo_exists():
            self.root.destroy()

    # ==================================================
    # USER DATABASE, GAME DAILY REWARDS & SESSION
    # ==================================================

    def load_users_db(self) -> dict:
        if USERS_DB_FILE.exists():
            try:
                data = json.loads(USERS_DB_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def save_users_db(self):
        try:
            USERS_DB_FILE.write_text(json.dumps(self.users_db, indent=2), encoding="utf-8")
        except Exception:
            pass

    def load_saved_session(self) -> bool:
        if SESSION_FILE.exists():
            try:
                data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
                gmail = data.get("gmail", "").strip().lower()
                username = data.get("username", "").strip()
                user_rec = self.users_db.get(gmail)
                if (
                    gmail
                    and "@" in gmail
                    and username
                    and user_rec
                    and user_rec.get("salt")
                    and user_rec.get("password_hash")
                ):
                    self.gmail = gmail
                    self.username = user_rec["username"]
                    self.auth_method = data.get("auth_method", "Gmail + Password")
                    self.credits = float(user_rec.get("credits", STARTING_CREDITS))
                    self.streak_day = int(user_rec.get("streak_day", 0))
                    self.last_claim_ts = user_rec.get("last_claim_ts")
                    return True
            except Exception:
                pass
        return False

    def save_session(self):
        if not self.gmail:
            return
        try:
            SESSION_FILE.write_text(
                json.dumps(
                    {
                        "gmail": self.gmail,
                        "username": self.username,
                        "auth_method": self.auth_method,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def sync_user_record(self):
        if self.gmail and self.gmail in self.users_db:
            self.users_db[self.gmail]["username"] = self.username
            self.users_db[self.gmail]["credits"] = round(max(0.0, self.credits), 2)
            self.users_db[self.gmail]["streak_day"] = self.streak_day
            self.users_db[self.gmail]["last_claim_ts"] = self.last_claim_ts
            self.save_users_db()

    def evaluate_streak_state(self):
        current = now_ts()
        if self.last_claim_ts is not None:
            elapsed = current - float(self.last_claim_ts)
            if elapsed >= GRACE_WINDOW_SECONDS and self.streak_day > 0:
                self.streak_day = 0
                self.fomo_notice = (
                    "STREAK LOST! Missed the window—reset to Day 1 (+0). "
                    "Claim on time: Days 2–6 (+2), Day 7 (+5), Days 8/15+ (+1)!"
                )
                self.sync_user_record()
                return

        if self.streak_day == 0:
            self.fomo_notice = (
                "Day 1: +0 • Days 2–6: +2 • Day 7: +5 • Days 8, 15+: +1! "
                "Miss a 24h cycle and you reset back to Day 1."
            )
        else:
            next_day = self.streak_day + 1
            next_reward = reward_for_streak_day(next_day)
            cycle_day = ((self.streak_day - 1) % 7) + 1
            days_to_day7 = 7 - cycle_day
            if days_to_day7 == 0:
                self.fomo_notice = (
                    f"Day {self.streak_day} (+5) reached! Next is Day {next_day} (+{next_reward:.0f})—"
                    "miss a window and you drop right back to Day 1 (+0)!"
                )
            else:
                self.fomo_notice = (
                    f"Streak Day {self.streak_day} • {days_to_day7} day{'s' if days_to_day7 != 1 else ''} "
                    f"from the +5 Day 7/14 drop (Days 8/15+: +1)—miss a window and you reset to Day 1 (+0)!"
                )

    def seconds_until_next_claim(self) -> float:
        if self.last_claim_ts is None:
            return 0.0
        remaining = (float(self.last_claim_ts) + CLAIM_WINDOW_SECONDS) - now_ts()
        return max(0.0, remaining)

    def can_claim_daily(self) -> bool:
        return self.seconds_until_next_claim() <= 0.0

    def claim_daily_reward(self):
        if not self.can_claim_daily():
            rem = format_duration(self.seconds_until_next_claim())
            messagebox.showinfo(
                "Daily Reward Locked",
                f"Already claimed! Next reward unlocks in {rem}."
            )
            return

        current = now_ts()
        if self.last_claim_ts is not None and (current - float(self.last_claim_ts)) >= GRACE_WINDOW_SECONDS:
            self.streak_day = 1
            missed_reset = True
        else:
            self.streak_day = self.streak_day + 1 if self.streak_day > 0 else 1
            missed_reset = False

        self.last_claim_ts = current
        cycle_day = ((self.streak_day - 1) % 7) + 1
        reward = reward_for_streak_day(self.streak_day)
        self.credits = round(self.credits + reward, 2)

        if missed_reset:
            self.fomo_notice = (
                "Missed window reset you to Day 1 (+0). Next 24h timer started—don't miss Day 2 (+2)!"
            )
        elif self.streak_day == 1:
            self.fomo_notice = (
                "Claimed Day 1 (+0)! 24h cooldown running—come back for Day 2 (+2) and build toward Day 7 (+5)!"
            )
        elif cycle_day == 1:
            self.fomo_notice = (
                f"Claimed Day {self.streak_day} (+1 bonus start)! 24h timer running—keep climbing to Day {self.streak_day + 6} (+5)!"
            )
        elif cycle_day == 7:
            self.fomo_notice = (
                f"DAY {self.streak_day} JACKPOT CLAIMED (+5)! 24h cooldown running—Day {self.streak_day + 1} pays +1!"
            )
        else:
            days_left = 7 - cycle_day
            self.fomo_notice = (
                f"Claimed Day {self.streak_day} (+{reward:.0f})! {days_left} day{'s' if days_left != 1 else ''} "
                f"left until the +5 drop—24h timer running!"
            )

        self.sync_user_record()
        self.refresh_credit_display()
        self.update_daily_reward_ui()

    def enter_workspace(self, gmail: str, username: str, auth_method: str, user_record: dict):
        self.gmail = gmail.strip().lower()
        self.username = username.strip()
        self.auth_method = auth_method
        self.credits = round(max(0.0, float(user_record.get("credits", STARTING_CREDITS))), 2)
        self.streak_day = int(user_record.get("streak_day", 0))
        self.last_claim_ts = user_record.get("last_claim_ts")

        self.evaluate_streak_state()
        self.save_session()

        if hasattr(self, "auth_screen") and self.auth_screen.winfo_exists():
            self.auth_screen.destroy()
        self.create_interface()

    # ==================================================
    # AUTH GATE: GMAIL + USERNAME + PASSWORD (REQUIRED)
    # ==================================================

    def show_auth_gate(self):
        self.auth_screen = tk.Frame(self.root, bg="#171717")
        self.auth_screen.place(relx=0, rely=0, relwidth=1, relheight=1)

        card_outer = tk.Frame(self.auth_screen, bg="#2F2F2F", padx=1, pady=1)
        card_outer.place(relx=0.5, rely=0.5, anchor="center")

        card = tk.Frame(card_outer, bg="#212121", padx=34, pady=24)
        card.pack()

        tk.Label(
            card,
            text="✦ Dictionary-GPT",
            font=("Segoe UI", 18, "bold"),
            bg="#212121",
            fg="#ECECEC",
        ).pack()

        tk.Label(
            card,
            text="Start: 5.00 • Day 1: +0 • Days 2–6: +2 • Day 7: +5 • Days 8, 15+: +1",
            font=("Segoe UI", 8, "bold"),
            bg="#212121",
            fg="#FBBF24",
        ).pack(pady=(3, 12))

        tk.Button(
            card,
            text="G   Verify Gmail via Google (Then Enter Username & Password)",
            command=self.handle_google_fill_gmail,
            bg="#2F2F2F",
            fg="#ECECEC",
            activebackground="#3A3A3A",
            activeforeground="#FFFFFF",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            pady=8,
            cursor="hand2",
        ).pack(fill="x")

        div_row = tk.Frame(card, bg="#212121")
        div_row.pack(fill="x", pady=10)
        tk.Frame(div_row, bg="#3A3A3A", height=1).pack(side="left", fill="x", expand=True, pady=7)
        tk.Label(
            div_row,
            text="  GMAIL, USERNAME & PASSWORD REQUIRED  ",
            font=("Segoe UI", 8, "bold"),
            bg="#212121",
            fg="#8A8A8A",
        ).pack(side="left")
        tk.Frame(div_row, bg="#3A3A3A", height=1).pack(side="left", fill="x", expand=True, pady=7)

        self.auth_mode = tk.StringVar(value="login")
        tab_row = tk.Frame(card, bg="#2A2A2A", padx=3, pady=3)
        tab_row.pack(fill="x", pady=(0, 12))

        self.login_tab_btn = tk.Button(
            tab_row,
            text="Log In",
            command=lambda: self.set_auth_mode("login"),
            bg="#3A3A3A",
            fg="#FFFFFF",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            pady=6,
            cursor="hand2",
        )
        self.login_tab_btn.pack(side="left", fill="x", expand=True)

        self.signup_tab_btn = tk.Button(
            tab_row,
            text="Sign Up",
            command=lambda: self.set_auth_mode("signup"),
            bg="#2A2A2A",
            fg="#9B9B9B",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            pady=6,
            cursor="hand2",
        )
        self.signup_tab_btn.pack(side="left", fill="x", expand=True)

        tk.Label(card, text="Gmail (@gmail.com)", font=("Segoe UI", 9, "bold"), bg="#212121", fg="#C4C4C4").pack(anchor="w")
        gmail_wrap = tk.Frame(card, bg="#3A3A3A", padx=1, pady=1)
        gmail_wrap.pack(fill="x", pady=(3, 10))
        self.auth_gmail_entry = tk.Entry(
            gmail_wrap,
            bg="#2A2A2A",
            fg="#ECECEC",
            insertbackground="#ECECEC",
            font=("Segoe UI", 10),
            relief="flat",
            width=38,
        )
        self.auth_gmail_entry.pack(fill="x", ipady=8, padx=8)
        self.auth_gmail_entry.focus_set()

        tk.Label(card, text="Username", font=("Segoe UI", 9, "bold"), bg="#212121", fg="#C4C4C4").pack(anchor="w")
        user_wrap = tk.Frame(card, bg="#3A3A3A", padx=1, pady=1)
        user_wrap.pack(fill="x", pady=(3, 10))
        self.auth_user_entry = tk.Entry(
            user_wrap,
            bg="#2A2A2A",
            fg="#ECECEC",
            insertbackground="#ECECEC",
            font=("Segoe UI", 10),
            relief="flat",
            width=38,
        )
        self.auth_user_entry.pack(fill="x", ipady=8, padx=8)

        tk.Label(card, text="Password", font=("Segoe UI", 9, "bold"), bg="#212121", fg="#C4C4C4").pack(anchor="w")
        pass_wrap = tk.Frame(card, bg="#3A3A3A", padx=1, pady=1)
        pass_wrap.pack(fill="x", pady=(3, 10))
        self.auth_pass_entry = tk.Entry(
            pass_wrap,
            bg="#2A2A2A",
            fg="#ECECEC",
            insertbackground="#ECECEC",
            font=("Segoe UI", 10),
            relief="flat",
            show="•",
            width=38,
        )
        self.auth_pass_entry.pack(fill="x", ipady=8, padx=8)

        self.auth_error_label = tk.Label(
            card,
            text="",
            font=("Segoe UI", 9),
            bg="#212121",
            fg="#F87171",
            wraplength=330,
        )
        self.auth_error_label.pack(pady=(0, 8))

        self.auth_submit_btn = tk.Button(
            card,
            text="Log In",
            command=self.submit_auth_form,
            bg="#10A37F",
            fg="#FFFFFF",
            activebackground="#0D8A6B",
            activeforeground="#FFFFFF",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            pady=9,
            cursor="hand2",
        )
        self.auth_submit_btn.pack(fill="x")

        tk.Button(
            card,
            text="Close App",
            command=self.close_app,
            bg="#2A2A2A",
            fg="#9B9B9B",
            activebackground="#3A3A3A",
            activeforeground="#FFFFFF",
            font=("Segoe UI", 8, "bold"),
            relief="flat",
            pady=6,
            cursor="hand2",
        ).pack(fill="x", pady=(8, 0))

        for entry in (self.auth_gmail_entry, self.auth_user_entry, self.auth_pass_entry):
            entry.bind("<Return>", lambda e: self.submit_auth_form())

    def set_auth_mode(self, mode: str):
        self.auth_mode.set(mode)
        self.auth_error_label.configure(text="")
        if mode == "login":
            self.login_tab_btn.configure(bg="#3A3A3A", fg="#FFFFFF")
            self.signup_tab_btn.configure(bg="#2A2A2A", fg="#9B9B9B")
            self.auth_submit_btn.configure(text="Log In")
        else:
            self.signup_tab_btn.configure(bg="#3A3A3A", fg="#FFFFFF")
            self.login_tab_btn.configure(bg="#2A2A2A", fg="#9B9B9B")
            self.auth_submit_btn.configure(text="Create Account & Sign Up")

    def handle_google_fill_gmail(self):
        if HAS_GOOGLE_OAUTH and CREDENTIALS_FILE.exists():
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_FILE),
                    scopes=[
                        "openid",
                        "https://www.googleapis.com/auth/userinfo.email",
                        "https://www.googleapis.com/auth/userinfo.profile",
                    ],
                )
                creds = flow.run_local_server(port=0)
                req = urllib.request.Request(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {creds.token}"},
                )
                with urllib.request.urlopen(req) as resp:
                    profile = json.loads(resp.read().decode("utf-8"))

                gmail = (profile.get("email") or "").strip().lower()
                if not gmail.endswith("@gmail.com"):
                    self.auth_error_label.configure(fg="#F87171", text="Google account must be a @gmail.com address.")
                    return

                self.auth_gmail_entry.delete(0, "end")
                self.auth_gmail_entry.insert(0, gmail)
                if not self.auth_user_entry.get().strip():
                    suggested = (profile.get("name") or gmail.split("@")[0]).strip()
                    self.auth_user_entry.insert(0, suggested)
                self.auth_pass_entry.focus_set()
                self.auth_error_label.configure(
                    fg="#38BDF8",
                    text=f"Verified {gmail}. Now enter your username and password.",
                )
                return
            except Exception as exc:
                self.auth_error_label.configure(fg="#F87171", text=f"Google OAuth error: {exc}")
                return

        google_win = tk.Toplevel(self.root)
        google_win.title("Google Gmail Verification")
        google_win.geometry("370x220")
        google_win.configure(bg="#212121")
        google_win.transient(self.root)
        google_win.grab_set()

        tk.Label(
            google_win,
            text="G  Enter Your Gmail",
            font=("Segoe UI", 13, "bold"),
            bg="#212121",
            fg="#ECECEC",
        ).pack(pady=(16, 4))

        tk.Label(
            google_win,
            text="You will still enter your username & password next",
            font=("Segoe UI", 8),
            bg="#212121",
            fg="#9B9B9B",
        ).pack(pady=(0, 10))

        g_var = tk.StringVar(value=self.auth_gmail_entry.get().strip())
        g_entry = tk.Entry(
            google_win,
            textvariable=g_var,
            bg="#2F2F2F",
            fg="#ECECEC",
            insertbackground="#ECECEC",
            font=("Segoe UI", 10),
            relief="flat",
        )
        g_entry.pack(fill="x", padx=28, ipady=7)
        g_entry.focus_set()

        g_err = tk.Label(google_win, text="", font=("Segoe UI", 8), bg="#212121", fg="#F87171")
        g_err.pack(pady=(5, 4))

        def confirm_gmail():
            email = g_var.get().strip().lower()
            if not re.match(r"^[a-zA-Z0-9._%+-]+@gmail\.com$", email):
                g_err.configure(text="Please enter a valid @gmail.com address.")
                return
            self.auth_gmail_entry.delete(0, "end")
            self.auth_gmail_entry.insert(0, email)
            if not self.auth_user_entry.get().strip():
                self.auth_user_entry.insert(0, email.split("@")[0].replace(".", " ").title())
            google_win.destroy()
            self.auth_pass_entry.focus_set()
            self.auth_error_label.configure(
                fg="#38BDF8",
                text="Gmail filled in—now enter your username and password.",
            )

        g_entry.bind("<Return>", lambda e: confirm_gmail())

        tk.Button(
            google_win,
            text="Use This Gmail",
            command=confirm_gmail,
            bg="#10A37F",
            fg="#FFFFFF",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            pady=8,
            cursor="hand2",
        ).pack(fill="x", padx=28, pady=6)

    def submit_auth_form(self):
        self.auth_error_label.configure(fg="#F87171")
        gmail = self.auth_gmail_entry.get().strip().lower()
        username = self.auth_user_entry.get().strip()
        password = self.auth_pass_entry.get()
        mode = self.auth_mode.get()

        if not re.match(r"^[a-zA-Z0-9._%+-]+@gmail\.com$", gmail):
            self.auth_error_label.configure(text="Enter a valid @gmail.com address.")
            return

        if not username or len(username) < 2:
            self.auth_error_label.configure(text="Username is required (at least 2 characters).")
            return

        if not password or len(password) < 4:
            self.auth_error_label.configure(text="Password is required (at least 4 characters).")
            return

        existing = self.users_db.get(gmail)

        if mode == "signup":
            if existing and existing.get("salt") and existing.get("password_hash"):
                self.auth_error_label.configure(
                    text="An account with this Gmail already exists. Switch to Log In."
                )
                return
            salt_hex, digest_hex = hash_password(password)
            rec = {
                "gmail": gmail,
                "username": username,
                "salt": salt_hex,
                "password_hash": digest_hex,
                "credits": STARTING_CREDITS,
                "streak_day": 0,
                "last_claim_ts": None,
            }
            self.users_db[gmail] = rec
            self.save_users_db()
            self.enter_workspace(gmail, username, "Gmail + Password", rec)
        else:
            if not existing or not existing.get("salt") or not existing.get("password_hash"):
                self.auth_error_label.configure(
                    text="No password set for this Gmail yet. Switch to Sign Up to register."
                )
                return
            _, check_digest = hash_password(password, existing["salt"])
            if check_digest != existing["password_hash"]:
                self.auth_error_label.configure(text="Incorrect password.")
                return
            if existing.get("username", "").lower() != username.lower():
                self.auth_error_label.configure(
                    text=f"Username mismatch (expected '{existing['username']}')."
                )
                return
            self.enter_workspace(gmail, existing["username"], "Gmail + Password", existing)

    def _cancel_countdown_job(self):
        if self._countdown_job is not None:
            try:
                self.root.after_cancel(self._countdown_job)
            except Exception:
                pass
            self._countdown_job = None

    def logout_account(self):
        if not messagebox.askyesno("Log Out", f"Log out of {self.username} ({self.gmail})?"):
            return
        self._cancel_countdown_job()
        self.sync_user_record()
        if SESSION_FILE.exists():
            try:
                SESSION_FILE.unlink()
            except Exception:
                pass
        for widget in self.root.winfo_children():
            widget.destroy()
        self.conversation.clear()
        self.message_records.clear()
        self.show_auth_gate()

    def delete_account(self):
        if not messagebox.askyesno(
            "Delete Account",
            f"Permanently delete {self.username} ({self.gmail}) and all its saved credits & streak?",
        ):
            return
        self._cancel_countdown_job()
        if self.gmail in self.users_db:
            del self.users_db[self.gmail]
            self.save_users_db()
        if SESSION_FILE.exists():
            try:
                SESSION_FILE.unlink()
            except Exception:
                pass
        for widget in self.root.winfo_children():
            widget.destroy()
        self.conversation.clear()
        self.message_records.clear()
        self.credits = STARTING_CREDITS
        self.streak_day = 0
        self.last_claim_ts = None
        self.show_auth_gate()

    # ==================================================
    # MAIN WORKSPACE LAYOUT
    # ==================================================

    def create_interface(self):
        self.root.grid_columnconfigure(0, weight=5)
        self.root.grid_columnconfigure(1, weight=1, minsize=1)
        self.root.grid_columnconfigure(2, weight=6)
        self.root.grid_rowconfigure(0, weight=1)

        self.left = tk.Frame(self.root, bg=self.theme["sidebar"])
        self.left.grid(row=0, column=0, sticky="nsew")

        self.divider = tk.Frame(self.root, bg=self.theme["border"], width=1)
        self.divider.grid(row=0, column=1, sticky="ns")

        self.right = tk.Frame(self.root, bg=self.theme["bg"])
        self.right.grid(row=0, column=2, sticky="nsew")

        self.create_dictionary_view()
        self.create_chat()
        self.start_daily_countdown_loop()

    # ==================================================
    # LEFT PANE: DICTIONARY VIEWER
    # ==================================================

    def create_dictionary_view(self):
        header = tk.Frame(self.left, bg=self.theme["sidebar"], height=56)
        header.pack(fill="x", padx=16, pady=(6, 0))
        header.pack_propagate(False)

        tk.Label(
            header,
            text="Dictionary.py",
            font=("Segoe UI", 13, "bold"),
            bg=self.theme["sidebar"],
            fg="#ECECEC",
        ).pack(side="left")

        tk.Label(
            header,
            text=f"{len(self.lines)} lines • Read-only",
            font=("Segoe UI", 9),
            bg=self.theme["sidebar"],
            fg=self.theme["muted"],
        ).pack(side="right")

        search_border = tk.Frame(self.left, bg=self.theme["border"], padx=1, pady=1)
        search_border.pack(fill="x", padx=16, pady=(4, 12))

        search_frame = tk.Frame(search_border, bg="#262626")
        search_frame.pack(fill="x")

        self.search_entry = tk.Entry(
            search_frame,
            bg="#262626",
            fg="#ECECEC",
            insertbackground="#ECECEC",
            font=("Segoe UI", 10),
            relief="flat",
        )
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=8, padx=10)
        self.search_entry.bind("<Return>", lambda event: self.search_dictionary())

        tk.Button(
            search_frame,
            text="Search",
            command=self.search_dictionary,
            bg="#343434",
            fg="#ECECEC",
            activebackground="#424242",
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=4,
            cursor="hand2",
        ).pack(side="right", padx=6, pady=4)

        viewer = tk.Frame(self.left, bg=self.theme["sidebar"])
        viewer.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        self.line_numbers = tk.Text(
            viewer,
            width=5,
            bg=self.theme["sidebar"],
            fg="#5A5A5A",
            font=("Consolas", 10),
            relief="flat",
            state="disabled",
            padx=6,
            wrap="none",
        )
        self.line_numbers.pack(side="left", fill="y")

        self.dictionary_view = tk.Text(
            viewer,
            bg=self.theme["sidebar"],
            fg="#D4D4D4",
            insertbackground="white",
            font=("Consolas", 10),
            relief="flat",
            wrap="none",
            undo=False,
        )
        self.dictionary_view.pack(side="left", fill="both", expand=True)

        self.scrollbar = tk.Scrollbar(viewer, command=self.scroll_dictionary)
        self.scrollbar.pack(side="right", fill="y")

        self.dictionary_view.configure(yscrollcommand=self.update_scroll)

        for widget in (self.dictionary_view, self.line_numbers):
            widget.bind("<MouseWheel>", self.on_dict_mouse_wheel)
            widget.bind("<Button-4>", self.on_dict_mouse_wheel)
            widget.bind("<Button-5>", self.on_dict_mouse_wheel)

        self.dictionary_view.tag_configure(
            "found",
            background="#423810",
            foreground="#FDE047",
        )

        self.dictionary_view.insert("1.0", self.dictionary)
        self.dictionary_view.configure(state="disabled")
        self.update_line_numbers()

    def on_dict_mouse_wheel(self, event):
        if event.num == 4:
            units = -3
        elif event.num == 5:
            units = 3
        else:
            units = int(-1 * (event.delta / 120)) if event.delta else 0

        if units:
            self.dictionary_view.yview_scroll(units, "units")
            self.line_numbers.yview_scroll(units, "units")
        return "break"

    def update_line_numbers(self):
        self.line_numbers.configure(state="normal")
        self.line_numbers.delete("1.0", "end")
        numbers = "\n".join(str(i) for i in range(1, len(self.lines) + 1))
        self.line_numbers.insert("1.0", numbers)
        self.line_numbers.configure(state="disabled")

    def update_scroll(self, first, last):
        self.scrollbar.set(first, last)
        self.line_numbers.yview_moveto(float(first))

    def scroll_dictionary(self, *args):
        self.dictionary_view.yview(*args)
        self.line_numbers.yview(*args)

    def search_dictionary(self):
        query = self.search_entry.get().strip()
        if not query:
            return

        matches = [
            i for i, line in enumerate(self.lines, 1)
            if query.lower() in line.lower()
        ]

        self.dictionary_view.configure(state="normal")
        self.dictionary_view.tag_remove("found", "1.0", "end")

        if matches:
            for number in matches:
                self.dictionary_view.tag_add("found", f"{number}.0", f"{number}.end")
            self.go_to_line(matches[0])
        else:
            messagebox.showinfo("Search", "No matching code was found.")

        self.dictionary_view.configure(state="disabled")

    def go_to_line(self, line_number):
        if not 1 <= line_number <= len(self.lines):
            return
        self.dictionary_view.configure(state="normal")
        self.dictionary_view.tag_remove("found", "1.0", "end")
        self.dictionary_view.tag_add("found", f"{line_number}.0", f"{line_number}.end")
        self.dictionary_view.see(f"{line_number}.0")
        self.dictionary_view.configure(state="disabled")

    # ==================================================
    # CREDITS: OUTPUT-ONLY COST MODEL (SUBTRACTED AFTER AI REPLY)
    # ==================================================

    def calculate_response_cost(self, answer: str) -> float:
        clean = answer.strip()
        if not clean:
            return MIN_COST

        out_words = len(clean.split())
        out_chars = len(clean)
        out_paras = len([p for p in clean.split("\n\n") if p.strip()])
        code_blocks = clean.count("```") // 2

        raw_cost = 0.01
        raw_cost += out_words * 0.0012
        raw_cost += (out_chars / 1000.0) * 0.015
        if out_paras > 1:
            raw_cost += (out_paras - 1) * 0.008
        if code_blocks > 0:
            raw_cost += code_blocks * 0.025

        final_cost = round(raw_cost * COST_MULTIPLIER, 2)
        return round(max(MIN_COST, min(MAX_COST, final_cost)), 2)

    def refresh_credit_display(self):
        if self.credits > 2.00:
            color = "#10A37F"
        elif self.credits > 0.75:
            color = "#F59E0B"
        else:
            color = "#F87171"

        next_day = self.streak_day + 1
        self.credit_label.configure(
            text=f"{self.credits:.2f} credits • Day {next_day}",
            fg=color,
            bg=self.theme["surface"],
        )

    def update_daily_reward_ui(self):
        if not hasattr(self, "claim_btn") or not self.claim_btn.winfo_exists():
            return

        next_day = self.streak_day + 1
        next_reward = reward_for_streak_day(next_day)

        if self.can_claim_daily():
            self.claim_btn.configure(
                text=f"Claim Day {next_day} (+{next_reward:.0f})",
                bg="#10A37F",
                fg="#FFFFFF",
                state="normal",
            )
            self.timer_label.configure(text="24h Ready", fg="#10A37F")
        else:
            rem_str = format_duration(self.seconds_until_next_claim())
            self.claim_btn.configure(
                text=f"24h: {rem_str}",
                bg="#2A2A2A",
                fg="#9B9B9B",
                state="disabled",
            )
            self.timer_label.configure(text=f"Next (+{next_reward:.0f}) in {rem_str}", fg="#FBBF24")

        self.fomo_banner.configure(text=self.fomo_notice)

    def start_daily_countdown_loop(self):
        self._cancel_countdown_job()

        def tick():
            if not self.root.winfo_exists():
                return
            if (
                self.last_claim_ts is not None
                and (now_ts() - float(self.last_claim_ts)) >= GRACE_WINDOW_SECONDS
                and self.streak_day > 0
            ):
                self.evaluate_streak_state()
            self.refresh_credit_display()
            self.update_daily_reward_ui()
            self._countdown_job = self.root.after(1000, tick)

        self._countdown_job = self.root.after(1000, tick)

    # ==================================================
    # RIGHT PANE: MODERN CHATGPT-STYLE INTERFACE
    # ==================================================

    def create_chat(self):
        self.chat_header = tk.Frame(self.right, bg=self.theme["bg"], height=56)
        self.chat_header.pack(fill="x", padx=18, pady=(6, 0))
        self.chat_header.pack_propagate(False)

        self.model_pill = tk.Frame(self.chat_header, bg=self.theme["bg"])
        self.model_pill.pack(side="left")

        self.ai_title_label = tk.Label(
            self.model_pill,
            text=f"{self.theme['ai_name']} ▾",
            font=("Segoe UI", 13, "bold"),
            bg=self.theme["bg"],
            fg="#ECECEC",
            cursor="hand2",
        )
        self.ai_title_label.pack(side="left")
        self.ai_title_label.bind("<Button-1>", lambda e: self.open_settings())

        self.ai_status_label = tk.Label(
            self.model_pill,
            text=f"  {self.theme['ai_vibe']}",
            font=("Segoe UI", 9),
            bg=self.theme["bg"],
            fg=self.theme["muted"],
        )
        self.ai_status_label.pack(side="left", pady=(2, 0))

        right_controls = tk.Frame(self.chat_header, bg=self.theme["bg"])
        right_controls.pack(side="right")

        self.credit_label = tk.Label(
            right_controls,
            text="",
            font=("Segoe UI", 9, "bold"),
            bg=self.theme["surface"],
            padx=9,
            pady=5,
        )
        self.credit_label.pack(side="left", padx=(0, 6))
        self.refresh_credit_display()

        self.claim_btn = tk.Button(
            right_controls,
            text="Claim Day 1 (+0)",
            command=self.claim_daily_reward,
            bg="#10A37F",
            fg="#FFFFFF",
            activebackground="#0D8A6B",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            padx=9,
            pady=5,
            cursor="hand2",
        )
        self.claim_btn.pack(side="left", padx=(0, 6))

        self.settings_btn = tk.Button(
            right_controls,
            text="⚙",
            command=self.open_settings,
            bg=self.theme["surface"],
            fg="#ECECEC",
            activebackground=self.theme["surface_hover"],
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Segoe UI", 10),
            padx=7,
            pady=3,
            cursor="hand2",
        )
        self.settings_btn.pack(side="left", padx=(0, 6))

        self.logout_btn = tk.Button(
            right_controls,
            text="Log Out",
            command=self.logout_account,
            bg=self.theme["surface"],
            fg="#ECECEC",
            activebackground=self.theme["surface_hover"],
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=5,
            cursor="hand2",
        )
        self.logout_btn.pack(side="left", padx=(0, 6))

        self.delete_acct_btn = tk.Button(
            right_controls,
            text="Delete Account",
            command=self.delete_account,
            bg="#3B1D24",
            fg="#FCA5A5",
            activebackground="#5C2630",
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=5,
            cursor="hand2",
        )
        self.delete_acct_btn.pack(side="left", padx=(0, 6))

        self.close_app_btn = tk.Button(
            right_controls,
            text="Close App",
            command=self.close_app,
            bg="#2A2A2A",
            fg="#ECECEC",
            activebackground="#3A3A3A",
            activeforeground="#FFFFFF",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=5,
            cursor="hand2",
        )
        self.close_app_btn.pack(side="left")

        fomo_row = tk.Frame(self.right, bg="#262114")
        fomo_row.pack(fill="x", padx=18, pady=(4, 2))

        self.fomo_banner = tk.Label(
            fomo_row,
            text=self.fomo_notice,
            font=("Segoe UI", 8, "bold"),
            bg="#262114",
            fg="#FBBF24",
            padx=10,
            pady=5,
            anchor="w",
        )
        self.fomo_banner.pack(side="left", fill="x", expand=True)

        self.timer_label = tk.Label(
            fomo_row,
            text="24h Ready",
            font=("Consolas", 8, "bold"),
            bg="#262114",
            fg="#10A37F",
            padx=10,
            pady=5,
        )
        self.timer_label.pack(side="right")

        self.update_daily_reward_ui()

        self.header_line = tk.Frame(self.right, bg=self.theme["border"], height=1)
        self.header_line.pack(fill="x", pady=(3, 0))

        feed_wrapper = tk.Frame(self.right, bg=self.theme["bg"])
        feed_wrapper.pack(fill="both", expand=True)

        self.chat_canvas = tk.Canvas(
            feed_wrapper,
            bg=self.theme["bg"],
            highlightthickness=0,
            bd=0,
        )
        self.chat_scrollbar = tk.Scrollbar(
            feed_wrapper,
            orient="vertical",
            command=self.chat_canvas.yview,
        )
        self.chat_canvas.configure(yscrollcommand=self.chat_scrollbar.set)

        self.chat_scrollbar.pack(side="right", fill="y")
        self.chat_canvas.pack(side="left", fill="both", expand=True)

        self.messages_frame = tk.Frame(self.chat_canvas, bg=self.theme["bg"])
        self.canvas_window = self.chat_canvas.create_window(
            (0, 0),
            window=self.messages_frame,
            anchor="nw",
        )

        self.messages_frame.bind(
            "<Configure>",
            lambda e: self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all")),
        )
        self.chat_canvas.bind("<Configure>", self.on_chat_canvas_resize)
        self.chat_canvas.bind_all("<MouseWheel>", self.on_global_mouse_wheel)

        composer_outer = tk.Frame(self.right, bg=self.theme["bg"], padx=24, pady=10)
        composer_outer.pack(fill="x")

        self.composer_canvas = tk.Canvas(
            composer_outer,
            height=64,
            bg=self.theme["bg"],
            highlightthickness=0,
            bd=0,
        )
        self.composer_canvas.pack(fill="x")

        self.composer_inner = tk.Frame(self.composer_canvas, bg=self.theme["surface"])
        self.composer_win = self.composer_canvas.create_window(
            14, 8,
            window=self.composer_inner,
            anchor="nw",
        )

        self.input_text = tk.Text(
            self.composer_inner,
            height=2,
            bg=self.theme["surface"],
            fg="#ECECEC",
            insertbackground="#ECECEC",
            font=("Segoe UI", self.theme["font_size"]),
            wrap="word",
            relief="flat",
            bd=0,
            padx=4,
            pady=4,
        )
        self.input_text.pack(side="left", fill="both", expand=True)
        self.input_text.bind("<Return>", self.on_enter_pressed)

        self.send_button = tk.Button(
            self.composer_inner,
            text="↑",
            command=self.send,
            bg="#ECECEC",
            fg="#171717",
            activebackground="#FFFFFF",
            activeforeground="#000000",
            relief="flat",
            font=("Segoe UI", 12, "bold"),
            width=3,
            pady=2,
            cursor="hand2",
        )
        self.send_button.pack(side="right", padx=(8, 2), pady=4)

        self.composer_canvas.bind("<Configure>", self.redraw_composer_pill)

        self.footer_hint = tk.Label(
            self.right,
            text=f"{self.username} ({self.gmail}) • Credits charged ONLY after AI reply based on response length",
            bg=self.theme["bg"],
            fg=self.theme["muted"],
            font=("Segoe UI", 8),
        )
        self.footer_hint.pack(pady=(0, 8))

        welcome = (
            f"Hey **{self.username}**! Welcome back.\n\n"
            f"- {self.fomo_notice}\n"
            "- Credits are **only subtracted after I reply**, scaled directly by how long my response is.\n"
            "- Claim your daily drop in the top bar—after claiming, a **24-hour** timer starts. Miss a day and you reset to **Day 1 (+0)**!"
        )
        self.add_message_bubble("ai", welcome)

    def redraw_composer_pill(self, event=None):
        w = self.composer_canvas.winfo_width()
        h = self.composer_canvas.winfo_height()
        if w < 40:
            return
        self.composer_canvas.delete("pill")
        draw_rounded_rect(
            self.composer_canvas,
            1, 1, w - 1, h - 1,
            radius=22,
            fill=self.theme["surface"],
            outline=self.theme["border"],
            width=1,
            tags="pill",
        )
        self.composer_canvas.tag_lower("pill")
        self.composer_canvas.itemconfigure(
            self.composer_win,
            width=max(40, w - 28),
            height=max(24, h - 16),
        )

    def on_chat_canvas_resize(self, event):
        self.chat_canvas.itemconfigure(self.canvas_window, width=event.width)

    def on_global_mouse_wheel(self, event):
        widget_under_mouse = self.root.winfo_containing(event.x_root, event.y_root)
        if not widget_under_mouse:
            return
        if str(widget_under_mouse).startswith(str(self.left)):
            return
        units = int(-1 * (event.delta / 120)) if event.delta else 0
        if units:
            self.chat_canvas.yview_scroll(units, "units")

    def on_enter_pressed(self, event):
        if event.state & 0x0001:
            return
        self.send()
        return "break"

    # ==================================================
    # RENDER MODERN MESSAGE STREAM (USER RIGHT / AI LEFT)
    # ==================================================

    def add_message_bubble(self, role, text, cost=None, record=True):
        if record:
            self.message_records.append((role, text, cost))

        row = tk.Frame(self.messages_frame, bg=self.theme["bg"])
        row.pack(fill="x", padx=24, pady=8)

        font_size = self.theme["font_size"]

        if role == "user":
            holder = tk.Frame(row, bg=self.theme["bg"])
            holder.pack(side="right", anchor="e")

            tk.Label(
                holder,
                text=self.username,
                font=("Segoe UI", 8),
                bg=self.theme["bg"],
                fg=self.theme["muted"],
            ).pack(anchor="e", padx=6, pady=(0, 3))

            bubble = tk.Frame(
                holder,
                bg=self.theme["user_bubble"],
                padx=14,
                pady=10,
            )
            bubble.pack(anchor="e")

            tk.Label(
                bubble,
                text=text,
                font=("Segoe UI", font_size),
                bg=self.theme["user_bubble"],
                fg=self.theme["user_text"],
                wraplength=390,
                justify="left",
            ).pack(anchor="w")

        else:
            holder = tk.Frame(row, bg=self.theme["bg"])
            holder.pack(side="left", anchor="w", fill="x", expand=True)

            header_row = tk.Frame(holder, bg=self.theme["bg"])
            header_row.pack(anchor="w", pady=(0, 4))

            tk.Label(
                header_row,
                text="✦",
                font=("Segoe UI", 9, "bold"),
                bg=self.theme["accent"],
                fg="#111111",
                width=2,
            ).pack(side="left", padx=(0, 8))

            ai_header_text = self.theme["ai_name"]
            if cost is not None:
                ai_header_text += f"  •  -{cost:.2f} credits"

            tk.Label(
                header_row,
                text=ai_header_text,
                font=("Segoe UI", 9, "bold"),
                bg=self.theme["bg"],
                fg="#ECECEC",
            ).pack(side="left")

            bubble = tk.Frame(
                holder,
                bg=self.theme["ai_bubble"],
                padx=10,
                pady=6,
            )
            bubble.pack(anchor="w", fill="x", padx=(24, 24))

            rich_text = self.build_rich_ai_text(bubble, text)
            rich_text.pack(fill="x", expand=True)

        self.scroll_chat_to_bottom()
        return row

    def build_rich_ai_text(self, parent, message):
        base_size = self.theme["font_size"]
        ai_bg = self.theme["ai_bubble"]
        ai_fg = self.theme["ai_text"]
        accent = self.theme["accent"]

        widget = tk.Text(
            parent,
            bg=ai_bg,
            fg=ai_fg,
            font=("Segoe UI", base_size),
            wrap="word",
            relief="flat",
            bd=0,
            highlightthickness=0,
            height=1,
            width=44,
        )

        widget.tag_configure("body", foreground=ai_fg, font=("Segoe UI", base_size), spacing2=2)
        widget.tag_configure("h1", foreground=accent, font=("Segoe UI", base_size + 4, "bold"), spacing1=6, spacing3=4)
        widget.tag_configure("h2", foreground="#E2E8F0", font=("Segoe UI", base_size + 2, "bold"), spacing1=5, spacing3=3)
        widget.tag_configure("bold", foreground="#FFFFFF", font=("Segoe UI", base_size, "bold"))
        widget.tag_configure("italic", foreground=accent, font=("Segoe UI", base_size, "italic"))
        widget.tag_configure("code", foreground="#6EE7B7", background="#171717", font=("Consolas", max(9, base_size - 1)))

        pattern = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)")

        lines = message.splitlines() or [message]
        for idx, raw_line in enumerate(lines):
            stripped = raw_line.lstrip()
            if stripped.startswith("## "):
                line_content = stripped[3:]
                line_tag = "h2"
            elif stripped.startswith("# "):
                line_content = stripped[2:]
                line_tag = "h1"
            else:
                line_content = raw_line
                line_tag = "body"

            pos = 0
            for match in pattern.finditer(line_content):
                if match.start() > pos:
                    widget.insert("end", line_content[pos:match.start()], line_tag)
                token = match.group(0)
                if token.startswith("**") and token.endswith("**"):
                    widget.insert("end", token[2:-2], "bold")
                elif token.startswith("`") and token.endswith("`"):
                    widget.insert("end", token[1:-1], "code")
                elif token.startswith("*") and token.endswith("*"):
                    widget.insert("end", token[1:-1], "italic")
                pos = match.end()

            if pos < len(line_content):
                widget.insert("end", line_content[pos:], line_tag)

            if idx < len(lines) - 1:
                widget.insert("end", "\n", line_tag)

        widget.configure(state="disabled")

        def sync_height(_event=None):
            widget.update_idletasks()
            display_lines = widget.count("1.0", "end", "displaylines")
            line_count = display_lines[0] if display_lines else len(lines)
            widget.configure(height=max(1, line_count))

        widget.bind("<Configure>", sync_height)
        self.root.after(10, sync_height)
        return widget

    def scroll_chat_to_bottom(self):
        self.root.update_idletasks()
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
        self.chat_canvas.yview_moveto(1.0)

    # ==================================================
    # SETTINGS MODAL (NO FREE CREDIT RESET)
    # ==================================================

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Appearance & AI Settings")
        win.geometry("440x560")
        win.configure(bg="#1E1E1E")
        win.transient(self.root)
        win.grab_set()

        tk.Label(
            win,
            text="Appearance & AI Companion",
            font=("Segoe UI", 14, "bold"),
            bg="#1E1E1E",
            fg="#ECECEC",
        ).pack(pady=(18, 4))

        tk.Label(
            win,
            text="Day 1: +0 • Days 2–6: +2 • Day 7: +5 • Days 8, 15+: +1",
            font=("Segoe UI", 9),
            bg="#1E1E1E",
            fg="#FBBF24",
        ).pack(pady=(0, 14))

        form = tk.Frame(win, bg="#1E1E1E")
        form.pack(fill="both", expand=True, padx=24)

        tk.Label(form, text="AI Name", font=("Segoe UI", 9, "bold"), bg="#1E1E1E", fg="#D4D4D4").pack(anchor="w")
        name_var = tk.StringVar(value=self.theme["ai_name"])
        tk.Entry(
            form,
            textvariable=name_var,
            bg="#2A2A2A",
            fg="#ECECEC",
            insertbackground="#ECECEC",
            relief="flat",
            font=("Segoe UI", 10),
        ).pack(fill="x", ipady=7, pady=(4, 12))

        tk.Label(form, text="Personality Style", font=("Segoe UI", 9, "bold"), bg="#1E1E1E", fg="#D4D4D4").pack(anchor="w")
        vibe_var = tk.StringVar(value=self.theme["ai_vibe"])
        vibes = [
            "Thoughtful & Natural",
            "Playful Game Dev Buddy",
            "Chill & Encouraging",
            "Energetic & Expressive",
            "Concise & Sharp",
        ]
        vibe_menu = tk.OptionMenu(form, vibe_var, *vibes)
        vibe_menu.configure(
            bg="#2A2A2A",
            fg="#ECECEC",
            activebackground="#3A3A3A",
            highlightthickness=0,
            relief="flat",
        )
        vibe_menu.pack(fill="x", pady=(4, 12))

        tk.Label(form, text="Text Size", font=("Segoe UI", 9, "bold"), bg="#1E1E1E", fg="#D4D4D4").pack(anchor="w")
        size_var = tk.IntVar(value=self.theme["font_size"])
        tk.Scale(
            form,
            from_=9,
            to=16,
            orient="horizontal",
            variable=size_var,
            bg="#1E1E1E",
            fg="#ECECEC",
            highlightthickness=0,
            troughcolor="#2A2A2A",
        ).pack(fill="x", pady=(2, 12))

        color_picks = {
            "bg": ("Workspace Background", self.theme["bg"]),
            "surface": ("Composer & Header Surface", self.theme["surface"]),
            "ai_bubble": ("AI Response Background", self.theme["ai_bubble"]),
            "ai_text": ("AI Text Color", self.theme["ai_text"]),
            "user_bubble": ("Your Message Pill Color", self.theme["user_bubble"]),
            "accent": ("AI Badge & Accent Color", self.theme["accent"]),
        }

        color_buttons = {}

        def pick_color(key):
            chosen = colorchooser.askcolor(color=color_picks[key][1], parent=win)[1]
            if chosen:
                color_picks[key] = (color_picks[key][0], chosen)
                color_buttons[key].configure(bg=chosen)

        for key, (label_text, hex_val) in color_picks.items():
            row = tk.Frame(form, bg="#1E1E1E")
            row.pack(fill="x", pady=4)
            tk.Label(
                row,
                text=label_text,
                font=("Segoe UI", 9),
                bg="#1E1E1E",
                fg="#ECECEC",
            ).pack(side="left")

            btn = tk.Button(
                row,
                text="  Color  ",
                bg=hex_val,
                fg="#FFFFFF",
                relief="flat",
                command=lambda k=key: pick_color(k),
                cursor="hand2",
            )
            btn.pack(side="right")
            color_buttons[key] = btn

        def apply_and_close():
            self.theme["ai_name"] = name_var.get().strip() or "Dictionary-GPT"
            self.theme["ai_vibe"] = vibe_var.get()
            self.theme["font_size"] = size_var.get()
            for k, (_, val) in color_picks.items():
                self.theme[k] = val
            self.apply_theme()
            win.destroy()

        btn_row = tk.Frame(win, bg="#1E1E1E")
        btn_row.pack(fill="x", padx=24, pady=16)

        tk.Button(
            btn_row,
            text="Save & Apply",
            command=apply_and_close,
            bg="#ECECEC",
            fg="#171717",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padx=18,
            pady=8,
            cursor="hand2",
        ).pack(side="right")

    def apply_theme(self):
        self.right.configure(bg=self.theme["bg"])
        self.chat_header.configure(bg=self.theme["bg"])
        self.model_pill.configure(bg=self.theme["bg"])
        self.ai_title_label.configure(text=f"{self.theme['ai_name']} ▾", bg=self.theme["bg"])
        self.ai_status_label.configure(
            text=f"  {self.theme['ai_vibe']}",
            bg=self.theme["bg"],
            fg=self.theme["muted"],
        )
        self.settings_btn.configure(bg=self.theme["surface"])
        self.logout_btn.configure(bg=self.theme["surface"])
        self.chat_canvas.configure(bg=self.theme["bg"])
        self.messages_frame.configure(bg=self.theme["bg"])
        self.composer_canvas.configure(bg=self.theme["bg"])
        self.composer_inner.configure(bg=self.theme["surface"])
        self.input_text.configure(bg=self.theme["surface"], font=("Segoe UI", self.theme["font_size"]))
        self.footer_hint.configure(
            text=f"{self.username} ({self.gmail}) • Credits charged ONLY after AI reply based on response length",
            bg=self.theme["bg"],
            fg=self.theme["muted"],
        )
        self.redraw_composer_pill()
        self.refresh_credit_display()
        self.update_daily_reward_ui()

        for child in self.messages_frame.winfo_children():
            child.destroy()

        for role, text, cost in self.message_records:
            self.add_message_bubble(role, text, cost=cost, record=False)

    # ==================================================
    # SEND MESSAGE & GOOGLE GENAI CALL (POST-RESPONSE BILLING)
    # ==================================================

    def send(self):
        question = self.input_text.get("1.0", "end-1c").strip()
        if not question:
            return

        if self.credits <= 0.00:
            self.add_message_bubble(
                "ai",
                "## 0 Credits Left\n"
                "You're out of credits and there are **no resets**! Watch the **24h** timer and claim your daily streak drop "
                "(**Day 1: +0**, **Days 2–6: +2**, **Day 7: +5**, **Days 8, 15+: +1**).",
            )
            return

        self.input_text.delete("1.0", "end")
        self.add_message_bubble("user", question)

        if not GEMINI_API_KEY:
            self.add_message_bubble(
                "ai",
                "Please set your Gemini API key in `GEMINI_API_KEY` or the `GEMINI_API_KEY` environment variable.",
            )
            return

        self.send_button.configure(state="disabled")
        self.input_text.configure(state="disabled")

        self.thinking_frame = self.add_message_bubble(
            "ai",
            f"*{self.theme['ai_name']} is generating a response...*",
            record=False,
        )

        self.request_id += 1
        current_id = self.request_id

        threading.Thread(
            target=self.ask_ai,
            args=(question, current_id),
            daemon=True,
        ).start()

    def ask_ai(self, question: str, current_id: int):
        try:
            if self.client is None:
                self.client = genai.Client(api_key=GEMINI_API_KEY)

            history = "\n".join(
                f"{role}: {content}"
                for role, content in self.conversation[-10:]
            )

            system_instructions = f"""
You are {self.theme['ai_name']}, a modern, natural AI companion and 2D game-dev partner.

PERSONALITY VIBE: {self.theme['ai_vibe']}

STYLE & BEHAVIOR:
- Respond naturally like a conversational AI companion, following length requests (like multiple paragraphs) when asked.
- You know Python, Tkinter, and 2D game architecture deeply, and you have the user's Dictionary.py notes as optional background reference if helpful.
- Format responses cleanly using `#` for main headings, `##` for subheadings, **bold** for key emphasis, *italics* for highlights, and `backticks` for inline code.
"""

            prompt = f"""
USER: {self.username} ({self.gmail})
AVAILABLE CREDITS: {self.credits:.2f}

RECENT CONVERSATION:
{history}

USER PROMPT:
{question}

OPTIONAL REFERENCE NOTES FROM USER'S DICTIONARY.PY:
{self.numbered_dictionary}
"""

            response = self.client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instructions,
                    max_output_tokens=2500,
                ),
            )

            answer = response.text or "I didn't get a response back that time—try sending that again."

            raw_cost = self.calculate_response_cost(answer)
            charged_cost = round(min(raw_cost, self.credits), 2)

            self.conversation.append((self.username, question))
            self.conversation.append((self.theme["ai_name"], answer))

            self.root.after(0, lambda: self.finish(answer, charged_cost, current_id, subtract=True))

        except Exception as error:
            error_message = f"{type(error).__name__}: {error}"
            self.root.after(
                0,
                lambda: self.finish(
                    f"## Connection Error\nCouldn't reach Google Gemini right now: `{error_message}`",
                    0.00,
                    current_id,
                    subtract=False,
                ),
            )

    def finish(self, answer: str, charged_cost: float, current_id: int, subtract: bool = True):
        if current_id != self.request_id:
            return
        if not self.root.winfo_exists():
            return

        if self.thinking_frame is not None:
            self.thinking_frame.destroy()
            self.thinking_frame = None

        if subtract and charged_cost > 0:
            self.credits = round(max(0.0, self.credits - charged_cost), 2)
            self.last_cost = charged_cost
        else:
            self.last_cost = 0.00

        self.sync_user_record()
        self.refresh_credit_display()
        self.add_message_bubble("ai", answer, cost=charged_cost if subtract else None)

        self.send_button.configure(state="normal")
        self.input_text.configure(state="normal")
        self.input_text.focus_set()


# ==================================================
# START PROGRAM
# ==================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = DictionaryGPT(root)
    if root.winfo_exists():
        root.protocol("WM_DELETE_WINDOW", app.close_app)
        root.mainloop()