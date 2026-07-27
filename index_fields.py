import argparse
import re
import sys

import mysql.connector
from mysql.connector import Error


def quote_identifier(identifier: str) -> str:
    """
    Safely quote a MySQL table, column, or index identifier.
    """
    return f"`{identifier.replace('`', '``')}`"


def clean_index_name(table_name: str, column_name: str) -> str:
    """
    Create a valid MySQL index name.

    MySQL index names are limited to 64 characters.
    """
    name = f"idx_{table_name}_{column_name}"
    name = re.sub(r"[^A-Za-z0-9_]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")

    return name[:64]


def get_table_columns(connection, table_name: str) -> set[str]:
    """
    Return the columns present in the specified table.
    """
    cursor = connection.cursor()

    try:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
            """,
            (table_name,),
        )

        return {
            row[0]
            for row in cursor.fetchall()
        }

    finally:
        cursor.close()


def get_existing_indexes(connection, table_name: str) -> set[str]:
    """
    Return the index names already present on the table.
    """
    cursor = connection.cursor()

    try:
        cursor.execute(
            """
            SELECT DISTINCT INDEX_NAME
            FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
            """,
            (table_name,),
        )

        return {
            row[0]
            for row in cursor.fetchall()
        }

    finally:
        cursor.close()


def add_indexes(
    connection,
    table_name: str,
    field_names: list[str],
) -> None:
    """
    Add a separate index for each requested field.
    """
    table_columns = get_table_columns(
        connection,
        table_name,
    )

    if not table_columns:
        raise RuntimeError(
            f"Table not found or has no columns: {table_name}"
        )

    missing_fields = [
        field_name
        for field_name in field_names
        if field_name not in table_columns
    ]

    if missing_fields:
        missing_list = ", ".join(missing_fields)

        raise RuntimeError(
            f"The following fields do not exist in "
            f"{table_name}: {missing_list}"
        )

    existing_indexes = get_existing_indexes(
        connection,
        table_name,
    )

    cursor = connection.cursor()

    try:
        for field_name in field_names:
            index_name = clean_index_name(
                table_name,
                field_name,
            )

            if index_name in existing_indexes:
                print(
                    f"Skipping existing index: {index_name}"
                )
                continue

            sql = (
                f"CREATE INDEX {quote_identifier(index_name)} "
                f"ON {quote_identifier(table_name)} "
                f"({quote_identifier(field_name)})"
            )

            print(
                f"Creating index {index_name} "
                f"on {table_name}.{field_name}..."
            )

            cursor.execute(sql)
            connection.commit()

            existing_indexes.add(index_name)

            print(f"Created index: {index_name}")

    except Exception:
        connection.rollback()
        raise

    finally:
        cursor.close()


def parse_field_list(value: str) -> list[str]:
    """
    Parse a comma-separated list of field names.
    """
    fields = [
        field.strip()
        for field in value.split(",")
        if field.strip()
    ]

    if not fields:
        raise argparse.ArgumentTypeError(
            "At least one field name must be supplied."
        )

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(fields))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Add separate indexes to a MySQL table using "
            "a comma-separated list of field names."
        )
    )

    parser.add_argument(
        "table_name",
        help="Name of the MySQL table.",
    )

    parser.add_argument(
        "fields",
        type=parse_field_list,
        help=(
            "Comma-separated list of fields to index, "
            'for example: "scientificName,family,country"'
        ),
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
        help="MySQL database name.",
    )

    args = parser.parse_args()

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

        add_indexes(
            connection=connection,
            table_name=args.table_name,
            field_names=args.fields,
        )

        print("\nIndex creation complete.")

    except Error as error:
        print(
            f"\nMySQL error: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    except RuntimeError as error:
        print(
            f"\nError: {error}",
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
