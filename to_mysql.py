import argparse
import csv
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import mysql.connector
from mysql.connector import Error


DEFAULT_SAMPLE_FACTOR = 0.6
DEFAULT_BATCH_SIZE = 5_000
PROGRESS_EVERY = 10_000

MAX_VARCHAR_LENGTH = 1_000
TEXT_MAX_LENGTH = 65_535
MEDIUMTEXT_MAX_LENGTH = 16_777_215

DEFAULT_MAX_ANALYSIS_ROWS = 50_000


# =============================================================================
# COLUMN INFORMATION
# =============================================================================


@dataclass
class ColumnInfo:
    original_name: str
    mysql_name: str
    inferred_type: Optional[str] = None
    max_length: int = 0
    max_integer_digits: int = 0
    max_decimal_places: int = 0

    # The currently created string type and length.
    mysql_string_type: Optional[str] = None
    mysql_length: Optional[int] = None


# =============================================================================
# MYSQL IDENTIFIERS
# =============================================================================


def clean_identifier(value: str, fallback: str) -> str:
    """
    Convert a CSV header into a safe MySQL identifier.
    """
    value = value.strip()
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip("_").lower()

    if not value:
        value = fallback

    if value[0].isdigit():
        value = f"column_{value}"

    return value[:64]


def make_unique_identifiers(headers: list[str]) -> list[str]:
    """
    Create unique MySQL-safe column names.
    """
    names = []
    used_names = set()

    for index, header in enumerate(headers, start=1):
        base_name = clean_identifier(
            header,
            fallback=f"column_{index}",
        )

        name = base_name
        suffix = 2

        while name.lower() in used_names:
            suffix_text = f"_{suffix}"

            name = (
                f"{base_name[:64 - len(suffix_text)]}"
                f"{suffix_text}"
            )

            suffix += 1

        used_names.add(name.lower())
        names.append(name)

    return names


def quote_identifier(identifier: str) -> str:
    """
    Quote a MySQL identifier.
    """
    escaped = identifier.replace("`", "``")
    return f"`{escaped}`"


# =============================================================================
# TYPE DETECTION
# =============================================================================


INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")

DECIMAL_PATTERN = re.compile(
    r"^[+-]?(?:\d+\.\d*|\d*\.\d+)$"
)

DATE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}$"
)

DATETIME_PATTERNS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
)


def has_significant_leading_zero(value: str) -> bool:
    """
    Return True for integer-like values such as 00123.

    These values are treated as text so leading zeroes are preserved.
    """
    unsigned = value.lstrip("+-")

    return (
        len(unsigned) > 1
        and unsigned.startswith("0")
        and unsigned.isdigit()
    )


def is_valid_bigint(value: str) -> bool:
    """
    Check whether a value fits in a signed MySQL BIGINT.
    """
    if not INTEGER_PATTERN.fullmatch(value):
        return False

    if has_significant_leading_zero(value):
        return False

    try:
        number = int(value)
    except ValueError:
        return False

    return (
        -9_223_372_036_854_775_808
        <= number
        <= 9_223_372_036_854_775_807
    )


def get_decimal_properties(
    value: str,
) -> Optional[tuple[int, int]]:
    """
    Return the integer digit count and decimal-place count.
    """
    if not DECIMAL_PATTERN.fullmatch(value):
        return None

    try:
        decimal_value = Decimal(value)
    except InvalidOperation:
        return None

    if not decimal_value.is_finite():
        return None

    unsigned = value.lstrip("+-")
    integer_part, decimal_part = unsigned.split(".", 1)

    integer_digits = max(
        1,
        len(integer_part.lstrip("0")),
    )

    decimal_places = len(decimal_part)

    return integer_digits, decimal_places


def is_valid_date(value: str) -> bool:
    """
    Check for an ISO date in YYYY-MM-DD format.
    """
    if not DATE_PATTERN.fullmatch(value):
        return False

    try:
        parsed = datetime.strptime(
            value,
            "%Y-%m-%d",
        )
    except ValueError:
        return False

    return 1000 <= parsed.year <= 9999


def is_valid_datetime(value: str) -> bool:
    """
    Check for a supported ISO datetime format.
    """
    for pattern in DATETIME_PATTERNS:
        try:
            parsed = datetime.strptime(
                value,
                pattern,
            )
        except ValueError:
            continue

        return 1000 <= parsed.year <= 9999

    return False


def detect_value_type(value: str) -> str:
    """
    Infer the most specific supported type for a value.
    """
    if is_valid_bigint(value):
        return "BIGINT"

    if get_decimal_properties(value) is not None:
        return "DECIMAL"

    if is_valid_date(value):
        return "DATE"

    if is_valid_datetime(value):
        return "DATETIME"

    return "TEXT"


def combine_types(
    current_type: Optional[str],
    new_type: str,
) -> str:
    """
    Combine detected types into a type that can represent both.
    """
    if current_type is None:
        return new_type

    if current_type == new_type:
        return current_type

    if {current_type, new_type} == {
        "BIGINT",
        "DECIMAL",
    }:
        return "DECIMAL"

    if {current_type, new_type} == {
        "DATE",
        "DATETIME",
    }:
        return "DATETIME"

    return "TEXT"


# =============================================================================
# CSV ANALYSIS
# =============================================================================


def analyse_csv(
    csv_path: Path,
    delimiter: str,
    sample_factor: float,
    random_seed: Optional[int],
    max_analysis_rows: int,
) -> tuple[list[ColumnInfo], int, int]:
    """
    Scan the complete CSV but include each row in schema analysis with the
    given probability.
    """
    print(
        "Analysing CSV with a "
        f"{sample_factor:.0%} row inclusion probability..."
    )

    random_generator = random.Random(random_seed)

    with csv_path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as csv_file:
        reader = csv.reader(
            csv_file,
            delimiter=delimiter,
            quotechar='"',
        )

        try:
            headers = next(reader)
        except StopIteration:
            raise RuntimeError("The CSV file is empty.")

        if not headers:
            raise RuntimeError(
                "The CSV file does not contain a header row."
            )

        mysql_names = make_unique_identifiers(headers)

        columns = [
            ColumnInfo(
                original_name=header,
                mysql_name=mysql_name,
            )
            for header, mysql_name in zip(
                headers,
                mysql_names,
            )
        ]

        rows_read = 0
        rows_sampled = 0

        for line_number, row in enumerate(
            reader,
            start=2,
        ):
            
            if rows_read >= max_analysis_rows:
                break
            
            if len(row) != len(columns):
                raise RuntimeError(
                    f"Line {line_number:,} contains "
                    f"{len(row)} fields, but the header "
                    f"contains {len(columns)} fields."
                )

            rows_read += 1

            include_row = (
                random_generator.random()
                <= sample_factor
            )

            if include_row:
                rows_sampled += 1

                for column, raw_value in zip(
                    columns,
                    row,
                ):
                    value = raw_value.strip()

                    if value == "":
                        continue

                    column.max_length = max(
                        column.max_length,
                        len(raw_value),
                    )

                    detected_type = detect_value_type(
                        value
                    )

                    column.inferred_type = combine_types(
                        column.inferred_type,
                        detected_type,
                    )

                    if detected_type == "BIGINT":
                        column.max_integer_digits = max(
                            column.max_integer_digits,
                            len(value.lstrip("+-")),
                        )

                    elif detected_type == "DECIMAL":
                        properties = (
                            get_decimal_properties(value)
                        )

                        if properties is not None:
                            (
                                integer_digits,
                                decimal_places,
                            ) = properties

                            column.max_integer_digits = max(
                                column.max_integer_digits,
                                integer_digits,
                            )

                            column.max_decimal_places = max(
                                column.max_decimal_places,
                                decimal_places,
                            )

            if rows_read % PROGRESS_EVERY == 0:
                print(
                    (
                        f"\rRows scanned: {rows_read:,} | "
                        f"Rows sampled: {rows_sampled:,}"
                    ),
                    end="",
                    flush=True,
                )

    print(
        (
            f"\rRows scanned: {rows_read:,} | "
            f"Rows sampled: {rows_sampled:,}"
        )
    )

    for column in columns:
        if column.inferred_type is None:
            # No populated value appeared in the sampled rows.
            column.inferred_type = "TEXT"
            column.max_length = 50

    return columns, rows_read, rows_sampled


# =============================================================================
# MYSQL TYPES
# =============================================================================


def round_up_to_nearest_ten(value: int) -> int:
    """
    Round a positive integer up to the nearest multiple of ten.
    """
    return ((value + 9) // 10) * 10


def determine_string_type(
    length: int,
) -> tuple[str, Optional[int]]:
    """
    Return the appropriate MySQL string type and tracked VARCHAR length.

    The second tuple value is None for TEXT types.
    """
    length = max(50, length)
    length = round_up_to_nearest_ten(length)

    if length <= MAX_VARCHAR_LENGTH:
        return f"VARCHAR({length})", length

    if length <= TEXT_MAX_LENGTH:
        return "TEXT", None

    if length <= MEDIUMTEXT_MAX_LENGTH:
        return "MEDIUMTEXT", None

    return "LONGTEXT", None


def initialize_mysql_types(
    columns: list[ColumnInfo],
) -> None:
    """
    Set the initial MySQL string type metadata.
    """
    for column in columns:
        if column.inferred_type != "TEXT":
            continue

        mysql_type, mysql_length = determine_string_type(
            column.max_length
        )

        column.mysql_string_type = mysql_type
        column.mysql_length = mysql_length


def get_mysql_type(column: ColumnInfo) -> str:
    """
    Return the current MySQL type for a column.
    """
    if column.inferred_type == "BIGINT":
        return "BIGINT"

    if column.inferred_type == "DECIMAL":
        integer_digits = max(
            1,
            column.max_integer_digits,
        )

        decimal_places = column.max_decimal_places
        precision = integer_digits + decimal_places

        if (
            precision <= 65
            and decimal_places <= 30
        ):
            return (
                f"DECIMAL("
                f"{precision},"
                f"{decimal_places}"
                f")"
            )

        if column.mysql_string_type is None:
            mysql_type, mysql_length = (
                determine_string_type(
                    column.max_length
                )
            )

            column.mysql_string_type = mysql_type
            column.mysql_length = mysql_length

        return column.mysql_string_type

    if column.inferred_type == "DATE":
        return "DATE"

    if column.inferred_type == "DATETIME":
        return "DATETIME(6)"

    if column.mysql_string_type is None:
        mysql_type, mysql_length = determine_string_type(
            column.max_length
        )

        column.mysql_string_type = mysql_type
        column.mysql_length = mysql_length

    return column.mysql_string_type


def display_column_types(
    columns: list[ColumnInfo],
) -> None:
    """
    Display the inferred schema.
    """
    print("\nDetected columns:")

    original_width = max(
        len("CSV column"),
        *(
            len(column.original_name)
            for column in columns
        ),
    )

    mysql_width = max(
        len("MySQL column"),
        *(
            len(column.mysql_name)
            for column in columns
        ),
    )

    print(
        f"{'CSV column':<{original_width}}  "
        f"{'MySQL column':<{mysql_width}}  "
        f"MySQL type"
    )

    print(
        f"{'-' * original_width}  "
        f"{'-' * mysql_width}  "
        f"{'-' * 24}"
    )

    for column in columns:
        print(
            f"{column.original_name:<{original_width}}  "
            f"{column.mysql_name:<{mysql_width}}  "
            f"{get_mysql_type(column)} NULL"
        )


# =============================================================================
# TABLE CREATION
# =============================================================================


def create_table(
    connection,
    table_name: str,
    columns: list[ColumnInfo],
    drop_existing: bool,
) -> None:
    """
    Create the MySQL table.
    """
    cursor = connection.cursor()

    try:
        quoted_table = quote_identifier(table_name)

        if drop_existing:
            cursor.execute(
                f"DROP TABLE IF EXISTS {quoted_table}"
            )

        column_definitions = [
            (
                f"{quote_identifier(column.mysql_name)} "
                f"{get_mysql_type(column)} NULL"
            )
            for column in columns
        ]

        create_sql = (
            f"CREATE TABLE {quoted_table} (\n    "
            + ",\n    ".join(column_definitions)
            + "\n) "
            "CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci"
        )

        cursor.execute(create_sql)
        connection.commit()

    finally:
        cursor.close()


# =============================================================================
# AUTOMATIC STRING COLUMN EXPANSION
# =============================================================================


def get_batch_max_lengths(
    batch: list[tuple],
    columns: list[ColumnInfo],
) -> list[int]:
    """
    Find the longest value in each tracked string column in the batch.
    """
    max_lengths = [0] * len(columns)

    for row in batch:
        for column_index, value in enumerate(row):
            column = columns[column_index]

            if column.mysql_string_type is None:
                continue

            if value is None:
                continue

            max_lengths[column_index] = max(
                max_lengths[column_index],
                len(str(value)),
            )

    return max_lengths


def determine_expanded_string_type(
    current_length: Optional[int],
    required_length: int,
) -> tuple[str, Optional[int]]:
    """
    Select a larger string type.

    VARCHAR columns expand to at least twice their current length, rounded
    up to the nearest ten. Larger values are promoted to a TEXT type.
    """
    if current_length is not None:
        target_length = max(
            required_length,
            current_length * 2,
        )
    else:
        target_length = required_length

    return determine_string_type(target_length)


def expand_columns_for_batch(
    connection,
    table_name: str,
    columns: list[ColumnInfo],
    batch: list[tuple],
) -> None:
    """
    Expand VARCHAR or text columns before inserting a batch when necessary.
    """
    batch_max_lengths = get_batch_max_lengths(
        batch,
        columns,
    )

    alterations = []

    for column_index, required_length in enumerate(
        batch_max_lengths
    ):
        if required_length == 0:
            continue

        column = columns[column_index]

        if column.mysql_string_type is None:
            continue

        current_type = column.mysql_string_type
        current_length = column.mysql_length

        if current_length is not None:
            needs_expansion = (
                required_length > current_length
            )
        elif current_type == "TEXT":
            needs_expansion = (
                required_length > TEXT_MAX_LENGTH
            )
        elif current_type == "MEDIUMTEXT":
            needs_expansion = (
                required_length > MEDIUMTEXT_MAX_LENGTH
            )
        else:
            # LONGTEXT cannot be expanded further.
            needs_expansion = False

        if not needs_expansion:
            continue

        if current_type == "TEXT":
            if required_length <= MEDIUMTEXT_MAX_LENGTH:
                new_type = "MEDIUMTEXT"
            else:
                new_type = "LONGTEXT"

            new_length = None

        elif current_type == "MEDIUMTEXT":
            new_type = "LONGTEXT"
            new_length = None

        else:
            new_type, new_length = (
                determine_expanded_string_type(
                    current_length=current_length,
                    required_length=required_length,
                )
            )

        alterations.append(
            (
                column,
                current_type,
                new_type,
                new_length,
                required_length,
            )
        )

    if not alterations:
        return

    cursor = connection.cursor()

    try:
        for (
            column,
            old_type,
            new_type,
            new_length,
            required_length,
        ) in alterations:
            alter_sql = (
                f"ALTER TABLE "
                f"{quote_identifier(table_name)} "
                f"MODIFY COLUMN "
                f"{quote_identifier(column.mysql_name)} "
                f"{new_type} NULL"
            )

            cursor.execute(alter_sql)

            column.mysql_string_type = new_type
            column.mysql_length = new_length

            print(
                f"\nExpanded {column.mysql_name}: "
                f"{old_type} -> {new_type} "
                f"(encountered {required_length:,} characters)"
            )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        cursor.close()


# =============================================================================
# CSV IMPORT
# =============================================================================


def normalize_value(
    value: str,
    inferred_type: str,
):
    """
    Convert blanks to NULL and normalize datetime separators.
    """
    value = value.strip()

    if value == "":
        return None

    if (
        inferred_type == "DATETIME"
        and "T" in value
    ):
        return value.replace(
            "T",
            " ",
            1,
        )

    return value


def insert_batch(
    connection,
    cursor,
    insert_sql: str,
    table_name: str,
    columns: list[ColumnInfo],
    batch: list[tuple],
) -> int:
    """
    Expand columns if needed and insert one batch.
    """
    expand_columns_for_batch(
        connection=connection,
        table_name=table_name,
        columns=columns,
        batch=batch,
    )

    cursor.executemany(
        insert_sql,
        batch,
    )

    connection.commit()

    return len(batch)


def import_csv(
    connection,
    csv_path: Path,
    table_name: str,
    columns: list[ColumnInfo],
    delimiter: str,
    batch_size: int,
) -> int:
    """
    Import the complete CSV file.
    """
    quoted_columns = ", ".join(
        quote_identifier(column.mysql_name)
        for column in columns
    )

    placeholders = ", ".join(
        ["%s"] * len(columns)
    )

    insert_sql = (
        f"INSERT INTO "
        f"{quote_identifier(table_name)} "
        f"({quoted_columns}) "
        f"VALUES ({placeholders})"
    )

    inferred_types = [
        column.inferred_type or "TEXT"
        for column in columns
    ]

    rows_imported = 0
    batch = []
    cursor = connection.cursor()

    print("\nImporting records...")

    try:
        with csv_path.open(
            "r",
            encoding="utf-8",
            newline="",
        ) as csv_file:
            reader = csv.reader(
                csv_file,
                delimiter=delimiter,
                quotechar='"',
            )

            next(reader)

            for line_number, row in enumerate(
                reader,
                start=2,
            ):
                if len(row) != len(columns):
                    raise RuntimeError(
                        f"Line {line_number:,} contains "
                        f"{len(row)} fields, but "
                        f"{len(columns)} were expected."
                    )

                normalized_row = tuple(
                    normalize_value(
                        value,
                        inferred_type,
                    )
                    for value, inferred_type in zip(
                        row,
                        inferred_types,
                    )
                )

                batch.append(normalized_row)

                if len(batch) >= batch_size:
                    rows_imported += insert_batch(
                        connection=connection,
                        cursor=cursor,
                        insert_sql=insert_sql,
                        table_name=table_name,
                        columns=columns,
                        batch=batch,
                    )

                    batch.clear()

                    print(
                        f"\rRows imported: "
                        f"{rows_imported:,}",
                        end="",
                        flush=True,
                    )

            if batch:
                rows_imported += insert_batch(
                    connection=connection,
                    cursor=cursor,
                    insert_sql=insert_sql,
                    table_name=table_name,
                    columns=columns,
                    batch=batch,
                )

        print(
            f"\rRows imported: "
            f"{rows_imported:,}"
        )

        return rows_imported

    except Exception:
        connection.rollback()
        raise

    finally:
        cursor.close()


# =============================================================================
# COMMAND-LINE ARGUMENTS
# =============================================================================


def parse_delimiter(value: str) -> str:
    """
    Parse a command-line delimiter.
    """
    if value == r"\t":
        return "\t"

    if len(value) != 1:
        raise argparse.ArgumentTypeError(
            "The delimiter must be a single character."
        )

    return value


def parse_sample_factor(value: str) -> float:
    """
    Validate the row sampling probability.
    """
    try:
        factor = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "The sample factor must be a number."
        ) from error

    if not 0 < factor <= 1:
        raise argparse.ArgumentTypeError(
            "The sample factor must be greater than "
            "0 and no greater than 1."
        )

    return factor


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Analyse a UTF-8 CSV file, create a MySQL table, "
            "and import all records while automatically expanding "
            "string columns when longer values are encountered."
        )
    )

    parser.add_argument(
        "csv_file",
        type=Path,
        help="UTF-8 CSV file to import.",
    )

    parser.add_argument(
        "table_name",
        help="Name of the MySQL table to create.",
    )

    parser.add_argument(
        "--host",
        default="localhost",
        help="MySQL host. Default: localhost",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=3306,
        help="MySQL port. Default: 3306",
    )

    parser.add_argument(
        "-u",
        "--user",
        required=True,
        help="MySQL username.",
    )

    parser.add_argument(
        "-p",
        "--password",
        required=True,
        help="MySQL password.",
    )

    parser.add_argument(
        "-d",
        "--database",
        required=True,
        help="Existing MySQL database.",
    )

    parser.add_argument(
        "-s",
        "--sample-factor",
        type=parse_sample_factor,
        default=DEFAULT_SAMPLE_FACTOR,
        help=(
            "Probability that each row is included in initial "
            "schema analysis. "
            f"Default: {DEFAULT_SAMPLE_FACTOR}"
        ),
    )

    parser.add_argument(
        "-m",
        "--max-analysis-rows",
        type=int,
        default=DEFAULT_MAX_ANALYSIS_ROWS,
        help=(
            "Maximum number of CSV records scanned during schema analysis. "
            f"Default: {DEFAULT_MAX_ANALYSIS_ROWS:,}"
        ),
    )

    parser.add_argument(
        "-r",
        "--random-seed",
        type=int,
        default=None,
        help=(
            "Optional random seed for repeatable sampling."
        ),
    )

    parser.add_argument(
        "-b",
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=(
            "Rows inserted per batch. "
            f"Default: {DEFAULT_BATCH_SIZE:,}"
        ),
    )

    parser.add_argument(
        "--delimiter",
        type=parse_delimiter,
        default=",",
        help="Field delimiter. Default: ','",
    )

    parser.add_argument(
        "--drop-table",
        action="store_true",
        help="Drop the table first if it already exists.",
    )

    args = parser.parse_args()

    if not args.csv_file.is_file():
        parser.error(
            f"CSV file not found: {args.csv_file}"
        )

    if args.batch_size < 1:
        parser.error(
            "--batch-size must be at least 1."
        )

    if args.max_analysis_rows < 1:
        parser.error("--max-analysis-rows must be at least 1.")

    columns, rows_read, rows_sampled = analyse_csv(
        csv_path=args.csv_file,
        delimiter=args.delimiter,
        sample_factor=args.sample_factor,
        random_seed=args.random_seed,
        max_analysis_rows=args.max_analysis_rows,
    )

    initialize_mysql_types(columns)
    display_column_types(columns)

    connection = None

    try:
        connection = mysql.connector.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            database=args.database,
            charset="utf8mb4",
            use_unicode=True,
        )

        create_table(
            connection=connection,
            table_name=args.table_name,
            columns=columns,
            drop_existing=args.drop_table,
        )

        print(
            f"\nCreated table: {args.table_name}"
        )

        rows_imported = import_csv(
            connection=connection,
            csv_path=args.csv_file,
            table_name=args.table_name,
            columns=columns,
            delimiter=args.delimiter,
            batch_size=args.batch_size,
        )

        actual_sample_percentage = (
            rows_sampled / rows_read * 100
            if rows_read
            else 0
        )

        print(
            f"\nImport complete: "
            f"{rows_imported:,} rows imported."
        )

        print(
            f"Schema analysis scanned "
            f"{rows_read:,} rows and sampled "
            f"{rows_sampled:,} rows "
            f"({actual_sample_percentage:.1f}%)."
        )

    except Error as error:
        print(
            f"\nMySQL error: {error}",
            file=sys.stderr,
        )

        raise SystemExit(1)

    except (
        OSError,
        RuntimeError,
        csv.Error,
    ) as error:
        print(
            f"\nImport error: {error}",
            file=sys.stderr,
        )

        raise SystemExit(1)

    finally:
        if (
            connection is not None
            and connection.is_connected()
        ):
            connection.close()


if __name__ == "__main__":
    main()
