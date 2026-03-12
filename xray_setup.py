#!/usr/bin/env python3
"""
Xray Advanced VPN Setup — Anti-Censorship / Anti-DPI
Protocols: VLESS+REALITY+Vision, VLESS+REALITY+gRPC, VLESS+WS+TLS,
           Trojan+gRPC+TLS, VLESS+H2+TLS, Shadowsocks2022, VLESS+SplitHTTP

REALITY dest selection logic (from Xray-core GitHub research):
  - The (IP, SNI) tuple must be logically consistent.
  - GitHub Actions runs on Microsoft Azure infra → Microsoft/Azure SNIs are ideal.
  - Never use Cloudflare-backed sites as dest (causes your server to be abused as SNI proxy).
  - dl.google.com bonus: TLS handshake messages are encrypted after Server Hello.
  - 1.1.1.1:443 with empty serverNames can bypass Iran throttling.
"""

import os, json, uuid, subprocess, time, urllib.request, urllib.parse
import base64, secrets, random, socket

# ─────────────────────────────────────────────────────────────
#  ENV  (set in GitHub Actions secrets)
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
DOMAIN             = os.getenv("DOMAIN", "")   # optional real domain

CONFIG_PATH = "/usr/local/etc/xray/config.json"

PORTS = {
    "reality_vision": 443,    # VLESS + REALITY + Vision  (port 443 = most trusted)
    "reality_grpc":   8443,   # VLESS + REALITY + gRPC    (more resistant in Iran per research)
    "ws_tls":         2053,   # VLESS + WS + TLS          (CDN-friendly, Cloudflare ports)
    "grpc_tls":       2083,   # Trojan + gRPC + TLS       (HTTP/2 multiplexed)
    "h2_tls":         2087,   # VLESS + H2 + TLS          (Cloudflare port, less suspicious)
    "ss2022":         1080,   # Shadowsocks 2022           (AEAD, anti-replay)
    "splithttp":      8880,   # VLESS + SplitHTTP          (looks like plain HTTP)
}

# ─────────────────────────────────────────────────────────────
#  REALITY DESTINATIONS
#
#  Selection rationale (GitHub Actions = Azure datacenters):
#  - Microsoft/Azure domains are the most logical match for GitHub Actions IPs
#  - dl.google.com: TLS messages encrypted after Server Hello (extra obfuscation)
#  - 1.1.1.1:443 + empty SNI: bypasses Iran SNI-based throttling entirely
#  - Never pick Cloudflare-backed sites (gets your server abused as SNI proxy)
#  - Never pick sites obviously mismatched to the VPS datacenter region
# ─────────────────────────────────────────────────────────────
REALITY_DEST_OPTIONS = [
    # Tier 1 — Microsoft/Azure: perfect match for GitHub Actions infra
    {
        "dest":   "www.microsoft.com:443",
        "sni":    ["www.microsoft.com", "microsoft.com"],
        "fp":     "chrome",
        "reason": "Exact ASN match with GitHub Actions (Azure)"
    },
    {
        "dest":   "login.microsoftonline.com:443",
        "sni":    ["login.microsoftonline.com"],
        "fp":     "chrome",
        "reason": "Azure AD — same infra as GitHub Actions"
    },
    {
        "dest":   "azure.microsoft.com:443",
        "sni":    ["azure.microsoft.com"],
        "fp":     "edge",
        "reason": "Azure portal — same Microsoft ASN"
    },
    # Tier 2 — Google: dl.google.com encrypts after Server Hello (bonus obfuscation)
    {
        "dest":   "dl.google.com:443",
        "sni":    ["dl.google.com"],
        "fp":     "chrome",
        "reason": "Encrypted handshake after Server Hello — harder to fingerprint"
    },
    {
        "dest":   "www.google.com:443",
        "sni":    ["www.google.com"],
        "fp":     "chrome",
        "reason": "Google — widely whitelisted, strong TLS 1.3 + H2"
    },
    # Tier 3 — Special: 1.1.1.1 with no SNI bypasses Iran SNI throttling
    {
        "dest":   "1.1.1.1:443",
        "sni":    [],           # empty = bypass SNI whitelist checks
        "fp":     "firefox",
        "reason": "Empty SNI trick — bypasses Iran speed throttling"
    },
    # Tier 4 — Other well-known TLS 1.3 + H2 sites
    {
        "dest":   "www.cloudflare.com:443",
        "sni":    ["www.cloudflare.com"],
        "fp":     "chrome",
        "reason": "Cloudflare main site (not CDN IP — safe)"
    },
    {
        "dest":   "www.github.com:443",
        "sni":    ["www.github.com", "github.com"],
        "fp":     "chrome",
        "reason": "GitHub — also Azure infra, highly trusted SNI"
    },
]

WS_PATH      = f"/{secrets.token_hex(8)}"
GRPC_SERVICE = secrets.token_hex(6)
H2_PATH      = f"/{secrets.token_hex(8)}"
SPLIT_PATH   = f"/{secrets.token_hex(8)}"


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────

def run(cmd):
    os.system(cmd)

def shell(cmd):
    return subprocess.getoutput(cmd)

def get_server_ip():
    for url in ["https://api.ipify.org", "https://ifconfig.me", "https://ipecho.net/plain"]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip:
                    return ip
        except:
            pass
    return shell("hostname -I | awk '{print $1}'")

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id":   TELEGRAM_CHAT_ID,
        "text":      text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            if json.loads(r.read().decode()).get("ok"):
                print("[Telegram] sent ✓")
    except Exception as e:
        print(f"[Telegram] failed: {e}")

def resolve_ip(hostname):
    """Resolve a hostname to its first IP address."""
    try:
        return socket.gethostbyname(hostname)
    except:
        return None


# ─────────────────────────────────────────────────────────────
#  INSTALLATION
# ─────────────────────────────────────────────────────────────

def install_xray():
    print("[*] Installing Xray ...")
    run('bash -c "$(curl -L https://raw.githubusercontent.com/XTLS/Xray-install/main/install-release.sh)"')
    print("[OK] Xray installed.")

def generate_reality_keys():
    out  = shell("xray x25519")
    priv = out.split("\n")[0].split(":", 1)[1].strip()
    pub  = out.split("\n")[1].split(":", 1)[1].strip()
    return priv, pub

def generate_ss2022_key():
    return base64.b64encode(secrets.token_bytes(32)).decode()

def create_self_signed_cert():
    d = "/usr/local/etc/xray/certs"
    os.makedirs(d, exist_ok=True)
    key, crt = f"{d}/server.key", f"{d}/server.crt"
    run(
        f'openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes '
        f'-keyout {key} -out {crt} '
        f'-subj "/CN=microsoft.com/O=Microsoft Corporation/C=US" '
        f'-addext "subjectAltName=DNS:microsoft.com,DNS:*.microsoft.com" '
        f'2>/dev/null'
    )
    return key, crt

def pick_best_reality_dest():
    """
    Pick the best REALITY dest based on connectivity from this machine.
    Tier 1 (Microsoft/Azure) is tried first since GitHub Actions runs on Azure.
    Falls back to Google, then others.
    """
    print("[*] Selecting REALITY dest (testing connectivity) ...")
    for option in REALITY_DEST_OPTIONS:
        host = option["dest"].split(":")[0]
        ip   = resolve_ip(host)
        if ip:
            print(f"[OK] Selected dest: {option['dest']}  ({option['reason']})")
            return option
    # Absolute fallback
    print("[!] All dests failed DNS. Using 1.1.1.1 fallback.")
    return REALITY_DEST_OPTIONS[5]   # 1.1.1.1 entry


# ─────────────────────────────────────────────────────────────
#  IPTABLES — forward UDP/443 + TCP/80 to dest IP
#  This makes the server look exactly like the dest site from outside.
# ─────────────────────────────────────────────────────────────

def setup_iptables_forwarding(dest_host):
    dest_ip = resolve_ip(dest_host)
    if not dest_ip:
        print(f"[!] Could not resolve {dest_host} for iptables forwarding.")
        return
    iface = shell("ip route | grep default | awk '{print $5}' | head -1")
    print(f"[*] Setting up UDP/443 + TCP/80 forwarding to {dest_ip} via {iface} ...")
    # UDP 443 (QUIC) forwarding — makes server respond to QUIC probes like the real site
    run(f"iptables -t nat -A PREROUTING -i {iface} -p udp --dport 443 -j DNAT --to-destination {dest_ip}:443")
    # TCP 80 forwarding — HTTP probes also match the real site
    run(f"iptables -t nat -A PREROUTING -i {iface} -p tcp --dport 80  -j DNAT --to-destination {dest_ip}:80")
    run("iptables -t nat -A POSTROUTING -j MASQUERADE")
    run("sysctl -w net.ipv4.ip_forward=1 > /dev/null")
    print(f"[OK] Forwarding active: UDP/443 + TCP/80 → {dest_ip}")


# ─────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────

def tls_settings(key, crt, alpn):
    return {
        "security": "tls",
        "tlsSettings": {
            "certificates": [{"certificateFile": crt, "keyFile": key}],
            "alpn":         alpn,
            "minVersion":   "1.2",
            "cipherSuites": (
                "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384"
                ":TLS_CHACHA20_POLY1305_SHA256"
            ),
        },
    }

def create_config(priv, uid, trojan_pass, ss_key, key, crt, dest_option, sid):
    dest  = dest_option["dest"]
    sni   = dest_option["sni"]
    fp    = dest_option["fp"]
    tls_h2   = tls_settings(key, crt, ["h2", "http/1.1"])
    tls_http = tls_settings(key, crt, ["http/1.1"])

    reality_base = {
        "show":        False,
        "dest":        dest,
        "xver":        0,
        "serverNames": sni,
        "privateKey":  priv,
        "shortIds":    [sid, secrets.token_hex(8), ""],
        "fingerprint": fp,
        "maxTimeDiff": 60000,
    }

    inbounds = [

        # 1 — VLESS + REALITY + Vision
        #     Best overall camouflage. Vision flow prevents TLS-in-TLS detection.
        {
            "tag": "vless-reality-vision",
            "port": PORTS["reality_vision"],
            "protocol": "vless",
            "settings": {
                "clients": [{"id": uid, "flow": "xtls-rprx-vision"}],
                "decryption": "none",
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": reality_base,
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
        },

        # 2 — VLESS + REALITY + gRPC
        #     Research shows gRPC variant is MORE resistant in Iran than Vision.
        #     HTTP/2 frames make traffic pattern very different from plain TCP.
        {
            "tag": "vless-reality-grpc",
            "port": PORTS["reality_grpc"],
            "protocol": "vless",
            "settings": {
                "clients": [{"id": uid}],
                "decryption": "none",
            },
            "streamSettings": {
                "network": "grpc",
                "security": "reality",
                "grpcSettings": {
                    "serviceName": GRPC_SERVICE,
                    "multiMode":   True,
                },
                "realitySettings": reality_base,
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },

        # 3 — VLESS + WebSocket + TLS  (CDN-compatible, Cloudflare port 2053)
        {
            "tag": "vless-ws-tls",
            "port": PORTS["ws_tls"],
            "protocol": "vless",
            "settings": {
                "clients": [{"id": uid}],
                "decryption": "none",
            },
            "streamSettings": {
                "network": "ws",
                "wsSettings": {
                    "path":    WS_PATH,
                    "headers": {"Host": "microsoft.com"},
                },
                **tls_http,
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },

        # 4 — Trojan + gRPC + TLS  (HTTP/2 multiplexed, looks like Google APIs)
        {
            "tag": "trojan-grpc-tls",
            "port": PORTS["grpc_tls"],
            "protocol": "trojan",
            "settings": {
                "clients": [{"password": trojan_pass}],
            },
            "streamSettings": {
                "network": "grpc",
                "grpcSettings": {
                    "serviceName":          GRPC_SERVICE,
                    "multiMode":            True,
                    "idle_timeout":         60,
                    "health_check_timeout": 20,
                },
                **tls_h2,
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },

        # 5 — VLESS + H2 + TLS  (Cloudflare port 2087 — less suspicious)
        {
            "tag": "vless-h2-tls",
            "port": PORTS["h2_tls"],
            "protocol": "vless",
            "settings": {
                "clients": [{"id": uid}],
                "decryption": "none",
            },
            "streamSettings": {
                "network": "h2",
                "httpSettings": {
                    "path": H2_PATH,
                    "host": ["microsoft.com"],
                    "read_idle_timeout":     30,
                    "health_check_timeout":  15,
                },
                **tls_h2,
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },

        # 6 — Shadowsocks 2022  (AEAD + anti-replay, traffic looks like random bytes)
        {
            "tag": "ss-2022",
            "port": PORTS["ss2022"],
            "protocol": "shadowsocks",
            "settings": {
                "method":   "2022-blake3-aes-256-gcm",
                "password": ss_key,
                "network":  "tcp,udp",
                "ivCheck":  True,
            },
        },

        # 7 — VLESS + SplitHTTP  (newest Xray protocol, mimics chunked HTTP uploads)
        {
            "tag": "vless-splithttp",
            "port": PORTS["splithttp"],
            "protocol": "vless",
            "settings": {
                "clients": [{"id": uid}],
                "decryption": "none",
            },
            "streamSettings": {
                "network": "splithttp",
                "splitHttpSettings": {
                    "path":                  SPLIT_PATH,
                    "host":                  "microsoft.com",
                    "maxUploadSize":         1000000,
                    "maxConcurrentUploads":  10,
                },
            },
        },
    ]

    config = {
        "log": {
            "loglevel": "warning",
            "access":   "/var/log/xray/access.log",
            "error":    "/var/log/xray/error.log",
        },
        "dns": {
            "servers": [
                {"address": "8.8.8.8",  "domains": ["geosite:geolocation-!cn"]},
                {"address": "1.1.1.1",  "domains": ["geosite:geolocation-!cn"]},
                "localhost",
            ],
            "queryStrategy": "UseIPv4",
        },
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                # Block ads
                {
                    "type":         "field",
                    "domain":       ["geosite:category-ads-all"],
                    "outboundTag":  "block"
                },
                # Block connections back to Iran (prevents probe traffic loop)
                {
                    "type":         "field",
                    "ip":           ["geoip:ir"],
                    "outboundTag":  "block"
                },
                # Block private IPs (prevents SSRF)
                {
                    "type":         "field",
                    "ip":           ["geoip:private"],
                    "outboundTag":  "block"
                },
            ],
        },
        "inbounds": inbounds,
        "outbounds": [
            {"tag": "direct", "protocol": "freedom",   "settings": {"domainStrategy": "UseIPv4"}},
            {"tag": "block",  "protocol": "blackhole",  "settings": {}},
        ],
        "policy": {
            "levels": {"0": {
                "handshake":   4,
                "connIdle":    300,
                "uplinkOnly":  2,
                "downlinkOnly": 5,
            }},
            "system": {
                "statsInboundUplink":   True,
                "statsInboundDownlink": True,
            },
        },
    }

    os.makedirs("/usr/local/etc/xray", exist_ok=True)
    os.makedirs("/var/log/xray", exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    print("[OK] Config written.")


# ─────────────────────────────────────────────────────────────
#  LINK GENERATION
# ─────────────────────────────────────────────────────────────

def generate_links(ip, uid, trojan_pass, ss_key, pub, dest_option, sid):
    q    = urllib.parse.quote
    host = DOMAIN if DOMAIN else ip
    sni  = dest_option["sni"][0] if dest_option["sni"] else ""
    fp   = dest_option["fp"]

    reality_params = (
        f"encryption=none&security=reality"
        f"&sni={sni}&fp={fp}&pbk={pub}&sid={sid}"
        f"&type=tcp&flow=xtls-rprx-vision"
    )
    reality_grpc_params = (
        f"encryption=none&security=reality"
        f"&sni={sni}&fp={fp}&pbk={pub}&sid={sid}"
        f"&type=grpc&serviceName={GRPC_SERVICE}"
    )

    return {
        "VLESS REALITY+Vision (best)": (
            f"vless://{uid}@{ip}:{PORTS['reality_vision']}?{reality_params}#REALITY-Vision"
        ),
        "VLESS REALITY+gRPC (Iran-resistant)": (
            f"vless://{uid}@{ip}:{PORTS['reality_grpc']}?{reality_grpc_params}#REALITY-gRPC"
        ),
        "VLESS WS+TLS (CDN)": (
            f"vless://{uid}@{host}:{PORTS['ws_tls']}"
            f"?encryption=none&security=tls&sni={host}"
            f"&type=ws&path={q(WS_PATH)}&host=microsoft.com"
            f"#VLESS-WS-TLS"
        ),
        "Trojan gRPC+TLS": (
            f"trojan://{trojan_pass}@{host}:{PORTS['grpc_tls']}"
            f"?security=tls&sni={host}&type=grpc"
            f"&serviceName={GRPC_SERVICE}&alpn=h2"
            f"#Trojan-gRPC-TLS"
        ),
        "VLESS H2+TLS": (
            f"vless://{uid}@{host}:{PORTS['h2_tls']}"
            f"?encryption=none&security=tls&sni={host}"
            f"&type=h2&path={q(H2_PATH)}&host=microsoft.com"
            f"#VLESS-H2-TLS"
        ),
        "Shadowsocks 2022": (
            "ss://" +
            base64.b64encode(
                f"2022-blake3-aes-256-gcm:{ss_key}".encode()
            ).decode().rstrip("=") +
            f"@{ip}:{PORTS['ss2022']}#SS2022"
        ),
        "VLESS SplitHTTP": (
            f"vless://{uid}@{ip}:{PORTS['splithttp']}"
            f"?encryption=none&type=splithttp"
            f"&path={q(SPLIT_PATH)}&host=microsoft.com"
            f"#VLESS-SplitHTTP"
        ),
    }


# ─────────────────────────────────────────────────────────────
#  TELEGRAM MESSAGE
# ─────────────────────────────────────────────────────────────

def build_message(links, ip, uid, trojan_pass, ss_key, dest_option):
    lines = [
        "🛡 <b>Advanced Xray VPN — Ready</b>", "",
        f"🌐 <b>IP:</b> <code>{ip}</code>",
        f"🔑 <b>UUID:</b> <code>{uid}</code>",
        f"🔐 <b>Trojan:</b> <code>{trojan_pass}</code>",
        f"🔒 <b>SS2022:</b> <code>{ss_key}</code>",
        f"🎭 <b>REALITY dest:</b> <code>{dest_option['dest']}</code>",
        f"📝 <b>Why:</b> {dest_option['reason']}",
        "⏱ <b>Uptime:</b> ~2 hours", "",
        "━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    icons = ["🔮", "🇮🇷", "☁️", "⚡", "🌐", "🔒", "🔀"]
    for (name, link), icon in zip(links.items(), icons):
        lines += ["", f"{icon} <b>{name}</b>", f"<code>{link}</code>"]

    lines += [
        "", "━━━━━━━━━━━━━━━━━━━━━━━",
        "💡 <b>Priority order:</b>",
        "1 REALITY+gRPC (Iran best)  2 REALITY+Vision",
        "3 Trojan gRPC  4 VLESS H2  5 VLESS WS  6 SS2022",
        "", "⚠️ <i>Server shuts down after 2 hours.</i>",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def main():
    uid         = str(uuid.uuid4())
    trojan_pass = secrets.token_hex(16)
    ip          = get_server_ip()
    sid         = secrets.token_hex(4)

    print(f"[*] Server IP : {ip}")
    print(f"[*] UUID      : {uid}")

    install_xray()

    priv, pub    = generate_reality_keys()
    ss_key       = generate_ss2022_key()
    key, crt     = create_self_signed_cert()
    dest_option  = pick_best_reality_dest()

    # Setup iptables so UDP/443 and TCP/80 forward to dest IP
    dest_host = dest_option["dest"].split(":")[0]
    if dest_host != "1.1.1.1":
        setup_iptables_forwarding(dest_host)

    create_config(priv, uid, trojan_pass, ss_key, key, crt, dest_option, sid)

    run("systemctl restart xray && systemctl enable xray")
    time.sleep(3)
    status = shell("systemctl is-active xray")
    print(f"[*] Xray status: {status}")

    links = generate_links(ip, uid, trojan_pass, ss_key, pub, dest_option, sid)

    print("\n" + "=" * 70)
    for name, link in links.items():
        print(f"\n[{name}]\n{link}")
    print("=" * 70)

    send_telegram(build_message(links, ip, uid, trojan_pass, ss_key, dest_option))
    send_telegram("✅ Server live — auto-shutdown in 2 hours.")

    print("\n[*] Sleeping 2 hours ...")
    time.sleep(7200)

    send_telegram("🔴 2h session ended. Shutting down.")
    print("[*] Done.")


if __name__ == "__main__":
    main()
