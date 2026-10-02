# Private sources

Everything in this folder except this README and `example_source.py` is git-ignored. Put sources here that you
don't want to publish.

1. Copy `example_source.py` to `<name>.py` and implement `fetch(ctx) -> SourceResult`. Each module defines
   `FETCHERS = {"<section key>": fetch}` and is loaded automatically.
2. Add the section to `config.local.yaml` (also git-ignored), with `position: first`, `after: <key>` or
   `before: <key>` to place it among the public sources.
3. Test with `bin/dailyread run --dry-run --only <key> --no-open -v`.
