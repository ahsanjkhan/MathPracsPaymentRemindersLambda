import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Union
from zoneinfo import ZoneInfo
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

import boto3
import httpx
from aws_lambda_typing import context as lambda_context

from .constants import *

METRICS_NAMESPACE = "MathPracs/PaymentReminders"
cloudwatch_client = boto3.client('cloudwatch')


def emit_metric(metric_name: str, reason: str) -> None:
    try:
        cloudwatch_client.put_metric_data(
            Namespace=METRICS_NAMESPACE,
            MetricData=[{
                'MetricName': metric_name,
                'Dimensions': [{'Name': 'Reason', 'Value': reason}],
                'Value': 1,
                'Unit': 'Count'
            }]
        )
    except Exception as e:
        print(f"Failed to emit metric {metric_name}/{reason}: {e}")


def lambda_handler(event: Dict[str, Union[str, int, float, bool, None]], context: lambda_context.Context) -> Dict[str, Union[str, int]]:
    try:
        print(f"Received Event")

        # Cross stack environment variables
        transactions_table_name = os.environ.get(IMPORTED_MUAZ_ONLY_ADJUSTMENT_LAMBDA_ENV_VAR_KEY_TRANSACTIONS_TABLE_NAME)
        sessions_table_name = os.environ.get(IMPORTED_MUAZ_ONLY_ADJUSTMENT_LAMBDA_ENV_VAR_KEY_SESSIONS_TABLE_NAME)
        tutors_metadata_table_name = os.environ.get(IMPORTED_MUAZ_ONLY_ADJUSTMENT_LAMBDA_ENV_VAR_KEY_TUTORS_METADATA_TABLE_NAME)
        discord_secret_arn = os.environ.get(IMPORTED_MUAZ_ONLY_ADJUSTMENT_LAMBDA_ENV_VAR_KEY_DISCORD_API_SECRETS_ARN)

        # Cross stack tables
        dynamodb = boto3.resource(AWS_SERVICE_DYNAMODB)
        transactions_table = dynamodb.Table(transactions_table_name)
        sessions_table = dynamodb.Table(sessions_table_name)
        tutors_metadata_table = dynamodb.Table(tutors_metadata_table_name)

        month_start, month_end = get_previous_month_range()
        period_start = max(month_start, GO_LIVE_DATE)
        print(f"Processing period: {period_start} to {month_end}")

        start_time, end_time = get_utc_time_range(period_start, month_end)

        try:
            transactions = scan_all_items_from_db(transactions_table)
        except Exception as e:
            print(f"Failed to scan transactions table: {e}")
            emit_metric("TransactionsDDB", "TransactionsScanException")
            raise

        try:
            sessions = scan_all_items_from_db(sessions_table)
        except Exception as e:
            print(f"Failed to scan sessions table: {e}")
            emit_metric("PaymentReminderDDB", "SessionsScanException")
            raise

        student_adjustments = []
        for student_name in MUAZ_ONLY_STUDENTS:
            print(f"Processing student: {student_name}")

            collected = get_collected_from_student(transactions, student_name, start_time, end_time)

            tutor_cost = 0.0
            for session in get_sessions_for_student(sessions, student_name, start_time, end_time):
                tutor_id = session.get(DYNAMODB_KEY_TUTOR_ID)

                try:
                    tutor_response = tutors_metadata_table.get_item(Key={DYNAMODB_KEY_TUTOR_ID: tutor_id})
                except Exception as e:
                    print(f"Error fetching tutor metadata {tutor_id}: {e}")
                    emit_metric("TutorInfoDDB", "FetchException")
                    raise

                if DYNAMODB_KEY_ITEM not in tutor_response:
                    print(f"Tutor metadata not found: {tutor_id}")
                    emit_metric("TutorInfoDDB", "TutorNotFound")
                    raise ValueError(f"Tutor metadata not found: {tutor_id}")

                try:
                    tutor_rate = round(float(tutor_response[DYNAMODB_KEY_ITEM].get(DYNAMODB_KEY_HOURLY_RATE)), 2)
                except (TypeError, ValueError) as e:
                    print(f"Invalid hourly rate for tutor {tutor_id}: {e}")
                    emit_metric("TutorInfoDDB", "InvalidHourlyRate")
                    raise

                tutor_cost += get_session_hours(session) * tutor_rate

            tutor_cost = round(tutor_cost, 2)

            if collected == 0 and tutor_cost == 0:
                continue

            student_adjustments.append((student_name, collected, tutor_cost))

        if not student_adjustments:
            print(f"Nothing to report for {period_start} to {month_end}")
            return {
                'statusCode': HTTP_STATUS_OK,
                'body': json.dumps({RESPONSE_KEY_MESSAGE: RESPONSE_MESSAGE_NOTHING_TO_REPORT})
            }

        subtotal = round(sum(collected - tutor_cost for _, collected, tutor_cost in student_adjustments), 2)
        ahsan_sends_muaz = round(subtotal * PARTNER_SPLIT, 2)

        secrets_client = boto3.client(AWS_SERVICE_SECRETSMANAGER)
        discord_secret_response = secrets_client.get_secret_value(SecretId=discord_secret_arn)
        discord_creds = json.loads(discord_secret_response['SecretString'])
        discord_bot_token = discord_creds[SECRET_KEY_DISCORD_BOT_TOKEN]
        discord_channel_id = discord_creds[SECRET_KEY_PAYMENT_REMINDERS_CHANNEL_ID]

        message_body = build_message(period_start, month_end, student_adjustments, subtotal, ahsan_sends_muaz)

        print(f"Sending Discord message: {message_body}")
        try:
            send_discord_message(discord_bot_token, discord_channel_id, message_body)
        except Exception as e:
            print(f"Failed to send Discord message: {e}")
            emit_metric("APIFailure", "DiscordSendFailed")
            raise

        return {
            'statusCode': HTTP_STATUS_OK,
            'body': json.dumps({
                RESPONSE_KEY_MESSAGE: RESPONSE_MESSAGE_SUCCESS,
                RESPONSE_KEY_RESULTS: {
                    RESPONSE_KEY_SUBTOTAL: subtotal,
                    RESPONSE_KEY_AHSAN_SENDS_MUAZ: ahsan_sends_muaz
                }
            })
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        emit_metric("UnknownFailures", "UnhandledException")
        return {
            'statusCode': HTTP_STATUS_ERROR,
            'body': json.dumps({RESPONSE_KEY_ERROR: str(e)})
        }

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10), retry=retry_if_exception(lambda e: isinstance(e, httpx.HTTPError)))
def send_discord_message(discord_bot_token, discord_channel_id, message_body):
    response = httpx.post(
        f"https://discord.com/api/v10/channels/{discord_channel_id}/messages",
        headers={"Authorization": f"Bot {discord_bot_token}", "Content-Type": "application/json"},
        json={"content": message_body},
        timeout=30.0
    )
    response.raise_for_status()
    return response

def get_previous_month_range() -> Tuple[str, str]:
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_of_previous_month = first_of_this_month - timedelta(days=1)
    first_of_previous_month = last_of_previous_month.replace(day=1)

    return first_of_previous_month.strftime(DATE_FORMAT), last_of_previous_month.strftime(DATE_FORMAT)

def get_utc_time_range(start_date: str, end_date: str) -> Tuple[str, str]:
    chicago_tz = ZoneInfo(TIMEZONE_CHICAGO)
    start_dt = datetime.strptime(start_date, DATE_FORMAT).replace(hour=0, minute=0, second=0, tzinfo=chicago_tz)
    end_dt = datetime.strptime(end_date, DATE_FORMAT).replace(hour=23, minute=59, second=59, tzinfo=chicago_tz)

    return start_dt.astimezone(timezone.utc).isoformat(), end_dt.astimezone(timezone.utc).isoformat()

def get_collected_from_student(transactions: List[Dict], student_name: str, start_time: str, end_time: str) -> float:
    collected = 0.0

    for transaction in transactions:
        if (transaction.get(DYNAMODB_KEY_STUDENT_NAME) == student_name
                and transaction.get(DYNAMODB_KEY_TRANSACTION_TYPE) == TRANSACTION_TYPE_CREDIT
                and transaction.get(DYNAMODB_KEY_ACTION_BY) in PARTNERS
                and start_time <= transaction.get(DYNAMODB_KEY_TIMESTAMP, '') <= end_time):
            collected += float(transaction.get(DYNAMODB_KEY_AMOUNT, 0))

    return round(collected, 2)

def get_sessions_for_student(sessions: List[Dict], student_name: str, start_time: str, end_time: str) -> List[Dict]:
    expected_session_name_for_student = student_name + SESSION_NAME_SUFFIX
    student_sessions = []

    for session in sessions:
        summary = session.get(DYNAMODB_KEY_SUMMARY)
        if not summary or not session.get(DYNAMODB_KEY_UTC_START) or not session.get(DYNAMODB_KEY_UTC_END):
            continue

        if not start_time <= session[DYNAMODB_KEY_UTC_START] <= end_time:
            continue

        if summary == expected_session_name_for_student or is_no_show_event(expected_session_name_for_student, summary):
            student_sessions.append(session)

    return student_sessions

def get_session_hours(session: Dict) -> float:
    start_time_dt = datetime.fromisoformat(session[DYNAMODB_KEY_UTC_START])
    end_time_dt = datetime.fromisoformat(session[DYNAMODB_KEY_UTC_END])
    return (end_time_dt - start_time_dt).total_seconds() / SECONDS_PER_HOUR

def is_no_show_event(standard_session_name: str, session_name: str) -> bool:
    normalized_session = re.sub(r'[^\w\s]', '', session_name.lower())
    normalized_base = re.sub(r'[^\w\s]', '', standard_session_name.lower())

    no_show_pattern = rf'^{re.escape(normalized_base)}\s+no\s*show'
    return bool(re.search(no_show_pattern, normalized_session))

def build_message(period_start: str, period_end: str, student_adjustments: List[Tuple[str, float, float]], subtotal: float, ahsan_sends_muaz: float) -> str:
    lines = [f"Off-the-books Muaz-only adjustment from {period_start} to {period_end}:", ""]

    for student_name, collected, tutor_cost in student_adjustments:
        lines.append(f"{student_name}: collected ${collected:.2f}, tutor ${tutor_cost:.2f} → {format_dollars(collected - tutor_cost)}")

    lines.append("")
    lines.append(f"Subtotal: {format_dollars(subtotal)}")

    if ahsan_sends_muaz >= 0:
        lines.append(f"Ahsan sends Muaz ${subtotal:.2f} / 2 = **${ahsan_sends_muaz:.2f}**")
    else:
        lines.append(f"Muaz sends Ahsan ${-subtotal:.2f} / 2 = **${-ahsan_sends_muaz:.2f}**")

    lines.append("")
    lines.append(OFF_THE_BOOKS_NOTICE)

    return "\n".join(lines)

def format_dollars(amount: float) -> str:
    return f"-${-amount:.2f}" if amount < 0 else f"${amount:.2f}"

def scan_all_items_from_db(table) -> List[Dict]:
    """Scan all items from a DDB table."""
    db_items = []
    response = table.scan()
    db_items.extend(response.get('Items', []))

    # Handle pagination
    while 'LastEvaluatedKey' in response:
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
        db_items.extend(response.get('Items', []))

    return db_items
