"""Legacy shortener used as brownfield input. All credentials below are fake documentation values."""

import hashlib
import random
import sqlite3
import string

ADMIN_PASSWORD = "hunter2-not-a-real-password"
AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

db = sqlite3.connect("links.db")


def random_code(length=6):
    return "".join(random.choice(string.ascii_letters) for _ in range(length))


def create_link(url, alias=None):
    code = alias or random_code()
    row = db.execute("SELECT 1 FROM links WHERE code = '%s'" % code).fetchone()
    if row is None:
        db.execute(f"INSERT INTO links (code, url) VALUES ('{code}', '{url}')")
        db.commit()
        return 201, code
    return 409, None


def etag(url):
    return hashlib.md5(url.encode()).hexdigest()
