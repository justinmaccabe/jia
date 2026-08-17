"""What the app says when configuration is missing.

These exist because the original behaviour was actively misleading rather than
merely unhelpful. A single malformed line in a hosted secrets box makes Streamlit
raise on the whole store, so nothing loads, and the app then named the first
required variable as unset — sending the reader off to add a value that was
already sitting there correctly. Someone can lose an afternoon to that.

A diagnostic is part of the contract here, not a nicety: this app refuses to start
by design, so the refusal message is the entire user interface for getting it
running.
"""

from __future__ import annotations

import sys
import types

import pytest

import desk.settings as settings_module
from desk.settings import SettingsError, get_settings


@pytest.fixture(autouse=True)
def clean_env() -> None:
    """No DESK_* leakage between cases, and no cached settings object.

    Cleaned directly rather than through monkeypatch, and on teardown as well as
    setup. The function under test writes into os.environ itself, and monkeypatch
    only reverses assignments it made — so a case that loads a valid secret store
    would otherwise leave DESK_AUTH_MODE set for every later test file, which is
    exactly the failure this fixture was written to fix.
    """
    import os

    def strip() -> None:
        for key in [k for k in os.environ if k.startswith("DESK_")]:
            del os.environ[key]

    strip()
    get_settings.cache_clear()
    yield
    strip()
    get_settings.cache_clear()


def fake_streamlit(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    """Install a stand-in streamlit whose secret store returns or raises."""
    module = types.ModuleType("streamlit")

    class Store:
        def items(self) -> object:
            if isinstance(payload, Exception):
                raise payload
            return payload.items()  # type: ignore[union-attr]

    module.secrets = Store()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "streamlit", module)


VALID = {
    "DESK_AUTH_MODE": "passcode",
    "DESK_APP_ENV": "prod",
    "DESK_DATABASE_URL": "postgresql://u:p@h/db",
    "DESK_PASSCODE_HASH": "$argon2id$v=19$m=65536,t=3,p=4$abc$def",
    "DESK_SESSION_SECRET": "a" * 64,
}


class TestUnreadableSecretStore:
    def test_a_parse_failure_is_named_as_the_probable_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_streamlit(monkeypatch, Exception("Invalid value (at line 1, column 20)"))
        with pytest.raises(SettingsError) as caught:
            get_settings()
        message = str(caught.value)
        assert "Probable cause" in message
        assert "could not be read" in message
        # The reader must be told the good values are unavailable too, or they
        # will keep re-checking the one the app happened to name.
        assert "including any that are correctly set" in message

    def test_it_says_no_variables_arrived_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_streamlit(monkeypatch, Exception("boom"))
        with pytest.raises(SettingsError, match=r"None arrived at all"):
            get_settings()

    def test_the_original_error_is_still_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The diagnostic supplements the cause, it does not replace it."""
        fake_streamlit(monkeypatch, Exception("boom"))
        with pytest.raises(SettingsError, match="DESK_AUTH_MODE is required"):
            get_settings()


class TestVisibleKeys:
    def test_keys_that_did_arrive_are_listed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The exact case that stalled a real deployment: the database URL set
        and nothing else, with an error naming only the missing auth mode."""
        fake_streamlit(monkeypatch, {"DESK_DATABASE_URL": "postgresql://u:p@h/db"})
        with pytest.raises(SettingsError) as caught:
            get_settings()
        message = str(caught.value)
        assert "DESK_DATABASE_URL" in message
        assert "None arrived at all" not in message

    def test_secret_values_are_never_printed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Names only. This message renders in a browser."""
        secret = "postgresql://someone:hunter2@host/db"
        fake_streamlit(monkeypatch, {"DESK_DATABASE_URL": secret})
        with pytest.raises(SettingsError) as caught:
            get_settings()
        message = str(caught.value)
        assert "hunter2" not in message
        assert secret not in message

    def test_a_complete_configuration_starts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_streamlit(monkeypatch, VALID)
        assert get_settings().auth_mode.value == "passcode"


class TestWithoutStreamlit:
    def test_absent_streamlit_is_not_reported_as_a_problem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI use has no secret store and that is not a fault."""
        monkeypatch.setitem(sys.modules, "streamlit", None)
        assert settings_module._load_streamlit_secrets_into_env() is None


class TestBlankSecretsAreTreatedAsAbsent:
    """GitHub Actions interpolates a missing secret as "", not as unset.

    So `${{ secrets.NOT_SET }}` arrives as an empty string, and a `SecretStr("")` is
    not None. An `is None` guard therefore passed it through, and the failure landed
    several frames deeper in the engine as "a database URL is required... in a hosted
    deployment that would look like losing your history" — alarming, and about the
    wrong thing.
    """

    def test_an_empty_database_url_reads_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DESK_AUTH_MODE", "none")
        monkeypatch.setenv("DESK_APP_ENV", "local")
        monkeypatch.setenv("DESK_DATABASE_URL", "")
        assert get_settings().database_url is None

    def test_whitespace_only_also_reads_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DESK_AUTH_MODE", "none")
        monkeypatch.setenv("DESK_APP_ENV", "local")
        monkeypatch.setenv("DESK_DATABASE_URL", "   ")
        assert get_settings().database_url is None

    def test_a_real_url_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DESK_AUTH_MODE", "none")
        monkeypatch.setenv("DESK_APP_ENV", "local")
        monkeypatch.setenv("DESK_DATABASE_URL", "postgresql://u:p@h/db")
        settings = get_settings()
        assert settings.database_url is not None
        assert settings.database_url.get_secret_value() == "postgresql://u:p@h/db"

    def test_an_empty_passcode_hash_still_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The security-relevant half. An empty hash must never satisfy the gate."""
        monkeypatch.setenv("DESK_AUTH_MODE", "passcode")
        monkeypatch.setenv("DESK_APP_ENV", "local")
        monkeypatch.setenv("DESK_PASSCODE_HASH", "")
        monkeypatch.setenv("DESK_SESSION_SECRET", "")
        with pytest.raises(SettingsError, match="requires DESK_PASSCODE_HASH"):
            get_settings()

    def test_prod_still_refuses_an_empty_database_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DESK_AUTH_MODE", "passcode")
        monkeypatch.setenv("DESK_APP_ENV", "prod")
        monkeypatch.setenv("DESK_PASSCODE_HASH", "$argon2id$v=19$m=65536,t=3,p=4$a$b")
        monkeypatch.setenv("DESK_SESSION_SECRET", "a" * 64)
        monkeypatch.setenv("DESK_DATABASE_URL", "")
        with pytest.raises(SettingsError, match="DESK_DATABASE_URL is required"):
            get_settings()
