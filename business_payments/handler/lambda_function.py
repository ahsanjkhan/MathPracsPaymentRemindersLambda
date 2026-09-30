import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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

        business_payment_reminders_table_name = os.environ.get(ENV_BUSINESS_PAYMENT_TABLE_NAME)

        # Cross stack environment variables
        transactions_table_name = os.environ.get(IMPORTED_BUSINESS_PAYMENT_LAMBDA_ENV_VAR_KEY_TRANSACTIONS_TABLE_NAME)
        tutor_transactions_table_name = os.environ.get(IMPORTED_BUSINESS_PAYMENT_LAMBDA_ENV_VAR_KEY_TUTOR_TRANSACTIONS_TABLE_NAME)
        tutors_table_name = os.environ.get(IMPORTED_BUSINESS_PAYMENT_LAMBDA_ENV_VAR_KEY_TUTORS_TABLE_NAME)
        business_internal_debts_table_name = os.environ.get(IMPORTED_BUSINESS_PAYMENT_LAMBDA_ENV_VAR_KEY_BUSINESS_INTERNAL_DEBTS_TABLE_NAME)
        discord_secret_arn = os.environ.get(IMPORTED_BUSINESS_PAYMENT_LAMBDA_ENV_VAR_KEY_DISCORD_API_SECRETS_ARN)

        # Cross stack tables
        dynamodb = boto3.resource(AWS_SERVICE_DYNAMODB)
        transactions_table = dynamodb.Table(transactions_table_name)
        tutor_transactions_table = dynamodb.Table(tutor_transactions_table_name)
        tutors_table = dynamodb.Table(tutors_table_name)
        business_internal_debts_table = dynamodb.Table(business_internal_debts_table_name)

        business_payment_reminders_table = dynamodb.Table(business_payment_reminders_table_name)

        month_start, month_end = get_previous_month_range()
        period_start = max(month_start, GO_LIVE_DATE)
        print(f"Processing period: {period_start} to {month_end}")

        uid = f"{UID_PREFIX}#{month_start}#{month_end}"

        try:
            response = business_payment_reminders_table.get_item(Key={DYNAMODB_KEY_UID: uid})
        except Exception as e:
            print(f"Error getting business payment reminder with uid: {uid}. Exception: {e}")
            emit_metric("PaymentReminderDDB", "GetReminderException")
            raise

        if DYNAMODB_KEY_ITEM in response and response[DYNAMODB_KEY_ITEM].get(DYNAMODB_KEY_PROCESSED_DISCORD):
            return {
                'statusCode': HTTP_STATUS_OK,
                'body': json.dumps({RESPONSE_KEY_MESSAGE: RESPONSE_MESSAGE_ALREADY_PROCESSED})
            }

        start_time, end_time = get_utc_time_range(period_start, month_end)

        try:
            transactions = scan_all_items_from_db(transactions_table)
        except Exception as e:
            print(f"Failed to scan transactions table: {e}")
            emit_metric("TransactionsDDB", "TransactionsScanException")
            raise

        try:
            tutor_transactions = scan_all_items_from_db(tutor_transactions_table)
        except Exception as e:
            print(f"Failed to scan tutor transactions table: {e}")
            emit_metric("TutorTransactionsDDB", "TutorTransactionsScanException")
            raise

        try:
            tutor_names_by_tutor_id = {t[DYNAMODB_KEY_TUTOR_ID]: t.get(DYNAMODB_KEY_TUTOR_NAME, t[DYNAMODB_KEY_TUTOR_ID]) for t in scan_all_items_from_db(tutors_table)}
        except Exception as e:
            print(f"Failed to scan tutors table: {e}")
            emit_metric("TutorInfoDDB", "TutorsScanException")
            raise

        try:
            business_internal_debts = scan_all_items_from_db(business_internal_debts_table)
        except Exception as e:
            print(f"Failed to scan business internal debts table: {e}")
            emit_metric("BusinessInternalDebtsDDB", "DebtsScanException")
            raise

        collected_by_partner = get_student_payments_collected_by_partner(transactions, start_time, end_time)
        tutor_payments_by_partner = get_tutor_payments_sent_by_partner(tutor_transactions, tutor_names_by_tutor_id, start_time, end_time)
        outstanding_by_partner = get_outstanding_owed_to_partner(business_internal_debts)

        tutor_totals_by_partner = {partner: round(sum(amount for _, amount in tutor_payments_by_partner[partner]), 2) for partner in PARTNERS}

        ahsan_owes_muaz = round(PARTNER_SPLIT * collected_by_partner[PARTNER_AHSAN] + PARTNER_SPLIT * tutor_totals_by_partner[PARTNER_MUAZ], 2)
        muaz_owes_ahsan = round(PARTNER_SPLIT * collected_by_partner[PARTNER_MUAZ] + PARTNER_SPLIT * tutor_totals_by_partner[PARTNER_AHSAN], 2)

        net_amount = round(abs(muaz_owes_ahsan - ahsan_owes_muaz), 2)
        net_owed_to = PARTNER_AHSAN if muaz_owes_ahsan > ahsan_owes_muaz else PARTNER_MUAZ if ahsan_owes_muaz > muaz_owes_ahsan else None

        total_owed_to_ahsan = outstanding_by_partner[PARTNER_AHSAN] + (net_amount if net_owed_to == PARTNER_AHSAN else 0)
        total_owed_to_muaz = outstanding_by_partner[PARTNER_MUAZ] + (net_amount if net_owed_to == PARTNER_MUAZ else 0)

        has_activity = any(collected_by_partner.values()) or any(tutor_payments_by_partner.values())
        has_outstanding = round(total_owed_to_ahsan, 2) != 0 or round(total_owed_to_muaz, 2) != 0

        if not has_activity and not has_outstanding:
            print(f"Nothing to report for {period_start} to {month_end}")
            return {
                'statusCode': HTTP_STATUS_OK,
                'body': json.dumps({RESPONSE_KEY_MESSAGE: RESPONSE_MESSAGE_NOTHING_TO_REPORT})
            }

        secrets_client = boto3.client(AWS_SERVICE_SECRETSMANAGER)
        discord_secret_response = secrets_client.get_secret_value(SecretId=discord_secret_arn)
        discord_creds = json.loads(discord_secret_response['SecretString'])
        discord_bot_token = discord_creds[SECRET_KEY_DISCORD_BOT_TOKEN]
        discord_channel_id = discord_creds[SECRET_KEY_PAYMENT_REMINDERS_CHANNEL_ID]

        try:
            business_payment_reminders_table.put_item(Item={
                DYNAMODB_KEY_UID: uid,
                DYNAMODB_KEY_MONTH_START: month_start,
                DYNAMODB_KEY_MONTH_END: month_end,
                DYNAMODB_KEY_AHSAN_OWES_MUAZ: Decimal(str(ahsan_owes_muaz)),
                DYNAMODB_KEY_MUAZ_OWES_AHSAN: Decimal(str(muaz_owes_ahsan)),
                DYNAMODB_KEY_NET_AMOUNT: Decimal(str(net_amount)),
                DYNAMODB_KEY_NET_OWED_TO: net_owed_to,
                DYNAMODB_KEY_PROCESSED_DISCORD: False
            })
        except Exception as e:
            print(f"Error putting business payment reminder with uid: {uid}. Exception: {e}")
            emit_metric("PaymentReminderDDB", "PutReminderException")
            raise

        message_body = build_message(period_start, month_end, collected_by_partner, tutor_payments_by_partner, tutor_totals_by_partner,
                                     net_amount, net_owed_to, total_owed_to_ahsan, total_owed_to_muaz)

        print(f"Sending Discord message: {message_body}")
        try:
            send_discord_message(discord_bot_token, discord_channel_id, message_body)
        except Exception as e:
            print(f"Failed to send Discord message: {e}")
            emit_metric("APIFailure", "DiscordSendFailed")
            raise

        try:
            business_payment_reminders_table.update_item(
                Key={DYNAMODB_KEY_UID: uid},
                UpdateExpression=DYNAMODB_UPDATE_EXPRESSION,
                ExpressionAttributeValues={':val': True}
            )
        except Exception as e:
            print(f"Failed to update processed_discord for uid {uid}: {e}")
            emit_metric("PaymentReminderDDB", "UpdateProcessedDiscordException")
            raise

        if net_owed_to:
            try:
                now_utc = datetime.now(timezone.utc).isoformat()
                transaction_type = TRANSACTION_TYPE_DEBIT
                transaction_key = transaction_type + '#' + now_utc
                business_internal_debts_table.put_item(Item={
                    DYNAMODB_KEY_DEBT_TO: net_owed_to,
                    DYNAMODB_KEY_TRANSACTION_KEY: transaction_key,
                    DYNAMODB_KEY_ACTION_BY: BUSINESS_PAYMENT_ACTION_BY,
                    DYNAMODB_KEY_AMOUNT: Decimal(str(net_amount)),
                    DYNAMODB_KEY_TIMESTAMP: now_utc,
                    DYNAMODB_KEY_TRANSACTION_TYPE: transaction_type
                })
            except Exception as e:
                print(f"Failed to record business internal debt owed to {net_owed_to}: {e}")
                emit_metric("BusinessInternalDebtsDDB", "PutDebtException")
                raise

        return {
            'statusCode': HTTP_STATUS_OK,
            'body': json.dumps({
                RESPONSE_KEY_MESSAGE: RESPONSE_MESSAGE_SUCCESS,
                RESPONSE_KEY_RESULTS: {
                    DYNAMODB_KEY_AHSAN_OWES_MUAZ: ahsan_owes_muaz,
                    DYNAMODB_KEY_MUAZ_OWES_AHSAN: muaz_owes_ahsan,
                    DYNAMODB_KEY_NET_AMOUNT: net_amount,
                    DYNAMODB_KEY_NET_OWED_TO: net_owed_to
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

def is_in_time_range(item: Dict, start_time: str, end_time: str) -> bool:
    return start_time <= item.get(DYNAMODB_KEY_TIMESTAMP, '') <= end_time

def get_student_payments_collected_by_partner(transactions: List[Dict], start_time: str, end_time: str) -> Dict[str, float]:
    collected = {partner: 0.0 for partner in PARTNERS}

    for transaction in transactions:
        action_by = transaction.get(DYNAMODB_KEY_ACTION_BY)
        if (transaction.get(DYNAMODB_KEY_TRANSACTION_TYPE) == TRANSACTION_TYPE_CREDIT
                and action_by in PARTNERS
                and is_in_time_range(transaction, start_time, end_time)):
            collected[action_by] += float(transaction.get(DYNAMODB_KEY_AMOUNT, 0))

    return {partner: round(amount, 2) for partner, amount in collected.items()}

def get_tutor_payments_sent_by_partner(tutor_transactions: List[Dict], tutor_names_by_tutor_id: Dict[str, str], start_time: str, end_time: str) -> Dict[str, List[Tuple[str, float]]]:
    payments = {partner: [] for partner in PARTNERS}

    for transaction in tutor_transactions:
        action_by = transaction.get(DYNAMODB_KEY_ACTION_BY)
        if (transaction.get(DYNAMODB_KEY_TRANSACTION_TYPE) == TRANSACTION_TYPE_DEBIT
                and action_by in PARTNERS
                and is_in_time_range(transaction, start_time, end_time)):
            tutor_id = transaction.get(DYNAMODB_KEY_TUTOR_ID)
            tutor_name = tutor_names_by_tutor_id.get(tutor_id, tutor_id)
            payments[action_by].append((tutor_name, round(float(transaction.get(DYNAMODB_KEY_AMOUNT, 0)), 2)))

    return payments

def get_outstanding_owed_to_partner(business_internal_debts: List[Dict]) -> Dict[str, float]:
    outstanding = {partner: 0.0 for partner in PARTNERS}

    for debt in business_internal_debts:
        debt_to = debt.get(DYNAMODB_KEY_DEBT_TO)
        if debt_to not in PARTNERS:
            continue
        amount = float(debt.get(DYNAMODB_KEY_AMOUNT, 0))
        if debt.get(DYNAMODB_KEY_TRANSACTION_TYPE) == TRANSACTION_TYPE_DEBIT:
            outstanding[debt_to] += amount
        elif debt.get(DYNAMODB_KEY_TRANSACTION_TYPE) == TRANSACTION_TYPE_CREDIT:
            outstanding[debt_to] -= amount

    return {partner: round(amount, 2) for partner, amount in outstanding.items()}

def format_owed(owed_to: Union[str, None], amount: float) -> str:
    if not owed_to or round(amount, 2) == 0:
        return "Nothing owed"
    owed_by = PARTNER_MUAZ if owed_to == PARTNER_AHSAN else PARTNER_AHSAN
    return f"{PARTNER_DISPLAY_NAMES[owed_by]} owes {PARTNER_DISPLAY_NAMES[owed_to]} ${amount:.2f}"

def build_message(period_start: str, period_end: str, collected_by_partner: Dict[str, float], tutor_payments_by_partner: Dict[str, List[Tuple[str, float]]],
                  tutor_totals_by_partner: Dict[str, float], net_amount: float, net_owed_to: Union[str, None],
                  total_owed_to_ahsan: float, total_owed_to_muaz: float) -> str:
    lines = [f"Business payments from {period_start} to {period_end}:", ""]

    lines.append(f"Ahsan collected ${collected_by_partner[PARTNER_AHSAN]:.2f} from students → Ahsan owes Muaz ${PARTNER_SPLIT * collected_by_partner[PARTNER_AHSAN]:.2f}")
    lines.append(f"Muaz collected ${collected_by_partner[PARTNER_MUAZ]:.2f} from students → Muaz owes Ahsan ${PARTNER_SPLIT * collected_by_partner[PARTNER_MUAZ]:.2f}")

    for partner in PARTNERS:
        other_partner = PARTNER_MUAZ if partner == PARTNER_AHSAN else PARTNER_AHSAN
        payments = tutor_payments_by_partner[partner]
        itemized = f" ({', '.join(f'{name} ${amount:.2f}' for name, amount in payments)})" if payments else ""
        lines.append(f"{PARTNER_DISPLAY_NAMES[partner]} paid tutors ${tutor_totals_by_partner[partner]:.2f}{itemized} → "
                     f"{PARTNER_DISPLAY_NAMES[other_partner]} owes {PARTNER_DISPLAY_NAMES[partner]} ${PARTNER_SPLIT * tutor_totals_by_partner[partner]:.2f}")

    lines.append("")
    lines.append(f"Net: {format_owed(net_owed_to, net_amount)}")

    total_net = round(total_owed_to_ahsan - total_owed_to_muaz, 2)
    total_owed_to = PARTNER_AHSAN if total_net > 0 else PARTNER_MUAZ if total_net < 0 else None
    lines.append(f"Total outstanding: {format_owed(total_owed_to, abs(total_net))}")

    return "\n".join(lines)

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
