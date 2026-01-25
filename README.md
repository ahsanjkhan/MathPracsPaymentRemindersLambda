### What Is This

This is the implementation of an AWS Lambda Functions which are defined in the https://github.com/ahsanjkhan/MathPracsPaymentRemindersCDK repository.

The purpose of this Lambda is to process automated payment text message reminders for students and tutors enrolled in tutoring with MathPracs.

You can learn more about MathPracs at https://mathpracs.com

### How Does It Work

The Lambdas are invoked by an AWS EventBridge Scheduler Rule.

The student payment reminders are invoked every Sunday at 6:00 PM (Timezone America/Chicago).

The tutor payment reminders are invoked every 1st of the Month at 2:00 PM (Timezone America/Chicago).

Once the total due is calculated per student/tutor, it stores the result in a DynamoDB Table.

Finally, it integrates with Twilio to send out the text message.

### What Are The Components

AWS Lambda, AWS DynamoDB, AWS EventBridge Scheduler, AWS SecretsManager, Google Calendar API, Google Sheets API, Twilio API.