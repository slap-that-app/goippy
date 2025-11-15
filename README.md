# goippy – GoIP UDP ↔ XMPP SMS/USSD Bridge

`goippy` is a lightweight UDP-only integration bridge between **GoIP GSM gateways** and **XMPP** using an external component.

It provides SMS, USSD, call logging, real-time GoIP state monitoring, bot commands, automatic log cleanup, watchdog reconnection, and multi-instance support.

## Features

- Inbound SMS → XMPP chat messages
- Outbound SMS from XMPP → GoIP via UDP
- Outbound USSD via XMPP
- Inbound USSD replies → XMPP
- Full parsing of GoIP `req:` state packets into DB
- BOT commands (`status`, `log`, `log N`)
- Automatic hourly log cleanup (`LOG_DAYS`)
- XMPP watchdog reconnect
- Multi-instance systemd support
- MySQL/MariaDB + SQLite (experimental)
- Prosody routing via `mod_sms_alias`

---
Tested on:
- goip1
- Firmware Version:	GHSFVT-1.1-68-9
- Module Version:	M25MAR01A01_RSIM
- GSM phone number must be seted in goip sim setup
---

## Installation

### Create sample config
goippy --sample

### Install systemd service
goippy --install

### Install instance per extension
goippy --install 2508

### List installed instances
goippy --list

### Uninstall instance
goippy --uninstall 2508

## Configuration (/etc/goippy.conf)

XMPP_DOMAIN            = "example.org"
XMPP_DATA_DOMAIN       = "sms.example.org"

XMPP_COMPONENT_JID     = "sms.example.org"
XMPP_COMPONENT_SECRET  = "replace_me"
XMPP_HOST              = "127.0.0.1"
XMPP_PORT              = 5347

GOIP_BIND              = "0.0.0.0"
GOIP_PORT              = 44444

DB_BACKEND             = "mysql"
DB_HOST                = "localhost"
DB_BASE                = "goippy"
DB_USER                = "goippy"
DB_PASS                = "secret"

LOG_DAYS               = 3

## Prosody Integration

### External component
Component "sms.example.org" "component"
component_secret = "replace_me"

### Enable sms aliasing
modules_enabled = {
"sms_alias";
}

### Rewriting performed
+372xxxxxxx@example.org  →  +372xxxxxxx@sms.example.org

---

# ✅ **SMS Alias Routing (mod_sms_alias.lua)**

Outgoing SMS messages are sent to phone-like JIDs:

```
<message to="+37255512345@domain.example">
```

A Prosody module rewrites them:

```
+37255512345@domain.example
    → +37255512345@data.domain.example
```

where `data.domain.example` is your goippy component.

This ensures:

- local users never collide with phone-number JIDs
- messages always reach goippy
- inbound/outbound traffic remains cleanly separated

Example Prosody config:

```lua
Component "data.domain.example" "component"
    component_secret = "yoursecret"

modules_enabled = {
    "sms_alias";   -- the routing filter
}
```

## Database Schema

### goippy_gateways
Maps extensions to GoIP credentials and msisdn.

### goippy_state
Stores parsed GoIP keepalive fields:  
msisdn, signal, gsm_status, voip_status, voip_state, remain_time, provider, disable_status, updated_at.

### goippy_calls
Stores RECORD: and HANGUP: events.

### goippy_log
Stores inbound & outbound SMS/USSD logs.

## Database Schema

### goippy_gateways
Maps extensions to GoIP credentials and msisdn.

### goippy_state
Stores parsed GoIP keepalive fields:  
msisdn, signal, gsm_status, voip_status, voip_state, remain_time, provider, disable_status, updated_at.

### goippy_calls
Stores RECORD: and HANGUP: events.

### goippy_log
Stores inbound & outbound SMS/USSD logs.

## BOT Commands (chatting with own MSISDN)

When chatting with the extension's **own SIM number**, the message is interpreted as a bot command.

### status
Shows last known GoIP state.

### log
Shows all recent call entries.

### log N
Shows last N call entries.

### anything else
Is sent to GoIP as USSD.

## GoIP Keepalive Parsing

Example:
req:1102;id:goippy_2508;pass:...;num:+37255620268;signal:25;
gsm_status:LOGIN;voip_status:LOGIN;voip_state:IDLE;remain_time:-1;
pro:Tele2;disable_status:0;...

Parsed fields stored in goippy_state:
- msisdn
- gsm_signal
- gsm_status
- voip_status
- voip_state
- remain_time
- provider
- disable_status
- updated_at  

## XMPP Watchdog

A background thread monitors XMPP every 5 seconds:

- verifies session started
- verifies TCP connection via send_raw(" ")
- reconnects automatically
- reattaches event handlers
- keeps UDP server running

Daemon survives Prosody restarts without downtime.

## Log Cleanup

LOG_DAYS=N  
Deletes entries older than N days (hourly).

When LOG_DAYS=0  
→ log cleanup disabled (permanent logs).

## Full Message Flow

### Inbound SMS
GoIP → UDP → goippy → DB log → XMPP message to user

### Outbound SMS
XMPP user → +number@sms.domain → goippy → UDP → GoIP → GSM

### USSD
User → own MSISDN contact → goippy → UDP → GoIP → operator → GoIP → goippy → XMPP reply

### Status / Logs
User → own MSISDN → command → goippy → DB query → XMPP reply

## Manual Run

Run daemon with explicit config:
goippy /etc/goippy-2508.conf

## Debugging

Restart Prosody in debug:
prosodyctl restart --debug

Check modules:
- sms_alias
- component
- storage_sql  

ASCII ARCHITECTURE DIAGRAM

              +----------------------+
              |      Prosody XMPP    |
              |  (External Component)|
              +----------+-----------+
                         ^
                         | XMPP (component)
                         |
                 +-------+--------+
                 |    goippy     |
                 |---------------|
                 | UDP listener  |
                 | XMPP runner   |
                 | BOT engine    |
                 | DB interface  |
                 +-------+-------+
                         |
                         | UDP
                         v
               +---------+---------+
               |      GoIP        |
               |  GSM Gateway     |
               +---------+--------+
                         |
                         | GSM / SMS / USSD
                         v
               +---------+---------+
               |    Mobile Network |
               +-------------------+

SEQUENCE – Outbound SMS

      User → +372xxxxxxx@sms.domain
                  |
                  v
      Prosody rewrites to sms.domain
                  |
                  v
      goippy.on_message()
                  |
                  v
      UDP: SMS <stamp> 1 <pass> <dest> <text>
                  |
                  v
      GoIP → mobile operator → recipient

SEQUENCE – Inbound SMS

    GoIP                    goippy                   Prosody/XMPP          User
    |                        |                           |                 |
    |-- UDP RECEIVE:msg -->  |                           |                 |
    |                        |-- log to DB ------------->|                 |
    |                        |-- send_message ---------->|-- deliver ----->|

SEQUENCE – BOT "status"

    User        Prosody        goippy             DB
    |            |              |                |
    |--status--> |              |                |
    |            |--rewrite---->|                |
    |            |              |--query state-->|
    |            |              |<---row----------|
    |            |<----reply via XMPP------------|
    |<-----------|                               |

## Multiple GoIP devices / many channels

Each GoIP channel identifies itself with its **ID** and **pass**:

```
goippy_2001 / secret
goippy_2002 / secret2
...
```

Any number of devices can be mixed, even across subnets.

The MSISDN of each gateway is automatically updated from GoIP’s `num:` field.

Outgoing SMS uses ACL (`allow_regex`) if you want to restrict destinations.

---

## Security

- UDP API access must be firewalled (GoIP → server only).
- XMPP component uses a shared secret.
- All passwords stored in DB.
- Gateways can be enabled/disabled without touching GoIP.
- SQLite supported; MariaDB recommended for production.

---

## License

MIT.  
Generated with assistance from ChatGPT and refined manually.

