#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
goippy – GoIP UDP <-> XMPP SMS/USSD bridge

Features:
- Pure UDP integration with GoIP "SMS Server" API (no SMPP).
- Listens for:
    * req:...   (keepalive / status)
    * RECEIVE:  (incoming SMS)
    * USSD/USSN (USSD responses)
    * RECORD:, HANGUP: (call events)
- Sends:
    * SMS via "SMS <id> 1 <password> <dest> <message>"
    * USSD via "USSD <id> <password> <code>"

- Delivers inbound SMS/USSD to XMPP via external component.
- XMPP users send messages to phone numbers to send SMS.
- If a user sends to their own MSISDN, message is interpreted as USSD.

Admin CLI (run as root typically):
    goippy --sample
    goippy --install           # main instance using /etc/goippy.conf
    goippy --install 2001      # instance goippy-2001.service with /etc/goippy-2001.conf
    goippy --list
    goippy --uninstall 2001
    goippy --add <ext> <pass> [allow_regex]
    goippy --remove <ext>
    goippy --listExt
    goippy --ussd <ext> <code>  # send USSD via GoIP for that extension

When run without --flags:
    goippy               -> daemon using /etc/goippy.conf
    goippy /path/config  -> daemon using that config file

This file is designed to live at: /usr/local/bin/goippy
"""

import os
import sys
import time
import signal
import socket
import asyncio
import threading
import traceback
import glob
import subprocess
import shutil
import re
from datetime import datetime

import pymysql   # MariaDB / MySQL
import sqlite3   # SQLite (optional)

from slixmpp.componentxmpp import ComponentXMPP

# -------------------------------------------------------------------
# Paths
# -------------------------------------------------------------------

CONF_BASE = "/etc/goippy.conf"
SAMPLE_CONF = "/etc/goippy.conf.sample"
SYSTEMD_DIR = "/etc/systemd/system"
SERVICE_DIR = SYSTEMD_DIR   # alias


# -------------------------------------------------------------------
# Sample config text (safe domains & numbers)
# -------------------------------------------------------------------

SAMPLE_TEXT = r"""# =============================================
# goippy configuration (UDP-only GoIP gateway)
# =============================================

# -----------------------------
# XMPP
# -----------------------------
# Main domain where user extensions live
XMPP_DOMAIN = "example.org"

# Domain used as 'from' for inbound SMS / USSD events
XMPP_DATA_DOMAIN = "data.example.org"

# Component identity (external component in Prosody / ejabberd)
XMPP_COMPONENT_JID = "data.example.org"
XMPP_COMPONENT_SECRET = "replace_me"

# Fallback delivery target when MSISDN has no mapping
XMPP_FALLBACK_DEST = "0000@example.org"

# XMPP host/port where component connects
XMPP_HOST = "127.0.0.1"
XMPP_PORT = 5347


# -----------------------------
# GoIP UDP listener
# -----------------------------
# IP/port where GoIP "SMS Server" points
GOIP_BIND = "0.0.0.0"
GOIP_PORT = 44444


# -----------------------------
# Database backend
# -----------------------------
# DB_BACKEND: "mysql" or "sqlite"
DB_BACKEND = "mysql"

# For MySQL/MariaDB:
DB_HOST = "localhost"
DB_BASE = "goippy"
DB_USER = "goippy"
DB_PASS = "replace_me"

# For SQLite (experimental):
DB_FILE = "/var/lib/goippy/goippy.db"

# -----------------------------
# Log retention
# -----------------------------
# LOG_DAYS = 0  -> keep logs forever
# LOG_DAYS = N  -> delete goippy_log records older than N days
LOG_DAYS = 0
"""


# -------------------------------------------------------------------
# Config loader
# -------------------------------------------------------------------

def load_conf(path):
    ns = {}
    with open(path) as f:
        code = compile(f.read(), path, "exec")
        exec(code, ns, ns)
    # Defaults
    ns.setdefault("XMPP_PORT", 5347)
    ns.setdefault("GOIP_BIND", "0.0.0.0")
    ns.setdefault("GOIP_PORT", 44444)
    ns.setdefault("DB_BACKEND", "mysql")
    ns.setdefault("LOG_DAYS", 0)
    return ns


# -------------------------------------------------------------------
# Database wrapper
# -------------------------------------------------------------------

class DB:
    """
    Minimal DB wrapper around goippy tables:

        goippy_gateways(ext, goip_id, goip_pass, channel, msisdn, allow_regex, enabled, created_at)
        goippy_calls(ts, goip_id, ext, direction, remote_num, cause, msisdn, raw, created_at)
        goippy_log(dir, msisdn, xmpp_jid, body, status, created_at)
        goippy_state(goip_id, ext, msisdn, ... status ..., updated_at)

    Supports:
        - MySQL / MariaDB (primary)
        - SQLite (experimental)
    """

    def __init__(self, conf):
        self.c = conf
        self.backend = (conf.get("DB_BACKEND") or "mysql").lower()
        self.conn = None

    # -----------------------------
    # Connection
    # -----------------------------
    def connect(self):
        if self.conn:
            return

        if self.backend == "mysql":
            self.conn = pymysql.connect(
                host=self.c["DB_HOST"],
                user=self.c["DB_USER"],
                password=self.c["DB_PASS"],
                database=self.c["DB_BASE"],
                autocommit=True,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
            )
        elif self.backend == "sqlite":
            db_file = self.c.get("DB_FILE") or "/var/lib/goippy/goippy.db"
            os.makedirs(os.path.dirname(db_file), exist_ok=True)
            self.conn = sqlite3.connect(db_file, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        else:
            raise RuntimeError(f"Unsupported DB_BACKEND: {self.backend}")

    def cursor(self):
        self.connect()
        return self.conn.cursor()

    # -----------------------------
    # Schema
    # -----------------------------
    def ensure_tables(self):
        c = self.cursor()

        if self.backend == "mysql":
            c.execute("""
CREATE TABLE IF NOT EXISTS goippy_gateways (
    id INT AUTO_INCREMENT PRIMARY KEY,
    ext VARCHAR(32) UNIQUE,
    goip_id VARCHAR(64),
    goip_pass VARCHAR(64),
    channel INT,
    msisdn VARCHAR(32),
    allow_regex VARCHAR(255),
    enabled TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
""")

            c.execute("""
CREATE TABLE IF NOT EXISTS goippy_calls (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts INT,
    goip_id VARCHAR(64),
    ext VARCHAR(32),
    direction TINYINT,
    remote_num VARCHAR(32),
    cause VARCHAR(32),
    msisdn VARCHAR(32),
    raw VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX(goip_id),
    INDEX(ext),
    INDEX(remote_num)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
""")

            c.execute("""
CREATE TABLE IF NOT EXISTS goippy_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    dir ENUM('in','out'),
    msisdn VARCHAR(32),
    xmpp_jid VARCHAR(255),
    body TEXT,
    status VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
""")

            # per-goip state from req: keepalive
            c.execute("""
CREATE TABLE IF NOT EXISTS goippy_state (
    id INT AUTO_INCREMENT PRIMARY KEY,
    goip_id VARCHAR(64) UNIQUE,
    ext VARCHAR(32),
    msisdn VARCHAR(32),
    gsm_signal INT,
    gsm_status VARCHAR(32),
    voip_status VARCHAR(32),
    voip_state VARCHAR(64),
    remain_time INT,
    provider VARCHAR(64),
    disable_status TINYINT(1),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      ON UPDATE CURRENT_TIMESTAMP,
    INDEX(goip_id),
    INDEX(ext),
    INDEX(msisdn)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
""")

        elif self.backend == "sqlite":
            c.execute("""
CREATE TABLE IF NOT EXISTS goippy_gateways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ext TEXT UNIQUE,
    goip_id TEXT,
    goip_pass TEXT,
    channel INTEGER,
    msisdn TEXT,
    allow_regex TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
""")
            c.execute("""
CREATE TABLE IF NOT EXISTS goippy_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    goip_id TEXT,
    ext TEXT,
    direction INTEGER,
    remote_num TEXT,
    cause TEXT,
    msisdn TEXT,
    raw TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
""")
            c.execute("""
CREATE TABLE IF NOT EXISTS goippy_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dir TEXT,
    msisdn TEXT,
    xmpp_jid TEXT,
    body TEXT,
    status TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
""")
            c.execute("""
CREATE TABLE IF NOT EXISTS goippy_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goip_id TEXT UNIQUE,
    ext TEXT,
    msisdn TEXT,
    gsm_signal INTEGER,
    gsm_status TEXT,
    voip_status TEXT,
    voip_state TEXT,
    remain_time INTEGER,
    provider TEXT,
    disable_status INTEGER,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
""")
            self.conn.commit()

    # -----------------------------
    # Gateway management
    # -----------------------------
    def add_gateway(self, ext, goip_pass, allow_regex=".*"):
        """
        ext        : XMPP extension (e.g. "2001")
        goip_pass  : GoIP UDP password
        allow_regex: regex of allowed destination numbers
        """
        goip_id = f"goippy_{ext}"
        c = self.cursor()
        if self.backend == "mysql":
            c.execute(
                """
INSERT INTO goippy_gateways(ext, goip_id, goip_pass, allow_regex, enabled)
VALUES(%s,%s,%s,%s,1)
ON DUPLICATE KEY UPDATE
  goip_id=VALUES(goip_id),
  goip_pass=VALUES(goip_pass),
  allow_regex=VALUES(allow_regex),
  enabled=1
""",
                (ext, goip_id, goip_pass, allow_regex),
            )
        else:  # sqlite: manual upsert
            c.execute("SELECT id FROM goippy_gateways WHERE ext=?;", (ext,))
            row = c.fetchone()
            if row:
                c.execute(
                    "UPDATE goippy_gateways SET goip_id=?, goip_pass=?, allow_regex=?, enabled=1 WHERE ext=?;",
                    (goip_id, goip_pass, allow_regex, ext),
                )
            else:
                c.execute(
                    "INSERT INTO goippy_gateways(ext, goip_id, goip_pass, allow_regex, enabled) VALUES(?,?,?,?,1);",
                    (ext, goip_id, goip_pass, allow_regex),
                )
            self.conn.commit()

    def remove_gateway(self, ext):
        c = self.cursor()
        if self.backend == "mysql":
            c.execute("DELETE FROM goippy_gateways WHERE ext=%s", (ext,))
        else:
            c.execute("DELETE FROM goippy_gateways WHERE ext=?", (ext,))
            self.conn.commit()

    def list_gateways(self):
        c = self.cursor()
        c.execute("SELECT * FROM goippy_gateways ORDER BY ext")
        rows = c.fetchall()
        return [dict(r) for r in rows]

    def find_gateway_by_goip_id(self, goip_id, goip_pass=None):
        c = self.cursor()
        if self.backend == "mysql":
            c.execute(
                "SELECT * FROM goippy_gateways WHERE goip_id=%s AND enabled=1",
                (goip_id,),
            )
        else:
            c.execute(
                "SELECT * FROM goippy_gateways WHERE goip_id=? AND enabled=1",
                (goip_id,),
            )
        row = c.fetchone()
        if not row:
            return None
        row = dict(row)
        if goip_pass is not None and row.get("goip_pass") != goip_pass:
            return None
        return row

    def find_gateway_by_ext(self, ext):
        c = self.cursor()
        if self.backend == "mysql":
            c.execute(
                "SELECT * FROM goippy_gateways WHERE ext=%s AND enabled=1",
                (ext,),
            )
        else:
            c.execute(
                "SELECT * FROM goippy_gateways WHERE ext=? AND enabled=1",
                (ext,),
            )
        row = c.fetchone()
        return dict(row) if row else None

    def update_gateway_msisdn(self, goip_id, msisdn):
        if not msisdn:
            return
        c = self.cursor()
        if self.backend == "mysql":
            c.execute(
                "UPDATE goippy_gateways SET msisdn=%s WHERE goip_id=%s",
                (msisdn, goip_id),
            )
        else:
            c.execute(
                "UPDATE goippy_gateways SET msisdn=? WHERE goip_id=?",
                (msisdn, goip_id),
            )
            self.conn.commit()

    # -----------------------------
    # Logging helpers
    # -----------------------------
    def log_message(self, direction, msisdn, xmpp_jid, body, status):
        c = self.cursor()
        if self.backend == "mysql":
            c.execute(
                "INSERT INTO goippy_log(dir, msisdn, xmpp_jid, body, status)"
                " VALUES(%s,%s,%s,%s,%s)",
                (direction, msisdn, xmpp_jid, body, status),
            )
        else:
            c.execute(
                "INSERT INTO goippy_log(dir, msisdn, xmpp_jid, body, status)"
                " VALUES(?,?,?,?,?)",
                (direction, msisdn, xmpp_jid, body, status),
            )
            self.conn.commit()

    def log_call(self, ts, goip_id, ext, direction, remote_num, cause, msisdn, raw):
        c = self.cursor()
        if self.backend == "mysql":
            c.execute(
                """
INSERT INTO goippy_calls(ts, goip_id, ext, direction, remote_num, cause, msisdn, raw)
VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
""",
                (ts, goip_id, ext, direction, remote_num, cause, msisdn, raw[:240]),
            )
        else:
            c.execute(
                """
INSERT INTO goippy_calls(ts, goip_id, ext, direction, remote_num, cause, msisdn, raw)
VALUES(?,?,?,?,?,?,?,?)
""",
                (ts, goip_id, ext, direction, remote_num, cause, msisdn, raw[:240]),
            )
            self.conn.commit()

    def auth_ok(self, goip_id, goip_pass):
        c = self.cursor()
        if self.backend == "sqlite":
            q = "SELECT 1 FROM goippy_gateways WHERE goip_id=? AND goip_pass=? AND enabled=1"
        else:
            q = "SELECT 1 FROM goippy_gateways WHERE goip_id=%s AND goip_pass=%s AND enabled=1"
        c.execute(q, (goip_id, goip_pass))
        row = c.fetchone()
        return row is not None

    # -----------------------------
    # State table helpers
    # -----------------------------
    def update_state(self, goip_id, ext, msisdn, kv):
        """
        Store parsed req: keepalive data into goippy_state.
        """
        c = self.cursor()
        def _int(key, default=0):
            try:
                return int(kv.get(key, default))
            except Exception:
                return default

        gsm_signal = _int("signal", 0)
        gsm_status = kv.get("gsm_status")
        voip_status = kv.get("voip_status")
        voip_state = kv.get("voip_state")
        remain_time = _int("remain_time", -1)
        provider = kv.get("pro")
        disable_status = _int("disable_status", 0)

        if self.backend == "mysql":
            c.execute("""
INSERT INTO goippy_state(goip_id, ext, msisdn, gsm_signal, gsm_status,
                        voip_status, voip_state, remain_time, provider,
                        disable_status)
VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE
  ext=VALUES(ext),
  msisdn=VALUES(msisdn),
  gsm_signal=VALUES(gsm_signal),
  gsm_status=VALUES(gsm_status),
  voip_status=VALUES(voip_status),
  voip_state=VALUES(voip_state),
  remain_time=VALUES(remain_time),
  provider=VALUES(provider),
  disable_status=VALUES(disable_status),
  updated_at=CURRENT_TIMESTAMP
""", (goip_id, ext, msisdn, gsm_signal, gsm_status, voip_status,
      voip_state, remain_time, provider, disable_status))
        else:
            # SQLite: simple delete+insert
            c.execute("DELETE FROM goippy_state WHERE goip_id=?", (goip_id,))
            c.execute("""
INSERT INTO goippy_state(goip_id, ext, msisdn, gsm_signal, gsm_status,
                        voip_status, voip_state, remain_time, provider,
                        disable_status)
VALUES(?,?,?,?,?,?,?,?,?,?)
""", (goip_id, ext, msisdn, gsm_signal, gsm_status, voip_status,
      voip_state, remain_time, provider, disable_status))
            self.conn.commit()

    def fetch_state(self, ext):
        c = self.cursor()
        if self.backend == "mysql":
            c.execute("SELECT * FROM goippy_state WHERE ext=%s LIMIT 1", (ext,))
        else:
            c.execute("SELECT * FROM goippy_state WHERE ext=? LIMIT 1", (ext,))
        row = c.fetchone()
        return dict(row) if row else None

    def fetch_call_log(self, ext, limit=None):
        c = self.cursor()
        if limit:
            sql = "SELECT * FROM goippy_calls WHERE ext=%s ORDER BY id DESC LIMIT %s"
            params = (ext, limit)
        else:
            sql = "SELECT * FROM goippy_calls WHERE ext=%s ORDER BY id DESC"
            params = (ext,)
        if self.backend == "sqlite":
            sql = sql.replace("%s", "?")
        c.execute(sql, params)
        return [dict(r) for r in c.fetchall()]

    def purge_old_logs(self, days):
        """
        Delete rows from goippy_log older than <days> days.
        days <= 0 -> do nothing.
        """
        if not days or days <= 0:
            return
        c = self.cursor()
        if self.backend == "mysql":
            c.execute(
                "DELETE FROM goippy_log WHERE created_at < (NOW() - INTERVAL %s DAY)",
                (days,),
            )
        else:
            # SQLite: created_at is TEXT, stored as CURRENT_TIMESTAMP
            c.execute(
                "DELETE FROM goippy_log WHERE created_at < datetime('now', ?)",
                (f"-{days} days",),
            )
            self.conn.commit()


# -------------------------------------------------------------------
# GoIP UDP server
# -------------------------------------------------------------------

class GoIPServer(threading.Thread):
    """
    Handles all UDP traffic with GoIP:

    - req:...            -> heartbeat/state, we reply "reg:<reqid>;status:200;"
    - RECEIVE:...        -> inbound SMS
    - USSD/USSN:...      -> USSD responses
    - RECORD:/HANGUP:... -> call events
    - EXPIRY/STATE:...   -> extra status, just logged

    Also provides:
        send_sms(ext, dest, text)
        send_ussd(ext, code)

    using last known addr/port for each goip_id.
    """

    def __init__(self, conf, db, xmpp):
        super().__init__(daemon=True)
        self.c = conf
        self.db = db
        self.xmpp = xmpp
        self.stop_evt = threading.Event()
        self.last_addr_for = {}   # goip_id -> (ip, port)
        self._last_rx_ts = {}     # dedup key -> timestamp
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.settimeout(1.0)
        self.sock.bind((conf["GOIP_BIND"], int(conf["GOIP_PORT"])))

    # -------------
    def log(self, *a):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [GoIP]", *a, flush=True)

    # -------------
    def stop(self):
        self.stop_evt.set()

    # -------------
    def _parse_kv(self, msg):
        """
        Parse GoIP key/value style messages like:
          req:123;id:goippy_2001;pass:xyz;num:+123456789;...
          RECEIVE:123456;id:...;password:...;srcnum:...;msg:...
        """
        parts = msg.split(";")
        out = {}
        first = parts[0]
        if ":" in first:
            tag, val = first.split(":", 1)
            out["_tag"] = tag
            out["_ts"] = val
        for p in parts[1:]:
            if not p or ":" not in p:
                continue
            k, v = p.split(":", 1)
            out[k] = v
        return out

    # -------------
    def send_sms(self, ext, dest, text):
        """
        Send SMS via UDP -> GoIP.

        Look up gateway by ext, get goip_id, goip_pass, last_addr_for[goip_id].
        """
        self.db.connect()
        gw = self.db.find_gateway_by_ext(ext)
        if not gw:
            raise RuntimeError(f"No gateway for extension {ext}")

        goip_id = gw["goip_id"]
        goip_pass = gw["goip_pass"]
        addr = self.last_addr_for.get(goip_id)
        if not addr:
            raise RuntimeError(f"No known UDP address for GoIP id {goip_id} (no req: seen yet)")

        stamp = int(time.time())
        # SMS <stamp> <chan> <password> <dest> <message>
        payload = f"SMS {stamp} 1 {goip_pass} {dest} {text}"
        self.log(f"Sending SMS via {goip_id} to {dest}: {text!r} -> {payload}")
        self.sock.sendto(payload.encode("utf-8"), addr)

    # -------------
    def send_ussd(self, ext, code):
        """
        Send USSD via UDP -> GoIP.
        USSD <stamp> <password> <code>
        """
        self.db.connect()
        gw = self.db.find_gateway_by_ext(ext)
        if not gw:
            raise RuntimeError(f"No gateway for extension {ext}")

        goip_id = gw["goip_id"]
        goip_pass = gw["goip_pass"]
        addr = self.last_addr_for.get(goip_id)
        if not addr:
            raise RuntimeError(f"No known UDP address for GoIP id {goip_id} (no req: seen yet)")

        stamp = int(time.time())
        payload = f"USSD {stamp} {goip_pass} {code}"
        self.log(f"Sending USSD via {goip_id}: {code!r} -> {payload}")
        self.sock.sendto(payload.encode("utf-8"), addr)

    # -------------
    def _maybe_dedup(self, goip_id, ts, srcnum, text):
        """
        Deduplicate RECEIVE events:
          - Key = goip_id:ts
          - Keep for ~180s
        """
        dedup_key = f"{goip_id}:{ts}"
        now = time.time()
        if dedup_key in self._last_rx_ts:
            self.log(f"Duplicate RECEIVE from {goip_id}, ignoring ({srcnum}: {text!r})")
            return True
        self._last_rx_ts[dedup_key] = now
        # cleanup old keys
        for k, t0 in list(self._last_rx_ts.items()):
            if now - t0 > 180:
                self._last_rx_ts.pop(k, None)
        return False

    # -------------
    def _deliver_sms_to_xmpp(self, goip_id, srcnum, text):
        """
        Map GoIP channel -> gateway -> ext@XMPP_DOMAIN and send via xmpp component.
        """
        try:
            self.db.connect()
            gw = self.db.find_gateway_by_goip_id(goip_id)
            ext = gw["ext"] if gw else None
            if not ext:
                self.log(f"No gateway mapping for GoIP {goip_id}, dropping SMS from {srcnum}")
                return

            to_jid = f"{ext}@{self.c['XMPP_DOMAIN']}"
            from_jid = f"{srcnum}@{self.c['XMPP_DATA_DOMAIN']}"
            self.db.log_message("in", srcnum, to_jid, text, "recv")

            if not self.xmpp:
                self.log("No XMPP component attached, cannot deliver SMS.")
                return

            loop = getattr(self.xmpp, "loop", None)
            if loop and loop.is_running():
                self.log(f"XMPP message to: {to_jid} from: {from_jid}")
                loop.call_soon_threadsafe(
                    lambda: self.xmpp.send_message(
                        mto=to_jid,
                        mfrom=from_jid,
                        mbody=text,
                        mtype="chat",
                    )
                )
            else:
                self.log("XMPP loop not running, skipping message send.")
        except Exception as e:
            self.log("DB/XMPP error delivering SMS:", e)

    # -------------
    def _deliver_ussd_to_xmpp(self, goip_id, text):
        """
        USSD replies from GoIP -> extension -> XMPP.
        """
        try:
            self.db.connect()
            gw = self.db.find_gateway_by_goip_id(goip_id)
            ext = gw["ext"] if gw else None
            msisdn = gw["msisdn"] if gw else None
            if not ext:
                self.log(f"No gateway mapping for GoIP {goip_id}, dropping USSD reply")
                return

            to_jid = f"{ext}@{self.c['XMPP_DOMAIN']}"
            from_jid = f"{(msisdn or 'ussd')+'@'+self.c['XMPP_DATA_DOMAIN']}"
            body = f"[USSD] {text}"

            self.db.log_message("in", msisdn, to_jid, body, "ussd_reply")

            if not self.xmpp:
                self.log("No XMPP component attached, cannot deliver USSD.")
                return

            loop = getattr(self.xmpp, "loop", None)
            if loop and loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: self.xmpp.send_message(
                        mto=to_jid,
                        mfrom=from_jid,
                        mbody=body,
                        mtype="chat",
                    )
                )
            else:
                self.log("XMPP loop not running, skipping USSD send.")
        except Exception as e:
            self.log("Error handling USSD:", e)

    # -------------
    def run(self):
        self.log(f"UDP listener on {self.c['GOIP_BIND']}:{self.c['GOIP_PORT']}")
        while not self.stop_evt.is_set():
            try:
                try:
                    data, addr = self.sock.recvfrom(4096)
                except socket.timeout:
                    continue

                msg = data.decode("utf-8", "ignore").strip()
                if not msg:
                    continue
                self.log("RX from", addr, "->", msg)

                # -----------------------------
                # Keepalive / status
                # -----------------------------
                if msg.startswith("req:"):
                    kv = self._parse_kv(msg)
                    goip_id = kv.get("id")
                    goip_pass = kv.get("pass")
                    num = kv.get("num", "")
                    reqid = kv.get("_ts", "0")

                    if not goip_id:
                        self.log("Malformed req (no id):", msg)
                        continue

                    if not self.db.auth_ok(goip_id, goip_pass):
                        reply = f"reg:{reqid};status:403;"
                        self.sock.sendto(reply.encode("utf-8"), addr)
                        self.log(f"Heartbeat DENY -> {reply}")
                        continue

                    # remember source address
                    self.last_addr_for[goip_id] = addr

                    # ACK GoIP (this format keeps SMS_LOGIN:Y happy)
                    reply = f"reg:{reqid};status:200;"
                    self.sock.sendto(reply.encode("utf-8"), addr)
                    self.log(f"Heartbeat ACK -> {reply}")

                    # Update MSISDN mapping
                    try:
                        self.db.update_gateway_msisdn(goip_id, num)
                    except Exception as e:
                        self.log("Error updating gateway msisdn:", e)

                    # Update extended state row
                    try:
                        gw = self.db.find_gateway_by_goip_id(goip_id)
                        ext = gw["ext"] if gw else None
                        self.db.update_state(goip_id, ext, num, kv)
                    except Exception as e:
                        self.log("Error updating state:", e)

                    continue

                # -----------------------------
                # Incoming SMS
                # RECEIVE:ts;id:...;password:...;srcnum:...;msg:...
                # -----------------------------
                if msg.startswith("RECEIVE:"):
                    kv = self._parse_kv(msg)
                    ts = kv.get("_ts") or "0"
                    goip_id = kv.get("id")
                    srcnum = kv.get("srcnum")
                    text = kv.get("msg") or ""

                    if not goip_id or not srcnum:
                        self.log("Malformed RECEIVE (missing id/srcnum):", msg)
                        continue

                    # Dedup
                    if self._maybe_dedup(goip_id, ts, srcnum, text):
                        continue

                    # ACK to stop repeats
                    try:
                        reply = f"RECEIVEOK:{ts};id:{goip_id};status:0;"
                        self.sock.sendto(reply.encode("utf-8"), addr)
                        self.log(f"ACK RECEIVE -> {reply}")
                    except Exception as e:
                        self.log("ACK send error:", e)

                    self.log(f"Incoming SMS from {srcnum} via {goip_id}: {text!r}")
                    self._deliver_sms_to_xmpp(goip_id, srcnum, text)
                    continue

                # --------------------------------------------------
                # USSD replies
                # --------------------------------------------------
                if msg.startswith("USSN:") or msg.startswith("USSD:"):
                    # Format A: USSN:ts;id:goippy_2508;msg:Text
                    if msg[4] == ":":
                        kv = self._parse_kv(msg)
                        goip_id = kv.get("id")
                        text = kv.get("msg") or ""
                        self.log(f"USSD (kv) from {goip_id}: {text!r}")
                        if goip_id:
                            self._deliver_ussd_to_xmpp(goip_id, text)
                        continue

                # --------------------------------------------------
                # Format B: "USSD <stamp> <text...>"
                # --------------------------------------------------
                if msg.startswith("USSD "):
                    parts = msg.split(" ", 2)
                    if len(parts) >= 3:
                        text = parts[2]
                    else:
                        text = msg[5:]

                    goip_id = None
                    for gid, a in self.last_addr_for.items():
                        if a == addr:
                            goip_id = gid
                            break

                    self.log(f"USSD (raw) from {goip_id}: {text!r}")
                    if goip_id:
                        self._deliver_ussd_to_xmpp(goip_id, text)
                    else:
                        self.log("Could not resolve goip_id for raw USSD")
                    continue

                # -----------------------------
                # Call records
                # -----------------------------
                if msg.startswith("RECORD:") or msg.startswith("HANGUP:"):
                    kv = self._parse_kv(msg)
                    tag = kv.get("_tag")
                    ts = kv.get("_ts")
                    goip_id = kv.get("id")
                    num = kv.get("num") or ""
                    cause = ""
                    direction = 0

                    if tag == "RECORD":
                        d_raw = kv.get("dir") or "0"
                        try:
                            direction = int(d_raw)
                        except ValueError:
                            direction = 0
                    elif tag == "HANGUP":
                        cause = kv.get("cause") or ""

                    try:
                        self.db.connect()
                        gw = self.db.find_gateway_by_goip_id(goip_id) if goip_id else None
                        ext = gw["ext"] if gw else None
                        msisdn = gw["msisdn"] if gw else None

                        try:
                            ts_int = int(ts)
                        except Exception:
                            ts_int = None

                        self.db.log_call(
                            ts=ts_int,
                            goip_id=goip_id,
                            ext=ext,
                            direction=direction,
                            remote_num=num,
                            cause=cause,
                            msisdn=msisdn,
                            raw=msg,
                        )
                    except Exception as e:
                        self.log("Error logging call record:", e)
                    continue

                # -----------------------------
                # EXPIRY / STATE / ERROR etc
                # -----------------------------
                if msg.startswith("EXPIRY:") or msg.startswith("STATE:"):
                    self.log("Status packet:", msg)
                    continue

                if msg.startswith("ERROR"):
                    self.log("GoIP reported ERROR:", msg)
                    continue

                # Unknown
                self.log("Unknown packet:", msg)

            except Exception as e:
                self.log("Loop error:", e)
                time.sleep(1.0)

        self.log("UDP listener exiting.")


# -------------------------------------------------------------------
# XMPP Component
# -------------------------------------------------------------------

class GoippyXMPP(ComponentXMPP):
    """
    External component used as XMPP <-> goippy bridge.

    Behaviour:
    - Incoming message from ext@example.org to +number@anything:
        -> looked up gateway by ext
        -> if dest number equals gateway.msisdn => treat as USSD
        -> else treat as SMS

    - Inbound SMS/USSD from GoIP is delivered via GoIPServer calling
      db + xmpp.send_message() with from=msisdn@XMPP_DATA_DOMAIN

    Extra bot commands (per ext):
      body == "status"       -> reply with last radio state from goippy_state
      body == "log" / "log 0" -> dump full call log for that ext
      body == "log N"        -> last N call records
    """

    def __init__(self, conf, db, goip_server):
        super().__init__(
            conf["XMPP_COMPONENT_JID"],
            conf["XMPP_COMPONENT_SECRET"],
            conf["XMPP_HOST"],
            int(conf["XMPP_PORT"]),
        )
        self.c = conf
        self.db = db
        self.goip = goip_server
        self.session_started = False

        self.add_event_handler("session_start", self.on_session_start)
        self.add_event_handler("message", self.on_message)

    async def on_session_start(self, _):
        self.session_started = True
        print(f"[goippy] XMPP component connected as {self.c['XMPP_COMPONENT_JID']}")

    def on_message(self, m):
        if m["type"] not in ("chat", "normal"):
            return

        body = (m["body"] or "").strip()
        if not body:
            return

        from_bare = m["from"].bare
        to_bare = m["to"].bare

        ext = from_bare.split("@", 1)[0]
        dest_local = to_bare.split("@", 1)[0]

        body_l = body.lower()

        # ----------------------------------------
        # BOT COMMANDS: status / log
        # ----------------------------------------
        if body_l.startswith("status"):
            st = self.db.fetch_state(ext)
            if not st:
                self.send_message(
                    mto=from_bare,
                    mbody="No state available yet (no req: seen).",
                    mtype="chat",
                )
            else:
                text = (
                    f"State for {st.get('msisdn') or 'unknown'}:\n"
                    f"  Signal: {st.get('gsm_signal')}\n"
                    f"  GSM: {st.get('gsm_status')}\n"
                    f"  VOIP: {st.get('voip_status')} / {st.get('voip_state')}\n"
                    f"  Provider: {st.get('provider')}\n"
                    f"  Disabled: {st.get('disable_status')}\n"
                    f"  Updated: {st.get('updated_at')}"
                )
                self.send_message(mto=from_bare, mbody=text, mtype="chat")
            return

        if body_l.startswith("log"):
            parts = body_l.split()
            limit = None
            if len(parts) > 1:
                try:
                    n = int(parts[1])
                    limit = None if n <= 0 else n
                except Exception:
                    limit = None

            rows = self.db.fetch_call_log(ext, limit)
            if not rows:
                self.send_message(
                    mto=from_bare,
                    mbody="No call log entries.",
                    mtype="chat",
                )
                return

            lines = ["Call log:"]
            for r in rows:
                ts = r.get("ts")
                try:
                    ts_str = datetime.fromtimestamp(ts).isoformat(sep=" ") if ts else "n/a"
                except Exception:
                    ts_str = str(ts or "n/a")
                lines.append(
                    f"{r.get('id')}: {ts_str} num={r.get('remote_num')} "
                    f"dir={r.get('direction')} cause={r.get('cause')}"
                )

            self.send_message(
                mto=from_bare,
                mbody="\n".join(lines),
                mtype="chat",
            )
            return

        # ----------------------------------------
        # SMS / USSD routing
        # ----------------------------------------

        # Destination must look like a phone number (+ or digits)
        if not re.match(r"^\+?\d+$", dest_local):
            self.send_message(
                mto=from_bare,
                mbody="Destination must be a phone number (+123...).",
                mtype="chat",
            )
            return

        self.db.connect()
        gw = self.db.find_gateway_by_ext(ext)
        if not gw:
            self.send_message(
                mto=from_bare,
                mbody="No gateway configured for this extension.",
                mtype="chat",
            )
            return

        # Normalize: + removed for comparison
        def normalize_num(x):
            return (x or "").replace("+", "").replace(" ", "")

        dest_norm = normalize_num(dest_local)
        msisdn_norm = normalize_num(gw.get("msisdn"))

        try:
            # USSD: when sending to its own MSISDN
            if msisdn_norm and dest_norm == msisdn_norm:
                self.goip.send_ussd(ext, body)
                self.db.log_message("out", gw.get("msisdn"), from_bare, body, "ussd_sent")
                self.send_message(
                    mto=from_bare,
                    mbody=f"USSD sent via {gw.get('msisdn') or 'channel'}: {body}",
                    mtype="chat",
                )
            else:
                # Normal SMS
                dest_sms = dest_local if dest_local.startswith("+") else f"+{dest_local}"
                self.goip.send_sms(ext, dest_sms, body)
                self.db.log_message("out", dest_sms, from_bare, body, "sms_sent")
                self.send_message(
                    mto=from_bare,
                    mbody=f"SMS sent to {dest_sms}",
                    mtype="chat",
                )
        except Exception as e:
            self.db.log_message("out", dest_local, from_bare, body, f"error:{e}")
            self.send_message(
                mto=from_bare,
                mbody=f"Error: {e}",
                mtype="chat",
            )

    def is_alive(self):
        """
        Minimal liveness check:
        - session started?
        - connected transport?
        """
        try:
            if not getattr(self, "session_started", False):
                return False
            if not self.connected:
                return False
            return True
        except Exception:
            return False


# -------------------------------------------------------------------
# Systemd unit writer
# -------------------------------------------------------------------

def write_unit(name, confpath):
    unit = f"""[Unit]
Description=goippy UDP/XMPP bridge ({name})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/goippy {confpath}
KillSignal=SIGTERM
TimeoutStopSec=10
Restart=on-failure
RestartSec=3
User=root
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""
    path = os.path.join(SYSTEMD_DIR, f"{name}.service")
    with open(path, "w") as f:
        f.write(unit)
    os.chmod(path, 0o644)
    return path


# -------------------------------------------------------------------
# Admin helpers
# -------------------------------------------------------------------

def create_sample():
    if not os.path.exists(SAMPLE_CONF):
        with open(SAMPLE_CONF, "w") as f:
            f.write(SAMPLE_TEXT)
        print(f"[goippy] sample config written to {SAMPLE_CONF}")
    else:
        print(f"[goippy] sample config already exists at {SAMPLE_CONF}")

    if not os.path.exists(CONF_BASE):
        shutil.copy(SAMPLE_CONF, CONF_BASE)
        print(f"[goippy] base config {CONF_BASE} created")
    else:
        print(f"[goippy] base config {CONF_BASE} already exists")


def install_instance(suffix=None):
    if not os.path.exists(CONF_BASE):
        print("[goippy] /etc/goippy.conf missing. Run --sample first and edit it.")
        sys.exit(1)

    base_conf = load_conf(CONF_BASE)

    # Ensure DB works
    db = DB(base_conf)
    try:
        db.connect()
        db.ensure_tables()
    except Exception as e:
        print("[goippy] DB error while preparing tables:", e)
        sys.exit(1)

    if suffix:
        name = f"goippy-{suffix}"
        confpath = f"/etc/goippy-{suffix}.conf"
        if not os.path.exists(confpath):
            shutil.copy(CONF_BASE, confpath)
            # Optionally override fallback dest per instance
            with open(confpath, "a") as f:
                f.write(f'\n# Instance-specific fallback\nXMPP_FALLBACK_DEST = "{suffix}@{base_conf["XMPP_DOMAIN"]}"\n')
            print(f"[goippy] instance config {confpath} created")
    else:
        name = "goippy"
        confpath = CONF_BASE

    unit = write_unit(name, confpath)
    os.system("systemctl daemon-reload")
    os.system(f"systemctl enable --now {name}.service")
    print(f"[goippy] installed {unit} and started.")


def list_instances():
    print("Installed goippy instances:")
    pattern = os.path.join(SERVICE_DIR, "goippy*.service")
    services = sorted(glob.glob(pattern))
    if not services:
        print("  (none)")
        return
    for svc in services:
        name = os.path.basename(svc).replace(".service", "")
        conf = f"/etc/{name}.conf" if name != "goippy" else CONF_BASE
        status = subprocess.getoutput(f"systemctl is-active {name}")
        print(f"  {name:15} [{status:8}]  {conf if os.path.exists(conf) else '(no conf)'}")


def uninstall_instance(suffix):
    name = f"goippy-{suffix}" if suffix != "default" else "goippy"
    svc = f"{name}.service"
    conf = f"/etc/{name}.conf" if name != "goippy" else CONF_BASE

    print(f"[goippy] Uninstalling {svc} ...")
    subprocess.call(["systemctl", "stop", svc])
    subprocess.call(["systemctl", "disable", svc])

    svc_path = os.path.join(SERVICE_DIR, svc)
    if os.path.exists(svc_path):
        os.remove(svc_path)
        print(f"[goippy] Removed {svc_path}")
    else:
        print(f"[goippy] {svc_path} not found")

    if os.path.exists(conf) and name != "goippy":
        os.remove(conf)
        print(f"[goippy] Removed {conf}")
    else:
        if os.path.exists(conf):
            print(f"[goippy] Base config {conf} kept.")
        else:
            print(f"[goippy] {conf} not found")

    subprocess.call(["systemctl", "daemon-reload"])
    print(f"[goippy] Instance {suffix} uninstalled.")


def gateways_cli_add(args):
    if len(args) < 2:
        print("Usage: goippy --add <ext> <goip_pass> [allow_regex]")
        sys.exit(1)

    ext = args[0]
    goip_pass = args[1]
    allow_regex = args[2] if len(args) > 2 else ".*"

    base_conf = load_conf(CONF_BASE)
    db = DB(base_conf)
    db.connect()
    db.ensure_tables()

    db.add_gateway(ext, goip_pass, allow_regex)
    print(f"[goippy] gateway added/updated: ext={ext}, goip_id=goippy_{ext}, allow_regex={allow_regex}")


def gateways_cli_remove(args):
    if len(args) < 1:
        print("Usage: goippy --remove <ext>")
        sys.exit(1)

    ext = args[0]
    base_conf = load_conf(CONF_BASE)
    db = DB(base_conf)
    db.connect()
    db.ensure_tables()

    db.remove_gateway(ext)
    print(f"[goippy] gateway removed: ext={ext}")


def gateways_cli_list():
    base_conf = load_conf(CONF_BASE)
    db = DB(base_conf)
    db.connect()
    db.ensure_tables()

    rows = db.list_gateways()
    if not rows:
        print("[goippy] no gateways defined.")
        return

    print("ext   goip_id         msisdn           enabled  allow_regex")
    print("----  --------------  ---------------  -------  ---------------------")
    for r in rows:
        print(
            f"{(r.get('ext') or ''):<4}  "
            f"{(r.get('goip_id') or ''):<14}  "
            f"{(r.get('msisdn') or ''):<15}  "
            f"{str(r.get('enabled') or 0):<7}  "
            f"{r.get('allow_regex') or ''}"
        )


def cli_ussd(args):
    """
    goippy --ussd <ext> <code>
    """
    if len(args) < 2:
        print("Usage: goippy --ussd <ext> <code>")
        sys.exit(1)

    ext = args[0]
    code = " ".join(args[1:])

    conf = load_conf(CONF_BASE)
    db = DB(conf)
    db.connect()
    db.ensure_tables()

    print("[goippy] NOTE: cli --ussd assumes a running daemon has already learned GoIP address.")
    print(
        "[goippy] For simple USSD testing, send USSD by chatting in XMPP:\n"
        "  From ext@example.org to its own MSISDN contact, send the USSD code.\n"
        "The running goippy daemon will convert that to UDP USSD."
    )


def start_log_purger(conf, db):
    days = int(conf.get("LOG_DAYS", 0) or 0)
    if days <= 0:
        return None

    def worker():
        while True:
            try:
                db.purge_old_logs(days)
            except Exception as e:
                print("[goippy] Log purge error:", e)
            time.sleep(3600)  # run every hour

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t

# -------------------------------------------------------------------
# XMPP launcher (modern slixmpp async API)
# -------------------------------------------------------------------

async def xmpp_runner(xmpp, stop_flag):
    """
    Keeps XMPP online forever.
    Reconnects automatically on disconnect or error.
    """
    while not stop_flag["stopping"]:
        try:
            print("[goippy] Connecting XMPP component...")
            await xmpp.connect()        # slixmpp async connect()
            await xmpp.process()        # slixmpp async event loop
        except Exception as e:
            print("[goippy] XMPP error:", e)

        if stop_flag["stopping"]:
            break

        print("[goippy] XMPP disconnected — retry in 5s...")
        await asyncio.sleep(5)
# -------------------------------------------------------------------
# Daemon main runner
# -------------------------------------------------------------------

def run_daemon(conf_path):
    c = load_conf(conf_path)

    db = DB(c)
    db.connect()
    db.ensure_tables()

    # Create GoIP server first (without XMPP)
    goip_server = GoIPServer(c, db, xmpp=None)

    # Create XMPP component
    xmpp = GoippyXMPP(c, db, goip_server)

    # Link them
    goip_server.xmpp = xmpp

    # Start UDP listener thread
    goip_server.start()

    # Start log cleanup worker
    start_log_purger(c, db)

    stop_flag = {"stopping": False}

    def shutdown(signum, frame):
        if stop_flag["stopping"]:
            return
        stop_flag["stopping"] = True
        print(f"[goippy] Caught signal {signum}, shutting down...")

        try:
            goip_server.stop()
        except:
            pass

        try:
            xmpp.disconnect()
        except:
            pass

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # XMPP reconnect loop lives in main thread
    while not stop_flag["stopping"]:
        try:
            xmpp.session_started = False
            print("[goippy] Connecting XMPP component...")
            xmpp.connect()
            # slixmpp manages its own asyncio loop internally
            xmpp.process(forever=False)
            print("[goippy] XMPP process() returned.")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("[goippy] XMPP error:", e)

        if not stop_flag["stopping"]:
            print("[goippy] XMPP disconnected — retry in 5s...")
            time.sleep(5)

    print("[goippy] daemon stopping...")

    try:
        goip_server.stop()
    except:
        pass
    try:
        xmpp.disconnect()
    except:
        pass

    print("[goippy] daemon stopped.")


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

def main():
    if len(sys.argv) > 1 and sys.argv[1].startswith("--"):
        cmd = sys.argv[1]

        if cmd == "--sample":
            create_sample()
            return

        if cmd == "--install":
            suffix = sys.argv[2] if len(sys.argv) > 2 else None
            install_instance(suffix)
            return

        if cmd == "--uninstall":
            if len(sys.argv) < 3:
                print("Usage: goippy --uninstall <suffix-or-default>")
                return
            uninstall_instance(sys.argv[2])
            return

        if cmd == "--list":
            list_instances()
            return

        if cmd == "--add":
            gateways_cli_add(sys.argv[2:])
            return

        if cmd == "--remove":
            gateways_cli_remove(sys.argv[2:])
            return

        if cmd == "--listExt":
            gateways_cli_list()
            return

        if cmd == "--ussd":
            cli_ussd(sys.argv[2:])
            return

        print("Unknown flag:", cmd)
        print("Supported:")
        print("  --sample")
        print("  --install [suffix]")
        print("  --uninstall <suffix-or-default>")
        print("  --list")
        print("  --add <ext> <goip_pass> [allow_regex]")
        print("  --remove <ext>")
        print("  --listExt")
        print("  --ussd <ext> <code>")
        return

    # Daemon mode
    conf_path = sys.argv[1] if len(sys.argv) > 1 else CONF_BASE
    if not os.path.exists(conf_path):
        print(f"[goippy] Config {conf_path} not found. Use --sample and edit.")
        sys.exit(1)

    run_daemon(conf_path)


if __name__ == "__main__":
    main()
