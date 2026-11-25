"""Create the demo users (idempotent). Password comes from DEMO_PASSWORD in .env."""
from api.security import hash_password
from common.config import settings
from common.db import pool

USERS = [("analyst1", "analyst"), ("analyst2", "analyst"), ("mlro1", "mlro"), ("mlro2", "mlro"),
         ("auditor1", "auditor")]


def main() -> None:
    pw = settings().demo_password
    if len(pw) < 8:
        raise SystemExit("DEMO_PASSWORD missing or too short — run scripts/make_env.sh")
    with pool().connection() as conn:
        for username, role in USERS:
            conn.execute("INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s) "
                         "ON CONFLICT (username) DO NOTHING", (username, hash_password(pw), role))
    print(f"seeded users: {', '.join(u for u, _ in USERS)}")


if __name__ == "__main__":
    main()
