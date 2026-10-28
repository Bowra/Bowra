# Sign in, sign up, and reset your password

The sign-in, sign-up, password reset, and invitation pages use the Bowra identity and follow your device’s light or dark setting.

## Sign in

1. Open the address your team gave you. If your team configured Feishu, choose “Sign in with Feishu” and follow the prompts.
2. To use email and password, enter them below "or sign in with email" and choose "Sign in".
3. The eye button at the end of the password box shows or hides the password.

If the email format is wrong or a field is empty, the message appears under that field. When you see "The email or password is incorrect", your email stays filled in and the password is cleared, so just enter the password again; to protect accounts, the page doesn't say which one was wrong. After too many attempts, wait as the message says and try again.

After signing in, you return to the page you were trying to open.

If sign-in reports that the public address is unavailable, reopen the complete application address your team gave you and use the sign-in button on that page. Keep any path after the domain. If the error continues, ask the team owner to check the team's public entry. Do not add an internal server port to the address.

## Email not verified yet

"Verify your email first" means the verification email for this account hasn't been opened yet. Open the link in that email, then sign in. If it hasn't arrived, choose "Resend verification email" (you can send it again after 60 seconds) and check your spam folder.

## Create an account

"Sign up" only appears where public sign-up is allowed. If you see "Ask your team admin for an invitation", request an invitation from an administrator.

1. Enter your email and a password. The display name is optional.
2. The password needs at least 8 characters with both letters and numbers; each requirement below the box is checked off as you type.
3. Enter the password again under "Confirm password", then choose "Create account".

If your email needs to be verified, you'll see "Check your email". If you typed the wrong email, choose "Go back and change it".

If the page says "Your account is ready" instead, your account was created but the verification email couldn't be sent yet. Choose "Resend verification email" to try again; for about 15 minutes after you register, the page shows the result of each try. After that it only confirms that a resend was requested, and you need to wait a minute between tries. If it still doesn't arrive, ask your team administrator to check the email settings (or the application address, when the page says so) and give them the reference shown on the page.

## Forgot your password

1. On the sign-in page, choose "Forgot password?", enter the email you sign in with, and choose "Send reset link".
2. When you see "Check your email", open the link in the email. To protect accounts, the page shows the same message whether or not the email is registered.
3. On "Set a new password", enter the new password twice and choose "Save new password".
4. You'll see "Password updated". A reset link you requested yourself signs you in, so choose the "Open" button; for a link an administrator created for you, choose "Go to sign-in" and sign in with the new password.

Reset links work only once and expire. If you see "This link no longer works", choose "Request a new reset link".

"We can't send email right now" means your team hasn't turned on email delivery. Ask your team owner or an administrator — they can create a password reset link for you on the Members page.

## Accept an invitation

Open the link in the invitation email. The page shows the team or space you're invited to, your role, the invited email, and when the invitation expires. If the person who invited you has a display name, the page also shows who invited you.

- No account yet: choose "Create profile", set a password for the invited email, and continue. The invited email is set by the invitation and can't be changed.
- Already have an account: choose "Sign in instead", then come back to the invitation after signing in.
- Signed in with a different account: choose "Sign out and switch account" and use the invited account.

If the invitation has expired or was revoked, ask the person who invited you to send a new one. If it says the invitation has already been accepted, just sign in. An invitation only grants access to the team or space; it doesn't change permissions for data sources such as Datarc.

## Change the language

Use the language button at the bottom of the page. The page reloads in the new language; your email stays filled in, but you'll need to enter the password again.

## First start of Bowra Community

A new Community installation opens the first-owner setup page. Ask the person operating the server for the setup proof, then enter the Team name, your email and a strong password. The server operator retrieves the proof with `python -m bowhead_gateway.installation setup-token` in the running application environment. Keep it private.

After setup succeeds, sign in normally and configure the Team's models. Setup can create the first owner only once; later members join through Team administration. Community needs no commercial activation or renewal and has no built-in user cap. Team permissions and configured usage budgets still apply.
