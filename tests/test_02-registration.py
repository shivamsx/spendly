"""
Tests for Step 02 — Registration.

These tests are written against .claude/specs/02-registration.md, not against
the implementation. They exercise GET/POST /register end-to-end through the
Flask test client and verify DB side effects directly against the `users`
table via database/db.get_db().

NOTE: Spendly's get_db() always connects to the real spendly.db file (no
test-DB switching exists yet), so these tests run against the real database.
Every test uses a unique, randomly generated email address and cleans up any
row it creates so the suite is repeatable and never pollutes seeded/demo data.
"""

import uuid

import pytest
from flask import url_for
from werkzeug.security import check_password_hash

from app import app as flask_app
from database.db import create_user, get_db, init_db


# --------------------------------------------------------------------- #
# Fixtures                                                              #
# --------------------------------------------------------------------- #

@pytest.fixture
def app():
    flask_app.config.update({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
    })
    with flask_app.app_context():
        init_db()
        yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# --------------------------------------------------------------------- #
# DB / payload helpers                                                  #
# --------------------------------------------------------------------- #

def _unique_email(prefix="regtest"):
    return f"{prefix}-{uuid.uuid4().hex}@example.com"


def _valid_payload(email=None):
    return {
        "name": "Test User",
        "email": email or _unique_email(),
        "password": "SuperSecret123!",
        "confirm_password": "SuperSecret123!",
    }


def _get_user_by_email(email):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT id, name, email, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    finally:
        conn.close()


def _count_users_by_email(email):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        return row["c"]
    finally:
        conn.close()


def _count_all_users():
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    finally:
        conn.close()


def _delete_user_by_email(email):
    conn = get_db()
    try:
        conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# GET /register                                                         #
# --------------------------------------------------------------------- #

def test_get_register_renders_form(client):
    resp = client.get("/register")

    assert resp.status_code == 200, "GET /register should render the form without errors"
    assert b'name="name"' in resp.data, "form should have an input named 'name'"
    assert b'name="email"' in resp.data, "form should have an input named 'email'"
    assert b'name="password"' in resp.data, "form should have an input named 'password'"
    assert b'name="confirm_password"' in resp.data, (
        "form should have an input named 'confirm_password'"
    )


# --------------------------------------------------------------------- #
# Happy path                                                            #
# --------------------------------------------------------------------- #

def test_post_register_valid_data_creates_user_and_redirects_to_login(client):
    email = _unique_email("happy")
    payload = _valid_payload(email)

    try:
        with flask_app.test_request_context():
            expected_login_url = url_for("login")

        resp = client.post("/register", data=payload)

        assert resp.status_code == 302, "a valid registration submission should redirect (302)"
        location = resp.headers.get("Location", "")
        assert location.endswith(expected_login_url), (
            f"expected redirect to end with {expected_login_url!r}, got {location!r}"
        )

        user = _get_user_by_email(email)
        assert user is not None, "a new row should be created in the users table on success"
        assert user["name"] == payload["name"], "stored name should match the submitted name"
        assert user["email"] == email, "stored email should match the submitted email"
    finally:
        _delete_user_by_email(email)


def test_post_register_stores_hashed_password_not_plaintext(client):
    email = _unique_email("hash")
    plaintext_password = "PlainTextPass789!"
    payload = _valid_payload(email)
    payload["password"] = plaintext_password
    payload["confirm_password"] = plaintext_password

    try:
        resp = client.post("/register", data=payload)
        assert resp.status_code == 302, "valid registration should still redirect"

        user = _get_user_by_email(email)
        assert user is not None, "user row should exist after successful registration"
        stored_hash = user["password_hash"]

        assert stored_hash != plaintext_password, "password must never be stored as plaintext"
        assert len(stored_hash) > 20, "stored value should look like a hash, not a short raw string"
        assert (":" in stored_hash) or ("$" in stored_hash), (
            "stored value should look like a werkzeug password hash "
            "(contains a method/salt separator such as ':' or '$')"
        )
        assert check_password_hash(stored_hash, plaintext_password) is True, (
            "the stored hash must successfully verify against the original plaintext password"
        )
        assert check_password_hash(stored_hash, "SomeCompletelyWrongPassword000") is False, (
            "the stored hash must not verify against an incorrect password"
        )
    finally:
        _delete_user_by_email(email)


# --------------------------------------------------------------------- #
# Validation: empty / missing fields                                    #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("blank_field", ["name", "email", "password", "confirm_password"])
def test_post_register_missing_field_rerenders_without_creating_user(client, blank_field):
    email = _unique_email("blank")
    payload = _valid_payload(email)
    payload[blank_field] = ""

    before_count = _count_all_users()

    try:
        resp = client.post("/register", data=payload)

        assert resp.status_code == 200, (
            f"missing '{blank_field}' should re-render the form (200), not redirect"
        )

        after_count = _count_all_users()
        assert after_count == before_count, (
            f"no user row should be created when '{blank_field}' is left blank"
        )
    finally:
        # Only meaningful cleanup target is when email itself was populated;
        # when blank_field == "email" no row could have been keyed to `email`.
        if blank_field != "email":
            _delete_user_by_email(email)


# --------------------------------------------------------------------- #
# Validation: mismatched passwords                                      #
# --------------------------------------------------------------------- #

def test_post_register_mismatched_passwords_rerenders_without_creating_user(client):
    email = _unique_email("mismatch")
    payload = _valid_payload(email)
    payload["password"] = "FirstPassword123!"
    payload["confirm_password"] = "DifferentPassword456!"

    try:
        resp = client.post("/register", data=payload)

        assert resp.status_code == 200, (
            "mismatched password/confirm_password should re-render the form (200), not redirect"
        )
        assert b"match" in resp.data.lower(), (
            "expected a flashed error message indicating the passwords do not match"
        )
        assert _count_users_by_email(email) == 0, (
            "no user row should be created when passwords do not match"
        )
    finally:
        _delete_user_by_email(email)


# --------------------------------------------------------------------- #
# Validation: duplicate email                                           #
# --------------------------------------------------------------------- #

def test_post_register_duplicate_email_rerenders_with_error_and_no_duplicate_row(client):
    email = _unique_email("dup")
    create_user("Existing User", email, "OriginalPass123!")

    try:
        payload = {
            "name": "New User",
            "email": email,
            "password": "NewPass456!",
            "confirm_password": "NewPass456!",
        }

        resp = client.post("/register", data=payload)

        assert resp.status_code == 200, (
            "registering with an already-used email should re-render the form (200), not redirect"
        )
        assert b"already" in resp.data.lower(), (
            "expected an 'already registered'-style error message for a duplicate email"
        )
        assert _count_users_by_email(email) == 1, (
            "a duplicate email submission must not create a second row for that email"
        )
    finally:
        _delete_user_by_email(email)


def test_post_register_repeated_valid_submissions_same_email_no_duplicate_user(client):
    email = _unique_email("repeat")
    payload = _valid_payload(email)

    try:
        first = client.post("/register", data=payload)
        assert first.status_code == 302, "first submission with a fresh email should succeed"
        assert _count_users_by_email(email) == 1, "first submission should create exactly one row"

        second = client.post("/register", data=payload)
        assert second.status_code == 200, (
            "resubmitting the same valid data again should re-render, not redirect"
        )
        assert _count_users_by_email(email) == 1, (
            "repeated valid submissions with the same email must not create duplicate users"
        )
    finally:
        _delete_user_by_email(email)


# --------------------------------------------------------------------- #
# HTTP semantics: unsupported method                                    #
# --------------------------------------------------------------------- #

def test_register_put_method_not_allowed(client):
    resp = client.put("/register")

    assert resp.status_code == 405, "an unsupported HTTP method on /register should return 405"
