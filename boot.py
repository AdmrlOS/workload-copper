#!/usr/bin/env python3
"""Prepare only confined app directories before dropping root privileges."""
import os


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def data_components(request):
    parts = request.split("/")
    if parts[:2] != ["", "data"] or any(p in {"", ".", ".."} or "\0" in p for p in parts[2:]):
        raise ValueError("COPPER_DATA_DIR must be /data or a child without dot or empty components")
    return parts[2:]


def open_child(parent, name, mode=0o750):
    created = False
    try:
        os.mkdir(name, mode=mode, dir_fd=parent)
        created = True
    except FileExistsError:
        pass
    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    if created:
        os.fchmod(fd, mode)  # Preserve traversal permissions even with a restrictive inherited umask.
    return fd


def prepare_data(request, root="/data"):
    # Validate the requested container path independently of the testable root.
    components = data_components(request)
    current = os.open(root, DIRECTORY_FLAGS)
    try:
        for index, name in enumerate(components):
            child = open_child(current, name, mode=0o755 if index < len(components) - 1 else 0o750)
            os.close(current)
            current = child
        runs = open_child(current, "runs")
        try:
            # Never chown parents, other volume contents, or a pathname after checking it.
            if os.geteuid() == 0:
                os.fchown(current, 10001, 10001)
                os.fchown(runs, 10001, 10001)
        finally:
            os.close(runs)
    finally:
        os.close(current)


def main():
    prepare_data(os.environ.get("COPPER_DATA_DIR", "/data"))
    os.chdir("/opt/copper")
    os.execvp("setpriv", ["setpriv", "--reuid=10001", "--regid=10001", "--clear-groups",
                         "--no-new-privs", "python3", "/opt/copper/service.py"])


if __name__ == "__main__":
    main()
