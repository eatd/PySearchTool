import argparse
import sys

from src.ui import App


def main():
    parser = argparse.ArgumentParser(description="PySearch Tool")
    parser.add_argument("--dir", type=str, help="Start directory")
    args = parser.parse_args()

    app = App()
    if args.dir:
        app.dir_var.set(args.dir)

    try:
        app.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
