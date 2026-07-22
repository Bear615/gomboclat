"""One-time, owner-only deployment of the web hub behind NGINX and HTTPS."""

from __future__ import annotations

import base64
import getpass
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DOMAIN = "dcgsl.duckdns.org"
SERVICE = "gomboclat-web"


@dataclass(frozen=True)
class ProvisionResult:
    url: str
    username: str
    password: str


def _run(command: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        command, input=input_text, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=600, check=False,
    )
    if result.returncode:
        tail = result.stdout.strip()[-2000:]
        raise RuntimeError(f"{' '.join(command)} failed ({result.returncode}):\n{tail}")
    return result.stdout


def _root(command: list[str], *, input_text: str | None = None) -> str:
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return _run(prefix + command, input_text=input_text)


def _install_file(content: str, destination: str, mode: str = "0644") -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        handle.write(content)
        temporary = handle.name
    try:
        _root(["install", "-m", mode, temporary, destination])
    finally:
        Path(temporary).unlink(missing_ok=True)


def provision_web() -> ProvisionResult:
    """Install the web service, NGINX proxy, Basic Auth, and Let's Encrypt TLS.

    This intentionally requires root or passwordless sudo: a Discord message must
    never be able to trigger an interactive privilege prompt.
    """
    if os.geteuid() != 0 and shutil.which("sudo") is None:
        raise RuntimeError("sudo is not installed; run the bot as root or install sudo first")
    repo = Path(__file__).resolve().parent.parent
    python = Path(sys.executable).resolve()
    user = getpass.getuser()
    password = secrets.token_urlsafe(24)
    digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()

    _root(["apt-get", "update"])
    _root(["apt-get", "install", "-y", "nginx", "certbot", "python3-certbot-nginx"])

    service = f"""[Unit]
Description=Gomboclat web control hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={repo}
ExecStart={python} {repo / 'run.py'} --web
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    nginx = f"""server {{
    listen 80;
    listen [::]:80;
    server_name {DOMAIN};

    auth_basic \"Gomboclat Control Hub\";
    auth_basic_user_file /etc/nginx/.gomboclat-htpasswd;

    location / {{
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
    }}

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
}}
"""
    _install_file(service, f"/etc/systemd/system/{SERVICE}.service")
    _install_file(f"admin:{{SHA}}{digest}\n", "/etc/nginx/.gomboclat-htpasswd", "0640")
    _root(["chown", "root:www-data", "/etc/nginx/.gomboclat-htpasswd"])
    _install_file(nginx, f"/etc/nginx/sites-available/{SERVICE}")
    _root(["ln", "-sfn", f"/etc/nginx/sites-available/{SERVICE}", f"/etc/nginx/sites-enabled/{SERVICE}"])
    _root(["rm", "-f", "/etc/nginx/sites-enabled/default"])
    _root(["systemctl", "daemon-reload"])
    _root(["systemctl", "enable", "--now", SERVICE])
    _root(["nginx", "-t"])
    _root(["systemctl", "reload", "nginx"])
    _root([
        "certbot", "--nginx", "--non-interactive", "--agree-tos",
        "--register-unsafely-without-email", "--redirect", "-d", DOMAIN,
    ])
    return ProvisionResult(f"https://{DOMAIN}", "admin", password)
