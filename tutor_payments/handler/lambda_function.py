import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Tuple, Union
from zoneinfo import ZoneInfo

import boto3
import httpx
from aws_lambda_typing import context as lambda_context

from .constants import *


def lambda_handler(event: Dict[str, Union[str, int, float, bool, None]], context: lambda_context.Context) -> Dict[str, Union[str, int]]:
    try:
        print(f"Received Event")

        tutors_payment_reminders_table_name = os.environ.get(ENV_TUTOR_PAYMENT_TABLE_NAME)

        # Cross stack environment variables
        sessions_table_name = os.environ.get(IMPORTED_TUTOR_PAYMENT_LAMBDA_ENV_VAR_KEY_SESSIONS_TABLE_NAME)
        students_table_name = os.environ.get(IMPORTED_TUTOR_PAYMENT_LAMBDA_ENV_VAR_KEY_STUDENTS_TABLE_NAME)
        students_metadata_table_name = os.environ.get(IMPORTED_TUTOR_PAYMENT_LAMBDA_ENV_VAR_KEY_STUDENTS_METADATA_TABLE_NAME)
        tutors_table_name = os.environ.get(IMPORTED_TUTOR_PAYMENT_LAMBDA_ENV_VAR_KEY_TUTORS_TABLE_NAME)
        tutors_metadata_table_name = os.environ.get(IMPORTED_TUTOR_PAYMENT_LAMBDA_ENV_VAR_KEY_TUTORS_METADATA_TABLE_NAME)
        discord_secret_arn = os.environ.get(IMPORTED_TUTOR_PAYMENT_LAMBDA_ENV_VAR_KEY_DISCORD_API_SECRETS_ARN)

        # Cross stack tables
        dynamodb = boto3.resource(AWS_SERVICE_DYNAMODB)
        sessions_table = dynamodb.Table(sessions_table_name)
        students_table = dynamodb.Table(students_table_name)
        students_metadata_table = dynamodb.Table(students_metadata_table_name)
        tutors_table = dynamodb.Table(tutors_table_name)
        tutors_metadata_table = dynamodb.Table(tutors_metadata_table_name)

        month_start, month_end = get_previous_month_range()

        valid_event_names = {(student.get('studentName') + ' Tutoring') for student in scan_all_items_from_db(students_metadata_table)}
        sessions = scan_all_items_from_db(sessions_table)

        tutor_payment_reminders_table = dynamodb.Table(tutors_payment_reminders_table_name)

        secrets_client = boto3.client(AWS_SERVICE_SECRETSMANAGER)
        discord_secret_response = secrets_client.get_secret_value(SecretId=discord_secret_arn)
        discord_creds = json.loads(discord_secret_response['SecretString'])
        discord_bot_token = discord_creds['bot_token']
        discord_channel_id = discord_creds['tutor_payment_channel_id']

        results = []

        tutors_metadata = scan_all_items_from_db(tutors_metadata_table)

        for tutor in tutors_metadata:
            tutor_id = tutor.get('tutorId')
            tutor_salary_rate = float(tutor.get('hourlyRate'))
            tutor_calendar_name = tutor.get('displayName')
            session_minutes = get_tutor_sessions_for_month(sessions, tutor_id, month_start, month_end, valid_event_names)
            no_show_minutes = get_tutor_no_shows_for_month(sessions, tutor_id, month_start, month_end, valid_event_names)

            if session_minutes > 0 or no_show_minutes > 0:
                session_hours = session_minutes / MINUTES_PER_HOUR
                no_show_hours = no_show_minutes / MINUTES_PER_HOUR

                amount_due = (session_hours * tutor_salary_rate) + (no_show_hours * tutor_salary_rate)
                
                uid = f"{tutor_calendar_name}#{month_start}#{month_end}"
                
                try:
                    response = tutor_payment_reminders_table.get_item(Key={DYNAMODB_KEY_UID: uid})
                    if DYNAMODB_KEY_ITEM in response and response[DYNAMODB_KEY_ITEM].get(DYNAMODB_KEY_PROCESSED_DISCORD):
                        continue
                    else:
                        tutor_payment_reminders_table.put_item(Item={
                            DYNAMODB_KEY_UID: uid,
                            DYNAMODB_KEY_CALENDAR_NAME: tutor_calendar_name,
                            DYNAMODB_KEY_MONTH_START: month_start,
                            DYNAMODB_KEY_MONTH_END: month_end,
                            DYNAMODB_KEY_SESSION_MINUTES: session_minutes,
                            DYNAMODB_KEY_NO_SHOW_MINUTES: no_show_minutes,
                            DYNAMODB_KEY_AMOUNT_DUE: Decimal(str(amount_due)),
                            DYNAMODB_KEY_PROCESSED_SMS: False,
                            DYNAMODB_KEY_PROCESSED_DISCORD: False
                        })
                        
                        message_body = f"The total payment for {tutor_calendar_name} from {month_start} to {month_end} due is ${amount_due:.2f} ({tutor_salary_rate}\*{session_hours:.1f} for sessions + 10.0\*{no_show_hours:.1f} for no-shows)."
                        
                        print(f"Sending Discord message: {message_body}")
                        try:
                            response = httpx.post(
                                f"https://discord.com/api/v10/channels/{discord_channel_id}/messages",
                                headers={
                                    "Authorization": f"Bot {discord_bot_token}",
                                    "Content-Type": "application/json"
                                },
                                json={"content": message_body},
                                timeout=30.0
                            )
                            if response.status_code == 200:
                                tutor_payment_reminders_table.update_item(
                                    Key={DYNAMODB_KEY_UID: uid},
                                    UpdateExpression=DYNAMODB_UPDATE_EXPRESSION,
                                    ExpressionAttributeValues={':val': True}
                                )
                        except Exception as e:
                            print(f"Failed to send Discord message: {e}")
                except Exception as e:
                    print(f"Error processing DDB update with uid: {uid}. Exception: {e}")
                
                results.append({
                    DYNAMODB_KEY_CALENDAR_NAME: tutor_calendar_name,
                    DYNAMODB_KEY_SESSION_MINUTES: session_minutes,
                    DYNAMODB_KEY_NO_SHOW_MINUTES: no_show_minutes,
                    DYNAMODB_KEY_AMOUNT_DUE: amount_due
                })
        
        return {
            'statusCode': HTTP_STATUS_OK,
            'body': json.dumps({
                RESPONSE_KEY_MESSAGE: RESPONSE_MESSAGE_SUCCESS,
                RESPONSE_KEY_RESULTS: results
            })
        }
        
    except Exception as e:
        return {
            'statusCode': HTTP_STATUS_ERROR,
            'body': json.dumps({RESPONSE_KEY_ERROR: str(e)})
        }

def get_previous_month_range() -> Tuple[str, str]:
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_of_previous_month = first_of_this_month - timedelta(days=1)
    first_of_previous_month = last_of_previous_month.replace(day=1)
    
    return first_of_previous_month.strftime(DATE_FORMAT), last_of_previous_month.strftime(DATE_FORMAT)

def get_tutor_no_shows_for_month(sessions: List[Dict], tutor_id: str, start_date: str, end_date: str, valid_event_names: set) -> int:
    total_minutes = 0

    chicago_tz = ZoneInfo(TIMEZONE_CHICAGO)
    start_dt = datetime.strptime(start_date, DATE_FORMAT).replace(hour=0, minute=0, second=0, tzinfo=chicago_tz)
    end_dt = datetime.strptime(end_date, DATE_FORMAT).replace(hour=23, minute=59, second=59, tzinfo=chicago_tz)

    start_time = start_dt.astimezone(timezone.utc).isoformat()
    end_time = end_dt.astimezone(timezone.utc).isoformat()

    all_sessions = sessions
    sessions_for_tutor = [s for s in all_sessions if s.get('tutorId') == tutor_id]
    sessions_in_date_range = [s for s in sessions_for_tutor if start_time <= s.get('utcStart', '') <= end_time]

    for session in sessions_in_date_range:
        if 'summary' not in session or 'utcStart' not in session or 'utcEnd' not in session:
            continue

        event_name = session.get('summary')

        if any(is_no_show_event(valid_name, event_name) for valid_name in valid_event_names):
            start_time_dt = datetime.fromisoformat(session.get('utcStart'))
            end_time_dt = datetime.fromisoformat(session.get('utcEnd'))
            duration_minutes = int((end_time_dt - start_time_dt).total_seconds() / MINUTES_PER_HOUR)
            total_minutes += duration_minutes

    return total_minutes

def get_tutor_sessions_for_month(sessions: List[Dict], tutor_id: str, start_date: str, end_date: str, valid_event_names: set) -> int:
    total_minutes = 0

    chicago_tz = ZoneInfo(TIMEZONE_CHICAGO)
    start_dt = datetime.strptime(start_date, DATE_FORMAT).replace(hour=0, minute=0, second=0, tzinfo=chicago_tz)
    end_dt = datetime.strptime(end_date, DATE_FORMAT).replace(hour=23, minute=59, second=59, tzinfo=chicago_tz)

    start_time = start_dt.astimezone(timezone.utc).isoformat()
    end_time = end_dt.astimezone(timezone.utc).isoformat()

    all_sessions = sessions
    sessions_for_tutor = [s for s in all_sessions if s.get('tutorId') == tutor_id]
    sessions_in_date_range = [s for s in sessions_for_tutor if start_time <= s.get('utcStart', '') <= end_time]

    for session in sessions_in_date_range:
        if 'summary' not in session or 'utcStart' not in session or 'utcEnd' not in session:
            continue

        event_name = session.get('summary')

        if is_valid_session_event(event_name, valid_event_names):
            start_time_dt = datetime.fromisoformat(session.get('utcStart'))
            end_time_dt = datetime.fromisoformat(session.get('utcEnd'))
            duration_minutes = int((end_time_dt - start_time_dt).total_seconds() / MINUTES_PER_HOUR)
            total_minutes += duration_minutes

    return total_minutes

def is_no_show_event(event_name: str, calendar_event_name: str) -> bool:
    """
    Check if calendar event is a no-show for the given event.
    event_name: "Joe Tutoring"
    calendar_event_name: "Joe Tutoring (no-show)" or "Joe Tutoring no-show" etc.
    """
    normalized_event = re.sub(r'[^\w\s]', '', calendar_event_name.lower())
    normalized_base = re.sub(r'[^\w\s]', '', event_name.lower())
    
    no_show_pattern = rf'^{re.escape(normalized_base)}\s+no\s*show'
    return bool(re.search(no_show_pattern, normalized_event))

def is_valid_session_event(calendar_event_name: str, valid_event_names: set) -> bool:
    """
    Check if calendar event is a valid session (including demos).
    Matches case-insensitively and includes demo sessions.
    calendar_event_name: "Joe Tutoring" or "Joe Tutoring Demo" etc.
    """
    normalized_event = re.sub(r'[^\w\s]', '', calendar_event_name.lower())
    
    # Remove 'demo' suffix if present for matching
    normalized_event_base = re.sub(r'\s+demo$', '', normalized_event).strip()
    
    # Check if matches any valid event name (case-insensitive)
    for valid_name in valid_event_names:
        normalized_valid = re.sub(r'[^\w\s]', '', valid_name.lower())
        if normalized_event == normalized_valid or normalized_event_base == normalized_valid:
            return True
    
    return False

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