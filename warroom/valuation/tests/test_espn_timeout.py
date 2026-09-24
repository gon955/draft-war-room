"""Every ESPN call gets a timeout (deployment hardening).

espn-api calls the module-level requests.get()/requests.post() and passes no
timeout on any path, and offers no way to supply one. Left alone, a connection
ESPN accepts and never answers holds the calling thread for the life of the
process. That thread comes out of the same AnyIO threadpool that serves every
other route including /health, so enough stuck syncs stop the health check and
the platform restarts the machine — taking every draft in flight with it over
one league's hung sync.

These tests never touch the network. They assert the shim is installed, that it
injects a timeout, and that a transport failure reaches the route layer as
EspnUnreachable rather than as a bare 500.
"""

from typing import Any

import pytest
from requests import ConnectTimeout, ReadTimeout, RequestException

from warroom.valuation.data_source import (
    DEFAULT_ESPN_TIMEOUT,
    EspnDataSource,
    EspnUnreachable,
    _install_request_timeout,
    _TimeoutRequests,
)


@pytest.fixture(autouse=True)
def restore_espn_requests():
    """Put espn-api's `requests` back after each test.

    The shim is a module-level patch on a third-party package, so a test that
    installed one and walked away would leave it in place for the rest of the
    session — and the idempotence test would then be asserting against its own
    leftovers rather than a clean install.
    """
    from espn_api.requests import espn_requests

    original = espn_requests.requests
    yield espn_requests
    espn_requests.requests = original


class _RecordingRequests:
    """Stands in for the `requests` module and records what it was handed."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.some_other_attribute = "forwarded"

    def get(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "response"

    def post(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "response"


class TestInstallRequestTimeout:
    def test_it_patches_espn_apis_requests_module(self, restore_espn_requests):
        _install_request_timeout(7.5)

        assert isinstance(restore_espn_requests.requests, _TimeoutRequests)

    def test_get_and_post_both_receive_a_timeout(self):
        recorder = _RecordingRequests()
        shim = _TimeoutRequests(recorder, timeout=7.5)

        shim.get("https://example.invalid")
        shim.post("https://example.invalid")

        assert [call["timeout"] for call in recorder.calls] == [7.5, 7.5]

    def test_an_explicit_timeout_is_not_overridden(self):
        """setdefault, not assignment: a caller passing its own timeout means
        it, and silently replacing it is the same class of surprise the shim
        exists to remove."""
        recorder = _RecordingRequests()
        shim = _TimeoutRequests(recorder, timeout=7.5)

        shim.get("https://example.invalid", timeout=1.0)

        assert recorder.calls[0]["timeout"] == 1.0

    def test_other_attributes_forward_untouched(self):
        shim = _TimeoutRequests(_RecordingRequests(), timeout=7.5)

        assert shim.some_other_attribute == "forwarded"

    def test_installing_twice_does_not_stack_wrappers(self, restore_espn_requests):
        """A wrapper per call would add a stack frame per request forever."""
        _install_request_timeout(7.5)
        first = restore_espn_requests.requests

        _install_request_timeout(7.5)

        assert restore_espn_requests.requests is first

    def test_reinstalling_updates_the_timeout(self, restore_espn_requests):
        _install_request_timeout(7.5)
        _install_request_timeout(2.0)

        assert restore_espn_requests.requests.timeout == 2.0

    def test_espn_api_uses_only_the_methods_the_shim_wraps(self):
        """__getattr__ forwards anything else WITHOUT a timeout, so if espn-api
        ever starts calling requests.put() this stops being complete. Failing
        here is the notice to add it."""
        import pathlib
        import re

        from espn_api.requests import espn_requests

        source = pathlib.Path(espn_requests.__file__).read_text()
        used = set(re.findall(r"\brequests\.(\w+)\(", source))

        assert used <= {"get", "post"}


class _UnreachableLeague:
    """An espn-api League whose every call fails at the transport layer."""

    def __init__(self, error: Exception):
        self._error = error

    @property
    def espn_request(self) -> Any:
        raise self._error

    def free_agents(self, size: int) -> list[Any]:
        raise self._error


class _StubbedSource(EspnDataSource):
    """EspnDataSource with the League handed in instead of constructed."""

    def __init__(self, league: Any, timeout: float = DEFAULT_ESPN_TIMEOUT):
        super().__init__(timeout=timeout)
        self._league_obj = league

    def _league(self, espn_league_id: int, season: int) -> Any:
        return self._league_obj


class TestTransportFailuresAreTranslated:
    @pytest.mark.parametrize(
        "error",
        [ReadTimeout("timed out"), ConnectTimeout("refused"), RequestException("reset")],
        ids=["read timeout", "connect timeout", "connection error"],
    )
    def test_settings_fetch_raises_espn_unreachable(self, error):
        source = _StubbedSource(_UnreachableLeague(error))

        with pytest.raises(EspnUnreachable):
            source.get_league_settings(19048, 2027)

    def test_player_pool_fetch_raises_espn_unreachable(self):
        source = _StubbedSource(_UnreachableLeague(ReadTimeout("timed out")))

        with pytest.raises(EspnUnreachable):
            source.get_player_pool(19048, 2027)

    def test_history_fetch_raises_espn_unreachable(self, monkeypatch):
        """History is fetched outside _league(), so it needs its own proof
        that a hung ESPN surfaces as a 504 rather than a bare 500."""
        from espn_api.requests.espn_requests import EspnFantasyRequests

        def get_player_card(self, player_ids, max_scoring_period):
            raise ReadTimeout("timed out")

        monkeypatch.setattr(EspnFantasyRequests, "get_player_card", get_player_card)

        with pytest.raises(EspnUnreachable):
            EspnDataSource().get_player_history(19048, 2027, [1])

    def test_the_message_names_the_timeout(self):
        """An operator reading this in a log should not have to go and find out
        what the limit was."""
        source = _StubbedSource(_UnreachableLeague(ReadTimeout("x")), timeout=9.0)

        with pytest.raises(EspnUnreachable, match="9s"):
            source.get_league_settings(19048, 2027)

    def test_non_transport_errors_pass_through(self):
        """A bad cookie is ESPNAccessDenied and the caller can act on it. Only
        transport failures are translated."""
        from espn_api.requests.espn_requests import ESPNAccessDenied

        source = _StubbedSource(_UnreachableLeague(ESPNAccessDenied("bad cookie")))

        with pytest.raises(ESPNAccessDenied):
            source.get_league_settings(19048, 2027)
