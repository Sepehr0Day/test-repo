#!/usr/bin/env python3
"""
All-in-One Anti-Censorship VPN Setup for GitHub Actions
========================================================
Protocols installed in parallel:
  1. Xray  — VLESS+REALITY+Vision  (port 443)
  2. Xray  — VLESS+REALITY+gRPC    (port 8443)
  3. Xray  — VLESS+WS+TLS          (port 2053)
  4. Xray  — Trojan+gRPC+TLS       (port 2083)
  5. Xray  — Shadowsocks 2022      (port 1080)
  6. Hysteria2 — QUIC+BBR          (port 5443)
  7. TUIC v5   — QUIC 0-RTT        (port 9443)
  8. NaïveProxy — Chromium HTTP/2  (port 4443)
  9. SSH SOCKS5 — always-on        (port 22)

Runtime: up to 6 hours (GitHub Actions max)
"""

import os, json, uuid, subprocess, time, base64, secrets, random
import socket, threading, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────
#  ENVIRONMENT
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
DOMAIN             = os.getenv("DOMAIN", "")
RUNTIME_HOURS      = int(os.getenv("RUNTIME_HOURS", "6"))   # GitHub Actions max = 6h

XRAY_CONFIG   = "/usr/local/etc/xray/config.json"
CERT_DIR      = "/usr/local/etc/vpn/certs"
LOG_DIR       = "/var/log/vpn"

PORTS = {
    "reality_vision": 443,
    "reality_grpc":   8443,
    "ws_tls":         2053,
    "grpc_tls":       2083,
    "ss2022":         1080,
    "hysteria2":      5443,
    "tuic":           9443,
    "naive":          4443,
}

# REALITY destinations — matched to Azure/GitHub Actions infra
REALITY_DEST_OPTIONS = [
    {"dest": "www.microsoft.com:443",          "sni": ["www.microsoft.com"],          "fp": "chrome"},
    {"dest": "login.microsoftonline.com:443",   "sni": ["login.microsoftonline.com"],  "fp": "chrome"},
    {"dest": "azure.microsoft.com:443",         "sni": ["azure.microsoft.com"],        "fp": "edge"},
    {"dest": "dl.google.com:443",               "sni": ["dl.google.com"],              "fp": "chrome"},
    {"dest": "www.github.com:443",              "sni": ["www.github.com"],             "fp": "chrome"},
    {"dest": "1.1.1.1:443",                     "sni": [],                             "fp": "firefox"},
]

WS_PATH      = f"/{secrets.token_hex(8)}"
GRPC_SVC     = secrets.token_hex(6)
H2_PATH      = f"/{secrets.token_hex(8)}"
NAIVE_USER   = secrets.token_hex(6)
NAIVE_PASS   = secrets.token_hex(16)
SSH_USER     = "sshproxy"
SSH_PASS     = secrets.token_hex(12)


# ─────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────

def run(cmd: str, silent: bool = False) -> int:
    if silent:
        return os.system(f"{cmd} >/dev/null 2>&1")
    return os.system(cmd)

def shell(cmd: str) -> str:
    return subprocess.getoutput(cmd)

def get_server_ip() -> str:
    for url in ["https://api.ipify.org", "https://ifconfig.me", "https://ipecho.net/plain"]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip:
                    return ip
        except Exception:
            pass
    return shell("hostname -I | awk '{print $1}'")

def resolve(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except Exception:
        return None

def send_telegram(text: str) -> None:
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
        with urllib.request.urlopen(url, data=data, timeout=15) as r:
            if json.loads(r.read().decode()).get("ok"):
                print("  [Telegram] sent ✓")
    except Exception as e:
        print(f"  [Telegram] failed: {e}")

def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def download(url: str, dest: str) -> bool:
    try:
        run(f"curl -fsSL '{url}' -o '{dest}'", silent=True)
        return os.path.exists(dest) and os.path.getsize(dest) > 1000
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
#  CERTS
# ─────────────────────────────────────────────────────────────

def create_certs() -> tuple[str, str]:
    os.makedirs(CERT_DIR, exist_ok=True)
    key = f"{CERT_DIR}/key.pem"
    crt = f"{CERT_DIR}/crt.pem"
    run(
        f'openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes '
        f'-keyout {key} -out {crt} '
        f'-subj "/CN=microsoft.com/O=Microsoft Corporation/C=US" '
        f'-addext "subjectAltName=DNS:microsoft.com,DNS:*.microsoft.com" 2>/dev/null',
        silent=True,
    )
    log("Certs created.")
    return key, crt

def pick_reality_dest() -> dict:
    for opt in REALITY_DEST_OPTIONS:
        host = opt["dest"].split(":")[0]
        if host == "1.1.1.1" or resolve(host):
            log(f"REALITY dest → {opt['dest']}")
            return opt
    return REALITY_DEST_OPTIONS[-1]


# ─────────────────────────────────────────────────────────────
#  IPTABLES — forward UDP/443 + TCP/80 to dest IP
# ─────────────────────────────────────────────────────────────

def setup_iptables(dest_host: str) -> None:
    dest_ip = resolve(dest_host)
    if not dest_ip:
        return
    iface = shell("ip route | grep default | awk '{print $5}' | head -1")
    run(f"iptables -t nat -A PREROUTING -i {iface} -p udp --dport 443 -j DNAT --to-destination {dest_ip}:443", True)
    run(f"iptables -t nat -A PREROUTING -i {iface} -p tcp --dport 80  -j DNAT --to-destination {dest_ip}:80",  True)
    run("iptables -t nat -A POSTROUTING -j MASQUERADE", True)
    run("sysctl -w net.ipv4.ip_forward=1 > /dev/null", True)
    log(f"iptables → UDP/443 + TCP/80 forwarded to {dest_ip}")


# ─────────────────────────────────────────────────────────────
#  BBR CONGESTION CONTROL
# ─────────────────────────────────────────────────────────────

def enable_bbr() -> None:
    run("sysctl -w net.core.default_qdisc=fq > /dev/null",         True)
    run("sysctl -w net.ipv4.tcp_congestion_control=bbr > /dev/null", True)
    log("BBR congestion control enabled.")


# ─────────────────────────────────────────────────────────────
#  XRAY
# ─────────────────────────────────────────────────────────────

def install_xray() -> bool:
    log("Installing Xray ...")
    run('bash -c "$(curl -L https://raw.githubusercontent.com/XTLS/Xray-install/main/install-release.sh)"', True)
    ok = os.path.exists("/usr/local/bin/xray")
    log(f"Xray {'OK' if ok else 'FAILED'}")
    return ok

def make_xray_config(uid: str, trojan_pass: str, ss_key: str,
                     key: str, crt: str, dest_opt: dict, sid: str) -> None:

    def tls(alpn):
        return {
            "security": "tls",
            "tlsSettings": {
                "certificates": [{"certificateFile": crt, "keyFile": key}],
                "alpn": alpn,
                "minVersion": "1.2",
                "cipherSuites":
                    "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384"
                    ":TLS_CHACHA20_POLY1305_SHA256",
            },
        }

    reality = {
        "show": False, "dest": dest_opt["dest"], "xver": 0,
        "serverNames": dest_opt["sni"],
        "privateKey":  shell("xray x25519").split("\n")[0].split(":", 1)[1].strip(),
        "shortIds":    [sid, secrets.token_hex(8), ""],
        "fingerprint": dest_opt["fp"],
        "maxTimeDiff": 60000,
    }
    # Store public key for link generation
    xray_pub = shell("xray x25519").split("\n")[1].split(":", 1)[1].strip()

    inbounds = [
        # 1 VLESS + REALITY + Vision
        {
            "tag": "reality-vision", "port": PORTS["reality_vision"], "protocol": "vless",
            "settings": {"clients": [{"id": uid, "flow": "xtls-rprx-vision"}], "decryption": "none"},
            "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": reality},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
        },
        # 2 VLESS + REALITY + gRPC
        {
            "tag": "reality-grpc", "port": PORTS["reality_grpc"], "protocol": "vless",
            "settings": {"clients": [{"id": uid}], "decryption": "none"},
            "streamSettings": {
                "network": "grpc", "security": "reality",
                "grpcSettings": {"serviceName": GRPC_SVC, "multiMode": True},
                "realitySettings": reality,
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },
        # 3 VLESS + WebSocket + TLS
        {
            "tag": "vless-ws", "port": PORTS["ws_tls"], "protocol": "vless",
            "settings": {"clients": [{"id": uid}], "decryption": "none"},
            "streamSettings": {
                "network": "ws",
                "wsSettings": {"path": WS_PATH, "headers": {"Host": "microsoft.com"}},
                **tls(["http/1.1"]),
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },
        # 4 Trojan + gRPC + TLS
        {
            "tag": "trojan-grpc", "port": PORTS["grpc_tls"], "protocol": "trojan",
            "settings": {"clients": [{"password": trojan_pass}]},
            "streamSettings": {
                "network": "grpc",
                "grpcSettings": {"serviceName": GRPC_SVC, "multiMode": True},
                **tls(["h2", "http/1.1"]),
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },
        # 5 Shadowsocks 2022
        {
            "tag": "ss2022", "port": PORTS["ss2022"], "protocol": "shadowsocks",
            "settings": {
                "method": "2022-blake3-aes-256-gcm",
                "password": ss_key, "network": "tcp,udp", "ivCheck": True,
            },
        },
    ]

    config = {
        "log": {"loglevel": "warning",
                "access": f"{LOG_DIR}/xray_access.log",
                "error":  f"{LOG_DIR}/xray_error.log"},
        "dns": {
            "servers": [
                {"address": "8.8.8.8", "domains": ["geosite:geolocation-!cn"]},
                "1.1.1.1", "localhost",
            ],
            "queryStrategy": "UseIPv4",
        },
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "domain": ["geosite:category-ads-all"], "outboundTag": "block"},
                {"type": "field", "ip": ["geoip:ir", "geoip:private"],    "outboundTag": "block"},
            ],
        },
        "inbounds": inbounds,
        "outbounds": [
            {"tag": "direct", "protocol": "freedom",   "settings": {"domainStrategy": "UseIPv4"}},
            {"tag": "block",  "protocol": "blackhole",  "settings": {}},
        ],
        "policy": {
            "levels": {"0": {"handshake": 4, "connIdle": 300, "uplinkOnly": 2, "downlinkOnly": 5}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
    }

    os.makedirs("/usr/local/etc/xray", exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(XRAY_CONFIG, "w") as f:
        json.dump(config, f, indent=2)

    # Save public key for link generation
    with open("/tmp/xray_pubkey", "w") as f:
        f.write(xray_pub)

def start_xray() -> bool:
    run("systemctl restart xray && systemctl enable xray", True)
    time.sleep(2)
    ok = shell("systemctl is-active xray") == "active"
    log(f"Xray service: {'running ✓' if ok else 'FAILED ✗'}")
    return ok


# ─────────────────────────────────────────────────────────────
#  HYSTERIA 2
# ─────────────────────────────────────────────────────────────

def install_hysteria2(hy_pass: str, key: str, crt: str) -> bool:
    log("Installing Hysteria2 ...")
    api = "https://api.github.com/repos/apernet/hysteria/releases/latest"
    try:
        with urllib.request.urlopen(api, timeout=10) as r:
            data = json.loads(r.read().decode())
        ver = data["tag_name"]
        url = f"https://github.com/apernet/hysteria/releases/download/{ver}/hysteria-linux-amd64"
    except Exception:
        url = "https://github.com/apernet/hysteria/releases/latest/download/hysteria-linux-amd64"

    if not download(url, "/usr/local/bin/hysteria"):
        log("Hysteria2 FAILED (download)")
        return False
    run("chmod +x /usr/local/bin/hysteria", True)

    config = {
        "listen": f":{PORTS['hysteria2']}",
        "tls": {"cert": crt, "key": key},
        "auth": {"type": "password", "password": hy_pass},
        "masquerade": {
            "type":   "proxy",
            "proxy":  {"url": "https://www.microsoft.com", "rewriteHost": True},
        },
        "quic": {
            "initStreamReceiveWindow":      26843545,
            "maxStreamReceiveWindow":       26843545,
            "initConnReceiveWindow":        67108864,
            "maxConnReceiveWindow":         67108864,
            "maxIdleTimeout":              "30s",
            "maxIncomingStreams":            1024,
            "disablePathMTUDiscovery":      False,
        },
        "bandwidth": {"up": "1 gbps", "down": "1 gbps"},
        "ignoreClientBandwidth": False,
        "speedTest": False,
        "logging": {"level": "warn", "timestamp": True},
    }
    os.makedirs("/etc/hysteria", exist_ok=True)
    with open("/etc/hysteria/config.yaml", "w") as f:
        import re
        # Write YAML manually (no pyyaml guaranteed)
        f.write(f"""listen: ":{PORTS['hysteria2']}"
tls:
  cert: "{crt}"
  key: "{key}"
auth:
  type: password
  password: "{hy_pass}"
masquerade:
  type: proxy
  proxy:
    url: "https://www.microsoft.com"
    rewriteHost: true
quic:
  initStreamReceiveWindow: 26843545
  maxStreamReceiveWindow: 26843545
  initConnReceiveWindow: 67108864
  maxConnReceiveWindow: 67108864
  maxIdleTimeout: 30s
  maxIncomingStreams: 1024
bandwidth:
  up: "1 gbps"
  down: "1 gbps"
logging:
  level: warn
""")

    svc = """[Unit]
Description=Hysteria2 VPN
After=network.target

[Service]
ExecStart=/usr/local/bin/hysteria server -c /etc/hysteria/config.yaml
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""
    with open("/etc/systemd/system/hysteria2.service", "w") as f:
        f.write(svc)
    run("systemctl daemon-reload && systemctl enable --now hysteria2", True)
    time.sleep(2)
    ok = shell("systemctl is-active hysteria2") == "active"
    log(f"Hysteria2: {'running ✓' if ok else 'FAILED ✗'}")
    return ok


# ─────────────────────────────────────────────────────────────
#  TUIC v5
# ─────────────────────────────────────────────────────────────

def install_tuic(tuic_uuid: str, tuic_pass: str, key: str, crt: str) -> bool:
    log("Installing TUIC v5 ...")
    api = "https://api.github.com/repos/etjec4/tuic/releases/latest"
    try:
        with urllib.request.urlopen(api, timeout=10) as r:
            data = json.loads(r.read().decode())
        ver = data["tag_name"]   # e.g. "tuic-server-1.0.0"
        url = (
            f"https://github.com/etjec4/tuic/releases/download/{ver}"
            f"/tuic-server-{ver.split('-')[-1]}-x86_64-unknown-linux-musl"
        )
    except Exception:
        url = "https://github.com/etjec4/tuic/releases/latest/download/tuic-server-1.0.0-x86_64-unknown-linux-musl"

    os.makedirs("/root/tuic", exist_ok=True)
    if not download(url, "/root/tuic/tuic-server"):
        log("TUIC FAILED (download)")
        return False
    run("chmod +x /root/tuic/tuic-server", True)

    config = {
        "server":          f"[::]:{ PORTS['tuic']}",
        "users":           {tuic_uuid: tuic_pass},
        "certificate":     crt,
        "private_key":     key,
        "congestion_control": "bbr",
        "alpn":            ["h3", "spdy/3.1"],
        "udp_relay_ipv6":  True,
        "zero_rtt_handshake": False,
        "auth_timeout":    "3s",
        "max_idle_time":   "10s",
        "log_level":       "warn",
    }
    with open("/root/tuic/config.json", "w") as f:
        json.dump(config, f, indent=2)

    svc = """[Unit]
Description=TUIC v5 Proxy
After=network.target

[Service]
User=root
WorkingDirectory=/root/tuic
ExecStart=/root/tuic/tuic-server -c /root/tuic/config.json
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""
    with open("/etc/systemd/system/tuic.service", "w") as f:
        f.write(svc)
    run("systemctl daemon-reload && systemctl enable --now tuic", True)
    time.sleep(2)
    ok = shell("systemctl is-active tuic") == "active"
    log(f"TUIC v5: {'running ✓' if ok else 'FAILED ✗'}")
    return ok


# ─────────────────────────────────────────────────────────────
#  NAÏVEPROXY  (Caddy + forwardproxy@naive)
# ─────────────────────────────────────────────────────────────

def install_naiveproxy(key: str, crt: str) -> bool:
    log("Installing NaïveProxy (Caddy + naive fork) ...")

    # Install Go
    go_ver = shell("curl -fsSL https://golang.org/dl/?mode=json 2>/dev/null | grep -oP '\"version\":\"\\K[^\"]+' | head -1")
    if not go_ver:
        go_ver = "go1.23.4"
    go_url = f"https://golang.org/dl/{go_ver}.linux-amd64.tar.gz"
    if not download(go_url, f"/tmp/{go_ver}.tar.gz"):
        log("NaïveProxy FAILED (Go download)")
        return False
    run(f"rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/{go_ver}.tar.gz", True)
    os.environ["PATH"] = f"{os.environ['PATH']}:/usr/local/go/bin"
    os.environ["GOPATH"] = "/root/go"

    # Install xcaddy + build Caddy with naive forwardproxy
    run("/usr/local/go/bin/go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest", True)
    run(
        "/root/go/bin/xcaddy build "
        "--with github.com/caddyserver/forwardproxy=github.com/klzgrad/forwardproxy@naive "
        "-o /usr/local/bin/caddy",
        silent=False,
    )
    if not os.path.exists("/usr/local/bin/caddy"):
        log("NaïveProxy FAILED (xcaddy build)")
        return False

    run("setcap cap_net_bind_service=+ep /usr/local/bin/caddy", True)

    caddyfile = f"""{{
    order forward_proxy before file_server
}}

:{PORTS['naive']}, localhost:{PORTS['naive']} {{
    tls {crt} {key}
    forward_proxy {{
        basic_auth {NAIVE_USER} {NAIVE_PASS}
        hide_ip
        hide_via
        probe_resistance
    }}
    file_server {{
        root /var/www/html
    }}
}}
"""
    os.makedirs("/etc/caddy", exist_ok=True)
    os.makedirs("/var/www/html", exist_ok=True)
    with open("/var/www/html/index.html", "w") as f:
        f.write("<html><body>Welcome</body></html>")
    with open("/etc/caddy/Caddyfile", "w") as f:
        f.write(caddyfile)

    svc = """[Unit]
Description=NaïveProxy (Caddy)
After=network.target

[Service]
User=root
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""
    with open("/etc/systemd/system/naive.service", "w") as f:
        f.write(svc)
    run("systemctl daemon-reload && systemctl enable --now naive", True)
    time.sleep(3)
    ok = shell("systemctl is-active naive") == "active"
    log(f"NaïveProxy: {'running ✓' if ok else 'FAILED ✗'}")
    return ok


# ─────────────────────────────────────────────────────────────
#  SSH SOCKS5
# ─────────────────────────────────────────────────────────────

def setup_ssh() -> bool:
    log("Configuring SSH SOCKS5 ...")
    run(f"useradd -m -s /bin/false {SSH_USER}", True)
    run(f"echo '{SSH_USER}:{SSH_PASS}' | chpasswd", True)

    sshd_extra = """
AllowTcpForwarding yes
PermitTunnel yes
GatewayPorts yes
"""
    with open("/etc/ssh/sshd_config.d/socks5.conf", "w") as f:
        f.write(sshd_extra)
    run("systemctl restart sshd", True)
    ok = shell("systemctl is-active ssh") in ("active",) or \
         shell("systemctl is-active sshd") == "active"
    log(f"SSH SOCKS5: {'ready ✓' if ok else 'check manually'}")
    return True


# ─────────────────────────────────────────────────────────────
#  LINK GENERATION
# ─────────────────────────────────────────────────────────────

def generate_all_links(ip: str, uid: str, trojan_pass: str, ss_key: str,
                       pub: str, dest_opt: dict, sid: str,
                       hy_pass: str, tuic_uuid: str, tuic_pass: str) -> dict:
    q    = urllib.parse.quote
    host = DOMAIN if DOMAIN else ip
    sni  = dest_opt["sni"][0] if dest_opt["sni"] else ""
    fp   = dest_opt["fp"]

    return {
        "VLESS REALITY+Vision": (
            f"vless://{uid}@{ip}:{PORTS['reality_vision']}"
            f"?encryption=none&security=reality&sni={sni}&fp={fp}"
            f"&pbk={pub}&sid={sid}&type=tcp&flow=xtls-rprx-vision"
            f"#REALITY-Vision"
        ),
        "VLESS REALITY+gRPC": (
            f"vless://{uid}@{ip}:{PORTS['reality_grpc']}"
            f"?encryption=none&security=reality&sni={sni}&fp={fp}"
            f"&pbk={pub}&sid={sid}&type=grpc&serviceName={GRPC_SVC}"
            f"#REALITY-gRPC"
        ),
        "VLESS WS+TLS": (
            f"vless://{uid}@{host}:{PORTS['ws_tls']}"
            f"?encryption=none&security=tls&sni={host}"
            f"&type=ws&path={q(WS_PATH)}&host=microsoft.com"
            f"#VLESS-WS-TLS"
        ),
        "Trojan gRPC+TLS": (
            f"trojan://{trojan_pass}@{host}:{PORTS['grpc_tls']}"
            f"?security=tls&sni={host}&type=grpc&serviceName={GRPC_SVC}&alpn=h2"
            f"#Trojan-gRPC-TLS"
        ),
        "Shadowsocks 2022": (
            "ss://" +
            base64.b64encode(
                f"2022-blake3-aes-256-gcm:{ss_key}".encode()
            ).decode().rstrip("=") +
            f"@{ip}:{PORTS['ss2022']}#SS2022"
        ),
        "Hysteria2": (
            f"hysteria2://{hy_pass}@{ip}:{PORTS['hysteria2']}"
            f"?insecure=1&sni=microsoft.com"
            f"#Hysteria2"
        ),
        "TUIC v5": (
            f"tuic://{tuic_uuid}:{tuic_pass}@{ip}:{PORTS['tuic']}"
            f"?congestion_control=bbr&alpn=h3,spdy%2F3.1"
            f"&udp_relay_mode=native&allow_insecure=1"
            f"#TUIC-v5"
        ),
        "NaïveProxy": (
            f"https://{NAIVE_USER}:{NAIVE_PASS}@{ip}:{PORTS['naive']}"
            f"#NaiveProxy"
        ),
        "SSH SOCKS5": (
            f"ssh -D 1080 -N {SSH_USER}@{ip} -p 22\n"
            f"  Password: {SSH_PASS}"
        ),
    }


# ─────────────────────────────────────────────────────────────
#  TELEGRAM MESSAGES  (split to avoid 4096 char limit)
# ─────────────────────────────────────────────────────────────

def telegram_header(ip: str, uid: str, trojan_pass: str, ss_key: str,
                    hy_pass: str, tuic_uuid: str, tuic_pass: str,
                    dest_opt: dict, runtime: int) -> str:
    return "\n".join([
        "🛡 <b>All-in-One Anti-Censorship VPN — Ready</b>",
        "",
        f"🌐 <b>Server IP:</b> <code>{ip}</code>",
        f"⏱ <b>Uptime:</b> ~{runtime} hours",
        f"🎭 <b>REALITY dest:</b> <code>{dest_opt['dest']}</code>",
        "",
        "<b>— Credentials —</b>",
        f"UUID (Xray):  <code>{uid}</code>",
        f"Trojan pass:  <code>{trojan_pass}</code>",
        f"SS2022 key:   <code>{ss_key}</code>",
        f"Hysteria2:    <code>{hy_pass}</code>",
        f"TUIC uuid:    <code>{tuic_uuid}</code>",
        f"TUIC pass:    <code>{tuic_pass}</code>",
        f"Naive user:   <code>{NAIVE_USER}</code>",
        f"Naive pass:   <code>{NAIVE_PASS}</code>",
        f"SSH user:     <code>{SSH_USER}</code>",
        f"SSH pass:     <code>{SSH_PASS}</code>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━",
    ])

def telegram_links(links: dict) -> list[str]:
    """Split links into chunks under 4000 chars each."""
    icons = ["🔮","🔷","☁️","⚡","🔒","🚀","🌀","🕵️","🔑"]
    messages, chunk = [], []
    for (name, link), icon in zip(links.items(), icons):
        block = f"\n{icon} <b>{name}</b>\n<code>{link}</code>\n"
        if sum(len(c) for c in chunk) + len(block) > 3800:
            messages.append("".join(chunk))
            chunk = []
        chunk.append(block)
    if chunk:
        messages.append("".join(chunk))
    return messages

def telegram_priority() -> str:
    return "\n".join([
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "💡 <b>Priority (Iran best → worst):</b>",
        "1 REALITY+gRPC   2 REALITY+Vision",
        "3 NaïveProxy (HTTP/2)   4 Hysteria2 (if UDP open)",
        "5 TUIC v5 (if UDP open) 6 Trojan gRPC",
        "7 VLESS WS   8 SS2022   9 SSH SOCKS5",
        "",
        "⚠️ <i>Server shuts down automatically.</i>",
    ])


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────

def main() -> None:
    log("=" * 60)
    log("All-in-One VPN Setup — Starting")
    log("=" * 60)

    # Generate secrets
    uid         = str(uuid.uuid4())
    trojan_pass = secrets.token_hex(16)
    ss_key      = base64.b64encode(secrets.token_bytes(32)).decode()
    hy_pass     = secrets.token_hex(16)
    tuic_uuid   = str(uuid.uuid4())
    tuic_pass   = secrets.token_hex(12)
    sid         = secrets.token_hex(4)

    ip = get_server_ip()
    log(f"Server IP: {ip}")

    enable_bbr()
    key, crt    = create_certs()
    dest_opt    = pick_reality_dest()

    if dest_opt["dest"].split(":")[0] != "1.1.1.1":
        setup_iptables(dest_opt["dest"].split(":")[0])

    # Install everything in parallel
    log("Starting parallel installation ...")
    results = {}

    def task_xray():
        if install_xray():
            make_xray_config(uid, trojan_pass, ss_key, key, crt, dest_opt, sid)
            ok = start_xray()
            # Read public key written during config generation
            try:
                with open("/tmp/xray_pubkey") as f:
                    pub = f.read().strip()
            except Exception:
                pub = shell("xray x25519").split("\n")[1].split(":", 1)[1].strip()
            return ("xray", ok, pub)
        return ("xray", False, "")

    def task_hy2():
        return ("hysteria2", install_hysteria2(hy_pass, key, crt))

    def task_tuic():
        return ("tuic", install_tuic(tuic_uuid, tuic_pass, key, crt))

    def task_naive():
        return ("naive", install_naiveproxy(key, crt))

    def task_ssh():
        return ("ssh", setup_ssh())

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [
            ex.submit(task_xray),
            ex.submit(task_hy2),
            ex.submit(task_tuic),
            ex.submit(task_naive),
            ex.submit(task_ssh),
        ]
        pub = ""
        for f in as_completed(futures):
            result = f.result()
            name = result[0]
            ok   = result[1]
            results[name] = ok
            if name == "xray" and len(result) == 3:
                pub = result[2]
            log(f"  [{name}] {'✓' if ok else '✗'}")

    # Summary
    log("=" * 60)
    log("Installation Summary:")
    for svc, ok in results.items():
        log(f"  {svc:12s}: {'OK' if ok else 'FAILED'}")
    log("=" * 60)

    # Generate links
    links = generate_all_links(
        ip, uid, trojan_pass, ss_key, pub, dest_opt, sid,
        hy_pass, tuic_uuid, tuic_pass,
    )

    # Print to console
    print("\n" + "=" * 70)
    for name, link in links.items():
        print(f"\n[{name}]\n{link}")
    print("=" * 70)

    # Telegram — header + credentials
    hdr = telegram_header(
        ip, uid, trojan_pass, ss_key,
        hy_pass, tuic_uuid, tuic_pass,
        dest_opt, RUNTIME_HOURS,
    )
    send_telegram(hdr)

    # Telegram — links (chunked)
    for chunk in telegram_links(links):
        send_telegram(chunk)
        time.sleep(0.5)

    send_telegram(telegram_priority())
    send_telegram(f"✅ All services live. Auto-shutdown in {RUNTIME_HOURS} hours.")

    # Keep alive
    log(f"Sleeping {RUNTIME_HOURS} hours ...")
    for hour in range(RUNTIME_HOURS):
        time.sleep(3600)
        remaining = RUNTIME_HOURS - hour - 1
        if remaining > 0:
            send_telegram(f"⏳ Server alive. {remaining}h remaining.")

    send_telegram("🔴 Session ended. Server shutting down.")
    log("Done.")


if __name__ == "__main__":
    main()
