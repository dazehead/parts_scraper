import csv
from .parser import Parser
import pandas as pd
from .database import Database


def process_shard(csv_path: str):

    db = Database()
    all_parts = {}
    try:
        with open(csv_path, newline="") as f:
            reader = csv.reader(f)
            for list_line in reader:
                line = ' '.join(list_line)

                parser = Parser(
                    db=db,
                    text=line)

                parser.get_links()
                part_id, tag_values = parser.download_images()

                all_parts[part_id] = tag_values

    except Exception as e:
        print(f"Error processing shard {csv_path}: {e}")

    rows = [(pid, url) for pid, urls in all_parts.items() for url in urls]
    df = pd.DataFrame(rows, columns=['part_id', 'tag_value'])

    db.upsert_append_new_only(
        df=df,
        target='dbo.part_tags',
        key_col='tag_value'
    )