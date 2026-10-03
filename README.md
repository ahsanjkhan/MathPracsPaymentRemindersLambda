### What Is This

This is the implementation of an AWS Lambda Functions which are defined in the https://github.com/ahsanjkhan/MathPracsPaymentRemindersCDK repository.

The purpose of this Lambda is to process automated payment message reminders for students and tutors enrolled in tutoring with MathPracs, and to calculate what the MathPracs business partners (Ahsan and Muaz) owe each other.

You can learn more about MathPracs at https://mathpracs.com

### How Does It Work

The Lambdas are invoked by an AWS EventBridge Scheduler Rule.

The student payment reminders are invoked every Sunday at 1:00 PM (Timezone America/Chicago).

The tutor payment reminders are invoked every 1st of the Month at 2:00 PM (Timezone America/Chicago).

The business payment reminders are invoked every 1st of the Month at 2:00 PM (Timezone America/Chicago).

Once the total due is calculated per student/tutor, it stores the result in a DynamoDB Table.

The tutor payment reminders also record each tutor's monthly earnings as a CREDIT in the TutorTransactions DynamoDB Table and lower the tutor's balance in the TutorsV2 DynamoDB Table. The tutor payment reminder message shows the total owed to the tutor, which is the previous balance (negative when the tutor was paid in advance) plus this month's earnings.

The business payment reminders add up, for the previous month (starting 2026-09-13), the student payments collected by Ahsan and by Muaz (Transactions DynamoDB Table) and the tutor payments each of them sent (TutorTransactions DynamoDB Table). Each partner owes the other half of what they collected, and is owed half of what they paid tutors. The net amount is recorded as a DEBIT in the BusinessInternalDebts DynamoDB Table.

Finally, it integrates with Discord API to send out the reminder message. The business payment reminder message itemizes each amount and shows the total still outstanding between Ahsan and Muaz.

### What Are The Components

AWS Lambda, AWS DynamoDB, AWS EventBridge Scheduler, AWS SecretsManager, Discord API.