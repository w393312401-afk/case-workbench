"""Preview or apply the same latest-package retention used by the workbench."""
import argparse
import json
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
CONFIG = json.loads((APP / "config.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(CONFIG["skill_root"]) / "scripts"))
from production_store import ProductionStore
from workbench_carousel_delivery import CarouselDeliveryMixin


class Maintenance(CarouselDeliveryMixin):
    def __init__(self, runtime):
        self.production = ProductionStore(runtime)
        self.lock = self.production.lock

    def _carousel_dir(self, identifier):
        return self.production.root / "carousels" / identifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--apply", action="store_true", help="Actually retire old packages; otherwise preview only")
    args = parser.parse_args()
    maintenance = Maintenance(args.runtime or Path(CONFIG["runtime_root"]))
    result = maintenance.prune_all_carousel_deliveries(dry_run=not args.apply)
    print(json.dumps({"dry_run": not args.apply, **result}, ensure_ascii=False, indent=2))
    return int(bool(result["warnings"]))


if __name__ == "__main__":
    raise SystemExit(main())
