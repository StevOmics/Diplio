"""Validation and atomic on-disk swap for the nginx reverse proxy's TLS cert
pair. Bytes live under /certs (bind-mounted from ./certs, same directory
nginx mounts read-only at /etc/nginx/certs) - never in Postgres, since nginx
can only read files and there's no worker-side consumer that needs DB access
to this secret (see CLAUDE.md's TlsConfig note)."""

import os
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

CERTS_DIR = "/certs"
ACTIVE_CERT = os.path.join(CERTS_DIR, "server.crt")
ACTIVE_KEY = os.path.join(CERTS_DIR, "server.key")
SELF_SIGNED_CERT = os.path.join(CERTS_DIR, "self-signed.crt")
SELF_SIGNED_KEY = os.path.join(CERTS_DIR, "self-signed.key")

# Shared with the nginx container via the ./nginx/generated bind mount (rw
# here, ro there) - see docker-compose.yml. nginx's static nginx.conf
# `include`s this directory, so we only ever have to rewrite this one file,
# never the static config nginx ships with.
GENERATED_CONF_DIR = "/nginx-generated"
DOMAINS_CONF_PATH = os.path.join(GENERATED_CONF_DIR, "domains.conf")


def validate_cert_key_pair(cert_pem: bytes, key_pem: bytes) -> None:
    """Raises ValueError with a user-facing message if the cert/key don't
    form a usable, current pair."""
    try:
        cert = x509.load_pem_x509_certificate(cert_pem)
    except ValueError as exc:
        raise ValueError("That doesn't look like a valid PEM certificate") from exc

    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except ValueError as exc:
        raise ValueError("That doesn't look like a valid, unencrypted PEM private key") from exc

    if not isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
        raise ValueError("Unsupported private key type - use an RSA or EC key")

    if key.public_key().public_numbers() != cert.public_key().public_numbers():
        raise ValueError("The certificate and private key don't match")

    now = datetime.now(timezone.utc)
    if cert.not_valid_after_utc < now:
        raise ValueError(f"That certificate expired on {cert.not_valid_after_utc:%Y-%m-%d}")
    if cert.not_valid_before_utc > now:
        raise ValueError(f"That certificate isn't valid until {cert.not_valid_before_utc:%Y-%m-%d}")


def _atomic_replace(src_bytes: bytes, dest_path: str) -> None:
    tmp_path = f"{dest_path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(src_bytes)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, dest_path)


def write_active_cert(cert_pem: bytes, key_pem: bytes) -> None:
    """Atomically swaps the cert/key nginx loads, so it never reads a
    half-written file."""
    _atomic_replace(cert_pem, ACTIVE_CERT)
    _atomic_replace(key_pem, ACTIVE_KEY)


def parse_domains(domains_text: str) -> list[str]:
    """Parses the Domains textarea: one hostname/IP per non-blank line.
    nginx's container-internal ports are fixed at 80/443 (docker-compose
    maps whatever host ports an operator wants onto those) - a per-domain
    port isn't something this app can act on, so it isn't accepted here."""
    return [line.strip() for line in (domains_text or "").splitlines() if line.strip()]


_SERVER_BLOCK = """\
server {{
    listen 443 ssl;
    server_name {server_name};

    ssl_certificate     /etc/nginx/certs/server.crt;
    ssl_certificate_key /etc/nginx/certs/server.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5:!3DES;
    ssl_prefer_server_ciphers on;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1h;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location ~ /\\. {{
        deny all;
        return 404;
    }}

    location /login {{
        limit_req zone=login burst=5 nodelay;
        proxy_pass http://web:8000;
        include /etc/nginx/proxy_params.conf;
    }}

    location / {{
        proxy_pass http://web:8000;
        include /etc/nginx/proxy_params.conf;
    }}
}}
"""

_REDIRECT_BLOCK = """\
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}
"""


def render_nginx_config(domains_text: str, redirect_http: bool) -> str:
    """Builds the nginx server-block config for the current domain list and
    redirect setting. All domains share the one cert pair at ACTIVE_CERT/KEY
    (see module docstring) and the one fixed listen 443 (plain HTTP redirect
    on 80, if enabled) - the domain list only affects server_name matching.
    Whatever host port(s) actually reach these is entirely a docker-compose
    port-mapping / external-proxy concern, out of this app's scope."""
    hosts = parse_domains(domains_text) or ["_"]
    blocks = [_REDIRECT_BLOCK] if redirect_http else []
    blocks.append(_SERVER_BLOCK.format(server_name=" ".join(hosts)))
    return "\n".join(blocks)


def write_nginx_config(domains_text: str, redirect_http: bool) -> None:
    """Atomically rewrites the generated nginx config nginx `include`s. Does
    not itself restart nginx - callers trigger that via terminator so a
    cert/domain change and the restart stay one user-visible action."""
    content = render_nginx_config(domains_text, redirect_http)
    os.makedirs(GENERATED_CONF_DIR, exist_ok=True)
    tmp_path = f"{DOMAINS_CONF_PATH}.tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, DOMAINS_CONF_PATH)


def revert_to_self_signed() -> None:
    if not (os.path.isfile(SELF_SIGNED_CERT) and os.path.isfile(SELF_SIGNED_KEY)):
        raise ValueError("No self-signed backup found under ./certs - run ./setup.sh --configure-tls")
    with open(SELF_SIGNED_CERT, "rb") as f:
        cert_pem = f.read()
    with open(SELF_SIGNED_KEY, "rb") as f:
        key_pem = f.read()
    write_active_cert(cert_pem, key_pem)
