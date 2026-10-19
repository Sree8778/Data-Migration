"""Create a throw-away self-signed TLS certificate for the encrypted test servers (SQL Server / PostgreSQL / MySQL that REQUIRE TLS, like Azure).

    python scripts/make_test_cert.py          # writes test_certs/server.crt, server.key, mssql.conf

The certificate is valid for `localhost` and 127.0.0.1 only and exists solely so tests can prove the connectors encrypt, verify the server
certificate against a CA file, and fail clearly when encryption is missing or the certificate is not trusted. Never use it for anything else.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

OUT = Path(__file__).resolve().parent.parent / "test_certs"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost"), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "erp-migration test only")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    (OUT / "server.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (OUT / "server.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    hba = ["local all all trust", "hostssl all all 0.0.0.0/0 scram-sha-256", "hostssl all all ::/0 scram-sha-256", "host all all 0.0.0.0/0 reject", "host all all ::/0 reject"]
    (OUT / "pg_hba.conf").write_text("\n".join(hba) + "\n", encoding="utf-8")   # TLS or nothing
    (OUT / "mssql.conf").write_text("[network]\ntlscert = /certs/server.crt\ntlskey = /certs/server.key\ntlsprotocols = 1.2\nforceencryption = 1\n", encoding="utf-8")
    print("wrote", ", ".join(sorted(p.name for p in OUT.iterdir())), "in", OUT)


if __name__ == "__main__":
    main()
