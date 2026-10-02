### What Is This

This is the implementation of an AWS Lambda Functions which are defined in the https://github.com/ahsanjkhan/MathPracsPaymentRemindersCDK repository.

The purpose of this Lambda is to process automated payment message reminders for students and tutors enrolled in tutoring with MathPracs, and to calculate what the MathPracs business partners (Ahsan and Muaz) owe each other.

You can learn more about MathPracs at https://mathpracs.com

### How Does It Work

The Lambdas are invoked by an AWS EventBridge Scheduler Rule.

The student payment reminders are invoked every Sunday at 1:00 PM (Timezone America/Chicago).

The tutor payment reminders are invoked every 1st of the Month at 2:00 PM (Timezone America/Chicago).

The business payment reminders are invoked every 1st of the Month at 2:00 PM (Timezone America/Chicago).

The Muaz-only adjustments are invoked every 1st of the Month at 2:00 PM (Timezone America/Chicago).

Once the total due is calculated per student/tutor, it stores the result in a DynamoDB Table.

The tutor payment reminders also record each tutor's monthly earnings as a CREDIT in the TutorTransactions DynamoDB Table and lower the tutor's balance in the TutorsV2 DynamoDB Table.

The business payment reminders add up, for the previous month (starting 2026-09-13), the student payments collected by Ahsan and by Muaz (Transactions DynamoDB Table) and the tutor payments each of them sent (TutorTransactions DynamoDB Table). Each partner owes the other half of what they collected, and is owed half of what they paid tutors. The net amount is recorded as a DEBIT in the BusinessInternalDebts DynamoDB Table.

The Muaz-only adjustments are an off-the-books calculation and write nothing to DynamoDB. The business payment reminders split everything 50/50, but Muaz keeps all the profit from the Muaz-only students (listed in `muaz_only_adjustments/handler/constants.py`). For the previous month (starting 2026-09-13), the adjustment adds up what those students paid (CREDITs recorded by Ahsan or Muaz in the Transactions DynamoDB Table) minus what their tutors earned for their sessions (Sessions DynamoDB Table, at each tutor's hourly rate). Ahsan sends Muaz half of the total, or Muaz sends Ahsan half when it is negative.

Finally, it integrates with Discord API to send out the reminder message. The business payment reminder message itemizes each amount and shows the total still outstanding between Ahsan and Muaz.

### What Are The Components

AWS Lambda, AWS DynamoDB, AWS EventBridge Scheduler, AWS SecretsManager, Discord API.