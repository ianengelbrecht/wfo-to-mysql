import csv
from os import path

dir = r"C:\Users\ianic\Downloads"
input_file = path.join(dir, "CITES-plants-2026-07-10-08-56-OpenRefine_higher-listings_select.csv")
output_file = path.join(dir, "CITES-plants-2026-07-10-08-56-OpenRefine_higher-listings_select_unique.csv")

seen = set()

with open(input_file, "r", newline="", encoding="utf-8-sig") as infile, \
     open(output_file, "w", newline="", encoding="utf-8") as outfile:

    reader = csv.reader(infile)
    writer = csv.writer(outfile)

    header = next(reader)
    writer.writerow(header)

    for row in reader:
        row_key = tuple(row)

        if row_key not in seen:
            seen.add(row_key)
            writer.writerow(row)

print(f"Unique rows written to {output_file}")