from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import datetime
import os

def generate_self_signed_cert():
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Shanghai"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Shanghai"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fund Management"),
        x509.NameAttribute(NameOID.COMMON_NAME, "frp-oil.com"),
    ])

    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.datetime.utcnow()
    ).not_valid_after(
        datetime.datetime.utcnow() + datetime.timedelta(days=3650)
    ).add_extension(
        x509.SubjectAlternativeName([x509.DNSName("frp-oil.com"), x509.DNSName("localhost")]),
        critical=False,
    ).sign(key, hashes.SHA256(), default_backend())

    cert_dir = "d:/软件/Obsidian/创新/AI工具/基金管理工具/web-app/backend"
    
    cert_path = os.path.join(cert_dir, "cert.pem")
    key_path = os.path.join(cert_dir, "key.pem")

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))

    print(f"证书已生成: {cert_path}")
    print(f"私钥已生成: {key_path}")

if __name__ == "__main__":
    generate_self_signed_cert()