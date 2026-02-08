# Environment variable keys
ENV_TUTOR_PAYMENT_TABLE_NAME = 'TUTOR_PAYMENT_TABLE_NAME'
ENV_SECRETS_ARN = 'SECRETS_ARN'
ENV_GOOGLE_SHEETS_SSM_NAME = 'GOOGLE_SHEETS_SSM_NAME'
ENV_PHONE_ENABLED_COLUMNS_SSM_NAME = 'PHONE_ENABLED_COLUMNS_SSM_NAME'
ENV_TUTOR_SALARY_RATE_SSM_NAME = 'TUTOR_SALARY_RATE_SSM_NAME'

# Secrets Manager keys
SECRET_KEY_GOOGLE_CALENDAR_OAUTH = 'googleCalendarOAuthCredentials'
SECRET_KEY_GOOGLE_SHEETS = 'googleSheetsCredentials'
SECRET_KEY_TWILIO_ACCOUNT_SID = 'twilioAccountSid'
SECRET_KEY_TWILIO_AUTH_TOKEN = 'twilioAuthToken'
SECRET_KEY_TWILIO_PHONE_NUMBER = 'twilioPhoneNumber'
SECRET_KEY_ACCESS_TOKEN = 'access_token'
SECRET_KEY_REFRESH_TOKEN = 'refresh_token'
SECRET_KEY_TOKEN_URI = 'token_uri'
SECRET_KEY_CLIENT_ID = 'client_id'
SECRET_KEY_CLIENT_SECRET = 'client_secret'

# DynamoDB keys
DYNAMODB_KEY_UID = 'uid'
DYNAMODB_KEY_CALENDAR_NAME = 'calendar_name'
DYNAMODB_KEY_MONTH_START = 'month_start'
DYNAMODB_KEY_MONTH_END = 'month_end'
DYNAMODB_KEY_SESSION_MINUTES = 'session_minutes'
DYNAMODB_KEY_NO_SHOW_MINUTES = 'no_show_minutes'
DYNAMODB_KEY_AMOUNT_DUE = 'amount_due'
DYNAMODB_KEY_PROCESSED_SMS = 'processed_sms'
DYNAMODB_KEY_ITEM = 'Item'

# Calendar API
CALENDAR_SCOPE = 'https://www.googleapis.com/auth/calendar.readonly'
CALENDAR_SERVICE_NAME = 'calendar'
CALENDAR_SERVICE_VERSION = 'v3'
CALENDAR_ITEMS_KEY = 'items'
CALENDAR_SUMMARY_KEY = 'summary'
CALENDAR_ID_KEY = 'id'
CALENDAR_EVENT_SUMMARY_KEY = 'summary'
CALENDAR_EVENT_START_KEY = 'start'
CALENDAR_EVENT_END_KEY = 'end'
CALENDAR_EVENT_DATETIME_KEY = 'dateTime'
CALENDAR_EVENT_DATE_KEY = 'date'
CALENDAR_ORDER_BY = 'startTime'

# Google Sheets API
SHEETS_SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'
SHEETS_SERVICE_NAME = 'sheets'
SHEETS_SERVICE_VERSION = 'v4'
SHEETS_RANGE_NAME = 'Sheet1!A:P'
SHEETS_VALUES_KEY = 'values'

# Sheet data keys
SHEET_KEY_EVENT_NAME = 'event_name'
SHEET_KEY_GOOGLE_DOC_LINK = 'google_doc_link'
SHEET_KEY_STANDARD_HOURLY_RATE = 'standard_hourly_rate'
SHEET_KEY_HOURLY_1_RATE = 'hourly_1_rate'
SHEET_KEY_HOURLY_2_RATE = 'hourly_2_rate'
SHEET_KEY_HOURLY_3_RATE = 'hourly_3_rate'
SHEET_KEY_HOURLY_4_RATE = 'hourly_4_rate'
SHEET_KEY_HOURLY_5_RATE = 'hourly_5_rate'
SHEET_KEY_PHONE_NUMBERS = 'phone_numbers'

# Timezone
TIMEZONE_CHICAGO = 'America/Chicago'

# Date format
DATE_FORMAT = '%Y-%m-%d'

# No-show identifier
NO_SHOW_SEARCH_TERM = '(no-show)'

# DynamoDB operations
DYNAMODB_UPDATE_EXPRESSION = 'SET processed_sms = :val'

# AWS service names
AWS_SERVICE_DYNAMODB = 'dynamodb'
AWS_SERVICE_SECRETSMANAGER = 'secretsmanager'
AWS_SERVICE_SSM = 'ssm'

# Response messages
RESPONSE_MESSAGE_SUCCESS = 'MathPracs Tutor Payment Reminder executed successfully'
RESPONSE_KEY_MESSAGE = 'message'
RESPONSE_KEY_RESULTS = 'results'
RESPONSE_KEY_ERROR = 'error'

# HTTP status codes
HTTP_STATUS_OK = 200
HTTP_STATUS_ERROR = 500

# Numeric constants
MINUTES_PER_HOUR = 60.0
