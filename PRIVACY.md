# Privacy policy

Last updated: 2026-10-07

**Short version.** point-and-speak runs on your Mac. It has no server. It sends nothing to its author or to any service of its own.

## Who runs it

point-and-speak is open source software by [powerpratik](https://github.com/powerpratik). There is no company and no hosted service.
To ask a question or report a problem, open an issue at https://github.com/powerpratik/point-and-speak/issues.

## What it reads

Only while you have turned it on with `/look on`:

- The position of your mouse pointer, and whether a microphone is in use. It records no audio.
- Screenshots of the display under the pointer.
- The element under the pointer: the app name, the window title, the control label, up to 160 characters of its text,
  and the page address (scheme, host and path only). It skips fields that look like passwords or secrets.
- The text of each prompt you submit, to find pointing words such as "this" and "that".

This content can include personal data, such as names or emails that appear on your screen.

## What it stores

- Files in `<your project>/.claude/lookat/`: an annotated screenshot, small crops, and a file of pointer positions.
  They are deleted when the answer ends. Anything left behind is swept within 15 minutes to 1 hour. `/look off` deletes them at once.
- A short mouse trail (the last 25 seconds) and up to 6 recent recordings in the helper's memory. `/look off` clears them.
- It does not store audio. It does not write your prompt text to disk.

## Where the data goes

- **From the plugin: nowhere outside your Mac.** The plugin talks only to its own helper at `127.0.0.1`.
- **Into your conversation.** The text and any image that Claude reads become part of your conversation with Claude. Anthropic's
  terms and privacy policy for the product you use apply to that: https://www.anthropic.com/legal/privacy.
  Claude Code also keeps images that Claude reads in your session transcript on your Mac. Deleting the plugin's files does not remove them.
- **Packages.** On the first run, `uv` downloads the pinned Python packages from PyPI. No data of yours is sent.

## Retention

The plugin keeps nothing on any server. Local files are kept only as described above.

## Your choices

- `/look off` stops all sampling and deletes the files.
- Set `screenshots` to off in `/config` to send only text, positions and control names.
- Keep `onlyWithPointingWords` on, so context is added only to prompts that contain a pointing word.
- Delete `<your project>/.claude/lookat/` at any time, or uninstall the plugin.

## Children

The plugin is not intended for users under 18.

## Changes

Changes to this policy appear in the history of this file on GitHub.
