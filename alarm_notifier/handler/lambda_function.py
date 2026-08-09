import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import boto3
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception


DISCORD_API_BASE = "https://discord.com/api/v10"
STUDENT_LOG_GROUP = "/aws/lambda/mathpracs-student-payment-reminder"
TUTOR_LOG_GROUP = "/aws/lambda/mathpracs-tutor-payment-reminder"
REGION = "us-east-1"


def lambda_handler(event, context):
    discord_secret_arn = os.environ.get('DISCORD_SECRETS_ARN')
    secrets_client = boto3.client('secretsmanager')
    secret_response = secrets_client.get_secret_value(SecretId=discord_secret_arn)
    discord_creds = json.loads(secret_response['SecretString'])
    bot_token = discord_creds['bot_token']
    channel_id = discord_creds['payment_reminders_channel_id']

    for record in event.get('Records', []):
        message = json.loads(record['Sns']['Message'])

        alarm_name = message.get('AlarmName', 'Unknown')
        timestamp = message.get('StateChangeTime', '')

        alarm_url = build_alarm_url(alarm_name)
        student_logs_url = build_logs_url(STUDENT_LOG_GROUP, timestamp)
        tutor_logs_url = build_logs_url(TUTOR_LOG_GROUP, timestamp)

        notification = (
            f"🚨 **PaymentReminders Alarm: {alarm_name}**\n"
            f"Time: {timestamp}\n\n"
            f"🔍 Alarm: {alarm_url}\n"
            f"📋 Student Logs: {student_logs_url}\n"
            f"📋 Tutor Logs: {tutor_logs_url}"
        )

        send_discord_message(bot_token, channel_id, notification)

    return {"statusCode": 200}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10), retry=retry_if_exception(lambda e: isinstance(e, httpx.HTTPError)))
def send_discord_message(bot_token: str, channel_id: str, message_body: str):
    response = httpx.post(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
        json={"content": message_body},
        timeout=10.0
    )
    response.raise_for_status()
    return response


def build_alarm_url(alarm_name: str) -> str:
    encoded_alarm_name = quote(alarm_name, safe='')
    return (
        f"https://console.aws.amazon.com/cloudwatch/home?region={REGION}"
        f"#alarmsV2:alarm/{encoded_alarm_name}"
    )


def build_logs_url(log_group: str, timestamp_str: str) -> str:
    try:
        ts = datetime.fromisoformat(timestamp_str.replace('+0000', '+00:00'))
    except (ValueError, AttributeError):
        ts = datetime.now(timezone.utc)

    start_ms = int((ts - timedelta(minutes=2)).timestamp() * 1000)
    end_ms = int((ts + timedelta(minutes=2)).timestamp() * 1000)

    encoded_log_group = quote(log_group, safe='')
    return (
        f"https://console.aws.amazon.com/cloudwatch/home?region={REGION}"
        f"#logsV2:log-groups/log-group/{encoded_log_group}"
        f"/log-events?start={start_ms}&end={end_ms}"
    )
