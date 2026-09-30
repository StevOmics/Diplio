from app.auth import hash_password, verify_password


def test_correct_password_verifies():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_wrong_password_fails():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("wrong password", hashed) is False


def test_hash_is_not_the_plaintext_password():
    hashed = hash_password("hunter2")
    assert hashed != "hunter2"


def test_hashing_same_password_twice_gives_different_hashes():
    # bcrypt salts each hash, so two hashes of the same password must differ
    # even though both verify correctly - guards against an unsalted regression.
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a) is True
    assert verify_password("same-password", b) is True
