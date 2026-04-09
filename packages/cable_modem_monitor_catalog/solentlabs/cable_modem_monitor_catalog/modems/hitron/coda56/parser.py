"""PostProcessor for {manufacturer}/{model} — system_info extraction.

Extracts hardware_version, software_version, and system_uptime from
the /data/getSysInfo.asp JSON endpoint. The response is a root-level
JSON array with one object containing system fields.

Response format::

    [{"hwVersion": "1A", "swVersion": "7.3.5.3.2b1",
      "systemUptime": "479h:40m:38s", ...}]

TODO: system_uptime is passed through as the raw firmware string
("479h:40m:38s") instead of the canonical fleet format. To
standardize, either:
  1. Apply convert_value(raw, "uptime",
     input_format="{hours}h:{minutes}m:{seconds}s") here, or
  2. Restructure to extract uptime via a parser.yaml system_info
     source with type: uptime / format so the PostProcessor is
     no longer needed for this field.
"""

from __future__ import annotations

from typing import Any


class PostProcessor:
    """Extract system_info from getSysInfo.asp JSON response."""

    def parse_system_info(
        self,
        system_info: dict[str, Any],
        resources: dict[str, Any],
    ) -> dict[str, Any]:
        """Extract system_info from the getSysInfo.asp response.

        The resource loader wraps the root-level array as ``{"_raw": [...]}``.
        """
        data = resources["/data/getSysInfo.asp"]

        # Unwrap: loader wraps root array as {"_raw": [...]}
        items = data.get("_raw", data)
        info = items[0]

        field_map = {
            "hwVersion": "hardware_version",
            "swVersion": "software_version",
            "systemUptime": "system_uptime",
        }

        for src_key, dst_key in field_map.items():
            value = info.get(src_key)
            if value and isinstance(value, str):
                system_info[dst_key] = value

        return system_info
