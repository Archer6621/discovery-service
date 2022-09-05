import dask.dataframe as dd
import pandas as pd

from io import StringIO

from minio.error import NoSuchKey

from ..clients import minio
from ..clients import dask



def table_exists(bucket, table_path):
    try:
        minio.minio_client.stat_object(bucket, path)
        return True
    except NoSuchKey:
        return False


def get_tables(bucket):
    objects = minio.minio_client.list_objects(bucket, recursive=True)
    return [o.object_name for o in objects]


def bucket_exists(bucket):
    return minio.minio_client.bucket_exists(bucket)


def get_df(bucket, path, rows=None):
    res = minio.minio_client.get_object(bucket, path)
    csv_string = res.data.decode("utf-8")
    res.close()
    res.release_conn()

    df = pd.read_csv(
        StringIO(csv_string), 
        header=0, 
        engine="python", 
        encoding="utf8", 
        quotechar='"',     
        escapechar='\\', 
        nrows=rows
    )

    return df


def get_ddf(bucket, path):
    minio_path = f"s3://{bucket}/{path}"

    ddf = dd.read_csv(
        minio_path,
        sample_rows=1000,  # Sample 1000 rows to auto-determine dtypes
        blocksize=25e6,  # 25MB per block
        header=0,
        engine="python",
        encoding="utf8",
        quotechar='"',
        escapechar='\\',
        # on_bad_lines='warn', # For some reason Dask doesn't like this keyword parameter all of a sudden, even though it is supported!
        storage_options=dask.get_s3_settings()
    )

    return ddf


def get_unique_values(ddf):
    ddf = ddf.select_dtypes(exclude=['number'])  # Drop numerics, no need to search these in ES

    # This might still cause memory issues for very large DFs
    # TODO: Dump to disk and read from there
    get_unique = lambda s: s.unique().compute()

    futures = dask.get_client().map(get_unique, [ddf[col] for col in ddf.columns])
    results = dask.get_client().gather(futures)

    return dict(zip(ddf.columns, results))
