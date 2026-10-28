# Send email

Once your team has saved its own configuration in **Team settings → Email Service**, you can ask Bowra in a chat to send an email with a plain-text body and optional file attachments. In a Lead chat, each email is sent only after you confirm it on the page yourself. The first part of this guide is for members who send email; the second part is for team owners and administrators who set up the mail service.

## Send an email from a chat

In the chat, state the recipient, subject, and body, and ask Bowra to send the email. For example: “Send an email to customer@example.com with the subject Project update and the following body: …”. You can also ask Bowra to turn the analysis results from the current chat into an email.

When you say “send it to my email”, Bowra can look up your own account email without asking you to type it again. An address you explicitly provide takes precedence. If your account has no email, Bowra asks for a recipient. The sending confirmation still shows the exact address; check it before sending.

### Confirm before sending

Before each email is sent, a **Confirm email** window shows the full recipient list, subject, body, and the names and sizes of any attachments. Check them, then choose **Send**. If you choose **Don't send**, or do nothing within the time shown, the email is not sent, and Bowra says so in the chat. A sent email cannot be recalled.

Only you, the person who started this chat, can see and confirm the email. Lead cannot send from scheduled tasks, Feishu, or other places where you cannot confirm on the web page.

### Numbers in the email

Amounts, percentages, and other numbers in the body must come from data actually retrieved in the current chat, or from numbers you typed in your own messages. If a number in Bowra's draft cannot be found in this chat's data, no confirmation window appears; Bowra corrects the numbers first and then asks you to confirm.

To send results from an earlier chat, ask Bowra to query the relevant data again in the current chat, then send the email.

### Attach a report or other file

Ask Bowra to attach a file already uploaded or generated in the current chat, for example: “Send this PPT report to my email as an attachment.” The file must belong to you and be available in this chat and Space. To send a Canvas report, first ask for it to be exported to a supported file; the Canvas itself is not an attachment.

Supported formats are PPTX, DOCX, XLSX, PDF, CSV, TXT, PNG and JPEG. You can send up to 5 files with a combined size of 10 MiB. For larger files, reduce their size or send them separately. Missing, deleted, unsupported or unreadable files stop the entire email; Bowra does not silently send it without the requested attachments. Your mail provider may impose additional limits.

Before confirming, review the recipient, file names and sizes, and the files themselves. The numeric check on the email subject and body does not verify the contents of attached documents. Once confirmed, the reviewed file contents are sent, even if the stored file changes while you decide; a file that becomes unavailable before sending prevents delivery.

A custom Agent whose Owner selected and released the Email resource sends under that grant without asking for confirmation on each run. The same file-access and attachment limits apply.

### When no mail service is set up

If your team has not saved its own mail service configuration, Bowra tells you that the service is currently unavailable. The same applies when **Effective delivery configuration** shows that the instance default is inherited: the instance default is used only for system email such as invitations, email verification, and password resets, not for sending email from chats. Ask a team owner or administrator to set it up as described below.

## Set up the team mail service

Team owners and administrators can set up an SMTP service for the team in **Team settings → Email Service**. After setup, invitations, email verification, password resets, and notification emails are sent through the team's own configuration first, and members can send email from chats as described above.

### Setup steps

1. Open **Team settings → Email Service**. **Effective delivery configuration** shows whether the team has its own configuration, inherits the instance default, or has none yet.
2. Under **SMTP connection**, enter the SMTP server, port, and encryption provided by your mail provider. Common ports are 465 (SSL/TLS) and 587 or 2525 (STARTTLS).
3. Under **Authentication**, enter the SMTP username and password or app password. Most mail services require a dedicated SMTP password or app password rather than your sign-in password. A saved password is never shown again; leave the field empty when saving again to keep the saved password.
4. Under **Sender identity**, enter a sender address authorized by your mail provider and a display name. Bowra has no default sender domain; email cannot be delivered without an authorized sender. Then choose **Save**. You can also choose **Save and test** to save and send a test email right away.
5. Choose **Send test email**. The test email goes to your own mailbox. If it arrives, the configuration works; if not, also check your spam folder.

When the team no longer needs its own configuration, choose **Clear config** (shown only when the team has saved its own configuration). System email then uses the instance default configuration, if there is one, and sending email from chats becomes unavailable.

### Notes

- The cloud (SaaS) edition can only use mail servers reachable from the public internet, on ports 25, 465, 587, or 2525. If you enter an internal company address or another port, saving reports that the address is blocked by the security policy or that the port is not supported. Use the public SMTP address from your mail provider instead.
- Private deployments can use internal company mail servers and other ports.
- Only team owners and team administrators can view and change the mail service configuration. Other members do not see this entry.
