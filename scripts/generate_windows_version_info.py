"""Generate a deterministic PyInstaller Windows version-resource file."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


PRODUCT_NAME = "Aruba Mini Dashboard"
COMPANY_NAME = "Local Network Operations"


def version_parts(version: str) -> tuple[int, int, int, int]:
    """Convert a SemVer-like version into the four integers required by PE."""

    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[.+-].*)?", version.strip())
    if not match:
        raise ValueError(f"Version must start with three numeric components: {version}")
    return tuple(int(value) for value in (*match.groups(), "0"))  # type: ignore[return-value]


def render_version_info(
    *,
    version: str,
    description: str,
    original_filename: str,
) -> str:
    parts = version_parts(version)
    dotted_version = ".".join(str(value) for value in parts)
    internal_name = Path(original_filename).stem
    values = {
        "CompanyName": COMPANY_NAME,
        "FileDescription": description,
        "FileVersion": dotted_version,
        "InternalName": internal_name,
        "OriginalFilename": original_filename,
        "ProductName": PRODUCT_NAME,
        "ProductVersion": dotted_version,
    }
    string_structs = "\n".join(
        f"          StringStruct({key!r}, {value!r})," for key, value in values.items()
    )
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={parts!r},
    prodvers={parts!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
{string_structs}
        ],
      ),
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--original-filename", required=True)
    args = parser.parse_args()

    content = render_version_info(
        version=args.version,
        description=args.description,
        original_filename=args.original_filename,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output.with_suffix(output.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8", newline="\n")
    temp_path.replace(output)
    print(f"Generated Windows version resource: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
