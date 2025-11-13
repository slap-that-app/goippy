# goippy

UDP-only **GoIP ↔ XMPP** bridge for SMS, USSD and call logging.

- Talks to GoIP via its **SMS Server UDP API** (`req:`, `RECEIVE:`, `USSD`, `RECORD:`, `HANGUP:`…)
- Talks to XMPP using an **external component** (Prosody, ejabberd, etc.).
- Stores configuration and history in **MariaDB/MySQL** or **SQLite**.

---

## Flow

### 1. GoIP → goippy

GoIP is configured with:

- SMS server IP: your goippy host
- SMS server port: `GOIP_PORT` (default `44444`)
- ID for SMS server: `goippy_<ext>` (e.g. `goippy_2001`)
- Password: same `goip_pass` you configure in DB.

GoIP sends:

- `req:nnn;id:goippy_2001;pass:...;num:+123456789;...` – keepalive  
  → goippy replies `resp:nnn;id:goippy_2001;status:0;` and stores MSISDN.

- `RECEIVE:ts;id:goippy_2001;password:...;srcnum:+123456789;msg:Hello`  
  → goippy ACKs with `RECEIVEOK:ts;id:goippy_2001;status:0;`  
  → logs into DB and forwards to XMPP as:

    - from: `+123456789@data.example.org`
    - to:   `2001@example.org`

- `USSN:ts;id:goippy_2001;msg:Your balance is...`  
  → delivered as `[USSD] Your balance is...` to `2001@example.org`.

- `RECORD:` / `HANGUP:`  
  → written into `goippy_calls` with ext, remote number, direction, cause.

### 2. XMPP → goippy → GoIP

XMPP clients connect to `example.org`.  
Your goippy component is `sms.example.org` in Prosody/ejabberd.

User `2001@example.org`:

- sends chat to `+123456789@example.org` with body `hello`  
  → goippy finds gateway `ext=2001`, uses last GoIP address and sends:

  ```text
  SMS <stamp> 1 <goip_pass> +123456789 hello
