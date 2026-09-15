"""Generate a self-signed TLS cert for wx-emu (the watsonx SDK insists on https)."""
import datetime, ipaddress, os, sys
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tls"
os.makedirs(out, exist_ok=True)
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wx-emu")])
sans = [x509.DNSName("wx-emu"), x509.DNSName("wx-emu.load-testing"), x509.DNSName("wx-emu.load-testing.svc"), x509.DNSName("localhost")]
for ip in os.environ.get("CERT_IPS", "192.168.178.216,127.0.0.1").split(","):
    ip = ip.strip()
    if ip: sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
now = datetime.datetime.now(datetime.timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650)).add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
open(f"{out}/tls.key", "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
open(f"{out}/tls.crt", "wb").write(cert.public_bytes(serialization.Encoding.PEM))
print("cert written to", out, "SANs:", [str(s.value) for s in sans])
