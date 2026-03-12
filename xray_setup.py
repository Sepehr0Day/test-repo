#!/usr/bin/env python3
"""
Xray VLESS VPN Setup Script
Installs and configures Xray with multiple protocols and sends results to Telegram.
"""

import os
import json
import uuid
import subprocess
import time
import sys
import urllib.request
import urllib.parse

# ─────────────────────────────────────────
#  CONFIG  (set via environment variables)
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

CONFIG_PATH   = "/usr/local/etc/xray/config.json"
WS_PATH       = "/vless"
GRPC_SERVICE  = "vless-grpc"

PORTS = {
    "tcp":     443,
    "ws":      8080,
    "grpc":    3000,
    "h2":      8443,
    "reality": 9443,
}

# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────

def run(cmd: str) -> None:
    os.system(cmd)


def shell(cmd: str) -> str:
    return subprocess.getoutput(cmd)


def get_server_ip() -> str:
    services = [
        "https://api.ipify.org",
        "https://ifconfig.me",
        "https://ipecho.net/plain",
    ]
    for url in services:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip:
                    return ip
        except Exception:
            continue
    return shell("hostname -I | awk '{print $1}'")


def send_telegram(text: str) -> bool:
    """Send a message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Token or Chat ID not set — skipping.")
        return False

    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }).encode()

    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as r:
            result = json.loads(r.read().decode())
            if result.get("ok"):
                print("[Telegram] Message sent ✓")
                return True
    except Exception as e:
        print(f"[Telegram] Failed: {e}")
    return False

# ─────────────────────────────────────────
#  INSTALLATION
# ─────────────────────────────────────────

def install_xray() -> None:
    print("[*] Installing Xray …")
    run('bash -c "$(curl -L https://raw.githubusercontent.com/XTLS/Xray-install/main/install-release.sh)"')
    print("[✓] Xray installed.")


def generate_reality_keys() -> tuple[str, str]:
    print("[*] Generating REALITY keys …")
    output = shell("xray x25519")
    lines  = output.strip().split("\n")
    private = lines[0].split(":", 1)[1].strip()
    public  = lines[1].split(":", 1)[1].strip()
    print("[✓] Keys generated.")
    return private, public

# ─────────────────────────────────────────
#  CONFIG GENERATION
# ─────────────────────────────────────────

def build_inbound(port: int, network: str, stream_extra: dict, client_id: str) -> dict:
    return {
        "port":     port,
        "protocol": "vless",
        "settings": {
            "clients":    [{"id": client_id}],
            "decryption": "none",
        },
        "streamSettings": {"network": network, **stream_extra},
    }


def create_config(private_key: str, client_id: str) -> None:
    print("[*] Writing Xray config …")

    inbounds = [
        # TCP
        build_inbound(PORTS["tcp"], "tcp", {}, client_id),

        # WebSocket
        build_inbound(PORTS["ws"], "ws",
                      {"wsSettings": {"path": WS_PATH}}, client_id),

        # gRPC
        build_inbound(PORTS["grpc"], "grpc",
                      {"grpcSettings": {"serviceName": GRPC_SERVICE}}, client_id),

        # REALITY
        build_inbound(PORTS["reality"], "tcp", {
            "security": "reality",
            "realitySettings": {
                "show":        False,
                "dest":        "www.cloudflare.com:443",
                "xver":        0,
                "serverNames": ["www.cloudflare.com"],
                "privateKey":  private_key,
                "shortIds":    [""],
            },
        }, client_id),
    ]

    config = {
        "log":       {"loglevel": "warning"},
        "inbounds":  inbounds,
        "outbounds": [{"protocol": "freedom"}],
    }

    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    print("[✓] Config written.")

# ─────────────────────────────────────────
#  SERVICE
# ─────────────────────────────────────────

def start_xray() -> None:
    print("[*] Starting Xray service …")
    run("systemctl restart xray")
    run("systemctl enable xray")
    time.sleep(2)
    status = shell("systemctl is-active xray")
    if status == "active":
        print("[✓] Xray is running.")
    else:
        print("[!] Xray may not be running. Check: systemctl status xray")

# ─────────────────────────────────────────
#  LINK GENERATION
# ─────────────────────────────────────────

def generate_links(server_ip: str, client_id: str, public_key: str) -> dict[str, str]:
    base = f"{client_id}@{server_ip}"

    return {
        "TCP":     (f"vless://{base}:{PORTS['tcp']}"
                    f"?encryption=none&type=tcp#VLESS-TCP"),

        "WS":      (f"vless://{base}:{PORTS['ws']}"
                    f"?encryption=none&type=ws&path=%2Fvless#VLESS-WS"),

        "gRPC":    (f"vless://{base}:{PORTS['grpc']}"
                    f"?encryption=none&type=grpc&serviceName={GRPC_SERVICE}#VLESS-GRPC"),

        "REALITY": (f"vless://{base}:{PORTS['reality']}"
                    f"?encryption=none&security=reality"
                    f"&sni=www.cloudflare.com&fp=chrome&pbk={public_key}"
                    f"&type=tcp#VLESS-REALITY"),
    }

# ─────────────────────────────────────────
#  REPORTING
# ─────────────────────────────────────────

def print_links(links: dict, server_ip: str, client_id: str) -> None:
    print("\n" + "=" * 50)
    print("  VLESS CONNECTION LINKS")
    print("=" * 50)
    print(f"  Server IP : {server_ip}")
    print(f"  UUID      : {client_id}")
    print("=" * 50)
    for name, link in links.items():
        print(f"\n[{name}]\n{link}\n")


def build_telegram_message(links: dict, server_ip: str, client_id: str) -> str:
    lines = [
        "🚀 <b>Xray VPN Server is Ready</b>",
        "",
        f"🌐 <b>IP:</b> <code>{server_ip}</code>",
        f"🔑 <b>UUID:</b> <code>{client_id}</code>",
        f"⏱ <b>Expires in:</b> ~2 hours",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for name, link in links.items():
        lines += [f"\n📡 <b>{name}</b>", f"<code>{link}</code>"]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "⚠️ <i>Server will shut down after 2 hours.</i>",
    ]
    return "\n".join(lines)

# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────

def main() -> None:
    client_id = str(uuid.uuid4())
    server_ip = get_server_ip()

    print(f"[*] Server IP : {server_ip}")
    print(f"[*] UUID      : {client_id}")

    install_xray()

    private_key, public_key = generate_reality_keys()

    create_config(private_key, client_id)

    start_xray()

    links = generate_links(server_ip, client_id, public_key)

    print_links(links, server_ip, client_id)

    # Send to Telegram
    message = build_telegram_message(links, server_ip, client_id)
    send_telegram(message)

    print("\n[*] Keeping server alive for 2 hours …")
    send_telegram("⏳ Server is alive. Will auto-shutdown in 2 hours.")

    time.sleep(7200)   # 2 hours

    print("[*] Time is up. Shutting down.")
    send_telegram("🔴 2-hour session ended. Server is shutting down.")


if __name__ == "__main__":
    main()
