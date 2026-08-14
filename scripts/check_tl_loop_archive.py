"""Import every non-test module from the packaged TL controller."""

import importlib
import pkgutil
import sys


def main() -> None:
    sys.path.insert(0, sys.argv[1])
    import tl_loop

    for module in pkgutil.walk_packages(tl_loop.__path__, tl_loop.__name__ + "."):
        if ".tests" not in module.name and not module.name.rsplit(".", 1)[-1].startswith("test"):
            importlib.import_module(module.name)


if __name__ == "__main__":
    main()
