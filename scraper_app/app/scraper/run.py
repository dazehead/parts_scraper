import csv
from .parser import Parser
import pandas as pd
from .database import Database


def process_shard(csv_path: str, tenant_id: str):
    """Process one shard CSV for a single tenant.

    ``tenant_id`` is validated by the caller (the worker) before we get
    here. We pass it through to every Parser and DB call so each row
    is scoped to the right tenant in both S3 and SQL.
    """
    db = Database(tenant_id=tenant_id)
    all_parts: dict[int, list[str]] = {}
    try:
        with open(csv_path, newline="") as f:
            reader = csv.reader(f)
            for list_line in reader:
                line = ' '.join(list_line)

                parser = Parser(db=db, text=line, tenant_id=tenant_id)
                parser.get_links()
                part_id, tag_values = parser.download_images()
                if part_id is None:
                    continue
                all_parts[part_id] = tag_values

    except Exception as e:
        # Re-raise so the worker can mark the message failed and let
        # SQS retry / DLQ. Previously this swallowed and silently
        # produced a partial result.
        print(f"Error processing shard {csv_path}: {e}")
        raise

    rows = [
        (tenant_id, pid, url)
        for pid, urls in all_parts.items()
        for url in urls
    ]
    if not rows:
        return
    df = pd.DataFrame(rows, columns=['tenant_id', 'part_id', 'tag_value'])

    db.upsert_append_new_only(
        df=df,
        target='dbo.part_tags',
        key_col=('tenant_id', 'tag_value'),
    )
