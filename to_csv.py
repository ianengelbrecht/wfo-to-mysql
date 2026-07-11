import argparse
import csv
from pathlib import Path


PROGRESS_EVERY = 10_000


def tsv_to_csv(input_path: Path, output_path: Path) -> None:
    rows_processed = 0

    with input_path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as input_file, output_path.open(
        "w",
        encoding="utf-8",
        newline="",
    ) as output_file:
        reader = csv.reader(
            input_file,
            delimiter="\t",
            quotechar='"',
        )

        writer = csv.writer(
            output_file,
            delimiter=",",
            quotechar='"',
            quoting=csv.QUOTE_MINIMAL,
        )

        for row in reader:
            writer.writerow(row)
            rows_processed += 1

            if rows_processed % PROGRESS_EVERY == 0:
                print(
                    f"\rRows converted: {rows_processed:,}",
                    end="",
                    flush=True,
                )

    print(f"\rRows converted: {rows_processed:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a tab-separated TSV file to CSV."
    )
    parser.add_argument(
        "input_file",
        type=Path,
        help="Path to the input TSV file.",
    )
    parser.add_argument(
        "output_file",
        type=Path,
        nargs="?",
        help="Output CSV file. Defaults to the input filename with .csv.",
    )

    args = parser.parse_args()

    input_path = args.input_file
    output_path = args.output_file or input_path.with_suffix(".csv")

    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    tsv_to_csv(input_path, output_path)

    print(f"Output written to: {output_path}")


if __name__ == "__main__":
    main()