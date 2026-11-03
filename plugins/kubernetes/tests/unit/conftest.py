import sys
from pathlib import Path

# Skills import their lib/ by module name at run time (`import workload`); do the same here.
for lib in sorted((Path(__file__).parents[2] / "skills").glob("*/lib")):
    sys.path.insert(0, str(lib))
