import json
import os
from .constants import *
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
        print(f"Received Event")

        table_name = os.environ.get(ENV_TUTOR_PAYMENT_TABLE_NAME)
        secrets_arn = os.environ.get(ENV_SECRETS_ARN)
        
        secrets = get_secrets(secrets_arn)
        month_start, month_end = get_previous_month_range()
        
        calendar_service, _ = get_calendar_service(json.loads(secrets[SECRET_KEY_GOOGLE_CALENDAR_OAUTH]), secrets_arn)
        sheets_service = get_sheets_service(secrets[SECRET_KEY_GOOGLE_SHEETS])
        sheet_data = get_sheet_data(sheets_service)
        
        valid_event_names = {row[SHEET_KEY_EVENT_NAME] for row in sheet_data}
        phone_numbers = list(set([phone for row in sheet_data for phone in row[SHEET_KEY_PHONE_NUMBERS]]))

        tutor_salary_rate_ssm_name = os.environ.get(ENV_TUTOR_SALARY_RATE_SSM_NAME)
        tutor_salary_rate = float(get_ssm_string_value(tutor_salary_rate_ssm_name))

        dynamodb = boto3.resource(AWS_SERVICE_DYNAMODB)
        table = dynamodb.Table(table_name)
        twilio_client = Client(secrets[SECRET_KEY_TWILIO_ACCOUNT_SID], secrets[SECRET_KEY_TWILIO_AUTH_TOKEN])
        
        results = []
        calendar_list = calendar_service.calendarList().list().execute()
        
        for calendar_item in calendar_list.get(CALENDAR_ITEMS_KEY, []):
            calendar_name = calendar_item[CALENDAR_SUMMARY_KEY]
            calendar_id = calendar_item[CALENDAR_ID_KEY]

            session_minutes = get_calendar_events_for_month(calendar_service, calendar_id, month_start, month_end, valid_event_names)
            no_show_minutes = get_calendar_no_shows_for_month(calendar_service, calendar_id, month_start, month_end, valid_event_names)

            if session_minutes > 0 or no_show_minutes > 0:
                session_hours = session_minutes / MINUTES_PER_HOUR
                no_show_hours = no_show_minutes / MINUTES_PER_HOUR

                amount_due = (session_hours * tutor_salary_rate) + (no_show_hours * tutor_salary_rate)
                
                uid = f"{calendar_name}#{month_start}#{month_end}"
                
                try:
                    response = table.get_item(Key={DYNAMODB_KEY_UID: uid})
                    if DYNAMODB_KEY_ITEM in response and response[DYNAMODB_KEY_ITEM][DYNAMODB_KEY_PROCESSED_SMS]:
                        continue
                    else:
                        table.put_item(Item={
                            DYNAMODB_KEY_UID: uid,
                            DYNAMODB_KEY_CALENDAR_NAME: calendar_name,
                            DYNAMODB_KEY_MONTH_START: month_start,
                            DYNAMODB_KEY_MONTH_END: month_end,
                            DYNAMODB_KEY_SESSION_MINUTES: session_minutes,
                            DYNAMODB_KEY_NO_SHOW_MINUTES: no_show_minutes,
                            DYNAMODB_KEY_AMOUNT_DUE: Decimal(str(amount_due)),
                            DYNAMODB_KEY_PROCESSED_SMS: False
                        })
                        
                        message_body = f"The total payment for {calendar_name} from {month_start} to {month_end} due is ${amount_due:.2f} ({tutor_salary_rate}*{session_hours:.1f} for sessions + {tutor_salary_rate}*{no_show_hours:.1f} for no-shows)."
                        
                        any_sent = False
                        for phone in phone_numbers:
                            print(f"Sending message {message_body} to {phone}")
                            try:
                                twilio_client.messages.create(
                                    body=message_body,
                                    from_=secrets[SECRET_KEY_TWILIO_PHONE_NUMBER],
                                    to=phone,
                                    messaging_service_sid=None
                                )
                                any_sent = True
                            except Exception as e:
                                print(f"Failed to send SMS to {phone}: {e}")
                        
                        if any_sent:
                            table.update_item(
                                Key={DYNAMODB_KEY_UID: uid},
                                UpdateExpression=DYNAMODB_UPDATE_EXPRESSION,
                                ExpressionAttributeValues={':val': True}
                            )
                except Exception as e:
                    print(f"Error processing DDB update with uid: {uid}. Exception: {e}")
                
                results.append({
                    DYNAMODB_KEY_CALENDAR_NAME: calendar_name,
                    DYNAMODB_KEY_SESSION_MINUTES: session_minutes,
                    DYNAMODB_KEY_NO_SHOW_MINUTES: no_show_minutes,
                    DYNAMODB_KEY_AMOUNT_DUE: amount_due,
                    'sms_sent': len(phone_numbers)
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

def get_secrets(secrets_arn: str) -> Dict[str, str]:
    client = boto3.client(AWS_SERVICE_SECRETSMANAGER)
    response = client.get_secret_value(SecretId=secrets_arn)
    return json.loads(response['SecretString'])

def get_previous_month_range() -> Tuple[str, str]:
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_of_previous_month = first_of_this_month - timedelta(days=1)
    first_of_previous_month = last_of_previous_month.replace(day=1)
    
    return first_of_previous_month.strftime(DATE_FORMAT), last_of_previous_month.strftime(DATE_FORMAT)

def get_calendar_service(oauth_credentials: Dict[str, str], secrets_arn: str) -> Tuple[Resource, bool]:
    credentials = Credentials(
        token=oauth_credentials[SECRET_KEY_ACCESS_TOKEN],
        refresh_token=oauth_credentials[SECRET_KEY_REFRESH_TOKEN],
        token_uri=oauth_credentials[SECRET_KEY_TOKEN_URI],
        client_id=oauth_credentials[SECRET_KEY_CLIENT_ID],
        client_secret=oauth_credentials[SECRET_KEY_CLIENT_SECRET],
        scopes=[CALENDAR_SCOPE]
    )
    
    refreshed = False
    if credentials.expired:
        credentials.refresh(Request())
        refreshed = True
        update_oauth_tokens(secrets_arn, credentials.token, credentials.refresh_token)
    
    return build(CALENDAR_SERVICE_NAME, CALENDAR_SERVICE_VERSION, credentials=credentials), refreshed

def get_calendar_no_shows_for_month(service: Resource, calendar_id: str, start_date: str, end_date: str, valid_event_names: set) -> int:
    no_show_search_term = NO_SHOW_SEARCH_TERM
    total_minutes = 0

    chicago_tz = ZoneInfo(TIMEZONE_CHICAGO)
    start_dt = datetime.strptime(start_date, DATE_FORMAT).replace(hour=0, minute=0, second=0, tzinfo=chicago_tz)
    end_dt = datetime.strptime(end_date, DATE_FORMAT).replace(hour=23, minute=59, second=59, tzinfo=chicago_tz)

    start_time = start_dt.isoformat()
    end_time = end_dt.isoformat()

    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=start_time,
            timeMax=end_time,
            singleEvents=True,
            orderBy=CALENDAR_ORDER_BY
        ).execute()

        for event in events_result.get(CALENDAR_ITEMS_KEY, []):
            if CALENDAR_EVENT_SUMMARY_KEY not in event or CALENDAR_EVENT_START_KEY not in event or CALENDAR_EVENT_END_KEY not in event:
                continue

            event_name = event[CALENDAR_EVENT_SUMMARY_KEY]

            if no_show_search_term in event_name and any(valid_name in event_name for valid_name in valid_event_names):
                start_time_dt = datetime.fromisoformat(event[CALENDAR_EVENT_START_KEY].get(CALENDAR_EVENT_DATETIME_KEY, event[CALENDAR_EVENT_START_KEY].get(CALENDAR_EVENT_DATE_KEY)))
                end_time_dt = datetime.fromisoformat(event[CALENDAR_EVENT_END_KEY].get(CALENDAR_EVENT_DATETIME_KEY, event[CALENDAR_EVENT_END_KEY].get(CALENDAR_EVENT_DATE_KEY)))
                duration_minutes = int((end_time_dt - start_time_dt).total_seconds() / MINUTES_PER_HOUR)
                total_minutes += duration_minutes

    except Exception:
        pass

    return total_minutes

def get_calendar_events_for_month(service: Resource, calendar_id: str, start_date: str, end_date: str, valid_event_names: set) -> int:
    total_minutes = 0
    
    chicago_tz = ZoneInfo(TIMEZONE_CHICAGO)
    start_dt = datetime.strptime(start_date, DATE_FORMAT).replace(hour=0, minute=0, second=0, tzinfo=chicago_tz)
    end_dt = datetime.strptime(end_date, DATE_FORMAT).replace(hour=23, minute=59, second=59, tzinfo=chicago_tz)
    
    start_time = start_dt.isoformat()
    end_time = end_dt.isoformat()
    
    try:
        events_result = service.events().list(
            calendarId=calendar_id,
            timeMin=start_time,
            timeMax=end_time,
            singleEvents=True,
            orderBy=CALENDAR_ORDER_BY
        ).execute()
        
        for event in events_result.get(CALENDAR_ITEMS_KEY, []):
            if CALENDAR_EVENT_SUMMARY_KEY not in event or CALENDAR_EVENT_START_KEY not in event or CALENDAR_EVENT_END_KEY not in event:
                continue
                
            event_name = event[CALENDAR_EVENT_SUMMARY_KEY]
            
            if event_name in valid_event_names:
                start_time_dt = datetime.fromisoformat(event[CALENDAR_EVENT_START_KEY].get(CALENDAR_EVENT_DATETIME_KEY, event[CALENDAR_EVENT_START_KEY].get(CALENDAR_EVENT_DATE_KEY)))
                end_time_dt = datetime.fromisoformat(event[CALENDAR_EVENT_END_KEY].get(CALENDAR_EVENT_DATETIME_KEY, event[CALENDAR_EVENT_END_KEY].get(CALENDAR_EVENT_DATE_KEY)))
                duration_minutes = int((end_time_dt - start_time_dt).total_seconds() / MINUTES_PER_HOUR)
                total_minutes += duration_minutes
                
    except Exception:
        pass
    
    return total_minutes

def get_sheets_service(credentials_json: str) -> Resource:
    credentials_dict = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_dict, scopes=[SHEETS_SCOPE]
    )
    return build(SHEETS_SERVICE_NAME, SHEETS_SERVICE_VERSION, credentials=credentials)

def update_oauth_tokens(secrets_arn: str, access_token: str, refresh_token: str) -> None:
    client = boto3.client(AWS_SERVICE_SECRETSMANAGER)
    secret = json.loads(client.get_secret_value(SecretId=secrets_arn)['SecretString'])
    oauth_creds = json.loads(secret[SECRET_KEY_GOOGLE_CALENDAR_OAUTH])
    oauth_creds[SECRET_KEY_ACCESS_TOKEN] = access_token
    oauth_creds[SECRET_KEY_REFRESH_TOKEN] = refresh_token
    secret[SECRET_KEY_GOOGLE_CALENDAR_OAUTH] = json.dumps(oauth_creds)
    client.update_secret(SecretId=secrets_arn, SecretString=json.dumps(secret))

def get_ssm_string_value(parameter_name: str) -> str:
    ssm = boto3.client(AWS_SERVICE_SSM)
    response = ssm.get_parameter(Name=parameter_name)
    return response['Parameter']['Value']

def get_ssm_list_of_strings_value(parameter_name: str) -> List[str]:
    ssm = boto3.client(AWS_SERVICE_SSM)
    response = ssm.get_parameter(Name=parameter_name)
    return response['Parameter']['Value'].split(',')

def get_sheet_data(service: Resource) -> List[Dict[str, Union[str, float, List[str]]]]:
    phone_enabled_indices_ssm_name = os.environ.get(ENV_PHONE_ENABLED_COLUMNS_SSM_NAME)
    phone_enabled_indices = [int(x) for x in get_ssm_list_of_strings_value(phone_enabled_indices_ssm_name)]
    spreadsheet_id_ssm_name = os.environ.get(ENV_GOOGLE_SHEETS_SSM_NAME)
    spreadsheet_id = get_ssm_string_value(spreadsheet_id_ssm_name)
    range_name = SHEETS_RANGE_NAME

    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name
        ).execute()

        values = result.get(SHEETS_VALUES_KEY, [])
        sheet_data = []

        for i, row in enumerate(values[1:], 1):
            if len(row) >= 0:
                event_name = row[0]
                phone_numbers = [row[i] for i in phone_enabled_indices if i < len(row) and row[i]]

                sheet_data.append({
                    SHEET_KEY_EVENT_NAME: event_name,
                    SHEET_KEY_PHONE_NUMBERS: phone_numbers
                })

        return sheet_data

    except Exception:
        return []