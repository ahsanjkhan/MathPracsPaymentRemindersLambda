import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Tuple, Union
from zoneinfo import ZoneInfo
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

import boto3
import httpx
from aws_lambda_typing import context as lambda_context

def lambda_handler(event: Dict[str, Union[str, int, float, bool, None]], context: lambda_context.Context) -> Dict[str, Union[str, int]]:
    try:
        print(f"Received Event: {event}")
        print(f"Received Context: {context}")

        student_payment_reminders_table_name = os.environ.get('STUDENT_PAYMENT_TABLE_NAME')

        # Cross stack environment variables
        sessions_table_name = os.environ.get('SESSIONS_TABLE_NAME')
        students_table_name = os.environ.get('STUDENTS_TABLE_NAME')
        students_metadata_table_name = os.environ.get('STUDENTS_METADATA_TABLE_NAME')
        discord_secret_arn = os.environ.get('DISCORD_SECRETS_ARN')

        # Cross stack tables
        dynamodb = boto3.resource('dynamodb')
        sessions_table = dynamodb.Table(sessions_table_name)
        students_table = dynamodb.Table(students_table_name)
        students_metadata_table = dynamodb.Table(students_metadata_table_name)
        
        week_start, week_end = get_previous_week_range()
        print(f"Processing week: {week_start} to {week_end}")

        all_sessions = scan_all_items_from_db(sessions_table)
        
        session_name_to_total_minutes = get_last_week_sessions_to_minutes_mapping(all_sessions, week_start, week_end)
        print(f"Found {len(session_name_to_total_minutes)} unique session names in last week's sessions")
        print(f"Unique session names: {list(session_name_to_total_minutes.keys())}")

        dynamodb = boto3.resource('dynamodb')
        student_payment_reminders_table = dynamodb.Table(student_payment_reminders_table_name)

        students = scan_all_items_from_db(students_metadata_table)

        secrets_client = boto3.client('secretsmanager')
        discord_secret_response = secrets_client.get_secret_value(SecretId=discord_secret_arn)
        discord_creds = json.loads(discord_secret_response['SecretString'])
        discord_bot_token = discord_creds['bot_token']
        
        results = []
        for student in students:
            print(f"Processing student: {student.get('studentName')}")
            discord_channel_id = student.get('discordChannelReminderId')
            expected_session_name_for_student = student.get('studentName') + ' Tutoring'
            hourly_rate = 0

            total_session_minutes = 0
            total_session_hours = 0
            total_due_for_sessions = 0

            total_no_show_minutes = 0
            total_no_show_hours = 0
            total_due_for_no_shows = 0

            no_show_rate = 0
            for session_name, minutes in session_name_to_total_minutes.items():
                if is_no_show_event(expected_session_name_for_student, session_name):
                    total_no_show_minutes += minutes
            
            if total_no_show_minutes > 0:
                total_no_show_hours = total_no_show_minutes / 60.0

            if expected_session_name_for_student in session_name_to_total_minutes:
                total_session_minutes = session_name_to_total_minutes[expected_session_name_for_student]
                total_session_hours = total_session_minutes / 60.0

                student_pricing_map = student.get('hourlyPricing')
                
                if total_session_hours < 2:
                    hourly_rate = float(student_pricing_map.get('1'))
                elif total_session_hours < 3:
                    hourly_rate = float(student_pricing_map.get('2'))
                elif total_session_hours < 4:
                    hourly_rate = float(student_pricing_map.get('3'))
                elif total_session_hours < 5:
                    hourly_rate = float(student_pricing_map.get('4'))
                else:
                    hourly_rate = float(student_pricing_map.get('5'))

                total_due_for_sessions = total_session_hours * hourly_rate

            if total_no_show_hours > 0:
                no_show_rate = float(student.get('noShowCustomRate')) if student.get('noShowCustomRate') else hourly_rate * 0.5
                total_due_for_no_shows = total_no_show_hours * no_show_rate

            if total_due_for_sessions > 0 or total_due_for_no_shows > 0:
                if total_due_for_sessions > 0 and total_due_for_no_shows > 0:
                    calculation = f"({hourly_rate:.0f}\*{total_session_hours:.1f} for sessions + {no_show_rate:.0f}\*{total_no_show_hours:.1f} for no-shows)"
                elif total_due_for_sessions > 0 and total_due_for_no_shows <= 0:
                    calculation = f"({hourly_rate:.0f}\*{total_session_hours:.1f})"
                else:
                    calculation = f"({no_show_rate:.0f}\*{total_no_show_hours:.1f} for no-shows)"

                total_amount_due = total_due_for_sessions + total_due_for_no_shows
                
                uid = f"{expected_session_name_for_student}#{week_start}#{week_end}"

                count_discord_messages = 0
                
                try:
                    response = student_payment_reminders_table.get_item(Key={'uid': uid})
                    if 'Item' in response and response['Item'].get('processed_discord'):
                        continue
                    else:
                        student_payment_reminders_table.put_item(Item={
                            'uid': uid,
                            'event_name': expected_session_name_for_student,
                            'week_start': week_start,
                            'week_end': week_end,
                            'session_minutes': total_session_minutes,
                            'no_show_minutes': total_no_show_minutes,
                            'amount_due': Decimal(str(total_amount_due)),
                            'processed_sms': False,
                            'processed_discord': False
                        })

                        message_body = f"Hello, the total due for {expected_session_name_for_student} with MathPracs for last week ({week_start} to {week_end}) is ${total_amount_due:.2f} {calculation}."

                        print(f"Sending Discord message: {message_body}")
                        try:
                            send_discord_message(discord_bot_token, discord_channel_id, message_body)
                            student_payment_reminders_table.update_item(
                                Key={'uid': uid},
                                UpdateExpression='SET processed_discord = :val',
                                ExpressionAttributeValues={':val': True}
                            )
                            count_discord_messages += 1
                        except Exception as e:
                            print(f"Failed to send Discord message: {e}")

                except Exception as e:
                    print(f"Error processing DDB update with uid: {uid}. Exception: {e}")
                
                results.append({
                    'event_name': expected_session_name_for_student,
                    'session_minutes': total_session_minutes,
                    'no_show_minutes': total_no_show_minutes,
                    'amount_due': total_amount_due,
                    'discord_messages_sent': count_discord_messages
                })
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'MathPracs Student Payment Reminder executed successfully',
                'results': results
            })
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
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

def get_previous_week_range() -> Tuple[str, str]:
    today = datetime.now()
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday + 7)
    last_saturday = last_sunday + timedelta(days=6)
    
    return last_sunday.strftime('%Y-%m-%d'), last_saturday.strftime('%Y-%m-%d')

def get_last_week_sessions_to_minutes_mapping(sessions: List[Dict], start_date: str, end_date: str) -> Dict[str, int]:
    chicago_tz = ZoneInfo('America/Chicago')
    start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(hour=0, minute=0, second=0, tzinfo=chicago_tz)
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=chicago_tz)

    start_time = start_dt.astimezone(timezone.utc).isoformat()
    end_time = end_dt.astimezone(timezone.utc).isoformat()

    sessions_in_date_range = [s for s in sessions if start_time <= s.get('utcStart', '') <= end_time]

    session_name_to_total_minutes = {}

    for session in sessions_in_date_range:
        summary = session.get('summary')
        start_time_dt = datetime.fromisoformat(session.get('utcStart'))
        end_time_dt = datetime.fromisoformat(session.get('utcEnd'))
        duration_minutes = int((end_time_dt - start_time_dt).total_seconds() / 60.0)
        if summary in session_name_to_total_minutes:
            session_name_to_total_minutes[summary] += duration_minutes
        else:
            session_name_to_total_minutes[summary] = duration_minutes

    return session_name_to_total_minutes

def is_no_show_event(standard_session_name: str, session_name: str) -> bool:
    """
    Check if session_name is a no-show for the given standard_session_name.
    standard_session_name: "Joe Tutoring"
    session_name: "Joe Tutoring (no-show)" or "Joe Tutoring no-show" etc.
    """
    normalized_session = re.sub(r'[^\w\s]', '', session_name.lower())
    normalized_base = re.sub(r'[^\w\s]', '', standard_session_name.lower())
    
    no_show_pattern = rf'^{re.escape(normalized_base)}\s+no\s*show'
    return bool(re.search(no_show_pattern, normalized_session))

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