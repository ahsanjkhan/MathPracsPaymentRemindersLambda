import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Tuple, Union
from zoneinfo import ZoneInfo

import boto3
from aws_lambda_typing import context as lambda_context
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource
from googleapiclient.discovery import build
from twilio.rest import Client


def lambda_handler(event: Dict[str, Union[str, int, float, bool, None]], context: lambda_context.Context) -> Dict[str, Union[str, int]]:
    try:
        print(f"Received Event: {event}")
        print(f"Received Context: {context}")

        table_name = os.environ.get('TUTOR_PAYMENT_TABLE_NAME')
        secrets_arn = os.environ.get('SECRETS_ARN')
        
        secrets = get_secrets(secrets_arn)
        month_start, month_end = get_previous_month_range()
        
        calendar_service, _ = get_calendar_service(json.loads(secrets['googleCalendarOAuthCredentials']), secrets_arn)
        sheets_service = get_sheets_service(secrets['googleSheetsCredentials'])
        sheet_data = get_sheet_data(sheets_service)
        
        valid_event_names = {row['event_name'] for row in sheet_data}
        phone_numbers = list(set([row['phone_numbers'][0] for row in sheet_data if row['phone_numbers']]))
        
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)
        twilio_client = Client(secrets['twilioAccountSid'], secrets['twilioAuthToken'])
        
        results = []
        calendar_list = calendar_service.calendarList().list().execute()
        
        for calendar_item in calendar_list.get('items', []):
            calendar_name = calendar_item['summary']
            calendar_id = calendar_item['id']

            session_minutes = get_calendar_events_for_month(calendar_service, calendar_id, month_start, month_end, valid_event_names)
            no_show_minutes = get_calendar_no_shows_for_month(calendar_service, calendar_id, month_start, month_end, valid_event_names)

            if session_minutes > 0 or no_show_minutes > 0:
                session_hours = session_minutes / 60.0
                no_show_hours = no_show_minutes / 60.0
                amount_due = (session_hours * 10) + (no_show_hours * 10)
                
                uid = f"{calendar_name}#{month_start}#{month_end}"
                
                try:
                    response = table.get_item(Key={'uid': uid})
                    if 'Item' in response and response['Item']['processed_sms']:
                        continue
                    else:
                        table.put_item(Item={
                            'uid': uid,
                            'calendar_name': calendar_name,
                            'month_start': month_start,
                            'month_end': month_end,
                            'session_minutes': session_minutes,
                            'no_show_minutes': no_show_minutes,
                            'amount_due': Decimal(str(amount_due)),
                            'processed_sms': False
                        })
                        
                        message_body = f"The total payment for {calendar_name} from {month_start} to {month_end} due is ${amount_due:.2f} (10*{session_hours:.1f} for sessions + 10*{no_show_hours:.1f} for no-shows)."
                        
                        any_sent = False
                        for phone in phone_numbers:
                            print(f"Sending message {message_body} to {phone}")
                            try:
                                twilio_client.messages.create(
                                    body=message_body,
                                    from_=secrets['twilioPhoneNumber'],
                                    to=phone,
                                    messaging_service_sid=None
                                )
                                any_sent = True
                            except Exception as e:
                                print(f"Failed to send SMS to {phone}: {e}")
                        
                        if any_sent:
                            table.update_item(
                                Key={'uid': uid},
                                UpdateExpression='SET processed_sms = :val',
                                ExpressionAttributeValues={':val': True}
                            )
                except Exception as e:
                    print(f"Error processing DDB update with uid: {uid}. Exception: {e}")
                
                results.append({
                    'calendar_name': calendar_name,
                    'session_minutes': session_minutes,
                    'no_show_minutes': no_show_minutes,
                    'amount_due': amount_due,
                    'sms_sent': len(phone_numbers)
                })
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'MathPracs Tutor Payment Reminder executed successfully',
                'results': results
            })
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }

def get_secrets(secrets_arn: str) -> Dict[str, str]:
    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId=secrets_arn)
    return json.loads(response['SecretString'])

def get_previous_month_range() -> Tuple[str, str]:
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_of_previous_month = first_of_this_month - timedelta(days=1)
    first_of_previous_month = last_of_previous_month.replace(day=1)
    
    return first_of_previous_month.strftime('%Y-%m-%d'), last_of_previous_month.strftime('%Y-%m-%d')

def get_calendar_service(oauth_credentials: Dict[str, str], secrets_arn: str) -> Tuple[Resource, bool]:
    credentials = Credentials(
        token=oauth_credentials['access_token'],
        refresh_token=oauth_credentials['refresh_token'],
        token_uri=oauth_credentials['token_uri'],
        client_id=oauth_credentials['client_id'],
        client_secret=oauth_credentials['client_secret'],
        scopes=['https://www.googleapis.com/auth/calendar.readonly']
    )
    
    refreshed = False
    if credentials.expired:
        credentials.refresh(Request())
        refreshed = True
        update_oauth_tokens(secrets_arn, credentials.token, credentials.refresh_token)
    
    return build('calendar', 'v3', credentials=credentials), refreshed

def get_calendar_no_shows_for_month(service: Resource, calendar_id: str, start_date: str, end_date: str, valid_event_names: set) -> int:
    no_show_search_term = '(no-show)'
    total_minutes = 0

    chicago_tz = ZoneInfo('America/Chicago')
    start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(hour=0, minute=0, second=0, tzinfo=chicago_tz)
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=chicago_tz)

    start_time = start_dt.isoformat()
    end_time = end_dt.isoformat()

    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=start_time,
            timeMax=end_time,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        for event in events_result.get('items', []):
            if 'summary' not in event or 'start' not in event or 'end' not in event:
                continue

            event_name = event['summary']

            if no_show_search_term in event_name and any(valid_name in event_name for valid_name in valid_event_names):
                start_time_dt = datetime.fromisoformat(event['start'].get('dateTime', event['start'].get('date')))
                end_time_dt = datetime.fromisoformat(event['end'].get('dateTime', event['end'].get('date')))
                duration_minutes = int((end_time_dt - start_time_dt).total_seconds() / 60.0)
                total_minutes += duration_minutes

    except Exception:
        pass

    return total_minutes

def get_calendar_events_for_month(service: Resource, calendar_id: str, start_date: str, end_date: str, valid_event_names: set) -> int:
    total_minutes = 0
    
    chicago_tz = ZoneInfo('America/Chicago')
    start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(hour=0, minute=0, second=0, tzinfo=chicago_tz)
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=chicago_tz)
    
    start_time = start_dt.isoformat()
    end_time = end_dt.isoformat()
    
    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=start_time,
            timeMax=end_time,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        for event in events_result.get('items', []):
            if 'summary' not in event or 'start' not in event or 'end' not in event:
                continue
                
            event_name = event['summary']
            
            if event_name in valid_event_names:
                start_time_dt = datetime.fromisoformat(event['start'].get('dateTime', event['start'].get('date')))
                end_time_dt = datetime.fromisoformat(event['end'].get('dateTime', event['end'].get('date')))
                duration_minutes = int((end_time_dt - start_time_dt).total_seconds() / 60.0)
                total_minutes += duration_minutes
                
    except Exception:
        pass
    
    return total_minutes

def get_sheets_service(credentials_json: str) -> Resource:
    credentials_dict = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )
    return build('sheets', 'v4', credentials=credentials)

def update_oauth_tokens(secrets_arn: str, access_token: str, refresh_token: str) -> None:
    client = boto3.client('secretsmanager')
    secret = json.loads(client.get_secret_value(SecretId=secrets_arn)['SecretString'])
    oauth_creds = json.loads(secret['googleCalendarOAuthCredentials'])
    oauth_creds['access_token'] = access_token
    oauth_creds['refresh_token'] = refresh_token
    secret['googleCalendarOAuthCredentials'] = json.dumps(oauth_creds)
    client.update_secret(SecretId=secrets_arn, SecretString=json.dumps(secret))

def get_sheet_data(service: Resource) -> List[Dict[str, Union[str, float, List[str]]]]:
    spreadsheet_id = '1-7aLNLkeUJmolMjaLVdjxjCa49fQxwWfwJ6aVfi0YSw'
    range_name = 'Sheet1!A:O'

    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name
        ).execute()

        values = result.get('values', [])
        sheet_data = []

        for i, row in enumerate(values[1:], 1):
            if len(row) >= 15:
                event_name = row[0]
                google_doc_link = row[1]
                phone1 = row[2]
                phone2 = row[3]
                phone3 = row[4]
                phone4 = row[5] # Muaz
                phone5 = row[6] # Ahsan
                phone6 = row[7]
                phone7 = row[8]
                standard_hourly_rate = float(row[9])
                hourly_1_hour_rate = float(row[10])
                hourly_2_hour_rate = float(row[11])
                hourly_3_hour_rate = float(row[12])
                hourly_4_hour_rate = float(row[13])
                hourly_5_hour_rate = float(row[14])

                # phone_numbers = [phone4, phone5]
                phone_numbers = [phone5]

                sheet_data.append({
                    'event_name': event_name,
                    'google_doc_link': google_doc_link,
                    'standard_hourly_rate': standard_hourly_rate,
                    'hourly_1_rate': hourly_1_hour_rate,
                    'hourly_2_rate': hourly_2_hour_rate,
                    'hourly_3_rate': hourly_3_hour_rate,
                    'hourly_4_rate': hourly_4_hour_rate,
                    'hourly_5_rate': hourly_5_hour_rate,
                    'phone_numbers': phone_numbers
                })

        return sheet_data

    except Exception:
        return []