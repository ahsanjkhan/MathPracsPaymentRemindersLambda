#!/usr/bin/env python3
"""
One-time OAuth setup script for Google Calendar API access.
Run this locally to get the refresh token for Lambda.
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def main():
    print("Google Calendar OAuth Setup")
    print("=" * 40)
    
    flow = InstalledAppFlow.from_client_secrets_file(
        'credentials.json', SCOPES)
    
    # Run local server for OAuth flow
    credentials = flow.run_local_server(port=0)
    
    # Extract the credentials we need for Lambda
    oauth_creds = {
        'access_token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret
    }
    
    print("\nOAuth credentials obtained!")
    print("Add this to your AWS Secrets Manager:")
    print("-" * 40)
    print(json.dumps(oauth_creds, indent=2))
    
    # Save to file for easy copying
    with open('oauth_credentials.json', 'w') as f:
        json.dump(oauth_creds, f, indent=2)
    
    print(f"\nCredentials also saved to: oauth_credentials.json")

if __name__ == '__main__':
    main()