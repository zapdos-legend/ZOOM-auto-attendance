from py_vapid import Vapid01

vapid = Vapid01()
vapid.generate_keys()

print("PUBLIC KEY:")
print(vapid.public_pem().decode())

print("\nPRIVATE KEY:")
print(vapid.private_pem().decode())