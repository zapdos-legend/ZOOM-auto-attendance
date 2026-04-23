from py_vapid import Vapid01
from cryptography.hazmat.primitives import serialization
import base64

vapid = Vapid01()
vapid.generate_keys()

public_key_obj = serialization.load_pem_public_key(vapid.public_pem())
public_der = public_key_obj.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint
)

public_b64 = base64.urlsafe_b64encode(public_der).rstrip(b"=").decode("utf-8")
private_pem = vapid.private_pem().decode("utf-8")

print("VAPID_PUBLIC_KEY_BASE64URL:")
print(public_b64)

print("\nVAPID_PRIVATE_KEY_PEM:")
print(private_pem)