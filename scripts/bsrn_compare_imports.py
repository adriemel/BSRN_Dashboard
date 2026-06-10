#!/usr/bin/env python3
"""Compare generated BSRN import files with Tool 1 import-file examples."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXAMPLES_DIR = PROJECT_ROOT / "tools" / "create-importfiles" / "importfiles-examples"
DEFAULT_GENERATED_DIR = PROJECT_ROOT / "output" / "current" / "import_files"
KNOWN_REGRESSION_JOBS = [
    "DRA_2025-02",
    "DRA_2025-03",
    "PAY_2023-01",
    "GVN_2023-01",
    "CAB_2025-04",
    "TAT_2026-04",
]
METAHEADER_TIMESTAMP_RE = re.compile(r"^// METAHEADER - BSRN data import at ")
NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$")


@dataclass
class FileReport:
    filename: str
    job: str
    example_path: str | None = None
    generated_path: str | None = None
    status: str = "missing"
    mode: str = ""
    differences: list[str] = field(default_factory=list)
    line_count_example: int | None = None
    line_count_generated: int | None = None
    data_row_count_example: int | None = None
    data_row_count_generated: int | None = None
    review_notes: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    mode: str
    examples_dir: str
    generated_dir: str
    jobs: list[str]
    compared: int = 0
    matched: int = 0
    different: int = 0
    missing_generated: int = 0
    missing_examples: int = 0
    files: list[FileReport] = field(default_factory=list)


class CompareError(Exception):
    pass


def project_path(path_text: str | Path) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else PROJECT_ROOT / path


def normalize_job(value: str) -> str:
    text = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{3}_\d{4}-\d{2}", text):
        raise argparse.ArgumentTypeError(f"Invalid job label: {value!r}; expected CAB_2025-04")
    return f"{text[:3].upper()}_{text[4:]}"


def job_from_filename(path: Path) -> str:
    match = re.match(r"^([A-Z0-9]{3}_\d{4}-\d{2})_", path.name, flags=re.IGNORECASE)
    if not match:
        return ""
    return normalize_job(match.group(1))


def import_files_for_job(root: Path, job: str) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob(f"{job}_*_imp.txt") if path.is_file())


def example_files(examples_dir: Path, jobs: list[str]) -> list[Path]:
    if not examples_dir.exists():
        raise CompareError(f"Examples directory not found: {examples_dir}")
    files: list[Path] = []
    if jobs:
        for job in jobs:
            files.extend(import_files_for_job(examples_dir, job))
    else:
        files = sorted(path for path in examples_dir.rglob("*_imp.txt") if path.is_file())
    return sorted(dict.fromkeys(files))


def find_generated_file(generated_dir: Path, job: str, filename: str) -> Path | None:
    if not generated_dir.exists():
        return None
    preferred = [
        generated_dir / job / filename,
        generated_dir / filename,
    ]
    for candidate in preferred:
        if candidate.exists() and candidate.is_file():
            return candidate
    matches = sorted(generated_dir.rglob(filename), key=lambda path: (len(path.parts), str(path).lower()))
    return matches[0] if matches else None


def generated_extra_files(generated_dir: Path, examples_dir: Path, jobs: list[str]) -> list[Path]:
    if not generated_dir.exists() or not jobs:
        return []
    extras: list[Path] = []
    example_names = {path.name for path in example_files(examples_dir, jobs)}
    for job in jobs:
        for path in import_files_for_job(generated_dir, job):
            if path.name not in example_names:
                extras.append(path)
    return sorted(dict.fromkeys(extras))


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def normalize_timestamp(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    if METAHEADER_TIMESTAMP_RE.match(lines[0]):
        return ["// METAHEADER - BSRN data import at <ignored>"] + lines[1:]
    return lines


def compare_strict(
    example_path: Path,
    generated_path: Path,
    ignore_metaheader_timestamp: bool,
    max_differences: int,
) -> tuple[list[str], int, int, int | None, int | None]:
    example_lines = read_lines(example_path)
    generated_lines = read_lines(generated_path)
    cmp_example = normalize_timestamp(example_lines) if ignore_metaheader_timestamp else example_lines
    cmp_generated = normalize_timestamp(generated_lines) if ignore_metaheader_timestamp else generated_lines
    differences: list[str] = []
    if len(cmp_example) != len(cmp_generated):
        differences.append(f"line-count difference: example={len(cmp_example)}, generated={len(cmp_generated)}")

    for index, (example_line, generated_line) in enumerate(zip(cmp_example, cmp_generated), start=1):
        if example_line != generated_line:
            differences.append(
                f"line {index} differs: example={shorten(example_line)!r}; generated={shorten(generated_line)!r}"
            )
            if len(differences) >= max_differences:
                break

    if not differences and len(cmp_example) != len(cmp_generated):
        longer = cmp_example if len(cmp_example) > len(cmp_generated) else cmp_generated
        differences.append(f"first extra line {min(len(cmp_example), len(cmp_generated)) + 1}: {shorten(longer[min(len(cmp_example), len(cmp_generated))])!r}")

    example_parts = split_import_file(example_lines)
    generated_parts = split_import_file(generated_lines)
    return differences, len(example_lines), len(generated_lines), len(example_parts["data"]), len(generated_parts["data"])


def split_import_file(lines: list[str]) -> dict[str, object]:
    try:
        end_index = lines.index("// METAHEADER END")
    except ValueError:
        end_index = -1
    if end_index < 0:
        return {"meta": lines, "table_header": [], "data": []}
    table_header = lines[end_index + 1].split("\t") if len(lines) > end_index + 1 else []
    data = lines[end_index + 2 :] if len(lines) > end_index + 2 else []
    return {
        "meta": lines[: end_index + 1],
        "table_header": table_header,
        "data": data,
    }


def parse_metaheader(lines: list[str]) -> dict[str, object] | None:
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "{")
        end = next(index for index, line in enumerate(lines[start:], start=start) if line.strip() == "}")
    except StopIteration:
        return None
    text = "\n".join(lines[start : end + 1])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def compare_semantic(
    example_path: Path,
    generated_path: Path,
    ignore_metaheader_timestamp: bool,
    max_differences: int,
) -> tuple[list[str], int, int, int, int]:
    example_lines = read_lines(example_path)
    generated_lines = read_lines(generated_path)
    differences: list[str] = []
    if len(example_lines) != len(generated_lines):
        differences.append(f"line-count difference: example={len(example_lines)}, generated={len(generated_lines)}")

    example_parts = split_import_file(example_lines)
    generated_parts = split_import_file(generated_lines)
    compare_metaheader(
        example_parts["meta"],
        generated_parts["meta"],
        differences,
        ignore_metaheader_timestamp,
        max_differences,
    )
    compare_table_header(
        list(example_parts["table_header"]),
        list(generated_parts["table_header"]),
        differences,
        max_differences,
    )
    compare_data_rows(
        list(example_parts["data"]),
        list(generated_parts["data"]),
        differences,
        max_differences,
    )
    return differences, len(example_lines), len(generated_lines), len(example_parts["data"]), len(generated_parts["data"])


def compare_metaheader(
    example_meta_lines: object,
    generated_meta_lines: object,
    differences: list[str],
    ignore_metaheader_timestamp: bool,
    max_differences: int,
) -> None:
    example_lines = list(example_meta_lines)
    generated_lines = list(generated_meta_lines)
    if not example_lines or not generated_lines:
        add_difference(differences, "metaheader missing in example or generated file", max_differences)
        return
    if not ignore_metaheader_timestamp and example_lines[0] != generated_lines[0]:
        add_difference(
            differences,
            f"metaheader timestamp differs: example={shorten(example_lines[0])!r}; generated={shorten(generated_lines[0])!r}",
            max_differences,
        )

    example_meta = parse_metaheader(example_lines)
    generated_meta = parse_metaheader(generated_lines)
    if example_meta is None or generated_meta is None:
        cmp_example = normalize_timestamp(example_lines) if ignore_metaheader_timestamp else example_lines
        cmp_generated = normalize_timestamp(generated_lines) if ignore_metaheader_timestamp else generated_lines
        if cmp_example != cmp_generated:
            add_difference(differences, "metaheader text differs and could not be parsed as JSON", max_differences)
        return

    compare_header_fields(example_meta, generated_meta, differences, max_differences)
    compare_parameters(
        list(example_meta.get("ParameterIDs") or []),
        list(generated_meta.get("ParameterIDs") or []),
        differences,
        max_differences,
    )


def compare_header_fields(
    example_meta: dict[str, object],
    generated_meta: dict[str, object],
    differences: list[str],
    max_differences: int,
) -> None:
    keys = sorted((set(example_meta) | set(generated_meta)) - {"ParameterIDs"})
    for key in keys:
        if key not in example_meta:
            add_difference(differences, f"header field only in generated: {key}={generated_meta[key]!r}", max_differences)
            continue
        if key not in generated_meta:
            add_difference(differences, f"header field missing in generated: {key}", max_differences)
            continue
        if not semantic_equal(example_meta[key], generated_meta[key]):
            add_difference(
                differences,
                f"header field differs for {key}: example={example_meta[key]!r}; generated={generated_meta[key]!r}",
                max_differences,
            )


def compare_parameters(
    example_parameters: list[object],
    generated_parameters: list[object],
    differences: list[str],
    max_differences: int,
) -> None:
    if len(example_parameters) != len(generated_parameters):
        add_difference(
            differences,
            f"ParameterIDs count differs: example={len(example_parameters)}, generated={len(generated_parameters)}",
            max_differences,
        )
    for index, (example, generated) in enumerate(zip(example_parameters, generated_parameters), start=1):
        if not isinstance(example, dict) or not isinstance(generated, dict):
            if example != generated:
                add_difference(differences, f"ParameterIDs[{index}] differs: example={example!r}; generated={generated!r}", max_differences)
            continue
        for key in sorted(set(example) | set(generated)):
            if key not in example:
                add_difference(differences, f"ParameterIDs[{index}] field only in generated: {key}={generated[key]!r}", max_differences)
                continue
            if key not in generated:
                add_difference(differences, f"ParameterIDs[{index}] field missing in generated: {key}", max_differences)
                continue
            if not semantic_equal(example[key], generated[key]):
                label = parameter_label(index, example, generated)
                add_difference(
                    differences,
                    f"{label} {key} differs: example={example[key]!r}; generated={generated[key]!r}",
                    max_differences,
                )


def parameter_label(index: int, example: dict[str, object], generated: dict[str, object]) -> str:
    parameter_id = example.get("ID", generated.get("ID", "?"))
    return f"ParameterIDs[{index}] ID={parameter_id}"


def compare_table_header(
    example_header: list[str],
    generated_header: list[str],
    differences: list[str],
    max_differences: int,
) -> None:
    if example_header != generated_header:
        add_difference(
            differences,
            f"table header differs: example={example_header}; generated={generated_header}",
            max_differences,
        )


def compare_data_rows(
    example_rows: list[str],
    generated_rows: list[str],
    differences: list[str],
    max_differences: int,
) -> None:
    if len(example_rows) != len(generated_rows):
        add_difference(
            differences,
            f"data row-count difference: example={len(example_rows)}, generated={len(generated_rows)}",
            max_differences,
        )

    mismatch_count = 0
    for row_index, (example_row, generated_row) in enumerate(zip(example_rows, generated_rows), start=1):
        example_values = example_row.split("\t")
        generated_values = generated_row.split("\t")
        if len(example_values) != len(generated_values):
            mismatch_count += 1
            add_difference(
                differences,
                f"data row {row_index} column-count differs: example={len(example_values)}, generated={len(generated_values)}",
                max_differences,
            )
            if mismatch_count >= max_differences:
                return
            continue
        for column_index, (example_value, generated_value) in enumerate(zip(example_values, generated_values), start=1):
            if not cell_equal(example_value, generated_value):
                mismatch_count += 1
                add_difference(
                    differences,
                    (
                        f"data row {row_index} column {column_index} differs: "
                        f"example={shorten(example_value)!r}; generated={shorten(generated_value)!r}"
                    ),
                    max_differences,
                )
                break
        if mismatch_count >= max_differences:
            return


def add_difference(differences: list[str], message: str, max_differences: int) -> None:
    if len(differences) < max_differences:
        differences.append(message)


def semantic_equal(left: object, right: object) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(semantic_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(semantic_equal(left[key], right[key]) for key in left)
    if isinstance(left, (int, float, Decimal)) and isinstance(right, (int, float, Decimal)):
        return Decimal(str(left)) == Decimal(str(right))
    if isinstance(left, str) and isinstance(right, str):
        return cell_equal(left, right)
    return left == right


def cell_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    left_stripped = left.strip()
    right_stripped = right.strip()
    if left_stripped == right_stripped:
        return True
    left_decimal = numeric_decimal(left_stripped)
    right_decimal = numeric_decimal(right_stripped)
    if left_decimal is not None and right_decimal is not None:
        return left_decimal == right_decimal
    return False


def numeric_decimal(value: str) -> Decimal | None:
    if not value or not NUMERIC_RE.fullmatch(value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def shorten(value: str, limit: int = 140) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def compare_files(
    example_path: Path,
    generated_path: Path | None,
    mode: str,
    ignore_metaheader_timestamp: bool,
    max_differences: int,
) -> FileReport:
    job = job_from_filename(example_path)
    report = FileReport(
        filename=example_path.name,
        job=job,
        example_path=rel_or_abs(example_path),
        generated_path=rel_or_abs(generated_path) if generated_path else None,
        mode=mode,
    )
    if generated_path is None:
        report.status = "missing_generated"
        report.differences.append("generated file is missing")
        report.line_count_example = len(read_lines(example_path))
        return report

    if mode == "strict":
        differences, example_count, generated_count, example_data_count, generated_data_count = compare_strict(
            example_path,
            generated_path,
            ignore_metaheader_timestamp,
            max_differences,
        )
    else:
        differences, example_count, generated_count, example_data_count, generated_data_count = compare_semantic(
            example_path,
            generated_path,
            ignore_metaheader_timestamp,
            max_differences,
        )
    report.line_count_example = example_count
    report.line_count_generated = generated_count
    report.data_row_count_example = example_data_count
    report.data_row_count_generated = generated_data_count
    report.differences = differences
    report.review_notes = known_difference_notes(differences)
    report.status = "different" if differences else "match"
    return report


def known_difference_notes(differences: list[str]) -> list[str]:
    notes: list[str] = []
    joined = "\n".join(differences)
    if "header field only in generated: ParentID=" in joined:
        notes.append("Known acceptable: generated LR0100/LR0100+LR0300 files can include station ParentID added after older Tool 1 examples.")
    if "header field differs for LoginID:" in joined and "generated=1" in joined:
        notes.append("Known acceptable for older fixtures: generated files use the current required LoginID=1.")
    if "header field only in generated: CurationLevelID=30" in joined:
        notes.append("Known acceptable for older fixtures: generated files include current required CurationLevelID=30.")
    if "header field only in generated: LicenseID=107" in joined:
        notes.append("Known acceptable for older fixtures: generated files include current required LicenseID=107.")
    if any(note.startswith("Known acceptable: generated LR0100") for note in notes) and any(
        difference.startswith("line-count difference:") for difference in differences
    ):
        notes.append("Line-count differences in the same file may be caused by the added ParentID metaheader line.")
    return notes


def rel_or_abs(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def write_plain_report(report: RunReport) -> None:
    print("BSRN import comparison")
    print(f"Mode:          {report.mode}")
    print(f"Examples:      {report.examples_dir}")
    print(f"Generated:     {report.generated_dir}")
    print(f"Jobs:          {', '.join(report.jobs) if report.jobs else '(all examples)'}")
    print(
        "Summary:       "
        f"matched={report.matched}, different={report.different}, "
        f"missing_generated={report.missing_generated}, missing_examples={report.missing_examples}"
    )
    print()

    for item in report.files:
        if item.status == "match":
            print(f"OK      {item.filename}")
            continue
        print(f"{item.status.upper():<8} {item.filename}")
        if item.example_path:
            print(f"  example:   {item.example_path}")
        if item.generated_path:
            print(f"  generated: {item.generated_path}")
        if item.line_count_example is not None or item.line_count_generated is not None:
            print(f"  lines:     example={item.line_count_example}, generated={item.line_count_generated}")
        if item.data_row_count_example is not None or item.data_row_count_generated is not None:
            print(f"  data rows: example={item.data_row_count_example}, generated={item.data_row_count_generated}")
        for difference in item.differences:
            print(f"  - {difference}")
        for note in item.review_notes:
            print(f"  note: {note}")
        print()


def run(args: argparse.Namespace) -> int:
    examples_dir = project_path(args.examples_dir)
    generated_dir = project_path(args.generated_dir)
    jobs = list(args.job or [])
    if args.known_regression_jobs:
        jobs = list(dict.fromkeys([*jobs, *KNOWN_REGRESSION_JOBS]))

    examples = example_files(examples_dir, jobs)
    report = RunReport(
        mode=args.mode,
        examples_dir=rel_or_abs(examples_dir) or str(examples_dir),
        generated_dir=rel_or_abs(generated_dir) or str(generated_dir),
        jobs=jobs,
    )

    for example_path in examples:
        job = job_from_filename(example_path)
        generated_path = find_generated_file(generated_dir, job, example_path.name)
        file_report = compare_files(
            example_path,
            generated_path,
            args.mode,
            args.ignore_metaheader_timestamp,
            args.max_differences,
        )
        report.files.append(file_report)

    if not examples and jobs:
        for job in jobs:
            generated = import_files_for_job(generated_dir, job)
            if generated:
                continue
            report.files.append(
                FileReport(
                    filename=f"{job}_*_imp.txt",
                    job=job,
                    status="missing_examples",
                    mode=args.mode,
                    differences=["no Tool 1 example import files found for requested job"],
                )
            )

    if not args.no_extra_generated:
        for path in generated_extra_files(generated_dir, examples_dir, jobs):
            report.files.append(
                FileReport(
                    filename=path.name,
                    job=job_from_filename(path),
                    generated_path=rel_or_abs(path),
                    status="missing_examples",
                    mode=args.mode,
                    differences=["generated file has no matching Tool 1 example"],
                    line_count_generated=len(read_lines(path)),
                )
            )

    report.compared = sum(1 for item in report.files if item.status in {"match", "different"})
    report.matched = sum(1 for item in report.files if item.status == "match")
    report.different = sum(1 for item in report.files if item.status == "different")
    report.missing_generated = sum(1 for item in report.files if item.status == "missing_generated")
    report.missing_examples = sum(1 for item in report.files if item.status == "missing_examples")

    if args.json_output:
        json_path = project_path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        write_plain_report(report)
    return 0 if report.different == 0 and report.missing_generated == 0 and report.missing_examples == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated-dir", default=str(DEFAULT_GENERATED_DIR), help="Generated import-file root")
    parser.add_argument("--examples-dir", default=str(DEFAULT_EXAMPLES_DIR), help="Tool 1 example import-file root")
    parser.add_argument("--job", action="append", type=normalize_job, help="Job label such as DRA_2025-02; repeatable")
    parser.add_argument("--known-regression-jobs", action="store_true", help="Compare the planned Milestone 15 regression jobs")
    parser.add_argument("--mode", choices=["strict", "semantic"], default="semantic")
    parser.add_argument("--ignore-metaheader-timestamp", action="store_true", help="Ignore the first METAHEADER timestamp line")
    parser.add_argument("--max-differences", type=int, default=8, help="Maximum differences reported per file")
    parser.add_argument("--json-output", help="Optional JSON report path")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of plain text")
    parser.add_argument("--no-extra-generated", action="store_true", help="Do not report generated files without examples for requested jobs")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
