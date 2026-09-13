"""Build the gateway set for the configured mode."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import LIVE_WRITES_OVERRIDE_PHRASE, Settings
from app.integrations.fixtures import FixtureGmail, FixtureHubSpot, FixtureSlack, FixtureStripe, FixtureWorld
from app.integrations.gateways import GmailGateway, HubSpotGateway, SlackGateway, StripeGateway
from app.integrations.gmail import LiveGmail
from app.integrations.hubspot import LiveHubSpot
from app.integrations.slack import LiveSlack
from app.integrations.stripe import LiveStripe


@dataclass
class Gateways:
    stripe: StripeGateway
    hubspot: HubSpotGateway
    slack: SlackGateway
    gmail: GmailGateway
    world: FixtureWorld | None = None

    @property
    def mode(self) -> str:
        return "fixture" if self.world is not None else "live"

    def close(self) -> None:
        for g in (self.stripe, self.hubspot, self.slack, self.gmail):
            if hasattr(g, "close"):
                g.close()


def build_fixture_gateways(world: FixtureWorld) -> Gateways:
    return Gateways(FixtureStripe(world), FixtureHubSpot(world), FixtureSlack(world), FixtureGmail(world), world)


def build_live_gateways(s: Settings) -> Gateways:
    required = {
        "STRIPE_SECRET_KEY": s.stripe_secret_key,
        "HUBSPOT_ACCESS_TOKEN": s.hubspot_access_token,
        "SLACK_BOT_TOKEN": s.slack_bot_token,
        "SLACK_CHANNEL_ID": s.slack_channel_id,
        "GMAIL_CLIENT_ID": s.gmail_client_id,
        "GMAIL_CLIENT_SECRET": s.gmail_client_secret,
        "GMAIL_REFRESH_TOKEN": s.gmail_refresh_token,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"SETTLE_MODE=live requires: {', '.join(missing)}")
    allow_live = s.live_writes_operator_override == LIVE_WRITES_OVERRIDE_PHRASE
    return Gateways(
        stripe=LiveStripe(s.stripe_secret_key, api_version=s.stripe_api_version, timeout=s.http_timeout_s,
                          allow_live_keys=allow_live),
        hubspot=LiveHubSpot(s.hubspot_access_token, timeout=s.http_timeout_s),
        slack=LiveSlack(s.slack_bot_token, s.slack_channel_id),
        gmail=LiveGmail(s.gmail_client_id, s.gmail_client_secret, s.gmail_refresh_token, user_id=s.gmail_user_id,
                        inbox_query=s.gmail_inbox_query, timeout=s.http_timeout_s),
    )


def build_gateways(s: Settings, world: FixtureWorld | None = None) -> Gateways:
    if s.mode == "live":
        return build_live_gateways(s)
    return build_fixture_gateways(world or FixtureWorld.from_file(s.fixture_world_path))
