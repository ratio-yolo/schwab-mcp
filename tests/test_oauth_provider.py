from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import AnyHttpUrl
from starlette.requests import Request

from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from schwab_mcp.remote.oauth import SchwabMCPOAuthProvider


def run(coro):
    return asyncio.run(coro)


CONSENT_SECRET = "correct-horse-battery-staple"


def make_provider(
    mcp_oauth_secret: str = CONSENT_SECRET,
) -> SchwabMCPOAuthProvider:
    return SchwabMCPOAuthProvider(
        server_url="https://mcp.example.com",
        mcp_oauth_secret=mcp_oauth_secret,
    )


def make_client(client_id: str = "test-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="test-secret",
        redirect_uris=[AnyHttpUrl("https://example.com/callback")],
    )


def make_auth_params(
    redirect_uri: str = "https://example.com/callback",
    state: str = "test-state",
    code_challenge: str = "test-challenge",
) -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=["mcp"],
        code_challenge=code_challenge,
        redirect_uri=AnyHttpUrl(redirect_uri),
        redirect_uri_provided_explicitly=True,
    )


async def make_consent_request(
    state: str, action: str = "approve", secret: str | None = CONSENT_SECRET
) -> Request:
    parts = [f"state={state}", f"action={action}"]
    if secret is not None:
        parts.append(f"secret={secret}")
    body = "&".join(parts).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }

    async def receive():
        return {"type": "http.request", "body": body}

    return Request(scope, receive)


async def do_full_auth_flow(
    provider: SchwabMCPOAuthProvider,
) -> tuple[OAuthClientInformationFull, OAuthToken]:
    """Register client, authorize, consent, exchange code. Returns (client, token)."""
    client = make_client()
    await provider.register_client(client)

    params = make_auth_params()
    url = await provider.authorize(client, params)
    state = url.split("state=")[1]

    request = await make_consent_request(state, "approve")
    await provider.handle_consent(request)

    # The auth code was stored during handle_consent
    code = next(iter(provider._auth_codes))
    auth_code = provider._auth_codes[code]

    token = await provider.exchange_authorization_code(client, auth_code)
    return client, token


class TestClientRegistration:
    def test_register_and_get_client(self):
        provider = make_provider()
        client = make_client()
        run(provider.register_client(client))

        result = run(provider.get_client("test-client"))
        assert result is not None
        assert result.client_id == "test-client"

    def test_get_unknown_client_returns_none(self):
        provider = make_provider()
        result = run(provider.get_client("unknown"))
        assert result is None

    def test_register_client_without_id_raises(self):
        provider = make_provider()
        client = OAuthClientInformationFull(
            client_id="",
            client_secret="test-secret",
            redirect_uris=[AnyHttpUrl("https://example.com/callback")],
        )
        with pytest.raises(ValueError, match="No client_id"):
            run(provider.register_client(client))


class TestAuthorization:
    def test_authorize_returns_consent_url(self):
        provider = make_provider()
        client = make_client()
        run(provider.register_client(client))

        params = make_auth_params()
        url = run(provider.authorize(client, params))

        assert "/consent?state=" in url

    def test_authorize_uses_provided_state(self):
        provider = make_provider()
        client = make_client()
        run(provider.register_client(client))

        params = make_auth_params(state="my-custom-state")
        url = run(provider.authorize(client, params))

        assert "state=my-custom-state" in url

    def test_consent_page_valid_state(self):
        provider = make_provider()
        client = make_client()
        run(provider.register_client(client))

        params = make_auth_params()
        url = run(provider.authorize(client, params))
        state = url.split("state=")[1]

        response = run(provider.get_consent_page(state))
        assert response.status_code == 200
        assert b"Authorize Schwab MCP" in response.body

    def test_consent_page_invalid_state(self):
        provider = make_provider()
        response = run(provider.get_consent_page("bad-state"))
        assert response.status_code == 400

    def test_consent_page_includes_passphrase_field(self):
        provider = make_provider()
        client = make_client()
        run(provider.register_client(client))

        params = make_auth_params()
        url = run(provider.authorize(client, params))
        state = url.split("state=")[1]

        response = run(provider.get_consent_page(state))
        assert b'name="secret"' in response.body
        assert b'type="password"' in response.body


class TestConsentPassphraseGate:
    def _authorize_to_state(self, provider: SchwabMCPOAuthProvider) -> str:
        client = make_client()
        run(provider.register_client(client))
        params = make_auth_params()
        url = run(provider.authorize(client, params))
        return url.split("state=")[1]

    def test_correct_passphrase_issues_code(self):
        provider = make_provider()
        state = self._authorize_to_state(provider)

        request = run(make_consent_request(state, "approve", CONSENT_SECRET))
        response = run(provider.handle_consent(request))

        assert response.status_code == 302
        assert len(provider._auth_codes) == 1

    def test_wrong_passphrase_rejected_no_code(self):
        provider = make_provider()
        state = self._authorize_to_state(provider)

        request = run(make_consent_request(state, "approve", "wrong-passphrase"))
        response = run(provider.handle_consent(request))

        assert response.status_code == 401
        assert len(provider._auth_codes) == 0
        # State preserved so the user can retry
        assert state in provider._state_mapping

    def test_missing_passphrase_rejected_no_code(self):
        provider = make_provider()
        state = self._authorize_to_state(provider)

        request = run(make_consent_request(state, "approve", secret=None))
        response = run(provider.handle_consent(request))

        assert response.status_code == 401
        assert len(provider._auth_codes) == 0

    def test_unconfigured_secret_fails_closed(self):
        provider = make_provider(mcp_oauth_secret="")
        state = self._authorize_to_state(provider)

        request = run(make_consent_request(state, "approve", "anything"))
        response = run(provider.handle_consent(request))

        assert response.status_code == 403
        assert len(provider._auth_codes) == 0

    def test_deny_does_not_require_passphrase(self):
        provider = make_provider()
        state = self._authorize_to_state(provider)

        request = run(make_consent_request(state, "deny", secret=None))
        response = run(provider.handle_consent(request))

        assert response.status_code == 302
        assert len(provider._auth_codes) == 0
        assert "error=access_denied" in response.headers["location"]


class TestCodeExchange:
    def test_full_auth_code_flow(self):
        provider = make_provider()
        _, token = run(do_full_auth_flow(provider))

        assert token.access_token.startswith("smcp_at_")
        assert token.refresh_token is not None
        assert token.refresh_token.startswith("smcp_rt_")
        assert token.token_type == "Bearer"

    def test_exchange_invalid_code_raises(self):
        provider = make_provider()
        client = make_client()
        run(provider.register_client(client))

        fake_code = AuthorizationCode(
            code="bogus",
            client_id="test-client",
            redirect_uri=AnyHttpUrl("https://example.com/callback"),
            redirect_uri_provided_explicitly=True,
            expires_at=time.time() + 300,
            scopes=["mcp"],
            code_challenge="test-challenge",
        )

        with pytest.raises(ValueError, match="Invalid authorization code"):
            run(provider.exchange_authorization_code(client, fake_code))

    def test_auth_code_removed_after_exchange(self):
        provider = make_provider()
        client, _ = run(do_full_auth_flow(provider))

        assert len(provider._auth_codes) == 0


class TestTokenManagement:
    def test_load_valid_access_token(self):
        provider = make_provider()
        _, token = run(do_full_auth_flow(provider))

        loaded = run(provider.load_access_token(token.access_token))
        assert loaded is not None
        assert loaded.token == token.access_token

    def test_load_expired_access_token_returns_none(self):
        provider = make_provider()
        _, token = run(do_full_auth_flow(provider))

        # Manually expire the token
        provider._access_tokens[token.access_token].expires_at = int(time.time()) - 1

        loaded = run(provider.load_access_token(token.access_token))
        assert loaded is None
        assert token.access_token not in provider._access_tokens

    def test_load_unknown_token_returns_none(self):
        provider = make_provider()
        loaded = run(provider.load_access_token("unknown-token"))
        assert loaded is None

    def test_refresh_token_flow(self):
        provider = make_provider()
        client, token = run(do_full_auth_flow(provider))

        assert token.refresh_token is not None
        old_refresh = token.refresh_token

        rt = run(provider.load_refresh_token(client, old_refresh))
        assert rt is not None

        new_token = run(provider.exchange_refresh_token(client, rt, ["mcp"]))
        assert new_token.access_token.startswith("smcp_at_")
        assert new_token.refresh_token is not None
        assert new_token.refresh_token != old_refresh

        # Old refresh token should be revoked
        assert old_refresh not in provider._refresh_tokens

    def test_refresh_with_expired_token_returns_none(self):
        provider = make_provider()
        client, token = run(do_full_auth_flow(provider))

        assert token.refresh_token is not None
        provider._refresh_tokens[token.refresh_token].expires_at = int(time.time()) - 1

        loaded = run(provider.load_refresh_token(client, token.refresh_token))
        assert loaded is None
        assert token.refresh_token not in provider._refresh_tokens

    def test_revoke_token(self):
        provider = make_provider()
        _, token = run(do_full_auth_flow(provider))

        run(provider.revoke_token(token.access_token))
        assert token.access_token not in provider._access_tokens

        assert token.refresh_token is not None
        run(provider.revoke_token(token.refresh_token))
        assert token.refresh_token not in provider._refresh_tokens
