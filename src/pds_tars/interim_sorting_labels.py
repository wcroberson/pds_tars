"""
interim_sorting_labels.py - TEMPORARY stand-in for the science sorting pairs
that ATM bundle labels will carry once the sorting dictionary is in place.

Until then, the registry returns no sorting pairs for any bundle. This module
fills them in from interim_sorting_labels.csv (one row per pair: bundle LID,
instrument LID, science sorting label), written into each bundle's properties
under the same keys and in the same two-list shape the registry will return:
SORTING_INST_LID_KEY and SORTING_LABEL_KEY, where item N of one list goes with
item N of the other.

Everything downstream reads those keys as if the registry had returned them.
Once real bundle labels carry the pairs, delete this module, its CSV (and the
CSV's entry in pyproject.toml's package-data), and its one call in build.py.
"""

import csv
import os
from collections import defaultdict

from pds_tars.registry import SORTING_INST_LID_KEY, SORTING_LABEL_KEY, bare_lid

_HERE = os.path.dirname(os.path.abspath(__file__))
INTERIM_LABELS_PATH = os.path.join(_HERE, "interim_sorting_labels.csv")


def _load_pairs():
    """Returns the CSV's pairs grouped by bundle.

    Returns (dict):
        bundle LID -> list of (instrument LID, label) tuples, in file order.
    """
    pairs = defaultdict(list)
    with open(INTERIM_LABELS_PATH, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pairs[row["bundle_lid"]].append((row["instrument_lid"], row["science_sorting_label"]))
    return pairs


def add_interim_sorting_labels(bundles):
    """Writes the CSV's sorting pairs into each bundle's properties.

    A bundle that already has sorting pairs from the registry is left alone,
    so real label content always wins over this stand-in. Mutates each
    bundle's properties in place.

    Args:
        bundles (list of peppi Products): bundles as returned by the
              registry.

    Returns (int):
        how many bundles were given pairs.
    """
    pairs = _load_pairs()
    filled = 0
    for bundle in bundles:
        props = bundle.properties
        if props.get(SORTING_INST_LID_KEY) or props.get(SORTING_LABEL_KEY):
            continue
        bundle_pairs = pairs.get(bare_lid(bundle.id))
        if not bundle_pairs:
            continue
        props[SORTING_INST_LID_KEY] = [inst_lid for inst_lid, _ in bundle_pairs]
        props[SORTING_LABEL_KEY] = [label for _, label in bundle_pairs]
        filled += 1
    return filled
