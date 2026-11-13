# Changelog

## 0.9.0: first public version

- Runs on macOS and Linux as well as Windows. One launcher per system replaces the Windows-only `.bat` files: `jobapp.cmd` and `jobapp.sh`. Double-click launchers for Windows (`.bat`) and Mac (`.command`) are in `scripts/`.
- Each person signs in with their own Claude account or their own Anthropic API key (`jobapp signin`). API keys are kept in the system's password store.
- Your data now lives in a per-user folder outside the program folder, so `git pull` never touches it. `jobapp import` brings data over from an older copy, and `jobapp forget` deletes all of it.
- Job App no longer fills, or clicks past, any page that asks for a password.
- The Claude runtime's transcripts of Job App's agent runs are deleted unless you choose to keep them.
- Settings has new buttons: "Open my data folder", "Copy diagnostics" for bug reports, and an optional notice when a new version is published.
- Lower default usage caps: $5 a day, $1 per application, $1.50 per search.
- "Claude's browser window" is now "Job App's browser window".

## 0.8.0 and earlier

Built for one Windows PC. See `docs/history/`.
