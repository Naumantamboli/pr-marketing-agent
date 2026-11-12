"""Load the reviewed baseline (seed/catalog_seed.json) into the product catalog.

    uv run python scripts/load_catalog_seed.py --check     # validate the seed, write nothing
    uv run python scripts/load_catalog_seed.py --preview   # print the catalog as markdown
    uv run python scripts/load_catalog_seed.py             # write to storage

Storage follows utils/storage.py: the TENANT_ID bucket, or ./output with LOCAL_OUTPUT=true.
Re-running is safe: texts are replaced, sources merged, recorded updates kept.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from utils import catalog  # noqa: E402

DEFAULT_SEED = ROOT / "seed" / "catalog_seed.json"


def preview(seed: dict) -> str:
    """Render the seed as the catalog would look, without touching storage."""
    arch = catalog.empty_architecture()
    arch["summary"] = seed["architecture"]["summary"]
    for c in seed["architecture"]["components"]:
        arch = catalog.upsert_component_record(
            arch, c["name"], c["role"], c.get("depends_on"), c["slug"]
        )
    index = {**catalog.empty_index(seed.get("product")), "overview": seed["overview"]}
    index = catalog.with_architecture(index, arch)
    features = []
    for f in seed["features"]:
        fields = {k: f[k] for k in ("summary", "status", "aliases", "components", "overview")}
        feature = catalog.edit_feature(catalog.new_feature(f["name"], f["slug"]), **fields)
        features.append(feature)
        index = catalog.with_feature(index, feature)
    return catalog.to_markdown(index, arch, features)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="validate only")
    mode.add_argument("--preview", action="store_true", help="print markdown, write nothing")
    args = parser.parse_args()

    seed = json.loads(args.seed.read_text())
    problems = catalog.check_seed(seed)
    if problems:
        print("Seed has problems:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 1
    if args.check:
        print(
            f"Seed OK: {len(seed['architecture']['components'])} components, "
            f"{len(seed['features'])} features (as of {seed.get('as_of')})."
        )
        return 0
    if args.preview:
        print(preview(seed))
        return 0

    index = catalog.apply_seed(seed)
    print(
        f"Loaded {len(index['features'])} features and "
        f"{len(index['architecture']['components'])} components into {catalog.INDEX_KEY}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
