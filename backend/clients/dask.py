import os

from dask.distributed import Client
from typing import Dict


dask_client: Client = None


def get_client() -> Client:
    global dask_client
    if dask_client is None:
        dask_client = Client(threads_per_worker=4, n_workers=8) # TODO: make configurable
    return dask_client


def get_s3_settings() -> Dict[str, str]:
    return {
      "key": os.environ["MINIO_ACCESS_KEY"],
      "secret": os.environ["MINIO_SECRET_KEY"],
      "client_kwargs": {"endpoint_url":f"http://{os.environ['MINIO_HOST']}:{os.environ['MINIO_PORT']}"}
    }