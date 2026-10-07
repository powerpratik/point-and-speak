# Security

## What this plugin can see

A helper runs on your Mac while the mod is on. It needs **Screen Recording** and **Accessibility** for your terminal app.
Those permissions belong to the terminal app, not to this plugin. Every program that runs inside that terminal app
inherits them. Grant them only to a terminal app that you trust.

While the mod is on, the helper:
- samples the mouse position about 60 times a second, and keeps the last 25 seconds in memory
- reads one flag from macOS: whether a microphone is in use. It records no audio
- takes a screenshot of the display under the pointer twice a second while a microphone is in use, and keeps only frames where
  the screen changed, at most 3 per recording
- reads the accessibility element under the pointer about 3 times a second during that time

When the mod is off, it does none of this. The helper exits when Claude Code goes away.

Anything the model reads may be sent to Anthropic as part of your conversation, and Claude Code saves it in the session
transcript on your Mac. See the Privacy section of the README.

## What protects the helper

The helper is a web server on `127.0.0.1`. It needs a secret token on every request, checks the `Host` header, and may only
write or delete inside the one `.claude/lookat` folder that the mod names. `helper/check_server.py` tests this against the real helper.

## Known limits

- Text on screen is untrusted. The mod labels it as data, but it cannot make a hostile page safe.
- Password-field detection is a help, not a guarantee.
- Dependencies are pinned and locked, but they still come from PyPI on the first run.

## Report a problem

Open a GitHub issue. For something sensitive, use GitHub's private vulnerability reporting on this repository.
