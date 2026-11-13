# Security

## Reporting a problem

Please report security problems privately: use **"Report a vulnerability"** on the repository's **Security** tab (GitHub's private vulnerability reporting). Do not open a public issue for them.

Say what you found, how to reproduce it, and what it affects. Please do not include anyone's personal data.

## What Job App is designed to guarantee

These are bugs if they ever fail:

- Job App never clicks a button that may submit an application.
- It never fills, reads out or sends passwords, payment details or ID numbers, and does not fill or click past a page that asks for a password.
- Sensitive questions are only answered with the user's own saved answer.
- An API key is never written to settings, logs or any file other than the private key file used when no system password store exists, and never shown.
- The dashboard listens only on 127.0.0.1. It needs a session cookie handed out through the link the program opens, and it checks the Host header.
- Text on web pages is treated as data. Instructions found in a page are never followed as commands.
