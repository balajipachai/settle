"""Verify the Slack bot token/channel by posting a status message.

    uv run python scripts/seed_slack.py
"""

from __future__ import annotations

from _common import need, settings

from app.integrations.slack import LiveSlack

s = settings()
slack = LiveSlack(need(s.slack_bot_token, "SLACK_BOT_TOKEN"), need(s.slack_channel_id, "SLACK_CHANNEL_ID"))
ts = slack.post_status("👋 Settle is connected. Approval requests for invoice credits will appear in this channel.")
print(f"Slack OK (ts={ts}). Set the app's Interactivity Request URL to {s.public_base_url}/api/slack/interactions")
