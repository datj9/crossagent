import sys

from .cli import main as cli_main
from .worker import parse_worker_args, worker_main


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "worker":
        job_id, state_dir = parse_worker_args(argv[1:])
        return worker_main(job_id, state_dir)
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
