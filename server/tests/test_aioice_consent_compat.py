import os

import aioice.ice as ice

import aioice_compat
from aioice_compat import install_aioice_consent_grace

_STOCK = 6


def _reset() -> None:
    aioice_compat._consent_installed = False
    ice.CONSENT_FAILURES = _STOCK


def _run(env: str | None, expected: float) -> None:
    _reset()
    if env is None:
        os.environ.pop("ICE_CONSENT_FAILURES", None)
    else:
        os.environ["ICE_CONSENT_FAILURES"] = env
    try:
        install_aioice_consent_grace()
        assert ice.CONSENT_FAILURES == expected, (
            f"env={env!r} -> {ice.CONSENT_FAILURES!r}, want {expected!r}"
        )
        # Idempotent: a second install must not change the threshold.
        install_aioice_consent_grace()
        assert ice.CONSENT_FAILURES == expected
    finally:
        os.environ.pop("ICE_CONSENT_FAILURES", None)
        _reset()


def test_default_is_60() -> None:
    _run(None, 60)


def test_stock_six() -> None:
    _run("6", _STOCK)


def test_zero_never_closes() -> None:
    _run("0", float("inf"))


def test_invalid_falls_back_to_default() -> None:
    _run("not-a-number", 60)


def test_consent_interval_untouched() -> None:
    _run(None, 60)
    assert ice.CONSENT_INTERVAL == 5


if __name__ == "__main__":
    tests = [
        test_default_is_60,
        test_stock_six,
        test_zero_never_closes,
        test_invalid_falls_back_to_default,
        test_consent_interval_untouched,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} passed")
