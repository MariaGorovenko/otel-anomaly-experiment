import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAGS_FILE = ROOT / "src" / "flagd" / "demo.flagd.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("flag")
    parser.add_argument("variant")
    args = parser.parse_args()

    data = json.loads(FLAGS_FILE.read_text(encoding="utf-8"))

    if args.flag not in data["flags"]:
        raise SystemExit(f"Unknown flag: {args.flag}")

    flag = data["flags"][args.flag]

    if args.variant not in flag["variants"]:
        raise SystemExit(f"Unknown variant: {args.variant}")

    previous = flag["defaultVariant"]

    if previous == args.variant:
        print(f"{args.flag}: already {args.variant}")
        return

    flag["defaultVariant"] = args.variant

    FLAGS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"{args.flag}: {previous} -> {args.variant}")


if __name__ == "__main__":
    main()
