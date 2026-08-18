import time

from backend.core.apikeys import CredentialPool
from backend.core.config import parse_api_keys

# ── Reading the keys out of .env ─────────────────────────────────────


def test_a_bracketed_env_value_is_read_as_a_list_of_keys():
    """`.env` has no list type, so the pool is written the way a person writes one."""

    assert parse_api_keys("[key-a, key-b,key-c]") == ("key-a", "key-b", "key-c")
    assert parse_api_keys('["quoted-a", "quoted-b"]') == ("quoted-a", "quoted-b")
    assert parse_api_keys("solo-key") == ("solo-key",)
    assert parse_api_keys("") == ()
    assert parse_api_keys("[ , ]") == ()


def test_the_same_key_written_twice_is_one_key():
    """Two slots on one quota would rotate straight back into the limit it hit."""

    assert parse_api_keys("[dup, other, dup]") == ("dup", "other")


# ── Rotation ─────────────────────────────────────────────────────────


def test_keys_are_handed_out_in_turn():
    """Round-robin, not always-the-first: two workers should spread across the
    pool instead of queueing on one key while the rest idle."""

    pool = CredentialPool(["a", "b", "c"])
    assert [pool.acquire()[0] for _ in range(4)] == ["a", "b", "c", "a"]


def test_a_penalised_key_is_skipped_while_it_cools_down():
    pool = CredentialPool(["a", "b"])
    pool.penalise("a", 60)
    assert [pool.acquire()[0] for _ in range(3)] == ["b", "b", "b"]


def test_a_key_comes_back_on_its_own_and_is_never_written_off():
    """The whole point of the pool: by the time the last key is limited, the one
    limited first has usually already had its window roll over."""

    pool = CredentialPool(["a", "b"])
    pool.penalise("a", 0.05)
    pool.penalise("b", 60)

    key, wait = pool.acquire()
    assert key is None
    assert 0 < wait <= 0.06, "the caller is told when the earliest key frees up"

    # Comfortably past the cooldown: time.monotonic() ticks about every 16 ms on
    # Windows, so a 50 ms deadline cannot be probed with a 50 ms sleep.
    time.sleep(0.15)
    assert pool.acquire()[0] == "a"


def test_an_empty_pool_asks_for_no_wait():
    """A local model needs no key at all; that must not read as "all limited"."""

    assert CredentialPool([]).acquire() == (None, 0.0)


def test_a_key_that_answers_forgets_its_earlier_strikes():
    """Otherwise one bad minute keeps lengthening the backoff of a key that is
    now serving the job perfectly well."""

    pool = CredentialPool(["a"])
    pool.penalise("a", 0)
    pool.penalise("a", 0)
    assert pool.strikes("a") == 2
    pool.release("a")
    assert pool.strikes("a") == 0


def test_a_key_is_identified_in_logs_by_position_not_by_value():
    pool = CredentialPool(["secret-value-a", "secret-value-b"])
    assert pool.label("secret-value-b") == "2/2"
    assert "secret" not in pool.label("secret-value-a")
    assert pool.authorization("secret-value-a") == {
        "Authorization": "Bearer secret-value-a"
    }
