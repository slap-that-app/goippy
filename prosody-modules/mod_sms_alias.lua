-- mod_sms_alias.lua
--
-- Rewrites messages sent to phone-like JIDs
--   +123456789@example.org
--   003725551234@example.org
-- into:
--   +123456789@data.example.org
--
-- This allows users to send SMS/USSD by simply chatting
-- to a phone number JID. The goippy XMPP component listens
-- on data.<domain> and performs SMS/USSD delivery.
--
-- Install:
--   1. Copy this file into: /usr/lib/prosody/modules/
--   2. In host config:
--        modules_enabled = {
--          "sms_alias";
--        }
--   3. Reload Prosody:
--        systemctl reload prosody

local jid_split = require "util.jid".split
local jid_join  = require "util.jid".join

module:hook("message/bare", function (event)
    local stanza = event.stanza
    local to = stanza.attr.to
    if not to then return end

    local user, domain = jid_split(to)
    if not user or domain ~= module.host then
        return
    end

    -- Only handle messages with a <body>
    if stanza.name ~= "message" then return end
    local body = stanza:get_child_text("body")
    if not body or body == "" then return end

    -- Match phone-like usernames: +372…, 00372…, etc.
    if user:match("^%+") or user:match("^00") then
        local new_to = jid_join(user, "data." .. module.host)
        module:log("info", "SMS alias: rewriting %s -> %s", to, new_to)
        stanza.attr.to = new_to

        module:send(stanza)
        return true -- consume stanza (stop regular routing)
    end
end)
