import csv
from .image_processing import Img_Proc
from .database import Database
import sys
import pandas as pd

def process_shard(csv_path: str):

    try:
        db = Database()
        img_proc = Img_Proc(db)
        urls = []
        part_numbers = []

        with open(csv_path, newline="") as f: #grabs all infomration from csv --we need to know len for grouped
            reader = csv.reader(f)
            for line in reader:
                url = line[0]
                urls.append(url)
                part_numbers.append(url.split('_')[:-1][0])


        tags = pd.Series(urls, index=None)
        num_df = pd.Series(part_numbers, index=None)

        end_idxs = num_df.index[num_df.ne(num_df.shift(-1))].tolist()

        n = len(num_df)
        grouped_strings = []
        start = 0

        for end in sorted(end_idxs):
            end = min(max(end, 0), n-1)
            grouped_strings.append(tags.iloc[start:end + 1].tolist())
            start = end +1
            grouped_list = grouped_strings[0]

            img_proc.retrieve_from_s3_and_run(grouped_list)

            grouped_strings = []

        if start < n:
            grouped_strings.append(tags.iloc[start:end + 1].tolist())
            grouped_list = grouped_strings[0]

            img_proc.retrieve_from_s3_and_run(grouped_list)

            grouped_strings = []


        df_delete = img_proc.db.to_delete_df
        db.create_table_if_not_exists('to_delete', df_delete)
        db.to_sql(df_delete, 'to_delete')
        db.send_delete_request_img_proc()




    except Exception as e:
        print(f"Error processing shard {csv_path}: {e}")