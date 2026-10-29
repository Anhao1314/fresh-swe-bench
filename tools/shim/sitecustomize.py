# Headless console shim for Windows.
# mini-swe-agent's batch entry points import interactive prompt modules at
# import time; under a non-tty stdout (CI / piped shells) prompt_toolkit
# crashes with NoConsoleScreenBufferError before main() runs. Swap in
# plain-text output and a dummy input when stdout is not a terminal.
# Usage: PYTHONPATH=<dir containing this file> mini-extra ...
import os
import sys

if os.name == "nt" and not sys.stdout.isatty():
    try:
        import prompt_toolkit.output.defaults as _out_defaults
        from prompt_toolkit.output.plain_text import PlainTextOutput

        def _headless_create_output(stdout=None, **kwargs):
            return PlainTextOutput(stdout or sys.stdout)

        _out_defaults.create_output = _headless_create_output

        import prompt_toolkit.input.defaults as _in_defaults
        from prompt_toolkit.input.base import DummyInput

        _in_defaults.create_input = lambda *args, **kwargs: DummyInput()
    except Exception:
        # If prompt_toolkit changes shape, fail loudly later rather than here.
        pass
