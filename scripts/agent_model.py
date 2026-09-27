"""The user's pinned model policy for every unattended agent role."""
import argparse

MODEL = "claude-opus-5.5"
EFFORT = "high"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("setting", choices=["model", "effort"])
    args = parser.parse_args()
    print({"model": MODEL, "effort": EFFORT}[args.setting])
