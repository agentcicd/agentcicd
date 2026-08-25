from __future__ import annotations

import argparse

from agentcicd.fixtures.manifest import generate_manifest_for_package, write_manifest


def main() -> int:
    parser = argparse.ArgumentParser(prog="agentcicd-fixtures")
    subparsers = parser.add_subparsers(dest="command", required=True)
    manifest_parser = subparsers.add_parser("manifest", help="Generate a AgentCICD fixture manifest")
    manifest_parser.add_argument("--package", required=True, help="Importable fixture package")
    manifest_parser.add_argument("--output", required=True, help="Output manifest JSON path")
    manifest_parser.add_argument("--namespace", help="SQL namespace for functions and environments")
    manifest_parser.add_argument("--version", help="Package version written into manifest metadata")
    manifest_parser.add_argument("--package-name", help="Distribution package name written into manifest metadata")
    args = parser.parse_args()
    if args.command == "manifest":
        manifest = generate_manifest_for_package(
            args.package,
            namespace=args.namespace,
            version=args.version,
            package_name=args.package_name,
        )
        write_manifest(manifest, args.output)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
