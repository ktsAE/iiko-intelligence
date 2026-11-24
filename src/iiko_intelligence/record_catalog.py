"""Fixed iiko endpoints and request contracts included in the signed connector."""
import json
from importlib.resources import files
from decimal import Decimal

CATALOG = json.loads(files(__package__).joinpath("record_schemas.json").read_bytes())
RESOURCES = CATALOG["resources"]
DEFINITIONS = CATALOG["definitions"]
READ_PATHS = {entry["base"] + "/" + op for entry in RESOURCES.values() for op in ("list", "get") if op in entry}
WRITE_PATHS = {entry["base"] + "/" + op for entry in RESOURCES.values() for op in ("create", "update")}
WRITE_TOOLS = {"create_iiko_record", "update_iiko_record"}


def json_text(value) -> str:
    # iiko returns Decimal values. Keep the original numerical precision on a read/modify/write.
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Nonfinite number in iiko record.")
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + json_text(value[k]) for k in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join(json_text(v) for v in value) + "]"
    return json.dumps(value, allow_nan=False)
