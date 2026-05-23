import os, shutil, stat
import boto3
from botocore.exceptions import NoCredentialsError
from sqlalchemy import create_engine, text
import pandas as pd
from threading import Lock
from dotenv import load_dotenv

from tenancy.ids import validate_tenant_id
from tenancy import attach_tenant_to_engine

load_dotenv()


class Database:
    def __init__(self, tenant_id=None):
        self.tenant_id = validate_tenant_id(tenant_id) if tenant_id else None
        self.user = os.getenv("DB_USER")
        self.password = os.getenv("DB_PASSWORD")
        self.host = os.getenv("DB_HOST")
        self.port = os.getenv("DB_PORT")
        self.db = 'parts_db'
        self.driver = "ODBC+Driver+18+for+SQL+Server"
        self.engine = self.get_engine()
        attach_tenant_to_engine(self.engine, self.tenant_id)
        self.lock = Lock()
        self.s3 = boto3.client("s3")
        self.bucket = os.getenv("BUCKET")
        self.delete_keys = []
        self.to_delete_df = pd.DataFrame({"tag_value": pd.Series(dtype="string")})

    def get_engine(self):
        url = (
            f"mssql+pyodbc://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}?driver={self.driver}"
            "&TrustServerCertificate=yes"
        )
        return create_engine(url, pool_pre_ping=True, fast_executemany=True)
    
    def change_db(self, new_db):
        self.db = new_db

    def execute_sql(self, sql_text, params=None):
        with self.lock, self.engine.begin() as conn:
            return conn.execute(text(sql_text), params or {})

    def read_sql_query(self, sql_text, params=None):
        """Execute a SQL query and return the result as a pandas DataFrame.

        Pass user-supplied values via ``params`` (a dict of bound parameters)
        rather than f-string interpolation, to avoid SQL injection.
        """
        with self.lock, self.engine.begin() as conn:
            return pd.read_sql_query(text(sql_text), conn, params=params or {})
    
    def to_sql(self, df, table_name, if_exists='append', index=False, schema=None):
        """Write records stored in a DataFrame to a SQL database."""
        with self.lock, self.engine.begin() as conn:
            df.to_sql(table_name, conn, if_exists=if_exists, index=index, method='multi', schema=schema, chunksize=20000)



    def create_table_if_not_exists(self, table_name, df):
        """
        Creates a table if it doesn't exist based on the DataFrame's schema.
        """
        try:
            # Check if the table exists
            table_exists_query = f"IF OBJECT_ID('{table_name}', 'U') IS NOT NULL SELECT 1 ELSE SELECT 0;"
            result = self.read_sql_query(table_exists_query)
            table_exists = result.iloc[0, 0] == 1

            if not table_exists:
                # Build the CREATE TABLE query based on DataFrame's schema
                columns = df.columns
                dtypes = df.dtypes
                sql_dtypes = []
                for col in columns:
                    dtype = dtypes[col]
                    if pd.api.types.is_integer_dtype(dtype):
                        sql_dtype = 'INT'
                    elif pd.api.types.is_float_dtype(dtype):
                        sql_dtype = 'FLOAT'
                    elif dtype == 'datetime64[ns]':
                        sql_dtype = 'DATETIME2'
                    else:
                        sql_dtype = 'NVARCHAR(MAX)'  # Use NVARCHAR(MAX) for TEXT

                    sql_dtypes.append(f'[{col}] {sql_dtype}')

                create_table_query = f"CREATE TABLE [{table_name}] ("
                create_table_query += ', '.join(sql_dtypes)
                create_table_query += ");"

                self.execute_sql(create_table_query)
                print(f"Table {table_name} created successfully.")
            else:
                print(f"Table {table_name} already exists.")
        except Exception as e:
            print(f"Error occurred while creating table {table_name}: {e}")


    def upload_to_folder(self,bucket_name:str, folder_name: str,local_file_path:str, s3_file_name: str=None, delete_after:bool=True):
        s3_file_name = folder_name

        try:
            self.s3.upload_file(local_file_path, bucket_name, s3_file_name, ExtraArgs = {'ContentType': "image/png"})


            print(f"uploaded {local_file_path}")
            
            if delete_after:
                os.remove(local_file_path)
                print(f"Deleted local file: {local_file_path}")
        

        except NoCredentialsError:
            print(" AWS credentials not found. Did you run 'aws configure'?\n")
        except Exception as e:
            print(f"Upload failed {e}")

    def download_group(self,bucket: str, group_list:list):
        group_map = {}
        for i, key in enumerate(group_list):
            formated_key = "/".join(key.split('/')[-2:])
            local_file = f"images/image_{i}.png"
            group_map[local_file]=formated_key

            self.s3.download_file(bucket, formated_key, f"images/image_{i}.png")#f"image_{i}")
        return group_map


    def empty_dir(self, folder: str) -> None:

        def _on_rm_error(func, path, exc_info):
            # Handle read-only files on Windows
            os.chmod(path, stat.S_IWRITE)
            func(path)

        if not (os.path.isdir(folder) and folder not in ("", "/", "\\")):
            raise ValueError(f"Refusing to operate on suspicious folder: {folder}")
        for entry in os.scandir(folder):
            p = entry.path
            try:
                if entry.is_file() or entry.is_symlink():
                    os.unlink(p)
                else:
                    shutil.rmtree(p, onerror=_on_rm_error)
            except FileNotFoundError:
                pass  # already gone

    def save_data_for_deletion_img_proc(self, all_data, keep):
        """"used in image processing"""
        base_url = f"https://{self.bucket}.s3.us-east-1.amazonaws.com/" 
        temp_delete = []
        delete = []
        for name in all_data:
            if name not in keep:
                delete.append(name)
        for key in delete:
            self.delete_keys.append({'Key': key})
            temp_delete.append(f"{base_url}{key}")
        
        df_urls = pd.DataFrame({"tag_value": temp_delete})
        self.to_delete_df = pd.concat([self.to_delete_df, df_urls], ignore_index=True)

        
    def send_delete_request_img_proc(self):
        """Delete the staged keys from S3 and the matching rows from part_tags.

        The DELETE is tenant-scoped when this Database was constructed
        with a tenant_id, so two tenants concurrently running the
        watermark step cannot wipe each other's rows.
        """
        def _chunk_list(data, limit=900):
            for i in range(0, len(data), limit):
                yield data[i:i + limit]

        for chunk in _chunk_list(self.delete_keys):
            deletion_request = {'Objects': chunk, 'Quiet': True}
            self.s3.delete_objects(
                Bucket=self.bucket,
                Delete=deletion_request,
            )

        if self.tenant_id:
            self.execute_sql(
                """
                DELETE pt
                FROM [dbo].[part_tags] AS pt
                INNER JOIN [dbo].[to_delete] AS td
                    ON td.tag_value = pt.tag_value
                WHERE pt.tenant_id = :tenant_id;
                """,
                params={"tenant_id": self.tenant_id},
            )
        else:
            self.execute_sql("""
                DELETE pt
                FROM [dbo].[part_tags] AS pt
                INNER JOIN [dbo].[to_delete] AS td
                    ON td.tag_value = pt.tag_value;
                """)
        self.execute_sql("DROP TABLE dbo.to_delete;")



    def empty_prefix(self, bucket_name, prefix):
        def _chunk_list(items, limit=900):
            for i in range(0, len(items), limit):
                yield items[i:i+limit]


        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        total_deleted = 0

        for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):

            contents = page['Contents']

            if not contents:
                continue

            objects = [{"Key": obj["Key"]} for obj in contents]

            for chunk in _chunk_list(objects):
                s3.delete_objects(
                    Bucket=bucket_name,
                    Delete={"Objects": objects, "Quiet": True}
                )
                total_deleted += len(chunk)

        return total_deleted

   
if __name__ == "__main__":
    db = Database()
    #db.retrieve_from_s3("partsbucket0000","images", False, True)
    #db.send_delete_request()