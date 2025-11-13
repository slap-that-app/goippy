# goippy
UDP-only GoIP ↔ XMPP bridge for SMS, USSD and call logging

goippy communicates with GoIP devices using their SMS-Server UDP API  
(`req:…`, `RECEIVE:…`, `USSD …`, `RECORD:…`, `HANGUP:…`)  
and exposes SMS/USSD to XMPP through an external component (Prosody, ejabberd).

All configuration and history are stored in MariaDB/MySQL or SQLite.

---

## Features

### GoIP → goippy → XMPP
- Heartbeats (`req:nnn;id:...;pass:...`)
   - Validated against database
   - goippy replies: `reg:<id>;status:200;`
   - MSISDN stored per gateway
- Incoming SMS:  
  `RECEIVE:ts;id:goippy_2001;srcnum:+123;msg:Hello`
   - ACK: `RECEIVEOK:ts;id:goippy_2001;status:0;`
   - Logged and forwarded to XMPP as:  
     from: `+123@data.example.org` → to: `2001@example.org`
- Incoming USSD:
  `USSD <stamp> <text…>`
   - Logged and delivered to XMPP as a `[USSD]` message
- Call records:
  `RECORD:ts;id:...;dir:1;num:+123;cause:ANSWER`
   - Saved into `goippy_calls`
   - Each record linked to extension and MSISDN
- GSM phone number must be seted in goip sim setup
### XMPP → goippy → GoIP
A user at XMPP:

```
2001@example.org
```

sends a chat to a phone JID:

```
+123456789@example.org
```

or (with the alias module) simply:

```
+123456789
```

goippy looks up the bound gateway (`goippy_2001`), and sends via UDP:

```
SMS <stamp> 1 <goip_pass> +123456789 <text>
```

USSD is sent using:

```
USSD <stamp> <goip_pass> *100#
```

Delivery reports (`DELIVER:`) are stored in `goippy_log`.

---

## Database Schema

Tables automatically created:

### goippy_gateways
- ext
- goip_id (`goippy_<ext>`)
- goip_pass
- channel
- msisdn
- allow_regex (ACL for outgoing SMS)
- enabled

### goippy_calls
Full call history:  
time, goip_id, ext, direction, remote_num, cause, msisdn, raw line.

### goippy_log
Traffic log (incoming/outgoing SMS, USSD, delivery reports).

---

## Configuration

Config file: `/etc/goippy.conf`

Example:

```python
DB_BACKEND = 'mysql'

DB_HOST = '127.0.0.1'
DB_USER = 'goippy'
DB_PASS = 'pass_goippy'
DB_NAME = 'messaging'
DB_PORT = 3306

# SQLite alternative:
# DB_BACKEND = 'sqlite'
# SQLITE_PATH = '/var/lib/goippy/goippy.sqlite3'

GOIP_UDP_BIND = '0.0.0.0'
GOIP_UDP_PORT = 44444

LOG_TO_STDOUT = True
```

---

## Admin CLI

```
goippy --sample                 # create /etc/goippy.conf
goippy --install [postfix]      # install + start systemd service
goippy --uninstall [postfix]    # remove service
goippy --list                   # list services

goippy --add <ext> <pass> [re]  # create gateway
goippy --remove <ext>           # delete
goippy --listExt                # list gateways
```

Example:

```
goippy --add 2001 secret123
goippy --add 2508 abc123 ^\+37255
```

This creates:

```
goip_id = goippy_2001
goip_pass = secret123
```

Set the same ID/pass in the GoIP SMS Server config.

---

## GoIP Configuration

In GoIP web UI:

```
SMS Server IP:     <your server>
SMS Server Port:   44444
ID for SMS:        goippy_<ext>
Password:          <goip_pass>
```

Nothing else required — no SMPP, no HTTP.

---

## Prosody XMPP Setup

goippy connects as an XMPP component, example:

```
Component "data.example.org"
    component_secret = "YOUR_SECRET"
```

### Optional helper module: mod_sms_alias

Simplifies sending messages to phone numbers.

Put this file into `/usr/lib/prosody/modules/mod_sms_alias.lua`:

Enable in the virtual host:

```lua
modules_enabled = { "sms_alias" }
```

Now users can send SMS by chatting to:

```
+123456789
```

---

## USSD Through XMPP

Work by contact with phone number of used gateway chanel

User sends:

```
*100#
```

to:

```
+123@example.org
```

goippy converts to GoIP UDP:

```
USSD <stamp> <goip_pass> *100#
```

GoIP replies:

```
USSD <stamp> <text>
```

goippy forwards to XMPP as a private message:

```
[USSD] <text>
```

---

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

