# Ashton's Origami — working notes for Claude

Order app for Nadia's origami business. Live at https://ashtonsorigami.34-66-15-141.sslip.io/
(Google Cloud VM `nadiaserver`, service `ashtonsorigami`, port 8091, data in /opt/ashtonsorigami/data).
Nadia is not a programmer: explain simply, one step at a time.

## "Check notes" routine
1. Read open notes in `notes/notes.json` (entries with `"done": false`). The app pushes them here automatically.
2. Build the changes in `app/` (`server.py` = Python 3.11 stdlib server, `index.html` = whole UI, inline JS).
3. Raise the number in `app/VERSION` (e.g. 1.1.0 -> 1.2.0).
4. Put the ids of the notes this version completes in `app/completed-notes.json` (a JSON list).
5. Test locally (run server.py with AO_ROOT=/tmp/x AO_PORT=xxxx) before pushing.
6. Commit and push to `main`. The app checks GitHub on start and every 6 hours; Nadia can tap Settings › Updates › Check now.

## Rules
- Never change or delete anything under /opt/ashtonsorigami/data; updates must keep old data working
  (add new record types with defaults, never rename fields).
- Don't touch Listing Book or other apps on the VM.
- Never commit passwords or tokens.
