#!/usr/bin/env python3
"""
GPT4Free API server — starts an OpenAI-compatible REST API on port 1337.
The Zenin API server calls http://localhost:1337/v1/chat/completions
when no OPENAI_API_KEY is set.
"""
import sys
import os
import signal

PORT = int(os.environ.get("G4F_PORT", "1337"))

def main():
    try:
        import g4f  # noqa: F401
    except ImportError:
        print("[g4f] g4f not installed — skipping AI server", flush=True)
        sys.exit(0)

    print(f"[g4f] Starting GPT4Free API server on 127.0.0.1:{PORT} ...", flush=True)

    # g4f >= 0.3.x  (FastAPI-based run_api)
    try:
        from g4f.api import run_api  # type: ignore
        run_api(host="127.0.0.1", port=PORT, debug=False)
        return
    except ImportError:
        pass

    # g4f <= 0.2.x  (older Flask-based server)
    try:
        from g4f.Provider.helper import run_api as _run  # type: ignore
        _run(host="127.0.0.1", port=PORT)
        return
    except ImportError:
        pass

    # Fallback: try the CLI entry point
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, "-m", "g4f.api", "--host", "127.0.0.1", "--port", str(PORT)],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    def _sig(signum, _frame):
        proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    proc.wait()


if __name__ == "__main__":
    main()
