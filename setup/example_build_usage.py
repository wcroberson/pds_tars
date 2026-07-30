"""Example: running the pipeline from Python instead of the command line.

build.run_pipeline() takes plain arguments (a target list plus the same
options as the CLI flags below), so calling it programmatically needs no
sys.argv setup.

Requires the package to be installed first (from the pds_tars/
directory): pip install -e .
"""

from pds_tars.build import run_pipeline

# Same options as the CLI (see README.md's Options table): targets, diagnostic,
# node, refresh_contexts all default the same way argparse does.
failed_targets = run_pipeline(["VENUS"], diagnostic=True)

if failed_targets:
    print(f"Failed: {', '.join(failed_targets)}")

# Output lands in tables/venus/ next to wherever OUTPUT_DIR resolves to
# (the pds_tars/ project root), exactly like a CLI run.
