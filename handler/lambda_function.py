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

        table_name = os.environ.get('PAYMENT_TABLE_NAME')
        secrets_arn = os.environ.get('SECRETS_ARN')
        
        secrets = get_secrets(secrets_arn)
        week_start, week_end = get_previous_week_range()
        
        calendar_service = get_calendar_service(json.loads(secrets['googleCalendarOAuthCredentials']))
        event_name_to_total_minutes = get_all_calendar_events(calendar_service, week_start, week_end)
        
        sheets_service = get_sheets_service(secrets['googleSheetsCredentials'])
        sheet_data = get_sheet_data(sheets_service)
        
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(table_name)
        twilio_client = Client(secrets['twilioAccountSid'], secrets['twilioAuthToken'])
        
        results = []
        for row in sheet_data:
            event_name = row['event_name']
            hourly_rate = row['standard_hourly_rate']
            phone_numbers = row['phone_numbers']
            
            if event_name in event_name_to_total_minutes:
                total_minutes = event_name_to_total_minutes[event_name]
                total_hours = total_minutes / 60.0
                
                if total_hours < 2:
                    hourly_rate = row['hourly_1_rate']
                elif total_hours < 3:
                    hourly_rate = row['hourly_2_rate']
                elif total_hours < 4:
                    hourly_rate = row['hourly_3_rate']
                elif total_hours < 5:
                    hourly_rate = row['hourly_4_rate']
                else:
                    hourly_rate = row['hourly_5_rate']
                
                amount_due = total_hours * hourly_rate
                
                uid = f"{event_name}#{week_start}#{week_end}"
                
                try:
                    response = table.get_item(Key={'uid': uid})
                    if 'Item' in response and response['Item']['processed_sms']:
                        continue
                    else:
                        table.put_item(Item={
                            'uid': uid,
                            'event_name': event_name,
                            'week_start': week_start,
                            'week_end': week_end,
                            'minutes': total_minutes,
                            'amount_due': Decimal(str(amount_due)),
                            'processed_sms': False
                        })
                        
                        calculation = f"({hourly_rate:.0f}*{total_hours:.1f})"
                        message_body = (f"Hello, the total due for {event_name} with MathPracs for last week ({week_start} to {week_end}) is ${amount_due:.2f} {calculation}.\n\n"
                                        f"Payment info: https://docs.google.com/document/d/1eR0Ld4fyhbk7xHOeg4_YRCP3Ybk3eE6Xyxb5hFCWDgU/edit?usp=drive_link")
                        
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
                    'event_name': event_name,
                    'minutes': total_minutes,
                    'amount_due': amount_due,
                    'sms_sent': len([p for p in phone_numbers if p])
                })
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'MathPracs Payment Reminder executed successfully',
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

def get_previous_week_range() -> Tuple[str, str]:
    today = datetime.now()
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday + 7)
    last_saturday = last_sunday + timedelta(days=6)
    
    return last_sunday.strftime('%Y-%m-%d'), last_saturday.strftime('%Y-%m-%d')

def get_calendar_service(oauth_credentials: Dict[str, str]) -> Resource:
    credentials = Credentials(
        token=oauth_credentials['access_token'],
        refresh_token=oauth_credentials['refresh_token'],
        token_uri=oauth_credentials['token_uri'],
        client_id=oauth_credentials['client_id'],
        client_secret=oauth_credentials['client_secret'],
        scopes=['https://www.googleapis.com/auth/calendar.readonly']
    )
    
    if credentials.expired:
        credentials.refresh(Request())
    
    return build('calendar', 'v3', credentials=credentials)

def get_all_calendar_events(service: Resource, start_date: str, end_date: str) -> Dict[str, int]:
    events = {}
    
    chicago_tz = ZoneInfo('America/Chicago')
    start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(hour=0, minute=0, second=0, tzinfo=chicago_tz)
    end_dt = datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59, tzinfo=chicago_tz)
    
    start_time = start_dt.isoformat()
    end_time = end_dt.isoformat()
    
    try:
        calendar_list = service.calendarList().list().execute()
    except Exception:
        return events
    
    for calendar_item in calendar_list.get('items', []):
        calendar_id = calendar_item['id']
        
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
                
                start_time_dt = datetime.fromisoformat(event['start'].get('dateTime', event['start'].get('date')))
                end_time_dt = datetime.fromisoformat(event['end'].get('dateTime', event['end'].get('date')))
                duration_minutes = int((end_time_dt - start_time_dt).total_seconds() / 60.0)
                
                if event_name in events:
                    events[event_name] += duration_minutes
                else:
                    events[event_name] = duration_minutes
                    
        except Exception:
            continue
    
    return events

def get_sheets_service(credentials_json: str) -> Resource:
    credentials_dict = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )
    return build('sheets', 'v4', credentials=credentials)

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