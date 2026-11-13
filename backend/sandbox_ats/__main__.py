"""Run the sandbox ATS on loopback: `python -m sandbox_ats [--port 8100]`."""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m sandbox_ats")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    # Loopback only: the sandbox is never reachable from another machine.
    uvicorn.run("sandbox_ats.app:app", host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
