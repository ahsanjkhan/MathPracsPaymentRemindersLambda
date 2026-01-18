### What Is This

This is the implementation of an AWS Lambda Function which is defined in the https://github.com/ahsanjkhan/MathPracsPaymentRemindersCDK repository.

The purpose of this Lambda is to process automated payment text message reminders for students enrolled in tutoring with MathPracs.

You can learn more about MathPracs at https://mathpracs.com

### How Does It Work

The Lambda is invoked by an AWS EventBridge Scheduler Rule every Sunday at 6:00 PM (Timezone America/Chicago).

Once invoked, it gathers the total tutoring sessions per student by integrating with the Google Calendar API.

Once the total sessions are calculated per student, it integrates with the Google Sheets API to find the relevant price for the student based on how many hours were completed that week. It also pulls the phone numbers for the student's parents or guardians.

Once the total due is calculated per student, it stores the result in a DynamoDB Table.

Finally, it integrates with Twilio to send out the text message.

### What Are The Components

AWS Lambda, AWS DynamoDB, AWS EventBridge Scheduler, AWS SecretsManager, Google Calendar API, Google Sheets API, Twilio API.