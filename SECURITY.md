# Security policy

Hey Mint can see your screen, press keys and read files, so security reports matter a lot.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately:
[Security ▸ Report a vulnerability](https://github.com/shivatmax/hey-mint/security/advisories/new).
Include what an attacker could do, how to reproduce it, and the version (commit) you tested.
You will get a reply within a week.

## What counts

Anything that lets Mint be made to act against its safety rules: typing into password
fields, sending messages or email without being asked, deleting instead of trashing,
reading private folders (keys, keychains, browser profiles, Mail, Messages), leaking keys
from `.env`, prompt injection from web pages or documents that leads to such actions, or
the voice lock accepting someone else's voice.

## Your own data

Keys live in `.env` (mode 600). Memory, skills, voice enrolment and history stay in
`~/Library/Application Support/Mint` on your Mac. Nothing is sent anywhere while Mint is asleep.
